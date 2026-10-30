"""
Keep oversized tool output out of the model's context.

A tool can return far more than a model can read: a binary file as base64 (a 224 KB PNG is about
245k tokens, since base64 tokenises at roughly 1.25 characters per token) or a whole document as
text. Both are cut down before the output is sent back to the model:

- Long base64 runs are replaced with a short marker naming the file type and size.
- What remains is cut to CHAT_RESPONSE_TOOL_RESULT_MAX_CHARS characters, with a note saying how
  much was left out.

The cut is applied when messages are built from the stored output, so it also covers chats that
already hold an oversized result.
"""

import re

from open_webui.env import CHAT_RESPONSE_TOOL_RESULT_MAX_CHARS

# Shorter runs are left alone: they are more likely to be an identifier than a file.
BASE64_BLOB_MIN_LENGTH = 1000
BASE64_BLOB_RE = re.compile(r'[A-Za-z0-9+/]{%d,}={0,2}' % BASE64_BLOB_MIN_LENGTH)

# Leading base64 of well-known file formats.
BASE64_SIGNATURES = (
    ('iVBORw0KGg', 'PNG'),
    ('/9j/', 'JPEG'),
    ('R0lGOD', 'GIF'),
    ('UklGR', 'WebP'),
    ('JVBERi', 'PDF'),
    ('UEsDB', 'ZIP or Office'),
    ('0M8R4KGx', 'Office'),
    ('SUQz', 'MP3'),
    ('T2dnUw', 'Ogg'),
)


def _base64_marker(match: re.Match) -> str:
    blob = match.group(0)
    # Real base64 mixes upper and lower case; a run of one case is most likely something else.
    if blob.isupper() or blob.islower():
        return blob

    kind = next((name for prefix, name in BASE64_SIGNATURES if blob.startswith(prefix)), None)
    size_kb = max(1, round(len(blob) * 3 / 4 / 1024))
    return f'[binary {kind + " " if kind else ""}data, {size_kb} KB, not shown]'


def replace_base64_blobs(text: str) -> str:
    """Replace long base64 runs with a marker such as ``[binary PNG data, 224 KB, not shown]``."""
    return BASE64_BLOB_RE.sub(_base64_marker, text)


def limit_tool_output(text: str, max_chars: int | None = None) -> str:
    """Replace base64 blobs, then cut the text to ``max_chars`` (default: the environment setting).

    A limit of 0 or less turns the cut off; blobs are still replaced.
    """
    if not isinstance(text, str) or not text:
        return text

    if max_chars is None:
        max_chars = CHAT_RESPONSE_TOOL_RESULT_MAX_CHARS

    text = replace_base64_blobs(text)
    if max_chars and max_chars > 0 and len(text) > max_chars:
        omitted = len(text) - max_chars
        text = (
            f'{text[:max_chars]}\n\n[Output cut: {omitted} of {len(text)} characters not shown. '
            'Ask for less (a smaller range, fewer fields or a more specific query) if the rest is needed.]'
        )
    return text
