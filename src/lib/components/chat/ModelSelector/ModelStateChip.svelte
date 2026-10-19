<script lang="ts">
	import { getContext } from 'svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import { availabilityDot, getModelAvailability } from '$lib/utils/modelAvailability';

	const i18n = getContext<typeof import('$lib/i18n').default>('i18n');

	export let model: any = null;
	/** `dot`: the colour only, with the sentence in a tooltip. `text`: the sentence too. */
	export let mode: 'dot' | 'text' = 'text';

	$: availability = getModelAvailability(model);

	const at = (epochSec: number) =>
		new Date(epochSec * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

	$: label = !availability
		? ''
		: availability.state === 'ready'
			? $i18n.t('Ready')
			: availability.state === 'starting'
				? $i18n.t('Starting, ready in about {{minutes}} min', { minutes: availability.etaMin })
				: availability.state === 'asleep'
					? $i18n.t('Asleep: your first message starts it (about {{minutes}} min)', {
							minutes: availability.etaMin
						})
					: availability.state === 'stopping'
						? $i18n.t('Shutting down')
						: availability.reason === 'admin'
							? availability.until
								? $i18n.t('Stopped by an administrator until {{time}}', {
										time: at(availability.until)
									})
								: $i18n.t('Stopped by an administrator')
							: availability.reason === 'limit'
								? $i18n.t('Daily limit reached, back tomorrow')
								: availability.reason === 'restart'
									? $i18n.t('Available after the next restart')
									: $i18n.t('Unavailable for now');
</script>

{#if availability}
	<Tooltip content={label} className="shrink-0">
		<span class="flex items-center gap-1 text-xs font-normal text-gray-500 dark:text-gray-400">
			<span
				class="size-2 shrink-0 rounded-full {availabilityDot(availability)}"
				aria-hidden="true"
			/>
			{#if mode === 'text' && availability.state !== 'ready'}
				<span class="truncate max-w-[12rem]">{label}</span>
			{:else}
				<span class="sr-only">{label}</span>
			{/if}
		</span>
	</Tooltip>
{/if}
