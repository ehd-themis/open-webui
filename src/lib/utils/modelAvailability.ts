/**
 * What the gateway (Lachésis) says about a model before a message is sent. It rides on the model
 * object Open WebUI already serves: `GET /v1/models` entries are kept whole under `model.openai`.
 * Everything here is optional, so a model from any other connection simply shows nothing.
 */
export type ModelAvailability =
	| { state: 'ready'; stopsAt?: number; stopsKind?: 'idle' | 'window' | 'override' }
	| { state: 'starting'; etaMin: number }
	| { state: 'asleep'; etaMin: number }
	| { state: 'stopping' }
	| { state: 'unavailable'; reason: string; until?: number; startsAt?: number };

type Raw =
	| {
			state?: string;
			eta_min?: number;
			reason?: string;
			until?: number;
			starts_at?: number;
			stops_at?: number;
			stops_kind?: string;
	  }
	| undefined;

/**
 * The availability of a model, as shown to the user. A workspace preset ("sub-model", `info.base_model_id`)
 * carries no gateway field of its own: it is served by its base model, so the base's state is the preset's
 * (followed up the chain, a few hops at most). `all` is the list of models the user can see.
 */
export const getModelAvailability = (model: any, all: any[] = []): ModelAvailability | null => {
	let current = model;
	for (let hop = 0; hop < 5 && current; hop++) {
		const found = readAvailability(current);
		if (found) return found;
		const baseId = current?.info?.base_model_id;
		if (!baseId) return null;
		current =
			all.find((m) => m?.id === baseId) ?? all.find((m) => m?.id === String(baseId).split(':')[0]);
	}
	return null;
};

/** Reads `lachesis` off a model object; falls back on the coarse `owned_by` (ready, starting, cold) older gateways send. */
const readAvailability = (model: any): ModelAvailability | null => {
	const raw: Raw = model?.openai?.lachesis ?? model?.lachesis;
	if (raw?.state) {
		switch (raw.state) {
			case 'ready':
				return {
					state: 'ready',
					...(typeof raw.stops_at === 'number' &&
					['idle', 'window', 'override'].includes(raw.stops_kind ?? '')
						? { stopsAt: raw.stops_at, stopsKind: raw.stops_kind as 'idle' | 'window' | 'override' }
						: {})
				};
			case 'stopping':
				return { state: 'stopping' };
			case 'starting':
			case 'asleep':
				return { state: raw.state, etaMin: Math.max(1, Math.round(raw.eta_min ?? 1)) };
			case 'unavailable':
				return {
					state: 'unavailable',
					reason: raw.reason ?? 'blocked',
					...(typeof raw.until === 'number' ? { until: raw.until } : {}),
					...(typeof raw.starts_at === 'number' ? { startsAt: raw.starts_at } : {})
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
