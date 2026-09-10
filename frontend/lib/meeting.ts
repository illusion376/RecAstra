export type Segment = { id: string; speaker: string; start: number; text: string };
export type Requirement = { role?: string; needs_clarification?: boolean; id: string; title: string; description: string; category: 'functional' | 'nonfunctional'; source: number | null };
export type Question = { needs_clarification?: boolean; constraint_index?: number; id: string; text: string; source: number | null; resolved: boolean };
const SUPPORTED_ANALYSIS_SECTIONS = [
  { key: 'functional', title: 'Функциональные требования', icon: 'list' },
  { key: 'scenarios', title: 'Пользовательские сценарии', icon: 'calendar' },
  { key: 'roles', title: 'Роли пользователей', icon: 'users' },
  { key: 'constraints', title: 'Ограничения', icon: 'alert' },
  { key: 'conditions', title: 'Важные условия', icon: 'file' },
  { key: 'questions', title: 'Открытые вопросы', icon: 'question' },
  { key: 'agreements', title: 'Договорённости', icon: 'check' },
] as const;

export const ANALYSIS_SECTIONS = SUPPORTED_ANALYSIS_SECTIONS.filter(section => section.key !== 'roles');
export type AnalysisKind = typeof SUPPORTED_ANALYSIS_SECTIONS[number]['key'];
export type AnalysisItem = {
  id: string; kind: AnalysisKind; title: string; description: string;
  source: number | null; role?: string; needs_clarification?: boolean; resolved?: boolean;
};
export type Meeting = {
  analysis?: AnalysisItem[];
  id: string; title: string; date: string; filename: string; duration: number;
  status: 'processing' | 'ready' | 'failed'; audio_url?: string;
  transcript: Segment[]; requirements: Requirement[]; questions: Question[]; constraints: string[];
};
export function time(seconds: number) {
  const value = Math.max(0, Math.floor(seconds));
  return `${Math.floor(value / 60).toString().padStart(2, '0')}:${(value % 60).toString().padStart(2, '0')}`;
}

export function analysisItems(meeting: Meeting): AnalysisItem[] {
  if (meeting.analysis !== undefined) return meeting.analysis;
  return [
    ...meeting.requirements.map(r => ({ ...r, id: `requirement:${r.id}`, kind: r.category === 'functional' ? 'functional' as const : 'conditions' as const })),
    ...meeting.constraints.map((title, i) => ({ id: `constraint:${i}`, kind: 'constraints' as const, title, description: '', source: null,
      needs_clarification: meeting.questions.some(q => q.constraint_index === i && !q.resolved) })),
    ...meeting.questions.map(q => ({ ...q, id: `question:${q.id}`, kind: 'questions' as const, title: q.text, description: '' })),
  ];
}
export function specification(meeting: Meeting) {
  const items = analysisItems(meeting);
  return [`# Техническое задание\n\n${meeting.title}`, 'Статус: черновик. Требует проверки и согласования.',
    ...ANALYSIS_SECTIONS.map(section => `## ${section.title}\n\n` + (items.filter(item => item.kind === section.key).map((item, i) => {
      const title = item.kind === 'questions' ? `- [${item.resolved ? 'x' : ' '}] ${item.title}` : `${i + 1}. **${item.title}**`;
      return title + (item.description ? `\n   ${item.description}` : '')
        + (item.role ? `\n   Роль: ${item.role}` : '')
        + (item.needs_clarification ? '\n   Требуется уточнение' : '')
        + (item.source !== null ? `\n   Источник: ${time(item.source)}` : '');
    }).join('\n\n') || 'Не указаны.'))].join('\n\n');
}

export function parseMeeting(value: unknown): Meeting {
  const fail = (): never => { throw new Error('Сервер вернул данные в неподдерживаемом формате.'); };
  if (!value || typeof value !== 'object') return fail();
  const m = value as Record<string, unknown>;
  const string = (v: unknown): v is string => typeof v === 'string';
  const number = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v >= 0;
  const source = (v: unknown) => v === null || number(v);
  if (!['id','title','date','filename'].every(k => string(m[k])) || !number(m.duration) || !['processing','ready','failed'].includes(m.status as string)) return fail();
  if (m.audio_url !== undefined && (!string(m.audio_url) || !/^(https?:\/\/|\/[^/])/.test(m.audio_url))) return fail();
  if (!Array.isArray(m.transcript) || !m.transcript.every(s => s && string(s.id) && string(s.speaker) && string(s.text) && number(s.start))) return fail();
  const requirements = m.requirements ?? [], questions = m.questions ?? [], constraints = m.constraints ?? [];
  if (!Array.isArray(requirements) || !requirements.every(r => r && string(r.id) && string(r.title) && string(r.description) && ['functional','nonfunctional'].includes(r.category) && source(r.source) && (r.role === undefined || string(r.role)) && (r.needs_clarification === undefined || typeof r.needs_clarification === 'boolean'))) return fail();
  if (!Array.isArray(questions) || !questions.every(q => q && string(q.id) && string(q.text) && source(q.source) && typeof q.resolved === 'boolean' && (q.needs_clarification === undefined || typeof q.needs_clarification === 'boolean') && (q.constraint_index === undefined || (Number.isInteger(q.constraint_index) && q.constraint_index >= 0)))) return fail();
  if (!Array.isArray(constraints) || !constraints.every(string)) return fail();
  if (questions.some(q => q.constraint_index !== undefined && q.constraint_index >= constraints.length)) return fail();
  if (new Set(questions.filter(q => q.constraint_index !== undefined).map(q => q.constraint_index)).size !== questions.filter(q => q.constraint_index !== undefined).length) return fail();
  for (const rows of [m.transcript, requirements, questions]) if (new Set(rows.map(r => r.id)).size !== rows.length) return fail();
  if (m.analysis !== undefined) {
    if (!Array.isArray(m.analysis) || !m.analysis.every(item => item && string(item.id) && item.id.length > 0
      && SUPPORTED_ANALYSIS_SECTIONS.some(section => section.key === item.kind)
      && string(item.title) && item.title.trim().length > 0 && string(item.description) && source(item.source)
      && (item.role === undefined || string(item.role))
      && (item.needs_clarification === undefined || typeof item.needs_clarification === 'boolean')
      && (item.resolved === undefined || typeof item.resolved === 'boolean'))) return fail();
    if (new Set(m.analysis.map(item => item.id)).size !== m.analysis.length) return fail();
  }
  return { ...m, requirements, questions, constraints } as Meeting;
}
