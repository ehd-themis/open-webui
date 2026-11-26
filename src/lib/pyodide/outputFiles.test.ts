import { describe, expect, it, vi, beforeEach } from 'vitest';

const uploads: { name: string; type: string; process: unknown }[] = [];
vi.mock('$lib/apis/files', () => ({
	uploadFile: vi.fn(async (_token: string, file: File, _meta: unknown, process: unknown) => {
		uploads.push({ name: file.name, type: file.type, process });
		return { id: `id-${file.name}` };
	})
}));

import { snapshotOutputFiles, uploadChangedOutputFiles } from './outputFiles';

type Entry = { name: string; type: 'file' | 'directory'; size: number; mtime: number };

// Answers fs:list / fs:read like the Pyodide sandbox does, over a fake /mnt/uploads.
const fakeWorker = (files: Map<string, Entry>) => {
	const listeners = new Set<(event: MessageEvent) => void>();
	return {
		postMessage(message: { id: string; type: string; path?: string }) {
			queueMicrotask(() => {
				let data: Record<string, unknown>;
				if (message.type === 'fs:list') {
					data = { id: message.id, type: 'fs:list', entries: [...files.values()] };
				} else {
					const name = message.path!.split('/').pop()!;
					data = { id: message.id, type: 'fs:read', data: new TextEncoder().encode(name).buffer };
				}
				for (const listener of [...listeners]) listener({ data } as MessageEvent);
			});
		},
		addEventListener: (_type: 'message', listener: (event: MessageEvent) => void) =>
			listeners.add(listener),
		removeEventListener: (_type: 'message', listener: (event: MessageEvent) => void) =>
			listeners.delete(listener)
	};
};

describe('code interpreter output files', () => {
	beforeEach(() => {
		uploads.length = 0;
	});

	it('uploads only new or changed files, not the chat attachments', async () => {
		const files = new Map<string, Entry>([
			['old.csv', { name: 'old.csv', type: 'file', size: 5, mtime: 1 }],
			['changed.txt', { name: 'changed.txt', type: 'file', size: 5, mtime: 1 }],
			['input.pdf', { name: 'input.pdf', type: 'file', size: 5, mtime: 1 }]
		]);
		const worker = fakeWorker(files);
		const before = await snapshotOutputFiles(worker);
		expect(before?.size).toBe(3);

		// The run writes a new file, rewrites one in place (same size) and the chat attachment is copied in again.
		files.set('cal.ics', { name: 'cal.ics', type: 'file', size: 9, mtime: 2 });
		files.set('changed.txt', { name: 'changed.txt', type: 'file', size: 5, mtime: 2 });
		files.set('input.pdf', { name: 'input.pdf', type: 'file', size: 5, mtime: 2 });
		files.set('dir', { name: 'dir', type: 'directory', size: 0, mtime: 2 });

		const result = await uploadChangedOutputFiles(worker, before!, new Set(['input.pdf']), 't');

		expect(result.map((file) => file.name).sort()).toEqual(['cal.ics', 'changed.txt']);
		expect(result.find((file) => file.name === 'cal.ics')?.id).toBe('id-cal.ics');
		expect(uploads.find((upload) => upload.name === 'cal.ics')).toEqual({
			name: 'cal.ics',
			type: 'text/calendar',
			process: false
		});
	});

	it('uploads nothing when nothing changed', async () => {
		const files = new Map<string, Entry>([
			['a.csv', { name: 'a.csv', type: 'file', size: 1, mtime: 1 }]
		]);
		const worker = fakeWorker(files);
		const before = await snapshotOutputFiles(worker);
		expect(await uploadChangedOutputFiles(worker, before!, new Set(), 't')).toEqual([]);
		expect(uploads).toEqual([]);
	});
});
