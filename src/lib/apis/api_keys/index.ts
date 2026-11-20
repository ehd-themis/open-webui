import { WEBUI_API_BASE_URL } from '$lib/constants';

export type ApiKeyInfo = {
	id: string;
	name: string | null;
	key_preview: string;
	created_at: number;
	updated_at: number;
	last_used_at: number | null;
	expires_at: number | null;
};

export type ApiKeyCreated = ApiKeyInfo & { key: string };

const request = async <T>(
	token: string,
	path: string,
	method: string,
	body?: object
): Promise<T> => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/api_keys${path}`, {
		method,
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		},
		body: body ? JSON.stringify(body) : undefined
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = err?.detail ?? err;
			return null;
		});

	if (error) {
		throw error;
	}

	return res as T;
};

// Own keys

export const getApiKeys = (token: string) => request<ApiKeyInfo[]>(token, '/', 'GET');

export const createApiKey = (token: string, name: string, expiresInDays: number | null) =>
	request<ApiKeyCreated>(token, '/', 'POST', { name, expires_in_days: expiresInDays });

export const rotateApiKey = (token: string, keyId: string) =>
	request<ApiKeyCreated>(token, `/${encodeURIComponent(keyId)}/rotate`, 'POST');

export const deleteApiKey = (token: string, keyId: string) =>
	request<boolean>(token, `/${encodeURIComponent(keyId)}`, 'DELETE');

// Another user's keys (admin)

export const getApiKeysByUserId = (token: string, userId: string) =>
	request<ApiKeyInfo[]>(token, `/users/${encodeURIComponent(userId)}`, 'GET');

export const rotateApiKeyByUserId = (token: string, userId: string, keyId: string) =>
	request<ApiKeyCreated>(
		token,
		`/users/${encodeURIComponent(userId)}/${encodeURIComponent(keyId)}/rotate`,
		'POST'
	);

export const deleteApiKeyByUserId = (token: string, userId: string, keyId: string) =>
	request<boolean>(
		token,
		`/users/${encodeURIComponent(userId)}/${encodeURIComponent(keyId)}`,
		'DELETE'
	);
