<script lang="ts">
	import { getContext } from 'svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import { models } from '$lib/stores';
	import { availabilityDot, getModelAvailability } from '$lib/utils/modelAvailability';

	const i18n = getContext<typeof import('$lib/i18n').default>('i18n');

	export let model: any = null;
	/** `dot`: the colour only, with the sentence in a tooltip. `text`: the short sentence too. */
	export let mode: 'dot' | 'text' = 'text';

	$: availability = getModelAvailability(model, $models);

	/** `14:35` today, `Fri 08:00` another day, in the reader's own time zone. */
	const when = (epochSec: number) => {
		const date = new Date(epochSec * 1000);
		const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
		return date.toDateString() === new Date().toDateString()
			? time
			: `${date.toLocaleDateString([], { weekday: 'short' })} ${time}`;
	};

	/** The full sentence (tooltip) and a short one for the list. */
	$: texts = describe(availability, $i18n);

	// `_i18n` makes the sentences follow a language change.
	const describe = (a: typeof availability, _i18n: unknown): { label: string; short: string } => {
		if (!a) return { label: '', short: '' };
		switch (a.state) {
			case 'ready':
				if (a.stopsAt && a.stopsKind === 'idle')
					return {
						label: $i18n.t('Ready. Stops around {{time}} if nobody writes again', {
							time: when(a.stopsAt)
						}),
						short: $i18n.t('Stops around {{time}}', { time: when(a.stopsAt) })
					};
				if (a.stopsAt)
					return {
						label: $i18n.t('Ready. On at least until {{time}}', { time: when(a.stopsAt) }),
						short: $i18n.t('Until {{time}}', { time: when(a.stopsAt) })
					};
				return { label: $i18n.t('Ready'), short: '' };
			case 'starting': {
				const label = $i18n.t('Starting, ready in about {{minutes}} min', { minutes: a.etaMin });
				return { label, short: label };
			}
			case 'asleep': {
				const label = $i18n.t('Asleep: your first message starts it (about {{minutes}} min)', {
					minutes: a.etaMin
				});
				return { label, short: label };
			}
			case 'stopping': {
				const label = $i18n.t('Shutting down');
				return { label, short: label };
			}
			default: {
				const label =
					a.reason === 'admin'
						? a.until
							? $i18n.t('Stopped by an administrator until {{time}}', { time: when(a.until) })
							: $i18n.t('Stopped by an administrator')
						: a.reason === 'limit'
							? $i18n.t('Daily limit reached, back tomorrow')
							: a.reason === 'restart'
								? $i18n.t('Available after the next restart')
								: a.reason === 'manual'
									? a.startsAt
										? $i18n.t('Starts by itself on {{time}}', { time: when(a.startsAt) })
										: $i18n.t('Does not start on request')
									: $i18n.t('Unavailable for now');
				return { label, short: label };
			}
		}
	};
</script>

{#if availability}
	<Tooltip content={texts.label} className="shrink-0">
		<span class="flex items-center gap-1 text-xs font-normal text-gray-500 dark:text-gray-400">
			<span
				class="size-2 shrink-0 rounded-full {availabilityDot(availability)}"
				aria-hidden="true"
			/>
			{#if mode === 'text' && texts.short}
				<span class="truncate max-w-[12rem]">{texts.short}</span>
			{:else}
				<span class="sr-only">{texts.label}</span>
			{/if}
		</span>
	</Tooltip>
{/if}
