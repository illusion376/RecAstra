import type { CSSProperties } from 'react';
const paths: Record<string, React.ReactNode> = {
 trash: <><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/></>,
 folder: <path d="M3 7V5h6l2 2h10v13H3V7Z"/>, calendar: <><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4m10-4v4M3 11h18m-13 4h2m4 0h2"/></>,
 file: <><path d="M6 3h8l4 4v14H6V3Z M14 3v5h4M9 12h6m-6 4h6"/></>, search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
 upload: <><path d="M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6"/></>, download: <><path d="M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4"/></>,
 chevron: <path d="m9 5 7 7-7 7"/>, plus: <path d="M12 5v14M5 12h14"/>, check: <path d="m5 12 4 4L19 6"/>, close: <path d="m6 6 12 12M6 18 18 6"/>,
 question: <><circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 0 1 6 0c0 2-3 2-3 5m0 3v.1"/></>, alert: <><path d="m12 3 10 18H2L12 3Z M12 9v5m0 3v.1"/></>,
 users: <><circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3m1-16a3 3 0 0 1 0 6m2 4a5 5 0 0 1 3 5"/></>, bell: <><path d="M5 16h14l-2-3V9A5 5 0 0 0 7 9v4l-2 3Zm5 4h4"/></>, clock: <><circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/></>,
 list: <path d="M8 6h13M8 12h13M8 18h13M3 6h.1M3 12h.1M3 18h.1"/>, edit: <><path d="m4 16 12-12 4 4L8 20H4v-4Zm9-9 4 4"/></>, play: <path d="m9 5 11 7-11 7V5Z"/>,
};
export function Icon({name, size = 20, style}: {name: string; size?: number; style?: CSSProperties}) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={style}>{paths[name] ?? paths.file}</svg>;
}
