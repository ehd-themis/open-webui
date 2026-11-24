"""
Server tools over plain HTTP (fork patch, utils/api_tool_loop.py).

One request may carry the caller's own `tools` and `tool_ids` (server tools such as `server:mcp:m365`). The server
runs its tools in the same HTTP stream; the caller's calls come back as ordinary `tool_calls`.
"""

import json
from types import SimpleNamespace

import pytest
from open_webui.utils import api_tool_loop as loop

USER = SimpleNamespace(id='user-1', role='user')


# --- helpers ---


def spec(name):
    return {
        'type': 'function',
        'function': {'name': name, 'description': name, 'parameters': {'type': 'object', 'properties': {}}},
    }


def caller(name):
    return spec(name)


def server_tool(name, result='ok', calls=None, type_='mcp'):
    async def run(**kwargs):
        if calls is not None:
            calls.append((name, kwargs))
        return result

    return {'spec': spec(name)['function'], 'callable': run, 'type': type_}


def line(payload):
    return f'data: {json.dumps(payload)}\n\n'.encode()


def text_turn(*parts, usage=None):
    out = [
        line(
            {
                'id': 'c1',
                'created': 1,
                'model': 'qwen',
                'choices': [{'index': 0, 'delta': {'content': p}, 'finish_reason': None}],
            }
        )
        for p in parts
    ]
    out.append(line({'id': 'c1', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}))
    if usage:
        out.append(line({'id': 'c1', 'choices': [], 'usage': usage}))
    out.append(b'data: [DONE]\n\n')
    return out


def calls_turn(*calls, text=''):
    """A turn that calls tools: (id, name, arguments) split over several deltas like a real provider."""
    out = []
    if text:
        out.append(
            line(
                {
                    'id': 'c1',
                    'created': 1,
                    'model': 'qwen',
                    'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': None}],
                }
            )
        )
    for index, (call_id, name, arguments) in enumerate(calls):
        out.append(
            line(
                {
                    'id': 'c1',
                    'choices': [
                        {
                            'index': 0,
                            'delta': {
                                'tool_calls': [
                                    {
                                        'index': index,
                                        'id': call_id,
                                        'type': 'function',
                                        'function': {'name': name, 'arguments': ''},
                                    }
                                ]
                            },
                            'finish_reason': None,
                        }
                    ],
                }
            )
        )
        half = len(arguments) // 2
        for piece in (arguments[:half], arguments[half:]):
            out.append(
                line(
                    {
                        'id': 'c1',
                        'choices': [
                            {
                                'index': 0,
                                'delta': {'tool_calls': [{'index': index, 'function': {'arguments': piece}}]},
                                'finish_reason': None,
                            }
                        ],
                    }
                )
            )
    out.append(line({'id': 'c1', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]}))
    out.append(b'data: [DONE]\n\n')
    return out


async def agen(items):
    for item in items:
        yield item


class Upstream:
    def __init__(self, items):
        self.body_iterator = agen(items)
        self.headers = {'content-type': 'text/event-stream'}
        self.background = None


class FakeModel:
    """Answers `generate_chat_completion` turn after turn and remembers what it was asked."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.requests = []

    async def __call__(self, request, form_data, user, bypass_filter=False, bypass_system_prompt=False):
        self.requests.append(form_data)
        return StreamingUpstream(self.turns.pop(0))


def StreamingUpstream(items):
    # a real StreamingResponse, as generate_chat_completion returns for a stream
    from fastapi.responses import StreamingResponse

    return StreamingResponse(agen(items), media_type='text/event-stream')


def request():
    return SimpleNamespace(
        state=SimpleNamespace(max_tool_call_iterations=10), app=SimpleNamespace(state=SimpleNamespace())
    )


async def run(first_turn, metadata, form_data=None, model=None, monkeypatch=None):
    if model is not None:
        monkeypatch.setattr('open_webui.utils.chat.generate_chat_completion', model)
    form_data = form_data or {'model': 'qwen', 'messages': [{'role': 'user', 'content': 'hi'}], 'stream': True}
    events = []
    async for raw in loop.stream_with_server_tools(request(), USER, form_data, metadata, Upstream(first_turn)):
        for data in raw.strip().split('\n\n'):
            payload = data.removeprefix('data: ')
            events.append('[DONE]' if payload == '[DONE]' else json.loads(payload))
    return events


def texts(events):
    return ''.join(e['choices'][0]['delta'].get('content', '') for e in events if e != '[DONE]' and e.get('choices'))


def finish_reasons(events):
    return [
        e['choices'][0]['finish_reason']
        for e in events
        if e != '[DONE]' and e.get('choices') and e['choices'][0]['finish_reason']
    ]


def caller_tool_calls(events):
    out = []
    for e in events:
        if e != '[DONE]' and e.get('choices'):
            out.extend(e['choices'][0]['delta'].get('tool_calls') or [])
    return out


def extras(events, key):
    return [e['open_webui'][key] for e in events if e != '[DONE]' and key in (e.get('open_webui') or {})]


# --- request side ---


def test_merge_keeps_caller_tools_first_and_the_caller_wins_a_clash():
    server = {'m365_mail': server_tool('m365_mail'), 'read_table': server_tool('read_table')}

    specs, remaining = loop.merge_caller_and_server_tools([caller('read_table'), caller('get_selection')], server)

    assert [loop.tool_name_of(s) for s in specs] == ['read_table', 'get_selection', 'm365_mail']
    assert list(remaining) == ['m365_mail']


def test_only_caller_tools_are_unchanged_by_merge():
    specs, remaining = loop.merge_caller_and_server_tools([caller('a')], {})
    assert specs == [caller('a')] and remaining == {}


def test_which_requests_take_the_loop():
    server = {'m365_mail': server_tool('m365_mail')}
    # only `tools`, no tool_ids: nothing to run here
    assert loop.wants_loop({'tools': {}}, None) is False
    # only tool_ids, plain HTTP (no emitter): yes
    assert loop.wants_loop({'tools': server}, None) is True
    # only tool_ids but through the chat UI / a saved chat (emitter): the upstream loop keeps it
    assert loop.wants_loop({'tools': server}, object()) is False
    # both tools and tool_ids: always, even with an emitter
    assert loop.wants_loop({'tools': server, 'api_tool_mix': True}, object()) is True
    # a connection that failed on a plain request: the caller must hear about it
    assert (
        loop.wants_loop({'tools': {}, 'tool_connection_errors': [{'server': 'm365', 'error': 'auth_required'}]}, None)
        is True
    )
    # legacy function calling is not supported
    assert loop.wants_loop({'tools': server, 'params': {'function_calling': 'legacy'}}, None) is False
    # built-in tools need a chat session: not run from plain HTTP
    assert loop.wants_loop({'tools': {'t': server_tool('t', type_='builtin')}}, None) is False


def test_connection_errors_are_recorded_once():
    metadata = {}
    loop.record_connection_error(metadata, 'm365', 'auth_required')
    loop.record_connection_error(metadata, 'm365', 'auth_required')
    loop.record_connection_error(metadata, 'gitlab', 'failed', 'boom')
    assert metadata['tool_connection_errors'] == [
        {'server': 'm365', 'error': 'auth_required'},
        {'server': 'gitlab', 'error': 'failed', 'detail': 'boom'},
    ]


# --- stream side ---


@pytest.mark.asyncio
async def test_only_tool_ids_a_server_turn_runs_the_tool_and_streams_the_next_turn(monkeypatch):
    made = []
    metadata = {'tools': {'m365_mail': server_tool('m365_mail', result='3 mails', calls=made)}}
    model = FakeModel(text_turn('Tu as ', '3 mails.', usage={'prompt_tokens': 50}))

    events = await run(
        calls_turn(('call_1', 'm365_mail', '{"top": 3}'), text='Je regarde. '),
        metadata,
        model=model,
        monkeypatch=monkeypatch,
    )

    assert made == [('m365_mail', {'top': 3})]
    assert texts(events) == 'Je regarde. Tu as 3 mails.'
    # the server call is never offered to the caller as a tool call
    assert caller_tool_calls(events) == []
    assert finish_reasons(events) == ['stop']
    assert extras(events, 'server_tool_call') == [{'id': 'call_1', 'name': 'm365_mail', 'status': 'completed'}]
    assert events[-1] == '[DONE]'
    # the model got the call and its result back
    sent = model.requests[0]['messages']
    assert sent[-2]['role'] == 'assistant' and sent[-2]['tool_calls'][0]['function']['name'] == 'm365_mail'
    assert sent[-1] == {'role': 'tool', 'tool_call_id': 'call_1', 'content': '3 mails'}
    # usage of the last turn reaches the caller
    assert [e['usage'] for e in events if e != '[DONE]' and e.get('usage')] == [{'prompt_tokens': 50}]


@pytest.mark.asyncio
async def test_only_caller_tools_come_back_as_tool_calls_and_nothing_runs_here(monkeypatch):
    made = []
    metadata = {'tools': {'m365_mail': server_tool('m365_mail', calls=made)}}

    events = await run(calls_turn(('call_9', 'read_paragraphs', '{"from": 1}')), metadata, monkeypatch=monkeypatch)

    assert made == []
    assert caller_tool_calls(events) == [
        {
            'index': 0,
            'id': 'call_9',
            'type': 'function',
            'function': {'name': 'read_paragraphs', 'arguments': '{"from": 1}'},
        }
    ]
    assert finish_reasons(events) == ['tool_calls']
    assert extras(events, 'server_tool_calls') == []
    assert events[-1] == '[DONE]'


@pytest.mark.asyncio
async def test_a_mixed_turn_runs_the_server_call_first_then_returns_the_caller_call(monkeypatch):
    made = []
    metadata = {
        'api_tool_mix': True,
        'tools': {'m365_mail': server_tool('m365_mail', result='objet: Devis', calls=made)},
    }

    events = await run(
        calls_turn(
            ('s1', 'm365_mail', '{}'), ('c1', 'insert_paragraphs', '{"text": "x"}'), text='Je lis puis j’écris. '
        ),
        metadata,
        monkeypatch=monkeypatch,
    )

    assert made == [('m365_mail', {})]
    # only the caller's call is streamed as a tool call, renumbered from 0
    assert caller_tool_calls(events) == [
        {
            'index': 0,
            'id': 'c1',
            'type': 'function',
            'function': {'name': 'insert_paragraphs', 'arguments': '{"text": "x"}'},
        }
    ]
    assert finish_reasons(events) == ['tool_calls']
    assert texts(events) == 'Je lis puis j’écris. '
    # the caller can rebuild a consistent history from what the server ran
    assert extras(events, 'server_tool_calls') == [
        [
            {
                'id': 's1',
                'type': 'function',
                'function': {'name': 'm365_mail', 'arguments': '{}'},
                'status': 'completed',
                'result': 'objet: Devis',
            }
        ]
    ]
    assert events[-1] == '[DONE]'


@pytest.mark.asyncio
async def test_server_calls_of_an_earlier_turn_are_listed_with_a_later_caller_call(monkeypatch):
    metadata = {'api_tool_mix': True, 'tools': {'m365_mail': server_tool('m365_mail', result='R1')}}
    model = FakeModel(calls_turn(('c1', 'get_selection', '{}')))

    events = await run(calls_turn(('s1', 'm365_mail', '{}')), metadata, model=model, monkeypatch=monkeypatch)

    assert [c['function']['name'] for c in caller_tool_calls(events)] == ['get_selection']
    (listed,) = extras(events, 'server_tool_calls')
    assert [c['id'] for c in listed] == ['s1'] and listed[0]['result'] == 'R1'


@pytest.mark.asyncio
async def test_a_failing_server_tool_goes_back_to_the_model_as_text(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError('graph is down')

    metadata = {'tools': {'m365_mail': {**server_tool('m365_mail'), 'callable': boom}}}
    model = FakeModel(text_turn('Je ne peux pas lire tes mails.'))

    events = await run(calls_turn(('s1', 'm365_mail', '{}')), metadata, model=model, monkeypatch=monkeypatch)

    assert extras(events, 'server_tool_call') == [{'id': 's1', 'name': 'm365_mail', 'status': 'failed'}]
    assert 'graph is down' in model.requests[0]['messages'][-1]['content']
    assert texts(events) == 'Je ne peux pas lire tes mails.'


@pytest.mark.asyncio
async def test_missing_authorization_is_visible_to_the_caller_and_the_answer_still_streams(monkeypatch):
    metadata = {'tools': {}, 'tool_connection_errors': [{'server': 'm365', 'error': 'auth_required'}]}

    events = await run(text_turn('Je ne peux pas lire ', 'tes mails.'), metadata, monkeypatch=monkeypatch)

    # first thing on the stream, before any text
    assert events[0]['open_webui'] == {'tool_connection_errors': [{'server': 'm365', 'error': 'auth_required'}]}
    assert texts(events) == 'Je ne peux pas lire tes mails.'
    assert finish_reasons(events) == ['stop']


@pytest.mark.asyncio
async def test_a_plain_request_without_tools_is_relayed_as_is(monkeypatch):
    events = await run(text_turn('Bonjour'), {'tools': {}}, monkeypatch=monkeypatch)
    assert texts(events) == 'Bonjour' and finish_reasons(events) == ['stop'] and events[-1] == '[DONE]'


@pytest.mark.asyncio
async def test_a_provider_error_is_forwarded_and_ends_the_stream(monkeypatch):
    metadata = {'tools': {'m365_mail': server_tool('m365_mail')}}

    events = await run([line({'error': {'message': 'model unavailable'}})], metadata, monkeypatch=monkeypatch)

    assert events[0] == {'error': {'message': 'model unavailable'}} and events[-1] == '[DONE]'


@pytest.mark.asyncio
async def test_the_round_limit_takes_the_tools_away_for_a_last_answer(monkeypatch):
    metadata = {'tools': {'m365_mail': server_tool('m365_mail')}}
    model = FakeModel(text_turn('Voilà ce que j’ai trouvé.'))
    form_data = {
        'model': 'qwen',
        'messages': [{'role': 'user', 'content': 'hi'}],
        'stream': True,
        'tools': [spec('m365_mail')],
    }
    req = request()
    req.state.max_tool_call_iterations = 1
    monkeypatch.setattr('open_webui.utils.chat.generate_chat_completion', model)

    out = []
    async for raw in loop.stream_with_server_tools(
        req, USER, form_data, metadata, Upstream(calls_turn(('s1', 'm365_mail', '{}')))
    ):
        out.append(raw)

    assert 'tools' not in model.requests[0]
    assert 'limit of tool-call rounds' in model.requests[0]['messages'][-1]['content']
    assert 'Voilà' in ''.join(out)


@pytest.mark.asyncio
async def test_the_loop_closes_the_mcp_clients_when_the_stream_ends(monkeypatch):
    closed = []

    class Client:
        async def disconnect(self):
            closed.append(True)

    metadata = {'tools': {}, 'mcp_clients': {'m365': Client(), 'gitlab': Client()}}

    await run(text_turn('ok'), metadata, monkeypatch=monkeypatch)

    assert closed == [True, True]
