#!/usr/bin/env python3
"""Fork: convert webui.db to or from SQLCipher, and take consistent snapshots of it.

Standalone on purpose (standard library plus ``sqlcipher3``): it must not import the application,
which would open the database and run migrations.

    python sqlcipher_tool.py status   --db /app/backend/data/webui.db
    python sqlcipher_tool.py encrypt  --db /app/backend/data/webui.db [--swap]
    python sqlcipher_tool.py decrypt  --db /app/backend/data/webui.db [--swap]
    python sqlcipher_tool.py snapshot --db /app/backend/data/webui.db --out /path/copy.db

The password is read from DATABASE_PASSWORD, or from the file named by DATABASE_PASSWORD_FILE.
It is never taken from the command line and never printed.

``encrypt`` and ``decrypt`` need the application stopped. Without ``--swap`` they only write a
converted file next to the database; with ``--swap`` the converted file takes its place and the
original is renamed, never deleted.
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

PLAINTEXT_HEADER = b'SQLite format 3\x00'


class ToolError(Exception):
    pass


def read_password(required: bool = True) -> str:
    password = os.environ.get('DATABASE_PASSWORD', '')
    path = os.environ.get('DATABASE_PASSWORD_FILE', '')
    if not password and path:
        with open(path, encoding='utf-8') as handle:
            password = handle.read().strip()
    if required and not password.strip():
        raise ToolError('Set DATABASE_PASSWORD or DATABASE_PASSWORD_FILE.')
    return password


def is_plaintext(path: str) -> bool:
    with open(path, 'rb') as handle:
        return handle.read(len(PLAINTEXT_HEADER)) == PLAINTEXT_HEADER


def quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def open_database(path: str, password: str | None):
    """Open an existing database; ``password`` is None for a plaintext file."""
    if not os.path.isfile(path):
        raise ToolError(f'No such database: {path}')
    if password is None:
        connection = sqlite3.connect(path, isolation_level=None, timeout=5)
    else:
        import sqlcipher3

        connection = sqlcipher3.connect(path, isolation_level=None, timeout=5)
        connection.execute(f'PRAGMA key = {quoted(password)}')
    try:
        connection.execute('SELECT count(*) FROM sqlite_master').fetchone()
    except Exception as error:
        connection.close()
        raise ToolError(f'Cannot read {path} (wrong password, damaged or locked file): {error}') from error
    return connection


def fingerprint(connection) -> dict:
    """Schema, user_version and the row count of every table, to compare two copies."""
    schema = connection.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    counts = {
        name: connection.execute(f'SELECT count(*) FROM {identifier(name)}').fetchone()[0]
        for kind, name, _ in schema
        if kind == 'table'
    }
    return {
        'schema': [tuple(row) for row in schema],
        'counts': counts,
        'user_version': connection.execute('PRAGMA user_version').fetchone()[0],
    }


def check_integrity(connection, encrypted: bool) -> None:
    result = connection.execute('PRAGMA integrity_check').fetchall()
    if [tuple(row) for row in result] != [('ok',)]:
        raise ToolError(f'integrity_check failed: {result[:5]}')
    if encrypted:
        problems = connection.execute('PRAGMA cipher_integrity_check').fetchall()
        if problems:
            raise ToolError(f'cipher_integrity_check failed: {problems[:5]}')


def require_exclusive_access(connection) -> None:
    """Fail if anything else holds the database open (the application must be stopped)."""
    try:
        connection.execute('PRAGMA locking_mode = EXCLUSIVE')
        connection.execute('BEGIN EXCLUSIVE')
        connection.execute('COMMIT')
    except Exception as error:
        raise ToolError(f'The database is in use; stop Open WebUI first ({error}).') from error


def require_space(path: str) -> None:
    size = sum(os.path.getsize(p) for p in (path, path + '-wal') if os.path.exists(p))
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(path))).free
    if free < size * 2:
        raise ToolError(f'Not enough free space: {free // 2**20} MiB free, {size * 2 // 2**20} MiB wanted.')


def convert(db: str, encrypting: bool, swap: bool) -> None:
    password = read_password()
    if not os.path.isfile(db):
        raise ToolError(f'No such database: {db}')
    if is_plaintext(db) != encrypting:
        raise ToolError(f'{db} is already {"encrypted" if encrypting else "plaintext"}.')

    try:
        import sqlcipher3
    except ImportError as error:
        raise ToolError('The sqlcipher3 module is missing (pip install sqlcipher3-wheels).') from error

    target = f'{db}.{"encrypted" if encrypting else "decrypted"}'
    if os.path.exists(target):
        raise ToolError(f'{target} already exists; move it away first.')
    require_space(db)

    os.umask(0o077)
    # sqlcipher3 opens both sides: with an empty key it reads and writes ordinary SQLite files.
    source = sqlcipher3.connect(db, isolation_level=None, timeout=5)
    try:
        if not encrypting:
            source.execute(f'PRAGMA key = {quoted(password)}')
        try:
            source.execute('SELECT count(*) FROM sqlite_master').fetchone()
        except Exception as error:
            raise ToolError(f'Cannot read {db} (wrong password or damaged file): {error}') from error
        require_exclusive_access(source)
        source.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        check_integrity(source, encrypted=not encrypting)
        before = fingerprint(source)

        source.execute('ATTACH DATABASE ? AS target KEY ?', (target, password if encrypting else ''))
        source.execute("SELECT sqlcipher_export('target')")
        source.execute(f'PRAGMA target.user_version = {int(before["user_version"])}')
        source.execute('DETACH DATABASE target')

        copy = open_database(target, password if encrypting else None)
        try:
            check_integrity(copy, encrypted=encrypting)
            after = fingerprint(copy)
        finally:
            copy.close()
        if after != before:
            differing = sorted(
                name
                for name in set(before['counts']) | set(after['counts'])
                if before['counts'].get(name) != after['counts'].get(name)
            )
            raise ToolError(f'The copy differs from the source (tables: {differing or "schema or user_version"}).')
        if is_plaintext(target) == encrypting:
            raise ToolError('The copy does not have the expected on-disk format.')
    except BaseException:
        if os.path.exists(target):
            os.remove(target)
        raise
    finally:
        source.close()

    os.chmod(target, 0o600)
    print(f'{len(before["counts"])} tables, {sum(before["counts"].values())} rows verified: {target}')

    if not swap:
        print('The original is unchanged. Run again with --swap to put the converted file in its place.')
        return

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    kept = f'{db}.{"plaintext" if encrypting else "encrypted"}-{stamp}'
    # The checkpoint emptied the WAL; leftovers belong to the old file and must not meet the new one.
    for suffix in ('-wal', '-shm'):
        if os.path.exists(db + suffix):
            if suffix == '-wal' and os.path.getsize(db + suffix) > 0:
                raise ToolError(f'{db}-wal is not empty after the checkpoint; nothing was swapped.')
            os.remove(db + suffix)
    os.replace(db, kept)
    os.chmod(kept, 0o600)
    os.replace(target, db)
    print(f'Swapped. The previous file is kept as {kept}')
    if encrypting:
        print('It is NOT encrypted: delete it once the application has been checked.')


def snapshot(db: str, out: str) -> None:
    """Consistent copy of a database in use (VACUUM INTO). An encrypted source gives an encrypted copy."""
    if os.path.exists(out):
        raise ToolError(f'{out} already exists.')
    if not os.path.isfile(db):
        raise ToolError(f'No such database: {db}')
    password = None if is_plaintext(db) else read_password()
    os.umask(0o077)
    source = open_database(db, password)
    try:
        source.execute(f'VACUUM INTO {quoted(out)}')
    except BaseException:
        if os.path.exists(out):
            os.remove(out)
        raise
    finally:
        source.close()
    copy = open_database(out, password)
    try:
        check_integrity(copy, encrypted=password is not None)
    except BaseException:
        copy.close()
        os.remove(out)
        raise
    copy.close()
    os.chmod(out, 0o600)
    print(f'Snapshot written and verified ({"encrypted" if password else "plaintext"}): {out}')


def status(db: str) -> None:
    if not os.path.isfile(db):
        raise ToolError(f'No such database: {db}')
    if is_plaintext(db):
        print(f'{db}: plaintext SQLite')
        return
    password = read_password(required=False)
    if not password:
        print(f'{db}: not a plaintext SQLite file (set the password to check it)')
        return
    connection = open_database(db, password)
    tables = len(fingerprint(connection)['counts'])
    connection.close()
    print(f'{db}: SQLCipher, the password opens it ({tables} tables)')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('status', 'encrypt', 'decrypt', 'snapshot'):
        command = commands.add_parser(name)
        command.add_argument('--db', required=True)
        if name in ('encrypt', 'decrypt'):
            command.add_argument('--swap', action='store_true')
        if name == 'snapshot':
            command.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'status':
            status(args.db)
        elif args.command == 'snapshot':
            snapshot(args.db, args.out)
        else:
            convert(args.db, encrypting=args.command == 'encrypt', swap=args.swap)
    except ToolError as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
