"""Fork: SQLite encrypted with SQLCipher (internal/sqlcipher.py and utils/sqlcipher_tool.py)."""

import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip('sqlcipher3')

from open_webui.internal import sqlcipher
from open_webui.utils import sqlcipher_tool

BACKEND = Path(__file__).resolve().parents[2]
PASSWORD = "p'ass word é"


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PASSWORD', PASSWORD)
    monkeypatch.delenv('DATABASE_PASSWORD_FILE', raising=False)
    path = tmp_path / 'webui.db'
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA user_version = 7')
    connection.execute('CREATE TABLE chat (id TEXT PRIMARY KEY, title TEXT)')
    connection.execute('CREATE INDEX ix_chat_title ON chat (title)')
    connection.executemany('INSERT INTO chat VALUES (?, ?)', [(str(i), f'Réunion {i}') for i in range(200)])
    connection.commit()
    connection.close()
    return path


def titles(connection):
    return [row[0] for row in connection.execute('SELECT title FROM chat ORDER BY CAST(id AS INTEGER)')]


@pytest.mark.parametrize(
    'url, path',
    [
        ('sqlite+sqlcipher:////app/backend/data/webui.db', '/app/backend/data/webui.db'),
        ('sqlite+sqlcipher:///data/webui.db', 'data/webui.db'),
    ],
)
def test_database_path(url, path):
    assert sqlcipher.database_path(url) == path


def test_encrypt_then_decrypt_keeps_everything(database):
    assert sqlcipher_tool.main(['encrypt', '--db', str(database), '--swap']) == 0
    assert not sqlcipher_tool.is_plaintext(str(database))
    assert database.stat().st_mode & 0o077 == 0
    [kept] = database.parent.glob('webui.db.plaintext-*')
    assert sqlcipher_tool.is_plaintext(str(kept))

    encrypted = sqlcipher.connect(str(database), PASSWORD)
    assert len(titles(encrypted)) == 200
    assert encrypted.execute('PRAGMA user_version').fetchone()[0] == 7
    encrypted.execute("INSERT INTO chat VALUES ('200', 'written while encrypted')")
    encrypted.commit()
    encrypted.close()

    assert sqlcipher_tool.main(['decrypt', '--db', str(database), '--swap']) == 0
    plaintext = sqlite3.connect(database)
    assert titles(plaintext)[-1] == 'written while encrypted'
    assert len(titles(plaintext)) == 201
    plaintext.close()


def test_without_swap_the_original_is_untouched(database):
    before = database.read_bytes()
    assert sqlcipher_tool.main(['encrypt', '--db', str(database)]) == 0
    assert database.read_bytes() == before
    assert not sqlcipher_tool.is_plaintext(str(database) + '.encrypted')


def test_refuses_a_database_in_use(database, capsys):
    # Another process, as the application is: SQLite's file locks do not apply within one process.
    holder = subprocess.Popen(
        [
            sys.executable,
            '-c',
            'import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); '
            "c.execute('SELECT count(*) FROM chat').fetchone(); print('ready', flush=True); sys.stdin.read()",
            str(database),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == 'ready'
        assert sqlcipher_tool.main(['encrypt', '--db', str(database), '--swap']) == 1
    finally:
        holder.communicate(timeout=10)
    assert 'in use' in capsys.readouterr().err
    assert sqlcipher_tool.is_plaintext(str(database))
    assert not Path(str(database) + '.encrypted').exists()


def test_refuses_without_password_or_twice(database, monkeypatch, capsys):
    monkeypatch.delenv('DATABASE_PASSWORD')
    assert sqlcipher_tool.main(['encrypt', '--db', str(database)]) == 1
    monkeypatch.setenv('DATABASE_PASSWORD', PASSWORD)
    assert sqlcipher_tool.main(['encrypt', '--db', str(database), '--swap']) == 0
    assert sqlcipher_tool.main(['encrypt', '--db', str(database), '--swap']) == 1
    assert 'already encrypted' in capsys.readouterr().err


def test_password_file(database, monkeypatch, tmp_path):
    key = tmp_path / 'key'
    key.write_text(PASSWORD + '\n', encoding='utf-8')
    monkeypatch.delenv('DATABASE_PASSWORD')
    monkeypatch.setenv('DATABASE_PASSWORD_FILE', str(key))
    assert sqlcipher_tool.main(['encrypt', '--db', str(database), '--swap']) == 0
    sqlcipher.connect(str(database), PASSWORD).close()


def test_wrong_password_is_reported(database):
    assert sqlcipher_tool.main(['encrypt', '--db', str(database), '--swap']) == 0
    with pytest.raises(RuntimeError, match='wrong password'):
        sqlcipher.connect(str(database), 'not the password')


@pytest.mark.parametrize('encrypt', [False, True])
def test_snapshot_keeps_the_format_of_the_source(database, tmp_path, encrypt):
    if encrypt:
        assert sqlcipher_tool.main(['encrypt', '--db', str(database), '--swap']) == 0
    copy = tmp_path / 'copy.db'
    assert sqlcipher_tool.main(['snapshot', '--db', str(database), '--out', str(copy)]) == 0
    assert sqlcipher_tool.is_plaintext(str(copy)) is not encrypt
    connection = sqlcipher.connect(str(copy), PASSWORD) if encrypt else sqlite3.connect(copy)
    assert len(titles(connection)) == 200
    connection.close()


def test_application_engines_run_on_an_encrypted_database(tmp_path):
    """Both engines of internal/db.py, in a fresh interpreter because they are built at import time."""
    script = textwrap.dedent(
        """
        import asyncio
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError
        from open_webui.internal import db

        with db.engine.begin() as connection:
            connection.execute(text('CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)'))
            connection.execute(text("INSERT INTO t VALUES (1, 'Éléphant')"))

        async def main():
            async with db.get_async_db() as session:
                # The application's own LIKE (case folding beyond ASCII) is installed on these connections.
                found = await session.execute(text("SELECT count(*) FROM t WHERE name LIKE '%élé%'"))
                assert found.scalar() == 1
                try:
                    await session.execute(text("INSERT INTO t VALUES (1, 'again')"))
                except IntegrityError:
                    await session.rollback()
                else:
                    raise AssertionError('no IntegrityError')
                assert (await session.execute(text('PRAGMA journal_mode'))).scalar() == 'wal'
            await db.async_engine.dispose()

        asyncio.run(main())
        try:
            with db.engine.begin() as connection:
                connection.execute(text("INSERT INTO t VALUES (1, 'again')"))
        except IntegrityError:
            print('OK')
        """
    )
    env = {
        **os.environ,
        'DATA_DIR': str(tmp_path),
        'DATABASE_TYPE': 'sqlite+sqlcipher',
        'DATABASE_PASSWORD': PASSWORD,
        'WEBUI_SECRET_KEY': 'test-only',
        'PYTHONPATH': str(BACKEND),
    }
    env.pop('DATABASE_URL', None)
    result = subprocess.run([sys.executable, '-c', script], env=env, cwd=BACKEND, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.strip().endswith('OK')
    assert not sqlcipher_tool.is_plaintext(str(tmp_path / 'webui.db'))
