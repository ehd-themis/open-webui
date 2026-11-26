"""
Files written by the Pyodide code interpreter, attached to the chat message.

Pyodide runs in the person's browser, in a sandboxed iframe with an opaque origin: what the code
writes to `/mnt/uploads/` lives only in that tab and is gone after a reload. After a code
interpreter run started by the model, the page (same origin as Hermes, outside the sandbox) uploads
the files the run created or changed with the person's own session and returns their ids next to
stdout/stderr. This module checks those ids, attaches the files to the assistant message (download
chips under the answer, persisted with the chat) and tells the model their download links.

The ids come from the browser, so each one must name an existing file owned by the person running
the chat; anything else is dropped.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from open_webui.models.files import Files

log = logging.getLogger(__name__)

# A run that writes hundreds of files should not flood the message.
MAX_ATTACHED_FILES = 20


def file_content_url(file_id: str) -> str:
    return f'/api/v1/files/{file_id}/content'


async def collect_code_interpreter_files(output: Any, user_id: str | None) -> list[dict]:
    """Return message file items for the uploaded ids in `output['files']` that belong to `user_id`."""
    if not isinstance(output, dict) or not user_id:
        return []
    entries = output.get('files')
    if not isinstance(entries, list):
        return []

    items: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        if len(items) >= MAX_ATTACHED_FILES:
            break
        file_id = entry.get('id') if isinstance(entry, dict) else None
        if not isinstance(file_id, str) or not file_id or file_id in seen:
            continue
        seen.add(file_id)

        file = await Files.get_file_by_id_and_user_id(file_id, user_id)
        if file is None:
            log.warning('Code interpreter returned a file id the user does not own: %s', file_id)
            continue

        meta = file.meta or {}
        items.append(
            {
                'type': 'file',
                'id': file.id,
                'url': file_content_url(file.id),
                'name': meta.get('name') or file.filename or 'file',
                'size': meta.get('size'),
                'content_type': meta.get('content_type'),
            }
        )
    return items


def describe_files_for_model(items: list[dict]) -> str:
    """One line per file, for the tool result, so the model can link the files it produced."""
    return '\n'.join(f'- {item["name"]}: {item["url"]}' for item in items)


async def attach_code_interpreter_files(
    output: Any,
    user_id: str | None,
    event_emitter: Callable[[dict], Awaitable[Any]] | None,
) -> list[dict]:
    """
    Attach the files of a code interpreter run to the current message and return their items.

    `output['files']` (the browser's list) is replaced by the checked items, without any content,
    so the tool result the model reads holds names and links only.
    """
    items = await collect_code_interpreter_files(output, user_id)
    if isinstance(output, dict) and 'files' in output:
        output['files'] = [{'name': item['name'], 'url': item['url']} for item in items]
    if items and event_emitter is not None:
        try:
            await event_emitter({'type': 'files', 'data': {'files': [dict(item) for item in items]}})
        except Exception as e:
            log.warning('Could not attach code interpreter files to the message: %s', e)
    return items
