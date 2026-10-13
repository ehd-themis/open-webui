"""
GET /api/models/{id} (and /api/v1/models/{id}) is OpenAI's "retrieve model": clients such as
Visual Studio's Copilot call it before adding a model, and without it the path fell through
to the web UI's HTML page. The route must not shadow the fixed paths next to it.
"""

from types import SimpleNamespace

import open_webui.main as main
import pytest
from fastapi import HTTPException
from starlette.routing import Match

USER = SimpleNamespace(id='user-1', name='Dev', email='dev@example.com', role='user')
MODELS = {'data': [{'id': 'qwen-thinking', 'object': 'model', 'owned_by': 'openai', 'context_length': 262144}]}


def endpoint_for(path, method='GET'):
    scope = {'type': 'http', 'path': path, 'method': method, 'root_path': ''}
    for route in main.app.router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return getattr(route, 'endpoint', None)
    return None


@pytest.mark.parametrize(
    'path, expected',
    [
        ('/api/models', main.get_models),
        ('/api/models/base', main.get_base_models),
        ('/api/models/qwen-thinking', main.get_model),
        ('/api/models/org/model-with-slash', main.get_model),
        ('/api/v1/models', main.get_models),
        ('/api/v1/models/qwen-thinking', main.get_model),
    ],
)
def test_routes_resolve_to_the_right_handler(path, expected):
    assert endpoint_for(path) is expected


def test_fixed_v1_model_routes_keep_their_handlers():
    # The models router (mounted at /api/v1/models) owns these; the retrieve route must not catch them.
    for path in ('/api/v1/models/list', '/api/v1/models/base', '/api/v1/models/model'):
        assert endpoint_for(path) is not main.get_model


@pytest.mark.asyncio
async def test_returns_the_model_the_user_can_see(monkeypatch):
    async def fake_get_models(request, refresh=False, user=None):
        return MODELS

    monkeypatch.setattr(main, 'get_models', fake_get_models)
    model = await main.get_model(SimpleNamespace(), 'qwen-thinking', user=USER)
    assert model['id'] == 'qwen-thinking'
    assert model['object'] == 'model'


@pytest.mark.asyncio
async def test_unknown_or_hidden_model_is_404(monkeypatch):
    async def fake_get_models(request, refresh=False, user=None):
        return MODELS

    monkeypatch.setattr(main, 'get_models', fake_get_models)
    with pytest.raises(HTTPException) as err:
        await main.get_model(SimpleNamespace(), 'not-a-model', user=USER)
    assert err.value.status_code == 404
