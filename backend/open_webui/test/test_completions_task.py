"""
/api/completions requests are forwarded as a task (``metadata.task``), so a
connection header templated with ``{{TASK}}`` (e.g. ``X-Gateway-Task``) tells a
gateway that IDE autocomplete is background traffic, not an interactive chat.
"""

import json
from types import SimpleNamespace

import open_webui.main as main
import open_webui.routers.openai as openai_router
import pytest
from open_webui.utils.completions import COMPLETIONS_TASK, with_completions_task

MODEL_ID = 'zeta-2.1'
USER = SimpleNamespace(id='user-1', name='Dev', email='dev@example.com', role='user')


class FakeResponse:
    closed = True  # nothing for cleanup_response to release

    def __init__(self, status, body, headers):
        self.status = status
        self.headers = headers
        self.body = body

    async def json(self, loads=None):
        if isinstance(self.body, str):
            raise ValueError('not a JSON body')
        return self.body

    async def text(self):
        return self.body if isinstance(self.body, str) else json.dumps(self.body)


class FakeSession:
    """Stands in for the pooled aiohttp session and records each upstream call."""

    def __init__(self):
        self.status = 200
        self.headers = {'Content-Type': 'application/json'}
        self.body = {'object': 'text_completion', 'choices': [{'text': ' return n'}]}
        self.calls = []

    async def request(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self.status, self.body, self.headers)


def make_request():
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(OPENAI_MODELS={MODEL_ID: {'urlIdx': 0}})),
        headers={},
        cookies={},
        state=SimpleNamespace(),
    )


@pytest.fixture
def upstream(monkeypatch):
    """One OpenAI connection serving MODEL_ID, with an X-Gateway-Task header templated with {{TASK}}."""
    session = FakeSession()

    async def config_get(key, default=None):
        return True if key == 'openai.enable' else default

    async def get_model_by_id(model_id):
        return None

    async def check_model_access(*args, **kwargs):
        return None

    async def get_openai_connection(idx):
        return 'http://gateway.test/v1', 'sk-test', {'headers': {'X-Gateway-Task': '{{TASK}}'}}

    async def get_session():
        return session

    async def publish_model_provider_request_failed(*args, **kwargs):
        return None

    monkeypatch.setattr(openai_router, 'Config', SimpleNamespace(get=config_get))
    monkeypatch.setattr(openai_router, 'Models', SimpleNamespace(get_model_by_id=get_model_by_id))
    monkeypatch.setattr(openai_router, 'check_model_access', check_model_access)
    monkeypatch.setattr(openai_router, 'get_openai_connection', get_openai_connection)
    monkeypatch.setattr(openai_router, 'get_session', get_session)
    monkeypatch.setattr(openai_router, 'publish_model_provider_request_failed', publish_model_provider_request_failed)
    return session


def test_with_completions_task_defaults_to_autocomplete():
    form_data = {'model': MODEL_ID, 'prompt': 'def fib(n):'}

    assert with_completions_task(form_data)['metadata'] == {'task': 'autocomplete'}
    assert 'metadata' not in form_data  # the caller's payload is left untouched


@pytest.mark.parametrize(
    'metadata, expected',
    [
        ({'task': 'query_generation', 'chat_id': 'chat-1'}, {'task': 'query_generation', 'chat_id': 'chat-1'}),
        ({'task': '', 'chat_id': 'chat-1'}, {'task': COMPLETIONS_TASK, 'chat_id': 'chat-1'}),
        (None, {'task': COMPLETIONS_TASK}),
    ],
)
def test_with_completions_task_keeps_caller_metadata(metadata, expected):
    assert with_completions_task({'model': MODEL_ID, 'metadata': metadata})['metadata'] == expected


@pytest.mark.asyncio
async def test_task_header_renders_autocomplete_without_metadata(upstream):
    # A plain legacy completions body, as IDE autocomplete (continue.dev) sends it.
    response = await main.completions(make_request(), {'model': MODEL_ID, 'prompt': 'def fib(n):'}, USER)

    [call] = upstream.calls
    assert call['url'] == 'http://gateway.test/v1/completions'
    assert call['headers']['X-Gateway-Task'] == 'autocomplete'
    assert 'metadata' not in json.loads(call['data'])  # metadata never reaches the provider
    assert response == upstream.body


@pytest.mark.asyncio
async def test_task_header_keeps_explicit_task(upstream):
    form_data = {'model': MODEL_ID, 'prompt': 'def fib(n):', 'metadata': {'task': 'query_generation'}}

    await main.completions(make_request(), form_data, USER)

    assert upstream.calls[0]['headers']['X-Gateway-Task'] == 'query_generation'


@pytest.mark.asyncio
async def test_chat_fallback_is_marked_as_task(upstream, monkeypatch):
    # A chat-only backend has no native /completions route and answers 404.
    upstream.status, upstream.body = 404, {'error': 'not found'}
    seen = {}

    async def chat_completion(request, form_data, user):
        seen['task'] = request.state.task
        return {'choices': [{'message': {'content': ' return n'}}]}

    monkeypatch.setattr(main, 'chat_completion', chat_completion)

    response = await main.completions(make_request(), {'model': MODEL_ID, 'prompt': 'def fib(n):'}, USER)

    assert seen['task'] == 'autocomplete'
    assert response['choices'][0]['text'] == ' return n'


MODEL_LOADING = {
    'error': {
        'message': 'Le modèle zeta-2.1 est en cours de chargement, réessayez dans 30 secondes.',
        'type': 'model_loading',
        'code': 503,
    }
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'content_type, body',
    [
        ('application/json', MODEL_LOADING),
        ('text/event-stream', MODEL_LOADING),  # error status sent with an SSE content-type
        ('text/plain', 'model loading'),
    ],
)
async def test_retry_after_reaches_client_on_503(upstream, content_type, body):
    # A gateway loading a cold model answers 503 with Retry-After (seconds).
    upstream.status, upstream.body = 503, body
    upstream.headers = {'Content-Type': content_type, 'Retry-After': '30', 'X-Upstream-Node': 'gpu-2'}

    response = await main.completions(make_request(), {'model': MODEL_ID, 'prompt': 'def fib(n):'}, USER)

    assert response.status_code == 503
    assert response.headers['Retry-After'] == '30'
    assert 'X-Upstream-Node' not in response.headers  # only Retry-After is forwarded
    content = response.body.decode()
    assert (json.loads(content) if isinstance(body, dict) else content) == body


@pytest.mark.asyncio
async def test_error_without_retry_after_adds_no_header(upstream):
    upstream.status, upstream.body = 500, {'error': {'message': 'boom'}}

    response = await main.completions(make_request(), {'model': MODEL_ID, 'prompt': 'def fib(n):'}, USER)

    assert response.status_code == 500
    assert 'Retry-After' not in response.headers
