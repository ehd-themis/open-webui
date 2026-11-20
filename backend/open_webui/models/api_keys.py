"""Several named API keys per user (fork addition).

Upstream keeps one ``api_key`` row per user. The table already carries ``data``,
``expires_at`` and ``last_used_at``, so this module only adds the data-access layer
for N rows per user: create, list, rotate and delete one key by id, and a throttled
``last_used_at`` update. The legacy one-key helpers in ``models/users.py`` are untouched.
"""

from __future__ import annotations

import time
import uuid

from open_webui.internal.db import get_async_db_context
from open_webui.models.users import ApiKey, ApiKeyModel
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

# Do not write last_used_at more often than this for the same key.
LAST_USED_WRITE_INTERVAL_SEC = 60
_last_touch: dict[str, float] = {}
_LAST_TOUCH_MAX = 4096


class ApiKeyInfo(BaseModel):
    """What the API returns about a key. Never contains the key value."""

    id: str
    name: str | None = None
    key_preview: str
    created_at: int
    updated_at: int
    last_used_at: int | None = None
    expires_at: int | None = None


class ApiKeyCreated(ApiKeyInfo):
    """Returned once, on creation or rotation, with the full key value."""

    key: str


def key_preview(key: str) -> str:
    return f'sk-…{key[-4:]}' if key else ''


def to_info(row: ApiKeyModel) -> ApiKeyInfo:
    data = row.data if isinstance(row.data, dict) else {}
    return ApiKeyInfo(
        id=row.id,
        name=data.get('name') or None,
        key_preview=key_preview(row.key),
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
    )


def to_created(row: ApiKeyModel) -> ApiKeyCreated:
    return ApiKeyCreated(**to_info(row).model_dump(), key=row.key)


class ApiKeysTable:
    async def list_by_user(self, user_id: str, db: AsyncSession | None = None) -> list[ApiKeyModel]:
        async with get_async_db_context(db) as session:
            result = await session.execute(
                select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at, ApiKey.id)
            )
            return [ApiKeyModel.model_validate(row) for row in result.scalars().all()]

    async def count_by_user(self, user_id: str, db: AsyncSession | None = None) -> int:
        async with get_async_db_context(db) as session:
            result = await session.execute(select(func.count()).select_from(ApiKey).where(ApiKey.user_id == user_id))
            return int(result.scalar() or 0)

    async def get(self, user_id: str, key_id: str, db: AsyncSession | None = None) -> ApiKeyModel | None:
        async with get_async_db_context(db) as session:
            result = await session.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id))
            row = result.scalars().first()
            return ApiKeyModel.model_validate(row) if row else None

    async def create(
        self,
        user_id: str,
        key: str,
        name: str,
        expires_at: int | None = None,
        db: AsyncSession | None = None,
    ) -> ApiKeyModel:
        async with get_async_db_context(db) as session:
            now_ts = int(time.time())
            row = ApiKey(
                # Millisecond prefix: ids sort in creation order even inside the same second.
                id=f'key_{int(time.time() * 1000):013x}{uuid.uuid4().hex[:16]}',
                user_id=user_id,
                key=key,
                data={'name': name},
                expires_at=expires_at,
                last_used_at=None,
                created_at=now_ts,
                updated_at=now_ts,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return ApiKeyModel.model_validate(row)

    async def rotate(
        self,
        user_id: str,
        key_id: str,
        new_key: str,
        db: AsyncSession | None = None,
    ) -> ApiKeyModel | None:
        """Replace the secret of one key. The old secret stops working at once; id, name and expiry stay."""
        async with get_async_db_context(db) as session:
            result = await session.execute(
                update(ApiKey)
                .where(ApiKey.id == key_id, ApiKey.user_id == user_id)
                .values(key=new_key, updated_at=int(time.time()), last_used_at=None)
            )
            if not result.rowcount:
                await session.rollback()
                return None
            await session.commit()
            row = (await session.execute(select(ApiKey).where(ApiKey.id == key_id))).scalars().first()
            return ApiKeyModel.model_validate(row) if row else None

    async def delete(self, user_id: str, key_id: str, db: AsyncSession | None = None) -> bool:
        async with get_async_db_context(db) as session:
            result = await session.execute(delete(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id))
            await session.commit()
            return bool(result.rowcount)

    async def touch_last_used(self, key: str, db: AsyncSession | None = None) -> None:
        """Record that a key was just used. Best effort: never raises, writes at most once a minute per key."""
        now = time.time()
        last = _last_touch.get(key)
        if last is not None and now - last < LAST_USED_WRITE_INTERVAL_SEC:
            return
        if len(_last_touch) >= _LAST_TOUCH_MAX:
            _last_touch.clear()
        _last_touch[key] = now
        try:
            async with get_async_db_context(db) as session:
                await session.execute(update(ApiKey).where(ApiKey.key == key).values(last_used_at=int(now)))
                await session.commit()
        except Exception:
            # Usage tracking must never break an authenticated request.
            _last_touch.pop(key, None)


ApiKeys = ApiKeysTable()
