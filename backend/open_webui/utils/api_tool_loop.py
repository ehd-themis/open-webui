"""
Server-side tools over plain HTTP (fork patch).

Upstream runs the tool loop ("the model calls a tool, Open WebUI runs it, the model goes on") only for requests that
have an event emitter, i.e. a chat id and a message id: the answer then reaches the browser over the websocket and
the chat is saved. A plain `POST /api/chat/completions` (a script, an Office add-in) that sent `tool_ids` got the
provider's stream back untouched: the tools were resolved (and an MCP server connected) but never run, and the caller
received `tool_calls` it could not execute. Upstream also ignores `tool_ids` as soon as the request carries its own
`tools`.

This module closes both gaps without touching the chat UI path:

* `merge_caller_and_server_tools`: one request may carry the caller's own `tools` (run by the caller) AND `tool_ids`
  (run here, with the person's own credentials, e.g. `server:mcp:m365`). Both specs go to the model. A name used by
  both belongs to the caller.
* `stream_with_server_tools`: the HTTP streaming loop. Nothing is saved and no websocket is used.
  - A model turn whose tool calls are all server tools: they are run, their results go back to the model and the
    next turn is streamed on, all in the same HTTP response.
  - A turn with at least one caller tool call: its server calls are run first, then the response ends with the
    caller's calls as ordinary OpenAI `tool_calls` (finish_reason `tool_calls`).
  - Text and reasoning deltas are relayed as they arrive. Server tool-call deltas are never relayed (the caller
    must not try to run them).

Extra information for the caller rides in the non-standard top-level key `open_webui` of a chunk, which OpenAI clients
ignore (see README of this change in CHANGELOG.fork.md for the exact format):

    {"open_webui": {"tool_connection_errors": [{"server": "m365", "error": "auth_required"}]}}
    {"open_webui": {"server_tool_call": {"id": "...", "name": "m365_list-mail-messages", "status": "completed"}}}
    {"open_webui": {"server_tool_calls": [{"id", "type": "function", "function": {"name", "arguments"},
                                           "status", "result"}]}}

`server_tool_calls` (only when the turn ends with caller tool calls) lists every server call run during this
response, in order, with its result, so that the caller can rebuild a consistent history: one assistant message
whose `tool_calls` are these calls followed by its own, then a `tool` message per call.
"""

import copy
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from fastapi.responses import StreamingResponse

log = logging.getLogger(__name__)

# Tools that need a chat session or a browser (terminal, direct tools, built-ins): never run from plain HTTP.
NOT_FROM_PLAIN_HTTP = frozenset({'builtin', 'terminal', 'direct', 'tether'})

FINAL_ROUND_NOTICE = (
    'The limit of tool-call rounds for this reply has been reached, so tools are switched off. '
    'Answer now with what you found and say what is still unfinished.'
)


# --- request side ---------------------------------------------------------------------------------------------


def tool_name_of(spec: Any) -> str:
    """Name of an OpenAI tool spec (`{"type": "function", "function": {"name": ...}}`), '' when absent."""
    function = spec.get('function') if isinstance(spec, dict) else None
    name = function.get('name') if isinstance(function, dict) else None
    return name if isinstance(name, str) else ''


def merge_caller_and_server_tools(caller_tools: list, server_tools: dict[str, dict]) -> tuple[list, dict[str, dict]]:
    """Specs for the model, and the server tools that remain.

    The caller's specs come first and are kept as they are. A server tool whose name is also a caller tool is dropped
    (the caller wins): it is not offered twice and never run here. Returns (specs, remaining server tools).
    """
    caller_names = {tool_name_of(spec) for spec in caller_tools if tool_name_of(spec)}
    remaining = {name: tool for name, tool in server_tools.items() if name not in caller_names}
    for name in server_tools.keys() & caller_names:
        log.warning('Tool name %r is both a caller tool and a server tool: the caller tool wins', name)
    specs = [*caller_tools, *({'type': 'function', 'function': tool.get('spec', {})} for tool in remaining.values())]
    return specs, remaining


def record_connection_error(metadata: dict, server_id: str, error: str, detail: str = '') -> None:
    """Remember a tool connection that failed, so the caller can be told (auth_required | unavailable | failed)."""
    errors = metadata.setdefault('tool_connection_errors', [])
    entry = {'server': server_id, 'error': error}
    if detail:
        entry['detail'] = detail[:200]
    if entry not in errors:
        errors.append(entry)


def server_tool_names(metadata: dict) -> set[str]:
    tools = metadata.get('tools') or {}
    return {
        name
        for name, tool in tools.items()
        if callable(tool.get('callable')) and (tool.get('type') or '') not in NOT_FROM_PLAIN_HTTP
    }


def wants_loop(metadata: dict, event_emitter: Any) -> bool:
    """Does this request take the plain-HTTP loop?

    Yes for a request that mixes caller tools with `tool_ids` (always: the caller must get its own calls back), and
    for a request without event emitter (no chat id + message id) that has server tools, or a connection that failed
    (so that the caller hears about it). A request with an emitter (the chat UI, the Teams bridge) keeps the upstream
    loop.
    """
    if metadata.get('params', {}).get('function_calling') == 'legacy':
        return False
    if metadata.get('api_tool_mix'):
        return True
    if event_emitter is not None:
        return False
    return bool(server_tool_names(metadata) or metadata.get('tool_connection_errors'))


# --- stream side ----------------------------------------------------------------------------------------------


def sse(payload: dict) -> str:
    return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'


def _events(raw: Any):
    """The `data:` payloads of one item of a provider stream (bytes or str, one or several events)."""
    text = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else raw
    for line in text.splitlines():
        if line.startswith('data:'):
            yield line.removeprefix('data:').strip()


class _Round:
    """One model turn, as it streams in."""

    def __init__(self) -> None:
        self.text = ''
        self.calls: dict[int, dict] = {}
        self.finish: str | None = None
        self.usage: dict | None = None
        self.error: Any = None

    def add_tool_call_delta(self, delta: dict) -> None:
        index = delta.get('index')
        if not isinstance(index, int):
            index = len(self.calls)
        call = self.calls.setdefault(index, {'id': '', 'name': '', 'arguments': ''})
        if delta.get('id'):
            call['id'] = delta['id']
        function = delta.get('function') or {}
        name = function.get('name')
        if name and not call['name']:
            call['name'] = name
        elif name and name != call['name'] and not call['name'].endswith(name):
            call['name'] += name  # a provider that splits the name over several deltas
        if isinstance(function.get('arguments'), str):
            call['arguments'] += function['arguments']

    def ordered_calls(self) -> list[dict]:
        calls = [self.calls[i] for i in sorted(self.calls)]
        for call in calls:
            if not call['id']:
                call['id'] = f'call_{uuid.uuid4().hex[:24]}'
            if not call['arguments'].strip():
                call['arguments'] = '{}'
        return [call for call in calls if call['name']]


async def run_server_call(request, user, metadata: dict, form_data: dict, call: dict, history: list) -> dict:
    """Run one server tool call; always returns {"status", "result"} (errors become text for the model)."""
    from open_webui.utils.middleware import (
        _is_tool_result_error,
        execute_tool_call,
        process_tool_result,
        tool_result_content,
    )
    from open_webui.utils.tool_output_limit import limit_tool_output

    tool_call = {
        'id': call['id'],
        'call_id': call['id'],
        'name': call['name'],
        'arguments': call['arguments'],
        'status': 'in_progress',
    }
    try:
        params, result, tool, tool_type = await execute_tool_call(form_data, metadata, None, tool_call, history)
        if result is None:
            text = (
                'Error: Tool call arguments could not be parsed. The model generated malformed or incomplete JSON '
                f'for `{call["name"]}`. Please try again.'
            )
        else:
            result, files, _embeds = await process_tool_result(request, call['name'], result, tool_type, metadata, user)
            text = tool_result_content(result)
            if files:
                text += f'\n[{len(files)} file(s) returned by the tool are not included over this API.]'
    except Exception as exc:  # a tool that raises must not end the stream
        log.exception('Server tool %s failed', call['name'])
        text = f'Error: {exc}'
    status = 'failed' if _is_tool_result_error(text) or tool_call.get('status') == 'failed' else 'completed'
    tool_call['status'] = status
    return {'status': status, 'result': limit_tool_output(text)}


async def stream_with_server_tools(  # noqa: C901 - one linear loop; splitting it would hide the order of events
    request, user, form_data: dict, metadata: dict, response
) -> AsyncGenerator[str, None]:
    """The HTTP response of a request that has server tools: see the module docstring."""
    from open_webui.env import CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS
    from open_webui.utils.chat import generate_chat_completion
    from open_webui.utils.response import convert_responses_stream_to_chat_completions

    server_names = server_tool_names(metadata)
    max_rounds = getattr(request.state, 'max_tool_call_iterations', CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS)
    messages = list(form_data.get('messages') or [])
    history: list[dict] = []  # server calls already made (repetition guard of execute_tool_call)
    executed: list[dict] = []  # every server call made in this response, with its result, for the caller
    base: dict[str, Any] = {}
    rounds = 0
    final_round = False
    last_usage: dict | None = None
    upstream = response

    def chunk(
        delta: dict | None = None, finish: str | None = None, extra: dict | None = None, usage: dict | None = None
    ):
        payload = {
            'id': base.get('id') or f'chatcmpl-{uuid.uuid4().hex}',
            'object': 'chat.completion.chunk',
            'created': base.get('created') or 0,
            'model': base.get('model') or form_data.get('model', ''),
            'choices': []
            if delta is None and finish is None
            else [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}],
        }
        if usage:
            payload['usage'] = usage
        if extra:
            payload['open_webui'] = extra
        return sse(payload)

    try:
        if metadata.get('tool_connection_errors'):
            yield chunk(extra={'tool_connection_errors': metadata['tool_connection_errors']})

        while True:
            this = _Round()
            async for item in convert_responses_stream_to_chat_completions(upstream.body_iterator):
                for data in _events(item):
                    if data == '[DONE]':
                        continue
                    try:
                        event = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if 'error' in event:
                        this.error = event['error']
                        break
                    for key in ('id', 'created', 'model'):
                        if event.get(key) and key not in base:
                            base[key] = event[key]
                    if isinstance(event.get('usage'), dict):
                        this.usage = event['usage']
                    choices = event.get('choices') or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = dict(choice.get('delta') or {})
                    for tool_delta in delta.pop('tool_calls', None) or []:
                        if isinstance(tool_delta, dict):
                            this.add_tool_call_delta(tool_delta)
                    if choice.get('finish_reason'):
                        this.finish = choice['finish_reason']
                    if isinstance(delta.get('content'), str):
                        this.text += delta['content']
                    if delta:
                        yield chunk(delta=delta)
                if this.error is not None:
                    break

            if this.error is not None:
                yield sse({'error': this.error})
                yield 'data: [DONE]\n\n'
                return
            if this.usage:
                last_usage = this.usage

            calls = [] if final_round else this.ordered_calls()
            server_calls = [c for c in calls if c['name'] in server_names]
            caller_calls = [c for c in calls if c['name'] not in server_names]

            # Server calls first (a mixed turn too): the caller gets their results with its own calls.
            for call in server_calls:
                outcome = await run_server_call(request, user, metadata, form_data, call, history)
                executed.append(
                    {
                        'id': call['id'],
                        'type': 'function',
                        'function': {'name': call['name'], 'arguments': call['arguments']},
                        **outcome,
                    }
                )
                yield chunk(
                    extra={
                        'server_tool_call': {'id': call['id'], 'name': call['name'], 'status': outcome['status']},
                    }
                )

            if caller_calls:
                yield chunk(
                    delta={
                        'tool_calls': [
                            {
                                'index': i,
                                'id': call['id'],
                                'type': 'function',
                                'function': {'name': call['name'], 'arguments': call['arguments']},
                            }
                            for i, call in enumerate(caller_calls)
                        ]
                    }
                )
                yield chunk(
                    finish='tool_calls', extra={'server_tool_calls': executed} if executed else None, usage=last_usage
                )
                yield 'data: [DONE]\n\n'
                return

            if not server_calls:
                yield chunk(finish=this.finish or 'stop', usage=last_usage)
                yield 'data: [DONE]\n\n'
                return

            # Server calls only: give the model the results and stream its next turn.
            rounds += 1
            messages = [
                *messages,
                {
                    'role': 'assistant',
                    'content': this.text or None,
                    'tool_calls': [
                        {
                            'id': c['id'],
                            'type': 'function',
                            'function': {'name': c['name'], 'arguments': c['arguments']},
                        }
                        for c in server_calls
                    ],
                },
                *(
                    {'role': 'tool', 'tool_call_id': c['id'], 'content': executed[-len(server_calls) + i]['result']}
                    for i, c in enumerate(server_calls)
                ),
            ]
            next_form = {**form_data, 'stream': True, 'metadata': metadata, 'messages': copy.copy(messages)}
            if max_rounds is not None and rounds >= max_rounds:
                final_round = True
                next_form.pop('tools', None)
                next_form.pop('tool_choice', None)
                next_form['messages'] = [
                    *next_form['messages'],
                    {'role': 'user', 'content': f'[System notice] {FINAL_ROUND_NOTICE}'},
                ]
            upstream = await generate_chat_completion(request, next_form, user, bypass_system_prompt=True)
            if not isinstance(upstream, StreamingResponse):
                detail = getattr(upstream, 'body', b'')
                detail = detail.decode('utf-8', 'replace')[:300] if isinstance(detail, bytes) else str(detail)[:300]
                yield sse({'error': {'message': f'The model did not answer after the tool results. {detail}'.strip()}})
                yield 'data: [DONE]\n\n'
                return
    except Exception as exc:
        log.exception('Plain-HTTP tool loop failed')
        yield sse({'error': {'message': f'Tool loop failed: {exc}'}})
        yield 'data: [DONE]\n\n'
    finally:
        # main.py leaves the MCP clients to this loop (`api_tool_loop_owns_mcp`): the stream is read after it returned.
        for client in reversed(list((metadata.get('mcp_clients') or {}).values())):
            try:
                await client.disconnect()
            except BaseException as exc:  # noqa: BLE001 - same policy as main.py: one failure must not skip the rest
                log.debug('Error disconnecting MCP client: %s', exc)


def streaming_response(request, user, form_data: dict, metadata: dict, response) -> StreamingResponse:
    """Hand the MCP clients to the stream and return it as the HTTP response."""
    metadata['api_tool_loop_owns_mcp'] = True
    return StreamingResponse(
        stream_with_server_tools(request, user, form_data, metadata, response),
        media_type='text/event-stream',
        headers={
            k: v for k, v in dict(response.headers).items() if k.lower() not in ('content-length', 'content-type')
        },
        background=response.background,
    )
