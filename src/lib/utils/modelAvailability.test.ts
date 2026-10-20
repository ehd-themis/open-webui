import { describe, expect, it } from 'vitest';
import { getModelAvailability } from './modelAvailability';

describe('getModelAvailability', () => {
	it('reads the gateway extension that Open WebUI keeps under model.openai', () => {
		expect(getModelAvailability({ openai: { lachesis: { state: 'asleep', eta_min: 6 } } })).toEqual(
			{
				state: 'asleep',
				etaMin: 6
			}
		);
		expect(
			getModelAvailability({
				openai: { lachesis: { state: 'unavailable', reason: 'admin', until: 1800000000 } }
			})
		).toEqual({ state: 'unavailable', reason: 'admin', until: 1800000000 });
		expect(getModelAvailability({ openai: { lachesis: { state: 'ready' } } })).toEqual({
			state: 'ready'
		});
	});

	it('falls back on the coarse owned_by of older gateways, and shows nothing for other connections', () => {
		expect(getModelAvailability({ openai: { owned_by: 'ready' } })).toEqual({ state: 'ready' });
		expect(getModelAvailability({ openai: { owned_by: 'cold' } })).toBeNull();
		expect(getModelAvailability({ openai: { owned_by: 'openai' } })).toBeNull();
		expect(getModelAvailability({ id: 'x' })).toBeNull();
		expect(getModelAvailability({ openai: { lachesis: { state: 'nonsense' } } })).toBeNull();
	});

	it('gives a preset (sub-model) the state of its base model, through the chain', () => {
		const base = { id: 'gpu.qwen38fn', openai: { lachesis: { state: 'asleep', eta_min: 6 } } };
		const preset = { id: 'qwen', info: { base_model_id: 'gpu.qwen38fn' } };
		const nested = { id: 'qwen-fr', info: { base_model_id: 'qwen' } };
		const all = [base, preset, nested];
		expect(getModelAvailability(preset, all)).toEqual({ state: 'asleep', etaMin: 6 });
		expect(getModelAvailability(nested, all)).toEqual({ state: 'asleep', etaMin: 6 });
		// no base in the list, or no base at all: nothing is shown
		expect(getModelAvailability(preset, [preset])).toBeNull();
		expect(getModelAvailability({ id: 'x' }, all)).toBeNull();
		// a loop never hangs
		const a = { id: 'a', info: { base_model_id: 'b' } };
		const b = { id: 'b', info: { base_model_id: 'a' } };
		expect(getModelAvailability(a, [a, b])).toBeNull();
	});
});
