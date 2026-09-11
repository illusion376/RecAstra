// Экспорт ТЗ: идентификатор снимка, имя файла и скачивание.

type RandomSource = Pick<Crypto, 'getRandomValues'> & { randomUUID?: () => string };

/**
 * UUID v4 для нового документа или карточки.
 *
 * crypto.randomUUID браузер даёт только в защищённом контексте — HTTPS или
 * localhost. Когда фронтенд открыт по http://IP-сервера (так он и живёт
 * после `docker compose up` на сервере), функции нет, и экспорт падал
 * с «crypto.randomUUID is not a function» ещё до запроса. getRandomValues
 * доступен в любом контексте.
 */
export function newId(source: RandomSource = globalThis.crypto): string {
  if (typeof source.randomUUID === 'function') return source.randomUUID();
  const bytes = source.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // версия 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // вариант RFC 4122
  const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** «Сервис онлайн-записи» → «ТЗ_Сервис_онлайн_записи.md». */
export function specFileName(title: string): string {
  const safe = title.replace(/[^\p{L}\p{N}_-]+/gu, '_').replace(/^[_-]+|[_-]+$/g, '').slice(0, 100);
  return `ТЗ_${safe || 'проект'}.md`;
}

/** Скачать текст файлом, не уходя со страницы. */
export function downloadText(fileName: string, content: string, type = 'text/markdown;charset=utf-8') {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  link.style.display = 'none';
  // Firefox не всегда скачивает по ссылке, которой нет в документе.
  document.body.append(link);
  link.click();
  link.remove();
  // Safari начинает скачивание не сразу — адрес отзываем с запасом.
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
