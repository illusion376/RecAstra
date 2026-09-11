import { Meeting, parseMeeting } from './meeting';
import { Session, User, clearSession, sessionToken } from './session';
export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? '').replace(/\/$/, '');
export const DEMO_MODE = !API_URL;
function errorMessage(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === 'string') return detail;
  // Ошибки валидации FastAPI приходят списком: [{loc, msg, ...}].
  const first = Array.isArray(detail) ? (detail[0] as { msg?: unknown } | undefined)?.msg : undefined;
  if (typeof first === 'string') return first.replace(/^Value error, /, '');
  return `Не удалось выполнить запрос (${status}). Попробуйте ещё раз.`;
}
async function request(path: string, init?: RequestInit, { auth = true } = {}): Promise<unknown> {
  const headers = new Headers(init?.headers);
  const token = auth ? sessionToken() : null;
  if (token) headers.set('Authorization', `Bearer ${token}`);
  try {
    const response = await fetch(`${API_URL}${path}`, { ...init, headers, signal: init?.signal ?? AbortSignal.timeout(120000) });

    if (!response.ok) {
      const body = await response.json().catch(() => null);
      // Токен истёк или отозван — возвращаем пользователя на экран входа.
      if (response.status === 401 && auth) {
        const message = 'Сессия истекла. Войдите снова.';
        clearSession(message);
        throw new Error(message);
      }
      throw new Error(errorMessage(body, response.status));
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

  if (m.audio_url?.startsWith('/')) {
    const url = new URL(m.audio_url, API_URL);
    // Плеер <audio> не умеет слать заголовок Authorization — токен идёт параметром.
    const token = sessionToken();
    if (token) url.searchParams.set('access_token', token);
    m.audio_url = url.href;
  }

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

// --- Авторизация ---------------------------------------------------------
function parseSession(value: unknown): Session {
  const s = value as { token?: unknown; user?: Record<string, unknown> } | null;
  if (typeof s?.token !== 'string' || !s.user || !['id', 'email', 'name', 'created_at'].every(key => typeof s.user?.[key] === 'string')) throw new Error('Сервер вернул некорректный ответ на вход.');
  return { token: s.token, user: s.user as User };
}
const json = (body: unknown): RequestInit => ({ method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
export async function login(email: string, password: string): Promise<Session> {
  return parseSession(await request('/auth/login', json({ email, password }), { auth: false }));
}
export async function register(name: string, email: string, password: string): Promise<Session> {
  return parseSession(await request('/auth/register', json({ name, email, password }), { auth: false }));
}
