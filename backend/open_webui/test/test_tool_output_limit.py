"""
A tool result is sent back to the model as text. A downloaded file (base64) or a whole document can
be larger than the model's window, so base64 is replaced with a marker and the rest is cut to
CHAT_RESPONSE_TOOL_RESULT_MAX_CHARS characters.
"""

import base64
import json
import os

import pytest
from open_webui.utils.misc import convert_output_to_messages
from open_webui.utils.tool_output_limit import limit_tool_output, replace_base64_blobs

PNG_HEADER = b'\x89PNG\r\n\x1a\n'


def fake_png(size):
    return base64.b64encode(PNG_HEADER + os.urandom(size)).decode()


def tool_output(text):
    return [
        {
            'type': 'function_call',
            'call_id': 'c1',
            'name': 'm365_download-bytes',
            'arguments': '{}',
            'status': 'completed',
        },
        {
            'type': 'function_call_output',
            'call_id': 'c1',
            'output': [{'type': 'input_text', 'text': text}],
            'status': 'completed',
        },
    ]


def test_short_text_is_unchanged():
    assert limit_tool_output('{"a": 1}', max_chars=100) == '{"a": 1}'


def test_long_base64_png_is_replaced_by_a_marker():
    blob = fake_png(224 * 1024)
    text = json.dumps({'message': 'OK!', 'data': blob})

    limited = limit_tool_output(text, max_chars=30000)

    assert blob not in limited
    assert json.loads(limited) == {'message': 'OK!', 'data': '[binary PNG data, 224 KB, not shown]'}


def test_base64_of_unknown_type_gets_a_generic_marker():
    blob = base64.b64encode(os.urandom(3000)).decode()
    assert replace_base64_blobs(f'x {blob} y') == 'x [binary data, 3 KB, not shown] y'


def test_short_base64_and_ordinary_text_are_kept():
    text = 'Bonjour ' * 500 + base64.b64encode(os.urandom(300)).decode()
    assert replace_base64_blobs(text) == text


def test_a_long_run_of_one_case_is_not_taken_for_base64():
    text = 'a' * 5000
    assert replace_base64_blobs(text) == text


def test_long_text_is_cut_with_a_note():
    limited = limit_tool_output('x' * 50000, max_chars=30000)

    assert limited.startswith('x' * 30000 + '\n\n[Output cut: 20000 of 50000 characters not shown.')
    assert len(limited) < 30500


def test_limit_of_zero_turns_the_cut_off_but_not_the_base64_marker():
    text = 'x ' * 40000 + fake_png(5000)

    limited = limit_tool_output(text, max_chars=0)

    assert limited.startswith('x ' * 40000)
    assert limited.endswith('[binary PNG data, 5 KB, not shown]')


def test_default_limit_comes_from_the_environment_setting():
    assert limit_tool_output('x' * 29999) == 'x' * 29999
    assert 'Output cut' in limit_tool_output('x' * 30001)


def test_a_stored_oversized_result_is_limited_in_the_messages_sent_to_the_model():
    messages = convert_output_to_messages(tool_output(json.dumps({'message': 'OK!', 'data': fake_png(224 * 1024)})))

    tool_message = next(m for m in messages if m['role'] == 'tool')
    assert len(tool_message['content']) < 200
    assert '[binary PNG data, 224 KB, not shown]' in tool_message['content']


def test_a_stored_long_text_result_is_cut_in_the_messages_sent_to_the_model():
    messages = convert_output_to_messages(tool_output('y' * 100000))

    tool_message = next(m for m in messages if m['role'] == 'tool')
    assert tool_message['content'].startswith('y' * 30000)
    assert 'Output cut: 70000 of 100000' in tool_message['content']


@pytest.mark.asyncio
async def test_process_tool_result_replaces_base64_from_an_mcp_tool():
    from open_webui.utils.middleware import process_tool_result

    blob = fake_png(224 * 1024)
    mcp_result = [{'type': 'text', 'text': json.dumps({'message': 'OK!', 'base64': blob})}]

    text, files, embeds = await process_tool_result(
        None, 'm365_download-bytes', mcp_result, 'mcp', metadata={}, user=None
    )

    assert blob not in text
    assert '[binary PNG data, 224 KB, not shown]' in text
    assert files == [] and embeds == []
