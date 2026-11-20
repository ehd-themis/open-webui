"""Several named API keys per user (fork addition).

Each key has its own id, name, optional expiry and last-use time, and can be rotated
or deleted on its own, so revoking one integration no longer breaks the others.

- ``/api/v1/api_keys``: the signed-in user's own keys.
- ``/api/v1/api_keys/users/{user_id}``: the same, for an administrator.

Keys are managed with a session (JWT) only: a request authenticated with an API key
cannot list, create, rotate or delete keys, so a leaked key cannot mint new ones.
The key value is returned once, on creation or rotation, never by the list endpoints.
The legacy one-key endpoints (``/auths/api_key``, ``/users/{id}/api_key``) are unchanged.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from open_webui.constants import ERROR_MESSAGES
from open_webui.events import EVENTS, publish_event
from open_webui.internal.db import get_async_session
from open_webui.models.api_keys import ApiKeyCreated, ApiKeyInfo, ApiKeys, to_created, to_info
from open_webui.models.config import Config
from open_webui.models.users import Users
from open_webui.utils.access_control import has_permission
from open_webui.utils.auth import create_api_key, get_admin_user, get_verified_user
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()

MAX_KEYS_PER_USER = 20
MAX_NAME_LENGTH = 64
MAX_EXPIRES_IN_DAYS = 3650


class CreateApiKeyForm(BaseModel):
    name: str = Field(..., max_length=MAX_NAME_LENGTH)
    expires_in_days: int | None = Field(None, ge=1, le=MAX_EXPIRES_IN_DAYS)

    @field_validator('name')
    @classmethod
    def _clean_name(cls, value: str) -> str:
        value = ' '.join(value.split())
        if not value:
            raise ValueError('name must not be empty')
        return value


def _no_store(response: Response) -> None:
    response.headers['Cache-Control'] = 'no-store'


def _session_only(request: Request) -> None:
    if getattr(request.state, 'auth_type', None) == 'api_key':
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)


async def get_session_user(request: Request, user=Depends(get_verified_user)):
    _session_only(request)
    return user


async def get_session_admin(request: Request, user=Depends(get_admin_user)):
    _session_only(request)
    return user


async def _check_can_create(user, db: AsyncSession) -> None:
    """Creating or rotating needs the feature on and the user's permission. Listing and deleting do not."""
    if not await Config.get('auth.enable_api_keys') or (
        user.role != 'admin'
        and not await has_permission(user.id, 'features.api_keys', await Config.get('user.permissions'), db=db)
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.API_KEY_CREATION_NOT_ALLOWED)


async def _target_user(session_user, user_id: str, db: AsyncSession):
    target = await Users.get_user_by_id(user_id, db=db)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.USER_NOT_FOUND)
    first_user = await Users.get_first_user(db=db)
    if first_user and first_user.id == user_id and session_user.id != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.ACTION_PROHIBITED)
    return target


async def _create(request: Request, actor, target, form: CreateApiKeyForm, db: AsyncSession, source: str):
    if await ApiKeys.count_by_user(target.id, db=db) >= MAX_KEYS_PER_USER:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f'Too many API keys (maximum {MAX_KEYS_PER_USER}). Delete one first.',
        )
    expires_at = int(time.time()) + form.expires_in_days * 86400 if form.expires_in_days else None
    row = await ApiKeys.create(target.id, create_api_key(), form.name, expires_at, db=db)
    await publish_event(
        request,
        EVENTS.AUTH_API_KEY_CREATED,
        actor=actor,
        subject_id=target.id,
        subject_type='user',
        source=source,
        data={'key_id': row.id, 'name': form.name},
    )
    return to_created(row)


async def _rotate(request: Request, actor, target, key_id: str, db: AsyncSession, source: str):
    row = await ApiKeys.rotate(target.id, key_id, create_api_key(), db=db)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.API_KEY_NOT_FOUND)
    await publish_event(
        request,
        EVENTS.AUTH_API_KEY_CREATED,
        actor=actor,
        subject_id=target.id,
        subject_type='user',
        source=source,
        data={'key_id': row.id, 'rotated': True},
    )
    return to_created(row)


async def _delete(request: Request, actor, target, key_id: str, db: AsyncSession, source: str) -> bool:
    if not await ApiKeys.delete(target.id, key_id, db=db):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.API_KEY_NOT_FOUND)
    await publish_event(
        request,
        EVENTS.AUTH_API_KEY_DELETED,
        actor=actor,
        subject_id=target.id,
        subject_type='user',
        source=source,
        data={'key_id': key_id},
    )
    return True


############################
# Own keys
############################


@router.get('/', response_model=list[ApiKeyInfo])
async def list_api_keys(user=Depends(get_session_user), db: AsyncSession = Depends(get_async_session)):
    return [to_info(row) for row in await ApiKeys.list_by_user(user.id, db=db)]


@router.post('/', response_model=ApiKeyCreated)
async def create_user_api_key(
    request: Request,
    response: Response,
    form_data: CreateApiKeyForm,
    user=Depends(get_session_user),
    db: AsyncSession = Depends(get_async_session),
):
    await _check_can_create(user, db)
    _no_store(response)
    return await _create(request, user, user, form_data, db, 'api')


@router.post('/{key_id}/rotate', response_model=ApiKeyCreated)
async def rotate_user_api_key(
    request: Request,
    response: Response,
    key_id: str,
    user=Depends(get_session_user),
    db: AsyncSession = Depends(get_async_session),
):
    await _check_can_create(user, db)
    _no_store(response)
    return await _rotate(request, user, user, key_id, db, 'api')


@router.delete('/{key_id}', response_model=bool)
async def delete_user_api_key(
    request: Request,
    key_id: str,
    user=Depends(get_session_user),
    db: AsyncSession = Depends(get_async_session),
):
    # Revocation stays available even when the feature or the user's permission is switched off.
    return await _delete(request, user, user, key_id, db, 'api')


############################
# Another user's keys (admin)
############################


@router.get('/users/{user_id}', response_model=list[ApiKeyInfo])
async def list_api_keys_by_user_id(
    user_id: str,
    session_user=Depends(get_session_admin),
    db: AsyncSession = Depends(get_async_session),
):
    target = await _target_user(session_user, user_id, db)
    return [to_info(row) for row in await ApiKeys.list_by_user(target.id, db=db)]


@router.post('/users/{user_id}/{key_id}/rotate', response_model=ApiKeyCreated)
async def rotate_api_key_by_user_id(
    request: Request,
    response: Response,
    user_id: str,
    key_id: str,
    session_user=Depends(get_session_admin),
    db: AsyncSession = Depends(get_async_session),
):
    target = await _target_user(session_user, user_id, db)
    await _check_can_create(target, db)
    _no_store(response)
    return await _rotate(request, session_user, target, key_id, db, 'admin')


@router.delete('/users/{user_id}/{key_id}', response_model=bool)
async def delete_api_key_by_user_id(
    request: Request,
    user_id: str,
    key_id: str,
    session_user=Depends(get_session_admin),
    db: AsyncSession = Depends(get_async_session),
):
    target = await _target_user(session_user, user_id, db)
    return await _delete(request, session_user, target, key_id, db, 'admin')
