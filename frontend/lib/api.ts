import { Meeting, parseMeeting } from './meeting';
export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? '').replace(/\/$/, '');
export const DEMO_MODE = !API_URL;
async function request(path: string, init?: RequestInit): Promise<unknown> {
  try {
    const response = await fetch(`${API_URL}${path}`, { ...init, signal: init?.signal ?? AbortSignal.timeout(120000) });

    if (!response.ok) throw new Error(`Не удалось выполнить запрос (${response.status}). Попробуйте ещё раз.`);

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
