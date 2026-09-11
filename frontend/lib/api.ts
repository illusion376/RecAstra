import { Meeting, parseMeeting } from './meeting';
export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? '').replace(/\/$/, '');
export const DEMO_MODE = !API_URL;
async function request(path: string, init?: RequestInit): Promise<unknown> {
  try {
    const response = await fetch(`${API_URL}${path}`, { ...init, signal: init?.signal ?? AbortSignal.timeout(120000) });

    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw new Error(typeof body?.detail === 'string' ? body.detail : `Не удалось выполнить запрос (${response.status}). Попробуйте ещё раз.`);
    }

    return await response.json();
  } catch (error) {
    if (error instanceof Error && error.name === 'TimeoutError') throw new Error('Сервер не ответил вовремя. Попробуйте ещё раз.');

    if (error instanceof TypeError) throw new Error('Нет соединения с сервером. Проверьте подключение и повторите запрос.');

    throw error;
  }
}
function normalize(value: unknown): Meeting {
  const m = parseMeeting(value);

  if (m.audio_url?.startsWith('/')) m.audio_url = new URL(m.audio_url, API_URL).href;

  return m;
}
export async function listMeetings(signal?: AbortSignal): Promise<Meeting[]> {
  const data = await request('/meetings', { signal });

  if (!Array.isArray(data)) throw new Error('Сервер вернул неподдерживаемый список встреч.');

  return data.map(normalize);
}
export async function getMeeting(id: string, signal?: AbortSignal): Promise<Meeting> {
  return normalize(await request(`/meetings/${encodeURIComponent(id)}`, { signal }));
}
export async function uploadMeeting(file: File, title: string, signal?: AbortSignal): Promise<Meeting> {
  const body = new FormData(); body.append('file', file); body.append('title', title);

  return normalize(await request('/meetings', { method: 'POST', body, signal }));
}

export type ExportedDocument = { id: string; meeting_id: string; title: string; content: string; created_at: string };
function parseDocument(value: unknown): ExportedDocument {
  if (!value || typeof value !== 'object') throw new Error('Некорректный документ от сервера.');
  const d = value as Record<string, unknown>;
  if (!['id', 'meeting_id', 'title', 'content', 'created_at'].every(key => typeof d[key] === 'string')) throw new Error('Некорректный документ от сервера.');
  return d as ExportedDocument;
}
export async function listDocuments(signal?: AbortSignal): Promise<ExportedDocument[]> {
  const value = await request('/documents', { signal });
  if (!Array.isArray(value)) throw new Error('Некорректный список документов от сервера.');
  return value.map(parseDocument);
}
export async function saveDocument(document: Omit<ExportedDocument, 'created_at'>): Promise<ExportedDocument> {
  return parseDocument(await request('/documents', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(document) }));
}

export async function deleteDocument(id: string): Promise<void> {
  await request(`/documents/${encodeURIComponent(id)}`, { method: 'DELETE' });
}
