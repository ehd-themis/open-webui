"""
/api/responses lets Responses-only clients (Codex) use the chat pipeline: the
request becomes a Chat Completions payload and the chat result becomes a
Responses object or event stream.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from open_webui.utils.responses_compat import (
    ResponsesRequestError,
    chat_response_to_stream,
    chat_response_to_stream,
    chat_stream_to_responses_stream,
    convert_chat_to_responses_response,
    convert_responses_to_chat_payload,
)

USER = SimpleNamespace(id='user-1', name='Dev', email='dev@example.com', role='user')


def sse(*chunks):
    lines = [f'data: {json.dumps(c)}\n\n' for c in chunks] + ['data: [DONE]\n\n']
    return [line.encode() for line in lines]


async def agen(items):
    for item in items:
        yield item


async def collect(stream):
    events = []
    async for raw in stream:
        for block in raw.decode().strip().split('\n\n'):
            data = block.split('\ndata: ', 1)[1]
            events.append(json.loads(data))
    return events


def delta(**fields):
    return {'choices': [{'index': 0, 'delta': fields, 'finish_reason': None}]}


def finish(reason):
    return {'choices': [{'index': 0, 'delta': {}, 'finish_reason': reason}]}


# --- request ---


def test_request_maps_instructions_input_and_sampling():
    payload, custom = convert_responses_to_chat_payload(
        {
            'model': 'qwen',
            'instructions': 'Be brief.',
            'input': [
                {'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'rules'}]},
                {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hi'}]},
                {'type': 'reasoning', 'summary': []},
                {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'hello'}]},
            ],
            'stream': True,
            'max_output_tokens': 50,
            'reasoning': {'effort': 'high'},
            'store': False,
            'include': ['reasoning.encrypted_content'],
            'prompt_cache_key': 'abc',
        }
    )

    assert payload['messages'] == [
        {'role': 'system', 'content': 'Be brief.\n\nrules'},
        {'role': 'user', 'content': 'hi'},
        {'role': 'assistant', 'content': 'hello'},
    ]
    assert payload['model'] == 'qwen'
    assert payload['max_tokens'] == 50
    assert payload['reasoning_effort'] == 'high'
    assert payload['stream'] is True
    assert payload['stream_options'] == {'include_usage': True}
    assert not {'store', 'include', 'prompt_cache_key', 'input'} & payload.keys()
    assert custom == set()


def test_request_string_input_is_one_user_message():
    payload, _ = convert_responses_to_chat_payload({'model': 'qwen', 'input': 'hi'})

    assert payload['messages'] == [{'role': 'user', 'content': 'hi'}]


def test_request_maps_tool_calls_and_results():
    payload, _ = convert_responses_to_chat_payload(
        {
            'model': 'qwen',
            'input': [
                {'type': 'message', 'role': 'user', 'content': 'ls'},
                {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Running.'}]},
                {'type': 'function_call', 'call_id': 'c1', 'name': 'shell', 'arguments': '{"cmd":"ls"}'},
                {'type': 'function_call', 'call_id': 'c2', 'name': 'shell', 'arguments': '{"cmd":"pwd"}'},
                {'type': 'function_call_output', 'call_id': 'c1', 'output': 'a.txt'},
                {'type': 'function_call_output', 'call_id': 'c2', 'output': '/tmp'},
            ],
        }
    )

    assert payload['messages'][1] == {
        'role': 'assistant',
        'content': 'Running.',
        'tool_calls': [
            {'id': 'c1', 'type': 'function', 'function': {'name': 'shell', 'arguments': '{"cmd":"ls"}'}},
            {'id': 'c2', 'type': 'function', 'function': {'name': 'shell', 'arguments': '{"cmd":"pwd"}'}},
        ],
    }
    assert payload['messages'][2:] == [
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'a.txt'},
        {'role': 'tool', 'tool_call_id': 'c2', 'content': '/tmp'},
    ]


def test_request_maps_tools_and_drops_builtin_ones():
    payload, custom = convert_responses_to_chat_payload(
        {
            'model': 'qwen',
            'input': 'hi',
            'tools': [
                {'type': 'function', 'name': 'shell', 'description': 'run', 'parameters': {'type': 'object'}},
                {'type': 'custom', 'name': 'apply_patch', 'description': 'patch', 'format': {'definition': 'GRAMMAR'}},
                {'type': 'web_search'},
                {'type': 'local_shell'},
            ],
            'tool_choice': {'type': 'function', 'name': 'shell'},
            'parallel_tool_calls': False,
        }
    )

    names = [t['function']['name'] for t in payload['tools']]
    assert names == ['shell', 'apply_patch']
    assert payload['tools'][1]['function']['parameters']['required'] == ['input']
    assert 'GRAMMAR' in payload['tools'][1]['function']['description']
    assert custom == {'apply_patch'}
    assert payload['tool_choice'] == {'type': 'function', 'function': {'name': 'shell'}}
    assert payload['parallel_tool_calls'] is False


def test_request_custom_tool_history_becomes_function_call():
    payload, _ = convert_responses_to_chat_payload(
        {
            'model': 'qwen',
            'input': [
                {'type': 'custom_tool_call', 'call_id': 'c1', 'name': 'apply_patch', 'input': '*** Begin Patch'},
                {'type': 'custom_tool_call_output', 'call_id': 'c1', 'output': 'done'},
            ],
        }
    )

    call = payload['messages'][0]['tool_calls'][0]
    assert call['function'] == {'name': 'apply_patch', 'arguments': json.dumps({'input': '*** Begin Patch'})}
    assert payload['messages'][1] == {'role': 'tool', 'tool_call_id': 'c1', 'content': 'done'}


def test_request_rejects_previous_response_id():
    with pytest.raises(ResponsesRequestError):
        convert_responses_to_chat_payload({'model': 'qwen', 'input': 'hi', 'previous_response_id': 'resp_1'})


def test_request_merges_system_messages_and_demotes_later_ones():
    payload, _ = convert_responses_to_chat_payload(
        {
            'model': 'qwen',
            'instructions': 'Base.',
            'input': [
                {'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'perms'}]},
                {'type': 'message', 'role': 'user', 'content': 'env context'},
                {'type': 'message', 'role': 'user', 'content': 'do it'},
                {'type': 'function_call', 'call_id': 'c1', 'name': 'sh', 'arguments': '{}'},
                {'type': 'function_call_output', 'call_id': 'c1', 'output': 'done'},
                {'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'mode changed'}]},
                {'type': 'message', 'role': 'user', 'content': 'next'},
            ],
        }
    )

    roles = [m['role'] for m in payload['messages']]
    assert roles == ['system', 'user', 'assistant', 'tool', 'user']
    assert payload['messages'][0]['content'] == 'Base.\n\nperms'
    assert payload['messages'][1]['content'] == 'env context\n\ndo it'
    assert payload['messages'][4]['content'] == 'mode changed\n\nnext'


def test_request_parallel_tool_calls_only_with_tools():
    without, _ = convert_responses_to_chat_payload(
        {'model': 'qwen', 'input': 'hi', 'parallel_tool_calls': True, 'tools': [{'type': 'web_search'}]}
    )
    with_tools, _ = convert_responses_to_chat_payload(
        {
            'model': 'qwen',
            'input': 'hi',
            'parallel_tool_calls': False,
            'tools': [{'type': 'function', 'name': 'sh'}],
        }
    )

    assert 'parallel_tool_calls' not in without and 'tools' not in without
    assert with_tools['parallel_tool_calls'] is False


def test_request_tool_choice_and_custom_tool_output():
    payload, _ = convert_responses_to_chat_payload(
        {
            'model': 'qwen',
            'input': [
                {'type': 'custom_tool_call', 'call_id': 'p1', 'name': 'apply_patch', 'input': 'P'},
                {'type': 'custom_tool_call_output', 'call_id': 'p1', 'output': 'ok'},
            ],
            'tools': [{'type': 'function', 'name': 'sh'}, {'type': 'custom', 'name': 'apply_patch'}],
            'tool_choice': {'type': 'function', 'name': 'sh'},
        }
    )

    assert payload['tool_choice'] == {'type': 'function', 'function': {'name': 'sh'}}
    assert payload['messages'][-1] == {'role': 'tool', 'tool_call_id': 'p1', 'content': 'ok'}


# --- non-streaming response ---


def test_response_text_reasoning_and_usage():
    response = convert_chat_to_responses_response(
        {
            'choices': [
                {
                    'message': {'role': 'assistant', 'content': 'Hello', 'reasoning_content': 'thinking'},
                    'finish_reason': 'stop',
                }
            ],
            'usage': {
                'prompt_tokens': 10,
                'completion_tokens': 5,
                'prompt_tokens_details': {'cached_tokens': 8},
                'completion_tokens_details': {'reasoning_tokens': 2},
            },
        },
        model='qwen',
    )

    assert response['object'] == 'response'
    assert response['status'] == 'completed'
    assert response['model'] == 'qwen'
    assert [item['type'] for item in response['output']] == ['reasoning', 'message']
    assert response['output'][1]['content'][0] == {'type': 'output_text', 'text': 'Hello', 'annotations': []}
    assert response['usage'] == {
        'input_tokens': 10,
        'input_tokens_details': {'cached_tokens': 8},
        'output_tokens': 5,
        'output_tokens_details': {'reasoning_tokens': 2},
        'total_tokens': 15,
    }


def test_response_tool_calls_and_custom_tools():
    response = convert_chat_to_responses_response(
        {
            'choices': [
                {
                    'message': {
                        'content': None,
                        'tool_calls': [
                            {'id': 'c1', 'function': {'name': 'shell', 'arguments': '{"cmd":"ls"}'}},
                            {'id': 'c2', 'function': {'name': 'apply_patch', 'arguments': '{"input":"PATCH"}'}},
                        ],
                    },
                    'finish_reason': 'tool_calls',
                }
            ]
        },
        model='qwen',
        custom_tools={'apply_patch'},
    )

    function_call, custom_call = response['output']
    assert (function_call['type'], function_call['call_id'], function_call['arguments']) == (
        'function_call',
        'c1',
        '{"cmd":"ls"}',
    )
    assert (custom_call['type'], custom_call['call_id'], custom_call['input']) == ('custom_tool_call', 'c2', 'PATCH')


def test_response_truncated_is_incomplete():
    response = convert_chat_to_responses_response(
        {'choices': [{'message': {'content': 'cut'}, 'finish_reason': 'length'}]}, model='qwen'
    )

    assert response['status'] == 'incomplete'
    assert response['incomplete_details'] == {'reason': 'max_output_tokens'}


# --- streaming response ---


@pytest.mark.asyncio
async def test_stream_reasoning_then_text():
    chunks = sse(
        delta(role='assistant'),
        delta(reasoning_content='hm'),
        delta(content='Hel'),
        delta(content='lo'),
        finish('stop'),
        {'choices': [], 'usage': {'prompt_tokens': 3, 'completion_tokens': 4}},
    )
    events = await collect(chat_stream_to_responses_stream(agen(chunks), model='qwen'))

    types = [e['type'] for e in events]
    assert types[:2] == ['response.created', 'response.in_progress']
    assert types[-1] == 'response.completed'
    assert [e['sequence_number'] for e in events] == list(range(len(events)))
    assert ''.join(e['delta'] for e in events if e['type'] == 'response.output_text.delta') == 'Hello'
    assert ''.join(e['delta'] for e in events if e['type'] == 'response.reasoning_summary_text.delta') == 'hm'

    done = [e['item'] for e in events if e['type'] == 'response.output_item.done']
    assert [item['type'] for item in done] == ['reasoning', 'message']
    assert [e['output_index'] for e in events if e['type'] == 'response.output_item.added'] == [0, 1]

    completed = events[-1]['response']
    assert completed['status'] == 'completed'
    assert completed['output'] == done
    assert completed['usage']['input_tokens'] == 3
    assert completed['usage']['output_tokens'] == 4


@pytest.mark.asyncio
async def test_stream_tool_calls_split_across_deltas():
    chunks = sse(
        delta(content='Checking.'),
        delta(tool_calls=[{'index': 0, 'id': 'c1', 'function': {'name': 'shell', 'arguments': ''}}]),
        delta(tool_calls=[{'index': 0, 'function': {'arguments': '{"cmd"'}}]),
        delta(tool_calls=[{'index': 0, 'function': {'arguments': ':"ls"}'}}]),
        delta(tool_calls=[{'index': 1, 'id': 'c2', 'function': {'name': 'apply_patch', 'arguments': '{"input":"P"}'}}]),
        finish('tool_calls'),
    )
    events = await collect(chat_stream_to_responses_stream(agen(chunks), model='qwen', custom_tools={'apply_patch'}))

    done = [e['item'] for e in events if e['type'] == 'response.output_item.done']
    assert [item['type'] for item in done] == ['message', 'function_call', 'custom_tool_call']
    assert done[1]['call_id'] == 'c1'
    assert done[1]['arguments'] == '{"cmd":"ls"}'
    assert done[2]['input'] == 'P'
    assert (
        ''.join(e['delta'] for e in events if e['type'] == 'response.function_call_arguments.delta') == '{"cmd":"ls"}'
    )
    assert events[-1]['type'] == 'response.completed'
    assert events[-1]['response']['output'] == done


@pytest.mark.asyncio
async def test_stream_handles_lines_split_across_reads():
    line = f'data: {json.dumps(delta(content="Hi"))}\n\ndata: [DONE]\n\n'.encode()
    events = await collect(chat_stream_to_responses_stream(agen([line[:20], line[20:]]), model='qwen'))

    assert ''.join(e['delta'] for e in events if e['type'] == 'response.output_text.delta') == 'Hi'


@pytest.mark.asyncio
async def test_stream_upstream_error_fails_the_response():
    chunks = sse(delta(content='par'), {'error': {'message': 'context overflow'}})
    events = await collect(chat_stream_to_responses_stream(agen(chunks), model='qwen'))

    assert events[-1]['type'] == 'response.failed'
    assert events[-1]['response']['status'] == 'failed'
    assert events[-1]['response']['error']['message'] == 'context overflow'
    assert 'response.completed' not in [e['type'] for e in events]


@pytest.mark.asyncio
async def test_stream_length_finish_is_incomplete():
    events = await collect(
        chat_stream_to_responses_stream(agen(sse(delta(content='x'), finish('length'))), model='qwen')
    )

    assert events[-1]['type'] == 'response.incomplete'
    assert events[-1]['response']['incomplete_details'] == {'reason': 'max_output_tokens'}


@pytest.mark.asyncio
async def test_stream_empty_answer_still_has_a_message():
    events = await collect(chat_stream_to_responses_stream(agen(sse(finish('stop'))), model='qwen'))

    assert [item['type'] for item in events[-1]['response']['output']] == ['message']


@pytest.mark.asyncio
async def test_stream_keeps_arguments_of_interleaved_tool_calls():
    chunks = sse(
        delta(tool_calls=[{'index': 0, 'id': 'a', 'function': {'name': 'sh', 'arguments': ''}}]),
        delta(tool_calls=[{'index': 1, 'id': 'b', 'function': {'name': 'ls', 'arguments': ''}}]),
        delta(tool_calls=[{'index': 0, 'function': {'arguments': '{"x":1}'}}]),
        delta(tool_calls=[{'index': 1, 'function': {'arguments': '{"y":2}'}}]),
        finish('tool_calls'),
    )
    events = await collect(chat_stream_to_responses_stream(agen(chunks), model='qwen'))

    done = [e['item'] for e in events if e['type'] == 'response.output_item.done']
    assert [(i['call_id'], i['arguments']) for i in done] == [('a', '{"x":1}'), ('b', '{"y":2}')]
    assert [e['output_index'] for e in events if e['type'] == 'response.output_item.added'] == [0, 1]
    assert events[-1]['response']['output'] == done


@pytest.mark.asyncio
async def test_stream_ending_without_finish_reason_fails_the_response():
    chunks = [f'data: {json.dumps(delta(content="hi"))}\n\n'.encode()]
    events = await collect(chat_stream_to_responses_stream(agen(chunks), model='qwen'))

    assert events[-1]['type'] == 'response.failed'
    assert events[-1]['response']['error']['message'] == 'Stream ended prematurely'
    assert 'response.completed' not in [e['type'] for e in events]


@pytest.mark.asyncio
async def test_stream_finish_reason_without_done_marker_completes():
    chunks = [f'data: {json.dumps(finish("stop"))}\n\n'.encode()]
    events = await collect(chat_stream_to_responses_stream(agen(chunks), model='qwen'))

    assert events[-1]['type'] == 'response.completed'


@pytest.mark.asyncio
async def test_stream_keeps_multibyte_characters_split_across_reads():
    line = f'data: {json.dumps(delta(content="café"), ensure_ascii=False)}\n\ndata: [DONE]\n\n'.encode()
    cut = line.index('é'.encode()) + 1  # in the middle of the two-byte character
    events = await collect(chat_stream_to_responses_stream(agen([line[:cut], line[cut:]]), model='qwen'))

    assert ''.join(e['delta'] for e in events if e['type'] == 'response.output_text.delta') == 'café'


@pytest.mark.asyncio
async def test_json_response_replayed_as_a_stream():
    chat_response = {
        'choices': [
            {
                'message': {
                    'content': 'Checking.',
                    'reasoning_content': 'thinking',
                    'tool_calls': [{'id': 'c1', 'function': {'name': 'sh', 'arguments': '{"cmd":"ls"}'}}],
                },
                'finish_reason': 'tool_calls',
            }
        ],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 4},
    }
    events = await collect(chat_stream_to_responses_stream(chat_response_to_stream(chat_response), model='qwen'))

    done = [e['item'] for e in events if e['type'] == 'response.output_item.done']
    assert [item['type'] for item in done] == ['reasoning', 'message', 'function_call']
    assert done[2]['arguments'] == '{"cmd":"ls"}'
    assert events[-1]['type'] == 'response.completed'
    assert events[-1]['response']['usage']['output_tokens'] == 4


# --- endpoint ---


@pytest.fixture
def main(monkeypatch):
    import open_webui.main as main

    return main


@pytest.fixture
def chat(main, monkeypatch):
    """Replaces the chat pipeline and records the payload it received."""
    state = SimpleNamespace(payload=None, result={'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}]})

    async def chat_completion(request, payload, user):
        state.payload = payload
        return state.result

    monkeypatch.setattr(main, 'chat_completion', chat_completion)
    return state


def make_request():
    return SimpleNamespace(state=SimpleNamespace())


@pytest.mark.asyncio
async def test_endpoint_routes_through_chat_pipeline(main, chat):
    response = await main.responses(make_request(), {'model': 'qwen', 'input': 'hi'}, USER)

    assert chat.payload['messages'] == [{'role': 'user', 'content': 'hi'}]
    assert response['object'] == 'response'
    assert response['model'] == 'qwen'
    assert response['output'][0]['content'][0]['text'] == 'ok'


@pytest.mark.asyncio
async def test_endpoint_streams_events(main, chat):
    async def body():
        for chunk in sse(delta(content='ok'), finish('stop')):
            yield chunk

    chat.result = StreamingResponse(body(), media_type='text/event-stream')
    response = await main.responses(make_request(), {'model': 'qwen', 'input': 'hi', 'stream': True}, USER)

    assert response.media_type == 'text/event-stream'
    events = await collect(response.body_iterator)
    assert events[-1]['type'] == 'response.completed'


@pytest.mark.asyncio
async def test_endpoint_passes_chat_errors_through(main, chat):
    chat.result = JSONResponse(status_code=429, content={'error': {'message': 'busy'}}, headers={'Retry-After': '3'})

    response = await main.responses(make_request(), {'model': 'qwen', 'input': 'hi'}, USER)

    assert response.status_code == 429
    assert response.headers['retry-after'] == '3'


@pytest.mark.asyncio
async def test_endpoint_rejects_previous_response_id(main, chat):
    with pytest.raises(HTTPException) as error:
        await main.responses(make_request(), {'model': 'qwen', 'previous_response_id': 'resp_1'}, USER)

    assert error.value.status_code == 400
    assert chat.payload is None


@pytest.mark.asyncio
async def test_endpoint_streams_when_pipeline_answers_with_json(main, chat):
    response = await main.responses(make_request(), {'model': 'qwen', 'input': 'hi', 'stream': True}, USER)

    assert response.media_type == 'text/event-stream'
    events = await collect(response.body_iterator)
    assert events[-1]['type'] == 'response.completed'
    assert events[-1]['response']['output'][0]['content'][0]['text'] == 'ok'


@pytest.mark.asyncio
async def test_endpoint_turns_a_200_error_body_into_an_error(main, chat):
    chat.result = {'error': {'message': 'pipe failed'}}

    with pytest.raises(HTTPException) as error:
        await main.responses(make_request(), {'model': 'qwen', 'input': 'hi'}, USER)

    assert error.value.status_code == 502
    assert error.value.detail == 'pipe failed'
