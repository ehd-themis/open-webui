"""
A preset (a custom model with a ``base_model_id``) runs on its base model, so /api/models
reports the base model's context window for it too: IDE clients that size prompts from the
model list (``context_length``, vLLM's ``max_model_len``) then see it on the ids people pick.
"""

from types import SimpleNamespace

import open_webui.utils.models as models_utils
import pytest
from open_webui.models.models import ModelMeta, ModelModel, ModelParams

BASE_ID = 'qwen3.8-27b-nvfp4'
BASE_MODELS = [
    {
        'id': BASE_ID,
        'name': BASE_ID,
        'object': 'model',
        'owned_by': 'openai',
        'connection_type': 'external',
        'context_length': 262144,
        'max_model_len': 262144,
        'lachesis': {'state': 'asleep', 'eta_min': 6},
    },
    {'id': 'no-limits', 'name': 'no-limits', 'object': 'model', 'owned_by': 'openai'},
]


def preset(model_id, base_model_id, is_active=True):
    return ModelModel(
        id=model_id,
        user_id='admin',
        base_model_id=base_model_id,
        name=model_id,
        params=ModelParams(),
        meta=ModelMeta(),
        is_active=is_active,
        updated_at=0,
        created_at=0,
    )


async def run(monkeypatch, custom_models):
    async def get_many(*keys):
        return {}

    async def base_models(request, user=None):
        return [dict(m) for m in BASE_MODELS]

    async def all_custom_models():
        return custom_models

    async def no_functions(ids):
        return []

    async def no_valves(ids):
        return {}

    monkeypatch.setattr(models_utils.Config, 'get_many', get_many)
    monkeypatch.setattr(models_utils, 'get_all_base_models', base_models)
    monkeypatch.setattr(models_utils.Models, 'get_all_models', all_custom_models)
    monkeypatch.setattr(models_utils.Functions, 'get_functions_by_ids', no_functions)
    monkeypatch.setattr(models_utils.Functions, 'get_function_valves_by_ids', no_valves)
    monkeypatch.setattr(models_utils, 'get_functions_cache', lambda request: {})
    monkeypatch.setattr(models_utils, 'ENABLE_FUNCTIONS', False)

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None, MODELS={}, BASE_MODELS=[])))
    return {m['id']: m for m in await models_utils.get_all_models(request)}


@pytest.mark.asyncio
async def test_preset_reports_its_base_model_context(monkeypatch):
    models = await run(monkeypatch, [preset('qwen-thinking', BASE_ID)])

    assert models['qwen-thinking']['preset'] is True
    assert models['qwen-thinking']['context_length'] == 262144
    assert models['qwen-thinking']['max_model_len'] == 262144
    # The gateway's availability follows too: a user who sees the preset may not see its base model.
    assert models['qwen-thinking']['lachesis'] == {'state': 'asleep', 'eta_min': 6}
    # The base model keeps its own values.
    assert models[BASE_ID]['context_length'] == 262144


@pytest.mark.asyncio
async def test_preset_without_known_limits_gets_no_keys(monkeypatch):
    models = await run(monkeypatch, [preset('plain', 'no-limits'), preset('orphan', 'missing-base')])

    for model_id in ('plain', 'orphan'):
        assert 'context_length' not in models[model_id]
        assert 'max_model_len' not in models[model_id]
        assert 'lachesis' not in models[model_id]
