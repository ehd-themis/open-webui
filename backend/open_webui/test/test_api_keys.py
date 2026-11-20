"""
Several named API keys per user: each key can be created, rotated and deleted on its own,
expired keys stop authenticating, and an API key cannot manage keys.
"""

import time
from types import SimpleNamespace

import httpx
import open_webui.models.api_keys as api_keys_model
import open_webui.routers.api_keys as api_keys_router
import pytest
import pytest_asyncio
from fastapi import FastAPI
from open_webui.internal.db import async_engine
from open_webui.models.api_keys import ApiKeys
from open_webui.models.users import ApiKey, User, Users
from open_webui.utils.auth import get_admin_user, get_verified_user

ALICE = SimpleNamespace(id='alice', role='user')
BOB = SimpleNamespace(id='bob', role='user')
ADMIN = SimpleNamespace(id='admin', role='admin')


def make_user_row(user_id, role='user', email=None):
    now = int(time.time())
    return User(
        id=user_id,
        email=email or f'{user_id}@example.org',
        role=role,
        name=user_id,
        profile_image_url='/user.png',
        last_active_at=now,
        created_at=now,
        updated_at=now,
    )


@pytest_asyncio.fixture
async def db():
    async with async_engine.begin() as conn:
        await conn.run_sync(lambda c: User.__table__.create(c, checkfirst=True))
        await conn.run_sync(lambda c: ApiKey.__table__.create(c, checkfirst=True))
    from open_webui.internal.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        for row in (make_user_row('alice'), make_user_row('bob'), make_user_row('admin', 'admin')):
            session.add(row)
        await session.commit()
    api_keys_model._last_touch.clear()
    yield
    async with async_engine.begin() as conn:
        await conn.run_sync(lambda c: ApiKey.__table__.drop(c, checkfirst=True))
        await conn.run_sync(lambda c: User.__table__.drop(c, checkfirst=True))


class Harness:
    """The router mounted on a bare app; who is calling, and how, is chosen per request."""

    def __init__(self, monkeypatch):
        self.events = []
        self.caller = ALICE
        self.auth_type = None
        self.enabled = True
        self.can_create = True

        async def config_get(key, default=None):
            return {'auth.enable_api_keys': self.enabled, 'user.permissions': {}}.get(key, default)

        async def has_permission(user_id, key, perms, db=None):
            return self.can_create

        async def publish_event(request, event, **kwargs):
            self.events.append((event.name, kwargs.get('subject_id'), kwargs.get('source'), kwargs.get('data')))

        monkeypatch.setattr(api_keys_router.Config, 'get', config_get)
        monkeypatch.setattr(api_keys_router, 'has_permission', has_permission)
        monkeypatch.setattr(api_keys_router, 'publish_event', publish_event)

        app = FastAPI()
        app.include_router(api_keys_router.router, prefix='/api/v1/api_keys')

        @app.middleware('http')
        async def mark_auth_type(request, call_next):
            request.state.auth_type = self.auth_type
            return await call_next(request)

        app.dependency_overrides[get_verified_user] = lambda: self.caller
        app.dependency_overrides[get_admin_user] = lambda: self.caller
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')

    async def create(self, name='Word add-in', **extra):
        return await self.client.post('/api/v1/api_keys/', json={'name': name, **extra})


@pytest.fixture
def h(monkeypatch, db):
    return Harness(monkeypatch)


@pytest.mark.asyncio
async def test_a_user_can_hold_several_keys_and_the_list_never_shows_secrets(h):
    first = (await h.create('Word add-in')).json()
    second = (await h.create('Teams bridge', expires_in_days=30)).json()

    assert first['key'].startswith('sk-') and second['key'].startswith('sk-')
    assert first['key'] != second['key'] and first['id'] != second['id']
    assert second['expires_at'] is not None and first['expires_at'] is None

    listed = (await h.client.get('/api/v1/api_keys/')).json()
    assert [k['name'] for k in listed] == ['Word add-in', 'Teams bridge']
    assert all('key' not in k for k in listed)
    assert listed[0]['key_preview'] == f'sk-…{first["key"][-4:]}'

    # Both secrets authenticate the same user.
    assert (await Users.get_user_by_api_key(first['key'])).id == 'alice'
    assert (await Users.get_user_by_api_key(second['key'])).id == 'alice'


@pytest.mark.asyncio
async def test_rotating_one_key_leaves_the_others_untouched(h):
    word = (await h.create('Word add-in')).json()
    teams = (await h.create('Teams bridge', expires_in_days=90)).json()

    rotated = (await h.client.post(f'/api/v1/api_keys/{teams["id"]}/rotate')).json()

    assert rotated['id'] == teams['id'] and rotated['name'] == 'Teams bridge'
    assert rotated['expires_at'] == teams['expires_at']
    assert rotated['key'] != teams['key']
    assert await Users.get_user_by_api_key(teams['key']) is None
    assert (await Users.get_user_by_api_key(rotated['key'])).id == 'alice'
    assert (await Users.get_user_by_api_key(word['key'])).id == 'alice'


@pytest.mark.asyncio
async def test_deleting_one_key_leaves_the_others_untouched(h):
    word = (await h.create('Word add-in')).json()
    teams = (await h.create('Teams bridge')).json()

    assert (await h.client.delete(f'/api/v1/api_keys/{word["id"]}')).json() is True

    assert await Users.get_user_by_api_key(word['key']) is None
    assert (await Users.get_user_by_api_key(teams['key'])).id == 'alice'
    assert [k['id'] for k in (await h.client.get('/api/v1/api_keys/')).json()] == [teams['id']]
    assert (await h.client.delete(f'/api/v1/api_keys/{word["id"]}')).status_code == 404


@pytest.mark.asyncio
async def test_nobody_can_touch_someone_elses_key(h):
    alice_key = (await h.create('Alice key')).json()
    h.caller = BOB

    assert (await h.client.post(f'/api/v1/api_keys/{alice_key["id"]}/rotate')).status_code == 404
    assert (await h.client.delete(f'/api/v1/api_keys/{alice_key["id"]}')).status_code == 404
    assert (await h.client.get('/api/v1/api_keys/')).json() == []
    assert (await Users.get_user_by_api_key(alice_key['key'])).id == 'alice'


@pytest.mark.asyncio
async def test_expired_keys_stop_authenticating(h):
    key = (await h.create('Short lived', expires_in_days=1)).json()
    assert (await Users.get_user_by_api_key(key['key'])) is not None

    from open_webui.internal.db import AsyncSessionLocal
    from sqlalchemy import update

    async with AsyncSessionLocal() as session:
        await session.execute(update(ApiKey).where(ApiKey.id == key['id']).values(expires_at=int(time.time()) - 5))
        await session.commit()

    assert await Users.get_user_by_api_key(key['key']) is None
    assert (await h.client.get('/api/v1/api_keys/')).json()[0]['expires_at'] is not None


@pytest.mark.asyncio
async def test_last_used_is_recorded_and_throttled(h):
    key = (await h.create('Script')).json()
    assert (await h.client.get('/api/v1/api_keys/')).json()[0]['last_used_at'] is None

    await ApiKeys.touch_last_used(key['key'])
    first = (await h.client.get('/api/v1/api_keys/')).json()[0]['last_used_at']
    assert first is not None

    # A second use inside the throttle window does not write again.
    api_keys_model._last_touch[key['key']] = time.time()
    from open_webui.internal.db import AsyncSessionLocal
    from sqlalchemy import update

    async with AsyncSessionLocal() as session:
        await session.execute(update(ApiKey).where(ApiKey.id == key['id']).values(last_used_at=1))
        await session.commit()
    await ApiKeys.touch_last_used(key['key'])
    assert (await h.client.get('/api/v1/api_keys/')).json()[0]['last_used_at'] == 1


@pytest.mark.asyncio
async def test_a_legacy_single_key_row_is_listed_rotatable_and_deletable(h):
    assert await Users.update_user_api_key_by_id('alice', 'sk-legacy0000abcd')
    created = (await h.create('Extra')).json()

    listed = (await h.client.get('/api/v1/api_keys/')).json()
    legacy = next(k for k in listed if k['id'] == 'key_alice')
    assert legacy['name'] is None and legacy['key_preview'] == 'sk-…abcd'

    rotated = (await h.client.post('/api/v1/api_keys/key_alice/rotate')).json()
    assert await Users.get_user_by_api_key('sk-legacy0000abcd') is None
    assert (await Users.get_user_by_api_key(rotated['key'])).id == 'alice'

    assert (await h.client.delete('/api/v1/api_keys/key_alice')).json() is True
    assert (await Users.get_user_by_api_key(created['key'])).id == 'alice'


@pytest.mark.asyncio
async def test_an_api_key_cannot_manage_keys(h):
    h.auth_type = 'api_key'
    assert (await h.client.get('/api/v1/api_keys/')).status_code == 403
    assert (await h.create()).status_code == 403
    assert (await h.client.post('/api/v1/api_keys/key_x/rotate')).status_code == 403
    assert (await h.client.delete('/api/v1/api_keys/key_x')).status_code == 403


@pytest.mark.asyncio
async def test_creating_needs_the_feature_but_revoking_does_not(h):
    key = (await h.create('Word add-in')).json()

    h.enabled = False
    assert (await h.create('Another')).status_code == 403
    assert (await h.client.post(f'/api/v1/api_keys/{key["id"]}/rotate')).status_code == 403
    assert (await h.client.delete(f'/api/v1/api_keys/{key["id"]}')).json() is True

    h.enabled, h.can_create = True, False
    assert (await h.create('Another')).status_code == 403


@pytest.mark.asyncio
async def test_input_is_validated_and_keys_are_capped(h, monkeypatch):
    assert (await h.create('   ')).status_code == 422
    assert (await h.create('x' * 65)).status_code == 422
    assert (await h.create('ok', expires_in_days=0)).status_code == 422
    assert (await h.create('  spaced   name ')).json()['name'] == 'spaced name'

    monkeypatch.setattr(api_keys_router, 'MAX_KEYS_PER_USER', 2)
    assert (await h.create('second')).status_code == 200
    assert (await h.create('third')).status_code == 400


@pytest.mark.asyncio
async def test_responses_with_a_secret_are_not_cacheable_and_events_never_carry_it(h):
    response = await h.create('Word add-in')
    assert response.headers['cache-control'] == 'no-store'
    key = response.json()
    rotated = await h.client.post(f'/api/v1/api_keys/{key["id"]}/rotate')
    assert rotated.headers['cache-control'] == 'no-store'
    await h.client.delete(f'/api/v1/api_keys/{key["id"]}')

    assert [e[0] for e in h.events] == ['auth.api_key.created', 'auth.api_key.created', 'auth.api_key.deleted']
    assert 'sk-' not in repr(h.events)


@pytest.mark.asyncio
async def test_admin_manages_one_key_of_a_user_without_touching_the_rest(h):
    word = (await h.create('Word add-in')).json()
    teams = (await h.create('Teams bridge')).json()
    h.caller = ADMIN

    listed = (await h.client.get('/api/v1/api_keys/users/alice')).json()
    assert {k['name'] for k in listed} == {'Word add-in', 'Teams bridge'}
    assert all('key' not in k for k in listed)

    rotated = (await h.client.post(f'/api/v1/api_keys/users/alice/{word["id"]}/rotate')).json()
    assert await Users.get_user_by_api_key(word['key']) is None
    assert (await Users.get_user_by_api_key(rotated['key'])).id == 'alice'
    assert (await Users.get_user_by_api_key(teams['key'])).id == 'alice'

    assert (await h.client.delete(f'/api/v1/api_keys/users/alice/{teams["id"]}')).json() is True
    assert await Users.get_user_by_api_key(teams['key']) is None
    assert (await Users.get_user_by_api_key(rotated['key'])).id == 'alice'

    assert h.events[-1][2] == 'admin'
    assert (await h.client.get('/api/v1/api_keys/users/nobody')).status_code == 404
