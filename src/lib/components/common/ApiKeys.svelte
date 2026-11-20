<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import {
		type ApiKeyInfo,
		createApiKey,
		deleteApiKey,
		deleteApiKeyByUserId,
		getApiKeys,
		getApiKeysByUserId,
		rotateApiKey,
		rotateApiKeyByUserId
	} from '$lib/apis/api_keys';
	import { deleteUserAPIKeyById } from '$lib/apis/users';
	import { copyToClipboard } from '$lib/utils';

	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import SensitiveInput from '$lib/components/common/SensitiveInput.svelte';

	const i18n = getContext<typeof import('$lib/i18n').default>('i18n');

	// Set userId to manage another user's keys as an administrator (no creation there).
	export let userId: string | null = null;

	const EXPIRY_CHOICES: { days: number | null; label: string }[] = [
		{ days: null, label: 'No expiration' },
		{ days: 30, label: '30 days' },
		{ days: 90, label: '90 days' },
		{ days: 365, label: '1 year' }
	];

	let keys: ApiKeyInfo[] = [];
	let loaded = false;
	let busy = false;

	let showCreateForm = false;
	let newName = '';
	let newExpiryDays: number | null = null;

	// The secret of a key that was just created or rotated. Shown once.
	let revealed: { name: string; key: string } | null = null;

	let rotateTarget: ApiKeyInfo | null = null;
	let deleteTarget: ApiKeyInfo | null = null;
	let showRotateConfirm = false;
	let showDeleteConfirm = false;
	let showDeleteAllConfirm = false;

	const inputClass =
		'h-7 rounded-lg border border-gray-100/50 bg-gray-50/40 px-2 text-xs text-gray-700 outline-hidden transition-colors placeholder:text-gray-300 focus:border-blue-400 dark:border-white/[0.04] dark:bg-white/[0.03] dark:text-gray-300 dark:placeholder:text-gray-700 dark:focus:border-blue-500';
	const linkButtonClass =
		'text-xs text-gray-500 transition-colors hover:text-gray-900 disabled:opacity-50 dark:text-gray-500 dark:hover:text-white';

	const errorText = (error: unknown) =>
		typeof error === 'string' ? error : error instanceof Error ? error.message : `${error}`;

	const formatDate = (ts: number | null) =>
		ts ? new Date(ts * 1000).toLocaleDateString(undefined, { dateStyle: 'medium' }) : '';

	const isExpired = (key: ApiKeyInfo) => !!key.expires_at && key.expires_at * 1000 < Date.now();

	const load = async () => {
		try {
			keys =
				(userId
					? await getApiKeysByUserId(localStorage.token, userId)
					: await getApiKeys(localStorage.token)) ?? [];
		} catch (error) {
			toast.error(errorText(error));
			keys = [];
		}
		loaded = true;
	};

	const createHandler = async () => {
		const name = newName.trim();
		if (!name || busy) return;
		busy = true;
		try {
			const created = await createApiKey(localStorage.token, name, newExpiryDays);
			revealed = { name: created.name ?? name, key: created.key };
			newName = '';
			newExpiryDays = null;
			showCreateForm = false;
			toast.success($i18n.t('API Key created.'));
			await load();
		} catch (error) {
			toast.error(errorText(error));
		} finally {
			busy = false;
		}
	};

	const rotateHandler = async () => {
		const target = rotateTarget;
		if (!target || busy) return;
		busy = true;
		try {
			const rotated = userId
				? await rotateApiKeyByUserId(localStorage.token, userId, target.id)
				: await rotateApiKey(localStorage.token, target.id);
			revealed = { name: rotated.name ?? $i18n.t('Default key'), key: rotated.key };
			toast.success($i18n.t('API Key rotated.'));
			await load();
		} catch (error) {
			toast.error(errorText(error));
		} finally {
			busy = false;
		}
	};

	const deleteHandler = async () => {
		const target = deleteTarget;
		if (!target || busy) return;
		busy = true;
		try {
			if (userId) {
				await deleteApiKeyByUserId(localStorage.token, userId, target.id);
			} else {
				await deleteApiKey(localStorage.token, target.id);
			}
			toast.success($i18n.t('API Key deleted.'));
			await load();
		} catch (error) {
			toast.error(errorText(error));
		} finally {
			busy = false;
		}
	};

	// Admin only: revoke every key of the user at once (uses the legacy endpoint).
	const deleteAllHandler = async () => {
		if (!userId || busy) return;
		busy = true;
		try {
			await deleteUserAPIKeyById(localStorage.token, userId);
			revealed = null;
			toast.success($i18n.t('API Key deleted.'));
			await load();
		} catch (error) {
			toast.error(errorText(error));
		} finally {
			busy = false;
		}
	};

	onMount(load);
</script>

<div class="flex flex-col gap-2">
	{#if loaded && keys.length === 0}
		<div class="text-xs text-gray-400 dark:text-gray-600">{$i18n.t('No API keys yet.')}</div>
	{/if}

	{#each keys as key (key.id)}
		<div
			class="flex items-center justify-between gap-3 rounded-lg border border-gray-100/50 px-2.5 py-1.5 dark:border-white/[0.04]"
		>
			<div class="min-w-0">
				<div class="flex items-center gap-1.5 text-xs text-gray-800 dark:text-gray-200">
					<span class="truncate">{key.name ?? $i18n.t('Default key')}</span>
					<span class="shrink-0 font-mono text-[0.625rem] text-gray-400">{key.key_preview}</span>
					{#if isExpired(key)}
						<span class="shrink-0 text-[0.625rem] text-red-500">{$i18n.t('Expired')}</span>
					{/if}
				</div>
				<div class="text-[0.625rem] text-gray-400 dark:text-gray-600">
					{$i18n.t('Created {{date}}', { date: formatDate(key.created_at) })}
					·
					{key.last_used_at
						? $i18n.t('Last used {{date}}', { date: formatDate(key.last_used_at) })
						: $i18n.t('Never used')}
					{#if key.expires_at}
						· {$i18n.t('Expires {{date}}', { date: formatDate(key.expires_at) })}
					{/if}
				</div>
			</div>

			<div class="flex shrink-0 items-center gap-3">
				<button
					type="button"
					class={linkButtonClass}
					disabled={busy}
					on:click={() => {
						rotateTarget = key;
						showRotateConfirm = true;
					}}>{$i18n.t('Rotate')}</button
				>
				<button
					type="button"
					class={linkButtonClass}
					disabled={busy}
					on:click={() => {
						deleteTarget = key;
						showDeleteConfirm = true;
					}}>{$i18n.t('Delete')}</button
				>
			</div>
		</div>
	{/each}

	{#if revealed}
		<div class="space-y-1.5 rounded-lg border border-blue-200/60 p-2.5 dark:border-blue-500/20">
			<div class="text-xs text-gray-700 dark:text-gray-300">{revealed.name}</div>
			<div class="flex items-center gap-2">
				<SensitiveInput
					variant="settings"
					placeholder={$i18n.t('API Key')}
					value={revealed.key}
					readOnly={true}
				/>
				<button
					type="button"
					class="shrink-0 text-xs text-gray-500 hover:text-gray-700 dark:hover:text-gray-200"
					aria-label={$i18n.t('Copy API Key')}
					on:click={async () => {
						if (revealed && (await copyToClipboard(revealed.key)))
							toast.success($i18n.t('Copied to clipboard'));
					}}>{$i18n.t('Copy')}</button
				>
				<button
					type="button"
					class="shrink-0 text-xs text-gray-500 hover:text-gray-700 dark:hover:text-gray-200"
					on:click={() => {
						revealed = null;
					}}>{$i18n.t('Close')}</button
				>
			</div>
			<p class="text-xs text-gray-500">
				{$i18n.t('Copy this key now. It will not be shown again.')}
			</p>
		</div>
	{/if}

	{#if !userId}
		{#if showCreateForm}
			<!-- Not a <form>: this component is rendered inside the Account settings form. -->
			<div
				class="flex flex-wrap items-center gap-2"
				role="group"
				on:keydown={(e) => {
					if (e.key === 'Enter') {
						e.preventDefault();
						createHandler();
					}
				}}
			>
				<input
					class="{inputClass} min-w-40 flex-1"
					type="text"
					maxlength="64"
					bind:value={newName}
					aria-label={$i18n.t('Key name')}
					placeholder={$i18n.t('Key name (e.g. Word add-in)')}
					required
				/>
				<select class={inputClass} bind:value={newExpiryDays} aria-label={$i18n.t('Expiration')}>
					{#each EXPIRY_CHOICES as choice}
						<option value={choice.days}>{$i18n.t(choice.label)}</option>
					{/each}
				</select>
				<button
					type="button"
					class={linkButtonClass}
					disabled={busy || !newName.trim()}
					on:click={createHandler}>{$i18n.t('Create')}</button
				>
				<button
					type="button"
					class={linkButtonClass}
					on:click={() => {
						showCreateForm = false;
					}}>{$i18n.t('Cancel')}</button
				>
			</div>
		{:else}
			<div>
				<button
					type="button"
					class={linkButtonClass}
					on:click={() => {
						showCreateForm = true;
					}}>+ {$i18n.t('settings.personal.account.createNewSecretKey.label')}</button
				>
			</div>
		{/if}
	{:else if keys.length > 1}
		<div>
			<button
				type="button"
				class={linkButtonClass}
				disabled={busy}
				on:click={() => {
					showDeleteAllConfirm = true;
				}}>{$i18n.t('Delete all keys')}</button
			>
		</div>
	{/if}
</div>

<ConfirmDialog
	bind:show={showRotateConfirm}
	title={$i18n.t('Rotate API Key?')}
	message={$i18n.t(
		'This will replace the secret of this key. Integrations using the old secret will stop working; your other keys are not affected.'
	)}
	confirmLabel={$i18n.t('Rotate')}
	on:confirm={rotateHandler}
/>

<ConfirmDialog
	bind:show={showDeleteConfirm}
	title={$i18n.t('Delete API Key?')}
	message={$i18n.t(
		'This will revoke this key. Integrations using it will stop working; your other keys are not affected.'
	)}
	confirmLabel={$i18n.t('Delete')}
	on:confirm={deleteHandler}
/>

<ConfirmDialog
	bind:show={showDeleteAllConfirm}
	title={$i18n.t('Delete all keys?')}
	message={$i18n.t(
		'This will revoke every API key of this user. Their integrations will stop working.'
	)}
	confirmLabel={$i18n.t('Delete all keys')}
	on:confirm={deleteAllHandler}
/>
