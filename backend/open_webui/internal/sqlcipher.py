"""Fork: SQLite encrypted at rest with SQLCipher (``DATABASE_TYPE=sqlite+sqlcipher``).

Upstream opens the encrypted file for its startup engine only: the engine that serves requests is
async and refuses ``sqlite+sqlcipher://`` URLs, so the application cannot start. This module gives
both engines their SQLCipher connections.

``sqlcipher3`` has its own exception classes, unrelated to those of ``sqlite3``. SQLAlchemy must be
told about them (``module=``), otherwise an ``IntegrityError`` is not recognised as one.
"""

from types import SimpleNamespace

URL_PREFIX = 'sqlite+sqlcipher://'


def database_path(url: str) -> str:
    """File path of a ``sqlite+sqlcipher://`` URL: three slashes for a relative path, four for an absolute one."""
    path = url[len(URL_PREFIX) :]
    return path[1:] if path.startswith('/') else path


def connect(path: str, password: str):
    """Open the encrypted database, failing at once with a clear message if the key does not fit."""
    if not password or not password.strip():
        raise ValueError('DATABASE_PASSWORD (or DATABASE_PASSWORD_FILE) is required with sqlite+sqlcipher')

    import sqlcipher3

    connection = sqlcipher3.connect(path, check_same_thread=False)
    try:
        # PRAGMA takes no bound parameters; doubling the quotes is the SQL escape.
        escaped = password.replace("'", "''")
        connection.execute(f"PRAGMA key = '{escaped}'")
        connection.execute('SELECT count(*) FROM sqlite_master')
    except sqlcipher3.dbapi2.DatabaseError as error:
        connection.close()
        raise RuntimeError(
            f'Cannot open {path} with SQLCipher: wrong password, or the file is not encrypted yet '
            '(convert it with open_webui/utils/sqlcipher_tool.py).'
        ) from error
    return connection


def sync_engine_options(path: str, password: str) -> dict:
    """``create_engine`` keyword arguments for the synchronous engine (URL ``sqlite://``)."""
    import sqlcipher3

    return {'module': sqlcipher3.dbapi2, 'creator': lambda: connect(path, password)}


def async_engine_options(path: str, password: str) -> dict:
    """``create_async_engine`` keyword arguments for the aiosqlite engine."""
    import aiosqlite
    import sqlcipher3
    from sqlalchemy.dialects.sqlite.aiosqlite import AsyncAdapt_aiosqlite_dbapi

    cipher = sqlcipher3.dbapi2
    # What SQLAlchemy reads from the driver module, with SQLCipher's exception classes and library version.
    driver = SimpleNamespace(
        Connection=aiosqlite.Connection,
        **{
            name: getattr(cipher, name)
            for name in (
                'DatabaseError',
                'Error',
                'IntegrityError',
                'NotSupportedError',
                'OperationalError',
                'ProgrammingError',
                'sqlite_version',
                'sqlite_version_info',
            )
        },
    )

    async def creator():
        connection = aiosqlite.Connection(lambda: connect(path, password), iter_chunk_size=64)
        # As SQLAlchemy does for its own aiosqlite connections: the worker thread must not block exit.
        thread = getattr(connection, '_thread', None)
        if thread is not None:
            thread.daemon = True
        return await connection

    return {'module': AsyncAdapt_aiosqlite_dbapi(driver, cipher), 'async_creator': creator}
