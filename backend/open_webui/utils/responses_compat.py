"""
OpenAI Responses API compatibility for ``/api/responses``.

Clients such as Codex only speak the Responses API (``wire_api = "responses"``).
The request is converted to a Chat Completions payload so it goes through the
regular chat pipeline (presets, access control, task headers), then the chat
result is converted back to a Responses object or event stream.

The conversion is stateless: the whole conversation is expected in ``input`` on
every call, which is how Codex works with ``store: false``.
"""

import json
import logging
import time as _time
import uuid as _uuid

log = logging.getLogger(__name__)

# Request keys copied as-is to the Chat Completions payload.
PASSTHROUGH_KEYS = ('model', 'stream', 'temperature', 'top_p', 'parallel_tool_calls', 'user', 'seed')

# Tool types without a Chat Completions equivalent (web_search, local_shell,
# image_generation, ...); they are dropped from the request.
FUNCTION_TOOL_TYPES = {'function'}
CUSTOM_TOOL_TYPE = 'custom'


class ResponsesRequestError(ValueError):
    """The Responses payload uses something this endpoint cannot translate."""


def _content_to_chat(content) -> str | list:
    """Convert a Responses message ``content`` to Chat Completions content."""
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if isinstance(part, str):
            parts.append({'type': 'text', 'text': part})
            continue
        if not isinstance(part, dict):
            continue
        part_type = part.get('type')
        if part_type in ('input_text', 'output_text', 'text'):
            parts.append({'type': 'text', 'text': part.get('text', '')})
        elif part_type == 'refusal':
            parts.append({'type': 'text', 'text': part.get('refusal', '')})
        elif part_type == 'input_image':
            image_url = part.get('image_url')
            if isinstance(image_url, dict):
                image_url = image_url.get('url')
            if image_url:
                image = {'url': image_url}
                if part.get('detail'):
                    image['detail'] = part['detail']
                parts.append({'type': 'image_url', 'image_url': image})
    if all(p['type'] == 'text' for p in parts):
        return '\n'.join(p['text'] for p in parts)
    return parts


def _output_to_text(output) -> str:
    """Tool result ``output`` is a string or a list of content parts."""
    if isinstance(output, str):
        return output
    if output is None:
        return ''
    content = _content_to_chat(output)
    if isinstance(content, str):
        return content
    return '\n'.join(p.get('text', '') for p in content if p.get('type') == 'text')


def _tool_call(call_id: str, name: str, arguments: str) -> dict:
    return {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}


def convert_responses_to_chat_payload(form_data: dict) -> tuple[dict, set]:
    """
    Convert a Responses API payload to a Chat Completions payload.

    Returns ``(chat_payload, custom_tools)`` where ``custom_tools`` holds the
    names of Responses "custom" (freeform) tools, which are exposed to the model
    as functions taking a single ``input`` string and must be converted back in
    the response.
    """
    if form_data.get('previous_response_id'):
        raise ResponsesRequestError('previous_response_id is not supported: send the full conversation in input')

    messages = []
    instructions = form_data.get('instructions')
    if instructions:
        messages.append({'role': 'system', 'content': instructions})

    items = form_data.get('input')
    if isinstance(items, str):
        items = [{'type': 'message', 'role': 'user', 'content': items}]

    for item in items or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get('type') or ('message' if item.get('role') else None)

        if item_type == 'message':
            role = item.get('role', 'user')
            role = 'system' if role == 'developer' else role
            messages.append({'role': role, 'content': _content_to_chat(item.get('content'))})

        elif item_type in ('function_call', 'custom_tool_call'):
            if item_type == 'custom_tool_call':
                arguments = json.dumps({'input': item.get('input', '')})
            else:
                arguments = item.get('arguments') or '{}'
            call = _tool_call(item.get('call_id') or item.get('id') or '', item.get('name', ''), arguments)
            # Consecutive calls (and the text said just before them) are one assistant turn.
            previous = messages[-1] if messages else None
            if previous and previous['role'] == 'assistant' and 'tool_calls' in previous:
                previous['tool_calls'].append(call)
            elif previous and previous['role'] == 'assistant' and not previous.get('tool_calls'):
                previous['tool_calls'] = [call]
            else:
                messages.append({'role': 'assistant', 'content': '', 'tool_calls': [call]})

        elif item_type in ('function_call_output', 'custom_tool_call_output'):
            messages.append(
                {
                    'role': 'tool',
                    'tool_call_id': item.get('call_id', ''),
                    'content': _output_to_text(item.get('output')),
                }
            )
        # reasoning items and built-in tool calls have no chat equivalent.

    payload = {key: form_data[key] for key in PASSTHROUGH_KEYS if form_data.get(key) is not None}
    payload['messages'] = messages
    if form_data.get('max_output_tokens') is not None:
        payload['max_tokens'] = form_data['max_output_tokens']
    if payload.get('stream'):
        payload['stream_options'] = {'include_usage': True}

    reasoning = form_data.get('reasoning')
    if isinstance(reasoning, dict) and reasoning.get('effort'):
        payload['reasoning_effort'] = reasoning['effort']

    text_format = (form_data.get('text') or {}).get('format')
    if isinstance(text_format, dict) and text_format.get('type') == 'json_schema':
        payload['response_format'] = {
            'type': 'json_schema',
            'json_schema': {k: text_format[k] for k in ('name', 'schema', 'strict') if k in text_format},
        }
    elif isinstance(text_format, dict) and text_format.get('type') == 'json_object':
        payload['response_format'] = {'type': 'json_object'}

    tools = []
    custom_tools = set()
    for tool in form_data.get('tools') or []:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get('type')
        if tool_type in FUNCTION_TOOL_TYPES and tool.get('name'):
            function = {'name': tool['name'], 'parameters': tool.get('parameters') or {'type': 'object'}}
            if tool.get('description'):
                function['description'] = tool['description']
            if tool.get('strict') is not None:
                function['strict'] = tool['strict']
            tools.append({'type': 'function', 'function': function})
        elif tool_type == CUSTOM_TOOL_TYPE and tool.get('name'):
            custom_tools.add(tool['name'])
            description = tool.get('description') or ''
            grammar = (tool.get('format') or {}).get('definition')
            if grammar:
                description = f'{description}\n\nThe input must follow this grammar:\n{grammar}'.strip()
            tools.append(
                {
                    'type': 'function',
                    'function': {
                        'name': tool['name'],
                        'description': description,
                        'parameters': {
                            'type': 'object',
                            'properties': {'input': {'type': 'string'}},
                            'required': ['input'],
                        },
                    },
                }
            )
    if tools:
        payload['tools'] = tools

        tool_choice = form_data.get('tool_choice')
        if isinstance(tool_choice, dict) and tool_choice.get('name'):
            tool_choice = {'type': 'function', 'function': {'name': tool_choice['name']}}
        elif isinstance(tool_choice, dict):
            tool_choice = None
        if tool_choice:
            payload['tool_choice'] = tool_choice

    return payload, custom_tools


# ---------------------------------------------------------------------------
# Response objects
# ---------------------------------------------------------------------------


def _usage(usage: dict | None) -> dict | None:
    if not isinstance(usage, dict) or not usage:
        return None
    input_tokens = usage.get('input_tokens', usage.get('prompt_tokens', 0)) or 0
    output_tokens = usage.get('output_tokens', usage.get('completion_tokens', 0)) or 0
    prompt_details = usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}
    completion_details = usage.get('completion_tokens_details') or usage.get('output_tokens_details') or {}
    return {
        'input_tokens': input_tokens,
        'input_tokens_details': {'cached_tokens': prompt_details.get('cached_tokens') or 0},
        'output_tokens': output_tokens,
        'output_tokens_details': {'reasoning_tokens': completion_details.get('reasoning_tokens') or 0},
        'total_tokens': usage.get('total_tokens') or input_tokens + output_tokens,
    }


def _response_object(response_id, created_at, model, status, output, usage=None, incomplete_reason=None, error=None):
    return {
        'id': response_id,
        'object': 'response',
        'created_at': created_at,
        'status': status,
        'error': error,
        'incomplete_details': {'reason': incomplete_reason} if incomplete_reason else None,
        'model': model,
        'output': output,
        'parallel_tool_calls': True,
        'tool_choice': 'auto',
        'tools': [],
        'usage': _usage(usage),
    }


def _new_id(prefix: str) -> str:
    return f'{prefix}_{_uuid.uuid4().hex[:24]}'


def _reasoning_item(item_id: str, text: str) -> dict:
    return {
        'id': item_id,
        'type': 'reasoning',
        'summary': [{'type': 'summary_text', 'text': text}] if text else [],
    }


def _message_item(item_id: str, text: str, status: str = 'completed') -> dict:
    return {
        'id': item_id,
        'type': 'message',
        'role': 'assistant',
        'status': status,
        'content': [{'type': 'output_text', 'text': text, 'annotations': []}] if text or status == 'completed' else [],
    }


def _call_item(item_id: str, call_id: str, name: str, arguments: str, custom: bool, status: str = 'completed') -> dict:
    if custom:
        try:
            parsed = json.loads(arguments or '{}')
        except json.JSONDecodeError:
            parsed = {}
        value = parsed.get('input') if isinstance(parsed, dict) else None
        return {
            'id': item_id,
            'type': 'custom_tool_call',
            'status': status,
            'call_id': call_id,
            'name': name,
            'input': value if isinstance(value, str) else (arguments or ''),
        }
    return {
        'id': item_id,
        'type': 'function_call',
        'status': status,
        'call_id': call_id,
        'name': name,
        'arguments': arguments or '{}',
    }


def _reasoning_text(source: dict) -> str:
    return source.get('reasoning_content') or source.get('reasoning') or source.get('thinking') or ''


def convert_chat_to_responses_response(chat_response: dict, model: str = '', custom_tools: set | None = None) -> dict:
    """Convert a non-streaming Chat Completions response to a Responses object."""
    custom_tools = custom_tools or set()
    choice = (chat_response.get('choices') or [{}])[0]
    message = choice.get('message') or {}
    output = []

    reasoning = _reasoning_text(message)
    if reasoning:
        output.append(_reasoning_item(_new_id('rs'), reasoning))

    text = message.get('content') or ''
    if text:
        output.append(_message_item(_new_id('msg'), text))

    for call in message.get('tool_calls') or []:
        function = call.get('function') or {}
        name = function.get('name', '')
        call_id = call.get('id') or _new_id('call')
        output.append(_call_item(_new_id('fc'), call_id, name, function.get('arguments', ''), name in custom_tools))

    if not output:
        output.append(_message_item(_new_id('msg'), ''))

    truncated = choice.get('finish_reason') == 'length'
    return _response_object(
        _new_id('resp'),
        int(chat_response.get('created') or _time.time()),
        model or chat_response.get('model', ''),
        'incomplete' if truncated else 'completed',
        output,
        usage=chat_response.get('usage'),
        incomplete_reason='max_output_tokens' if truncated else None,
    )


async def chat_stream_to_responses_stream(chat_stream_generator, model: str = '', custom_tools: set | None = None):
    """
    Convert a Chat Completions SSE stream to a Responses API event stream.

    Output items are emitted in the order they start: reasoning summary, then
    the assistant message, then each tool call. Custom (freeform) tool input is
    buffered and emitted once complete, since the model streams it as JSON.
    """
    custom_tools = custom_tools or set()
    response_id = _new_id('resp')
    created_at = int(_time.time())
    sequence = 0

    def event(event_type: str, **fields) -> bytes:
        nonlocal sequence
        data = {'type': event_type, 'sequence_number': sequence, **fields}
        sequence += 1
        return f'event: {event_type}\ndata: {json.dumps(data)}\n\n'.encode()

    output: list = []  # completed items, in output order
    current = None  # the open item: {'kind', 'index', 'id', ...}
    usage = None
    finish_reason = None
    error_message = None
    tool_calls: dict = {}  # tool call id -> open/closed state
    index_to_call_id: dict = {}

    def close_current():
        """Events that finish the open item, and append it to ``output``."""
        nonlocal current
        if current is None:
            return []
        events = []
        kind, index = current['kind'], current['index']
        if kind == 'reasoning':
            text = current['text']
            events.append(
                event(
                    'response.reasoning_summary_text.done',
                    item_id=current['id'],
                    output_index=index,
                    summary_index=0,
                    text=text,
                )
            )
            events.append(
                event(
                    'response.reasoning_summary_part.done',
                    item_id=current['id'],
                    output_index=index,
                    summary_index=0,
                    part={'type': 'summary_text', 'text': text},
                )
            )
            item = _reasoning_item(current['id'], text)
        elif kind == 'message':
            text = current['text']
            part = {'type': 'output_text', 'text': text, 'annotations': []}
            events.append(
                event(
                    'response.output_text.done',
                    item_id=current['id'],
                    output_index=index,
                    content_index=0,
                    text=text,
                )
            )
            events.append(
                event(
                    'response.content_part.done',
                    item_id=current['id'],
                    output_index=index,
                    content_index=0,
                    part=part,
                )
            )
            item = _message_item(current['id'], text)
        else:  # tool call
            call = current['call']
            item = _call_item(current['id'], call['id'], call['name'], call['arguments'], call['custom'])
            if call['custom']:
                events.append(
                    event(
                        'response.custom_tool_call_input.delta',
                        item_id=current['id'],
                        output_index=index,
                        delta=item['input'],
                    )
                )
                events.append(
                    event(
                        'response.custom_tool_call_input.done',
                        item_id=current['id'],
                        output_index=index,
                        input=item['input'],
                    )
                )
            else:
                events.append(
                    event(
                        'response.function_call_arguments.done',
                        item_id=current['id'],
                        output_index=index,
                        name=call['name'],
                        arguments=item['arguments'],
                    )
                )
            call['closed'] = True
        events.append(event('response.output_item.done', output_index=index, item=item))
        output.append(item)
        current = None
        return events

    def open_item(kind: str, item: dict, **extra):
        nonlocal current
        index = len(output)
        current = {'kind': kind, 'index': index, 'id': item['id'], 'text': '', **extra}
        return [event('response.output_item.added', output_index=index, item=item)]

    skeleton = _response_object(response_id, created_at, model, 'in_progress', [])
    yield event('response.created', response=skeleton)
    yield event('response.in_progress', response=skeleton)

    def handle(data: dict):
        nonlocal usage, finish_reason, error_message, current
        events = []

        if data.get('error'):
            error = data['error']
            error_message = (error.get('message') if isinstance(error, dict) else str(error)) or 'Stream failed'
            return events
        if isinstance(data.get('usage'), dict) and data['usage']:
            usage = data['usage']

        choices = data.get('choices') or []
        if not choices:
            return events
        delta = choices[0].get('delta') or {}
        message = choices[0].get('message') or {}
        if choices[0].get('finish_reason'):
            finish_reason = choices[0]['finish_reason']

        reasoning = _reasoning_text(delta)
        if reasoning:
            if current is None or current['kind'] != 'reasoning':
                events += close_current()
                item_id = _new_id('rs')
                events += open_item('reasoning', _reasoning_item(item_id, ''))
                events.append(
                    event(
                        'response.reasoning_summary_part.added',
                        item_id=item_id,
                        output_index=current['index'],
                        summary_index=0,
                        part={'type': 'summary_text', 'text': ''},
                    )
                )
            current['text'] += reasoning
            events.append(
                event(
                    'response.reasoning_summary_text.delta',
                    item_id=current['id'],
                    output_index=current['index'],
                    summary_index=0,
                    delta=reasoning,
                )
            )

        content = delta.get('content')
        if content:
            if current is None or current['kind'] != 'message':
                events += close_current()
                item_id = _new_id('msg')
                events += open_item('message', _message_item(item_id, '', status='in_progress'))
                events.append(
                    event(
                        'response.content_part.added',
                        item_id=item_id,
                        output_index=current['index'],
                        content_index=0,
                        part={'type': 'output_text', 'text': '', 'annotations': []},
                    )
                )
            current['text'] += content
            events.append(
                event(
                    'response.output_text.delta',
                    item_id=current['id'],
                    output_index=current['index'],
                    content_index=0,
                    delta=content,
                )
            )

        for tool_call in delta.get('tool_calls') or message.get('tool_calls') or []:
            position = tool_call.get('index', 0)
            call_id = tool_call.get('id')
            function = tool_call.get('function') or {}
            name = function.get('name') or ''
            arguments = function.get('arguments') or ''

            if call_id and call_id not in tool_calls:
                tool_calls[call_id] = {
                    'id': call_id,
                    'name': name,
                    'arguments': '',
                    'custom': name in custom_tools,
                    'closed': False,
                    'item_id': None,
                }
            if call_id:
                index_to_call_id[position] = call_id
            call = tool_calls.get(call_id or index_to_call_id.get(position))
            if call is None:
                # Argument-only delta for a call that never announced itself.
                call_id = _new_id('call')
                call = tool_calls[call_id] = {
                    'id': call_id,
                    'name': name,
                    'arguments': '',
                    'custom': name in custom_tools,
                    'closed': False,
                    'item_id': None,
                }
                index_to_call_id[position] = call_id
            if name and not call['name']:
                call['name'] = name
                call['custom'] = name in custom_tools
            if call['closed']:
                continue

            # A tool call item opens once its name is known; any other open item ends first.
            if call['item_id'] is None and call['name']:
                if current is not None:
                    events += close_current()
                call['item_id'] = _new_id('fc')
                events += open_item(
                    'tool',
                    _call_item(call['item_id'], call['id'], call['name'], '', call['custom'], status='in_progress'),
                    call=call,
                )
            call['arguments'] += arguments
            if arguments and not call['custom'] and current is not None and current.get('call') is call:
                events.append(
                    event(
                        'response.function_call_arguments.delta',
                        item_id=call['item_id'],
                        output_index=current['index'],
                        delta=arguments,
                    )
                )
        return events

    pending = ''
    try:
        async for chunk in chat_stream_generator:
            if isinstance(chunk, bytes):
                chunk = chunk.decode('utf-8', errors='ignore')
            pending += chunk
            lines = pending.split('\n')
            pending = lines.pop()
            for line in lines:
                line = line.strip()
                if not line.startswith('data:'):
                    continue
                data_string = line[5:].strip()
                if data_string in ('', '[DONE]', '{}'):
                    continue
                try:
                    data = json.loads(data_string)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(data, dict):
                    for e in handle(data):
                        yield e
                if error_message:
                    break
            if error_message:
                break
        if pending.strip().startswith('data:') and not error_message:
            data_string = pending.strip()[5:].strip()
            if data_string not in ('', '[DONE]', '{}'):
                try:
                    data = json.loads(data_string)
                    if isinstance(data, dict):
                        for e in handle(data):
                            yield e
                except (json.JSONDecodeError, TypeError):
                    pass
    except Exception as e:
        log.error(f'Error in Responses stream conversion: {e}')
        error_message = 'Chat completion stream failed'

    if error_message:
        failed = _response_object(
            response_id,
            created_at,
            model,
            'failed',
            output,
            usage=usage,
            error={'code': 'server_error', 'message': error_message},
        )
        yield event('response.failed', response=failed)
        return

    for e in close_current():
        yield e
    # A model that answered with nothing still gets an (empty) message, as the API does.
    if not output:
        item_id = _new_id('msg')
        for e in open_item('message', _message_item(item_id, '', status='in_progress')):
            yield e
        for e in close_current():
            yield e

    truncated = finish_reason == 'length'
    final = _response_object(
        response_id,
        created_at,
        model,
        'incomplete' if truncated else 'completed',
        output,
        usage=usage,
        incomplete_reason='max_output_tokens' if truncated else None,
    )
    yield event('response.incomplete' if truncated else 'response.completed', response=final)
