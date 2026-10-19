/**
 * What the gateway (Lachésis) says about a model before a message is sent. It rides on the model
 * object Open WebUI already serves: `GET /v1/models` entries are kept whole under `model.openai`.
 * Everything here is optional, so a model from any other connection simply shows nothing.
 */
export type ModelAvailability =
	| { state: 'ready' }
	| { state: 'starting'; etaMin: number }
	| { state: 'asleep'; etaMin: number }
	| { state: 'stopping' }
	| { state: 'unavailable'; reason: string; until?: number };

type Raw = { state?: string; eta_min?: number; reason?: string; until?: number } | undefined;

/** Reads `lachesis` off a model object; falls back on the coarse `owned_by` (ready, starting, cold) older gateways send. */
export const getModelAvailability = (model: any): ModelAvailability | null => {
	const raw: Raw = model?.openai?.lachesis ?? model?.lachesis;
	if (raw?.state) {
		switch (raw.state) {
			case 'ready':
			case 'stopping':
				return { state: raw.state };
			case 'starting':
			case 'asleep':
				return { state: raw.state, etaMin: Math.max(1, Math.round(raw.eta_min ?? 1)) };
			case 'unavailable':
				return {
					state: 'unavailable',
					reason: raw.reason ?? 'blocked',
					...(typeof raw.until === 'number' ? { until: raw.until } : {})
				};
		}
		return null;
	}
	switch (model?.openai?.owned_by) {
		case 'ready':
			return { state: 'ready' };
		case 'starting':
			return { state: 'starting', etaMin: 1 };
		default:
			return null;
	}
};

/** Tailwind colour classes for the dot. */
export const availabilityDot = (a: ModelAvailability): string =>
	a.state === 'ready'
		? 'bg-green-500'
		: a.state === 'starting' || a.state === 'stopping'
			? 'bg-yellow-500'
			: a.state === 'asleep'
				? 'bg-gray-400'
				: 'bg-red-500';
