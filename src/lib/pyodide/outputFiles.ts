// Fork: files written by a code interpreter run, uploaded so they can be attached to the message.
//
// Pyodide runs in a sandboxed iframe (opaque origin): what the code writes to /mnt/uploads lives in
// this tab only and cannot reach Hermes by itself. The page, which is same-origin, lists the folder
// before and after a run started by the model, uploads what was created or changed with the
// person's own session (no processing, no knowledge base), and hands the file ids to the backend,
// which attaches them to the assistant message. The sandbox never sees the session.

import { uploadFile } from '$lib/apis/files';

export const OUTPUT_DIR = '/mnt/uploads';
const MAX_FILES = 20;
const MAX_FILE_BYTES = 25 * 1024 * 1024;
const FS_TIMEOUT_MS = 30000;

type FsEntry = { name: string; type: 'file' | 'directory'; size: number; mtime?: number };
export type OutputSnapshot = Map<string, string>;
export type UploadedOutputFile = { id: string; name: string; size: number };

type PyodideLike = {
	postMessage: (message: unknown) => void;
	addEventListener: (type: 'message', listener: (event: MessageEvent) => void) => void;
	removeEventListener: (type: 'message', listener: (event: MessageEvent) => void) => void;
};

let requestId = 0;

type FsResponse = { id?: string; entries?: FsEntry[]; data?: ArrayBuffer; error?: string };

const fsRequest = (worker: PyodideLike, message: Record<string, unknown>): Promise<FsResponse> => {
	const id = `fs-output-${++requestId}`;
	return new Promise((resolve, reject) => {
		const timer = setTimeout(() => {
			worker.removeEventListener('message', onMessage);
			reject(new Error('Timeout'));
		}, FS_TIMEOUT_MS);
		function onMessage(event: MessageEvent) {
			if (event.data?.id !== id) return;
			clearTimeout(timer);
			worker.removeEventListener('message', onMessage);
			if (event.data?.error) reject(new Error(event.data.error));
			else resolve(event.data);
		}
		worker.addEventListener('message', onMessage);
		worker.postMessage({ ...message, id });
	});
};

const signature = (entry: FsEntry) => `${entry.size}:${entry.mtime ?? ''}`;

const listFiles = async (worker: PyodideLike): Promise<FsEntry[]> => {
	const res = await fsRequest(worker, { type: 'fs:list', path: OUTPUT_DIR });
	return (res?.entries ?? []).filter((entry) => entry.type === 'file');
};

/** Name -> size and modification time of the files in /mnt/uploads; null if it could not be read. */
export const snapshotOutputFiles = async (worker: PyodideLike): Promise<OutputSnapshot | null> => {
	try {
		const entries = await listFiles(worker);
		return new Map(entries.map((entry) => [entry.name, signature(entry)]));
	} catch (e) {
		console.warn('Could not list code interpreter files:', e);
		return null;
	}
};

const guessType = (name: string) => {
	const ext = name.split('.').pop()?.toLowerCase() ?? '';
	return (
		{
			csv: 'text/csv',
			ics: 'text/calendar',
			json: 'application/json',
			md: 'text/markdown',
			txt: 'text/plain',
			html: 'text/html',
			pdf: 'application/pdf',
			png: 'image/png',
			jpg: 'image/jpeg',
			jpeg: 'image/jpeg',
			svg: 'image/svg+xml',
			xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
			docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
			pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
			zip: 'application/zip'
		}[ext] ?? 'application/octet-stream'
	);
};

/**
 * Upload the files of /mnt/uploads that are new or changed since `before`, except `skip` (the
 * chat's attachments, copied in at the start of the run). Errors are logged and skipped: the run's
 * result must reach the model even if an upload fails.
 */
export const uploadChangedOutputFiles = async (
	worker: PyodideLike,
	before: OutputSnapshot,
	skip: Set<string>,
	token: string
): Promise<UploadedOutputFile[]> => {
	let entries: FsEntry[];
	try {
		entries = await listFiles(worker);
	} catch (e) {
		console.warn('Could not list code interpreter files:', e);
		return [];
	}

	const changed = entries
		.filter((entry) => !skip.has(entry.name) && before.get(entry.name) !== signature(entry))
		.filter((entry) => entry.size <= MAX_FILE_BYTES)
		.slice(0, MAX_FILES);

	const uploaded: UploadedOutputFile[] = [];
	for (const entry of changed) {
		try {
			const res = await fsRequest(worker, { type: 'fs:read', path: `${OUTPUT_DIR}/${entry.name}` });
			if (!res.data) continue;
			const file = new File([res.data], entry.name, { type: guessType(entry.name) });
			const item = await uploadFile(token, file, { source: 'code_interpreter' }, false, false);
			if (item?.id) uploaded.push({ id: item.id, name: entry.name, size: entry.size });
		} catch (e) {
			console.warn(`Could not upload code interpreter file ${entry.name}:`, e);
		}
	}
	return uploaded;
};
