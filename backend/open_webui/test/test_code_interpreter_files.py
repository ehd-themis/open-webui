"""
Files a Pyodide code interpreter run writes to /mnt/uploads are uploaded by the page and come back
as ids next to stdout. Only ids of files owned by the person are attached to the message, and the
model only sees names and download links.
"""

import asyncio
from types import SimpleNamespace

from open_webui.utils import code_interpreter_files as cif


def fake_file(file_id, user_id, name, size=10, content_type='text/calendar'):
    return SimpleNamespace(
        id=file_id,
        user_id=user_id,
        filename=name,
        meta={'name': name, 'size': size, 'content_type': content_type},
    )


FILES = {
    'f-own': fake_file('f-own', 'u1', 'calendrier.ics'),
    'f-other': fake_file('f-other', 'u2', 'secret.txt'),
}


async def fake_get(file_id, user_id):
    file = FILES.get(file_id)
    return file if file and file.user_id == user_id else None


def run(coro):
    return asyncio.run(coro)


def test_attaches_own_files_and_drops_others(monkeypatch):
    monkeypatch.setattr(cif.Files, 'get_file_by_id_and_user_id', fake_get)
    events = []

    async def emitter(event):
        events.append(event)

    output = {
        'stdout': 'ok',
        'files': [{'id': 'f-own', 'name': 'x'}, {'id': 'f-other'}, {'id': 'missing'}, 'junk', {'id': 'f-own'}],
    }
    items = run(cif.attach_code_interpreter_files(output, 'u1', emitter))

    assert items == [
        {
            'type': 'file',
            'id': 'f-own',
            'url': '/api/v1/files/f-own/content',
            'name': 'calendrier.ics',
            'size': 10,
            'content_type': 'text/calendar',
        }
    ]
    assert events == [{'type': 'files', 'data': {'files': items}}]
    # The model sees names and links only.
    assert output['files'] == [{'name': 'calendrier.ics', 'url': '/api/v1/files/f-own/content'}]


def test_no_files_no_event(monkeypatch):
    monkeypatch.setattr(cif.Files, 'get_file_by_id_and_user_id', fake_get)
    events = []

    async def emitter(event):
        events.append(event)

    output = {'stdout': 'ok'}
    assert run(cif.attach_code_interpreter_files(output, 'u1', emitter)) == []
    assert events == []
    assert 'files' not in output

    output = {'stdout': 'ok', 'files': [{'id': 'f-other'}]}
    assert run(cif.attach_code_interpreter_files(output, 'u1', emitter)) == []
    assert events == []
    assert output['files'] == []


def test_non_dict_output_and_missing_user():
    assert run(cif.attach_code_interpreter_files('error text', 'u1', None)) == []
    assert run(cif.attach_code_interpreter_files({'files': [{'id': 'f-own'}]}, None, None)) == []


def test_emitter_failure_does_not_break_the_run(monkeypatch):
    monkeypatch.setattr(cif.Files, 'get_file_by_id_and_user_id', fake_get)

    async def emitter(event):
        raise RuntimeError('socket gone')

    output = {'files': [{'id': 'f-own'}]}
    items = run(cif.attach_code_interpreter_files(output, 'u1', emitter))
    assert [item['id'] for item in items] == ['f-own']


def test_caps_the_number_of_files(monkeypatch):
    many = {f'f{i}': fake_file(f'f{i}', 'u1', f'out{i}.csv') for i in range(cif.MAX_ATTACHED_FILES + 5)}

    async def get(file_id, user_id):
        return many.get(file_id)

    monkeypatch.setattr(cif.Files, 'get_file_by_id_and_user_id', get)
    output = {'files': [{'id': file_id} for file_id in many]}
    assert len(run(cif.attach_code_interpreter_files(output, 'u1', None))) == cif.MAX_ATTACHED_FILES
