import json
import logging
import time as _time
import uuid as _uuid

log = logging.getLogger(__name__)


# Keys that only exist in the legacy (text) completions request and must NOT
# be forwarded to the Chat Completions handler.
LEGACY_ONLY_KEYS = {'prompt', 'suffix', 'echo', 'best_of', 'logprobs'}

# Task recorded for /api/completions requests, which come from IDE autocomplete
# rather than an interactive chat. Connection headers templated with {{TASK}}
# (e.g. X-Gateway-Task) render it, so a gateway can tell the two apart.
COMPLETIONS_TASK = 'autocomplete'


def with_completions_task(form_data: dict) -> dict:
    """
    Return a copy of a legacy completions payload whose ``metadata.task`` is
    set: the caller's own task if it gave one, :data:`COMPLETIONS_TASK`
    otherwise. The rest of the caller's metadata (e.g. ``chat_id``) is kept.
    """
    metadata = form_data.get('metadata')
    metadata = metadata if isinstance(metadata, dict) else {}
    return {**form_data, 'metadata': {**metadata, 'task': metadata.get('task') or COMPLETIONS_TASK}}


def normalize_completion_prompt(prompt) -> str:
    """
    Normalize the OpenAI legacy ``prompt`` field to a single string.

    The OpenAI spec allows ``prompt`` to be a string, an array of strings, or an
    array of token arrays. We support string and array-of-strings (joined with
    newlines); anything else is coerced to ``str`` as a best effort.
    """
    if prompt is None:
        return ''
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        parts = []
        for p in prompt:
            parts.append(p if isinstance(p, str) else str(p))
        return '\n'.join(parts)
    return str(prompt)


def convert_completions_to_chat_payload(form_data: dict) -> dict:
    """
    Convert an OpenAI legacy (text) completions payload to the Chat Completions
    payload understood by the internal chat completion pipeline.

    The ``prompt`` is forwarded verbatim as a single user message so callers
    (e.g. continue.dev autocomplete) can supply their own FIM template. When a
    ``suffix`` is provided we fall back to a best-effort fill-in-the-middle
    framing, since chat models cannot natively insert between prefix/suffix.
    All other recognized sampling parameters (``max_tokens``, ``temperature``,
    ``top_p``, ``n``, ``stream``, ``stop``, ``presence_penalty``,
    ``frequency_penalty``, ``seed``, ``user``, ...) are passed through as-is.
    """
    prompt_text = normalize_completion_prompt(form_data.get('prompt', ''))
    suffix = form_data.get('suffix') or ''

    if suffix:
        content = (
            'You are a fill-in-the-middle completion engine. Output only the '
            'text that belongs between <prefix> and <suffix>, with no '
            'explanation or surrounding markup.\n'
            f'<prefix>{prompt_text}</prefix>\n<suffix>{suffix}</suffix>'
        )
    else:
        content = prompt_text

    chat_payload = {k: v for k, v in form_data.items() if k not in LEGACY_ONLY_KEYS}
    chat_payload['messages'] = [{'role': 'user', 'content': content}]
    return chat_payload


def convert_chat_to_completions_response(chat_response: dict, model: str = '', echo_prompt: str = '') -> dict:
    """
    Convert a non-streaming Chat Completions response to the legacy
    ``text_completion`` response shape.
    """
    choices_out = []
    for choice in chat_response.get('choices', []) or []:
        message = choice.get('message', {}) or {}
        text = message.get('content') or ''
        if echo_prompt:
            text = echo_prompt + text
        choices_out.append(
            {
                'text': text,
                'index': choice.get('index', len(choices_out)),
                'logprobs': None,
                'finish_reason': choice.get('finish_reason', 'stop'),
            }
        )

    if not choices_out:
        choices_out = [
            {
                'text': echo_prompt or '',
                'index': 0,
                'logprobs': None,
                'finish_reason': 'stop',
            }
        ]

    response_id = chat_response.get('id') or f'cmpl-{_uuid.uuid4().hex}'
    response_id = response_id.replace('chatcmpl-', 'cmpl-')

    return {
        'id': response_id,
        'object': 'text_completion',
        'created': chat_response.get('created', int(_time.time())),
        'model': model or chat_response.get('model', ''),
        'choices': choices_out,
        'usage': chat_response.get('usage', {}),
    }


async def chat_stream_to_completions_stream(chat_stream_generator, model: str = '', echo_prompt: str = ''):
    """
    Convert a Chat Completions SSE stream to a legacy completions SSE stream.

    Chat sends: ``data: {"choices": [{"delta": {"content": "..."}}]}``
    Legacy sends: ``data: {"object": "text_completion", "choices": [{"text": "..."}]}``
    """
    cmpl_id = f'cmpl-{_uuid.uuid4().hex}'
    created = int(_time.time())

    def _make_chunk(text, index, finish_reason, usage=None):
        chunk = {
            'id': cmpl_id,
            'object': 'text_completion',
            'created': created,
            'model': model,
            'choices': [
                {
                    'text': text,
                    'index': index,
                    'logprobs': None,
                    'finish_reason': finish_reason,
                }
            ],
        }
        if usage is not None:
            chunk['usage'] = usage
        return chunk

    def _make_usage_chunk(usage):
        return {
            'id': cmpl_id,
            'object': 'text_completion',
            'created': created,
            'model': model,
            'choices': [],
            'usage': usage,
        }

    # Echo the prompt back first if requested.
    if echo_prompt:
        yield f'data: {json.dumps(_make_chunk(echo_prompt, 0, None))}\n\n'.encode()

    def _convert_line(line):
        """Convert one SSE line to zero or more legacy chunks. Returns (chunks, stop)."""
        line = line.strip()
        if not line or not line.startswith('data:'):
            return [], False

        data_str = line[5:].strip()
        if data_str == '[DONE]':
            return [], False

        try:
            data = json.loads(data_str)
        except (json.JSONDecodeError, TypeError):
            return [], False
        if not isinstance(data, dict):
            return [], False

        out = []
        choices = data.get('choices') or []
        usage = data.get('usage')

        if not choices:
            # Error event (e.g. context overflow, model load failure): surface it
            # instead of ending with an empty, apparently successful completion.
            if data.get('error'):
                log.warning(f'Upstream error while streaming completions: {data["error"]}')
                out.append({'error': data['error']})
                return out, True
            # Final usage-only chunk.
            if usage:
                out.append(_make_usage_chunk(usage))
            return out, False

        emitted = 0
        for choice in choices:
            index = choice.get('index', 0)
            delta = choice.get('delta', {}) or {}
            text = delta.get('content')
            finish_reason = choice.get('finish_reason')

            # Skip role-only / empty keep-alive deltas.
            if text is None and finish_reason is None:
                continue

            out.append(_make_chunk(text or '', index, finish_reason))
            emitted += 1

        # Some providers (e.g. the Ollama conversion) put usage on the final
        # choice-bearing chunk rather than on a trailing usage-only chunk.
        if usage:
            if emitted:
                out[-1]['usage'] = usage
            else:
                out.append(_make_usage_chunk(usage))
        return out, False

    # Upstream may relay raw network reads, so an SSE line can be split across
    # chunks: buffer partial lines instead of dropping them.
    pending = ''
    stopped = False
    try:
        async for chunk in chat_stream_generator:
            if isinstance(chunk, bytes):
                chunk = chunk.decode('utf-8', errors='ignore')

            pending += chunk
            lines = pending.split('\n')
            pending = lines.pop()

            for line in lines:
                chunks, stopped = _convert_line(line)
                for c in chunks:
                    yield f'data: {json.dumps(c)}\n\n'.encode()
                if stopped:
                    break
            if stopped:
                break

        if pending.strip() and not stopped:
            chunks, stopped = _convert_line(pending)
            for c in chunks:
                yield f'data: {json.dumps(c)}\n\n'.encode()

    except Exception as e:
        log.error(f'Error in completions stream conversion: {e}')

    yield b'data: [DONE]\n\n'
