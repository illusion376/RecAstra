// Сессия пользователя: токен и профиль в localStorage.
// Компоненты читают её через useSyncExternalStore, поэтому выход в одной
// вкладке (или 401 от сервера) сразу возвращает все вкладки к экрану входа.
export type User = { id: string; email: string; name: string; created_at: string };
export type Session = { token: string; user: User };

const KEY = 'recastra.session';
const CHANGE_EVENT = 'recastra:session';
let cache: { raw: string | null; value: Session | null } = { raw: null, value: null };
let logoutReason = '';

function parse(raw: string | null): Session | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<Session>;
    const user = value?.user as Partial<User> | undefined;
    if (typeof value?.token !== 'string' || !user || !['id', 'email', 'name', 'created_at'].every(key => typeof user[key as keyof User] === 'string')) return null;
    return value as Session;
  } catch { return null; }
}
// undefined — хранилище недоступно (приватный режим), тогда живём на кэше.
function read(): string | null | undefined {
  try { return window.localStorage.getItem(KEY); } catch { return undefined; }
}
function write(raw: string | null) {
  try { if (raw === null) window.localStorage.removeItem(KEY); else window.localStorage.setItem(KEY, raw); } catch { /* приватный режим: сессия живёт до перезагрузки */ }
  cache = { raw, value: parse(raw) };
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

export function getSession(): Session | null {
  const stored = read();
  const raw = stored === undefined ? cache.raw : stored;
  if (raw !== cache.raw) cache = { raw, value: parse(raw) };
  return cache.value;
}
export function subscribeSession(onChange: () => void): () => void {
  window.addEventListener('storage', onChange);
  window.addEventListener(CHANGE_EVENT, onChange);
  return () => { window.removeEventListener('storage', onChange); window.removeEventListener(CHANGE_EVENT, onChange); };
}
export function saveSession(session: Session) {
  logoutReason = '';
  write(JSON.stringify(session));
}
/** reason показывается на экране входа — например, «сессия истекла». */
export function clearSession(reason = '') {
  logoutReason = reason;
  write(null);
}
export function logoutMessage(): string {
  return logoutReason;
}
export function sessionToken(): string | null {
  return typeof window === 'undefined' ? null : getSession()?.token ?? null;
}
