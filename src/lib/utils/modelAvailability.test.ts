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
});
