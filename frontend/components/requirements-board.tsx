'use client';

import { useRef, useState } from 'react';
import { Icon } from './icon';
import { newId } from '@/lib/export';
import { ANALYSIS_SECTIONS, confidenceLabel, PRIORITY_LABELS, sortByPriority, analysisItems, time, participantLabel } from '@/lib/meeting';
import type { AnalysisItem, AnalysisKind, Meeting, Priority } from '@/lib/meeting';

type Props = {
  meeting: Meeting;
  onUpdate: (change: (meeting: Meeting) => Meeting) => void;
  onSource: (seconds: number) => void;
};

export function RequirementsBoard({ meeting, onUpdate, onSource }: Props) {
  const editor = useRef<HTMLDialogElement>(null);
  const [editing, setEditing] = useState<AnalysisItem | null>(null);
  const [kind, setKind] = useState<AnalysisKind>('functional');
  const [collapsed, setCollapsed] = useState<Partial<Record<AnalysisKind, boolean>>>(() => Object.fromEntries(ANALYSIS_SECTIONS.map(section => [section.key, true])));
  const [version, setVersion] = useState(0);
  const [formError, setFormError] = useState('');
  const [deleted, setDeleted] = useState<{item: AnalysisItem; index: number} | null>(null);
  const items = analysisItems(meeting);
  const editable = meeting.status === 'ready';

  function update(change: (items: AnalysisItem[]) => AnalysisItem[]) {
    onUpdate(m => ({ ...m, analysis: change(analysisItems(m)) }));
  }
  function edit(section: AnalysisKind, item?: AnalysisItem) {
    setKind(section); setEditing(item ?? null); setVersion(v => v + 1); setFormError('');
    editor.current?.showModal();
  }
  function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const title = String(data.get('title') ?? '').trim();
    if (!title) { setFormError('Укажите название или формулировку пункта.'); return; }
    const item: AnalysisItem = {
      ...editing, id: editing?.id ?? newId(), kind, title,
      description: String(data.get('description') ?? '').trim(),
      role: String(data.get('role') ?? '').trim(), source: editing?.source ?? null,
      confidence: editing && title === editing.title && String(data.get('description') ?? '').trim() === editing.description
        && String(data.get('role') ?? '').trim() === (editing.role ?? '') ? editing.confidence : null,
      priority: (String(data.get('priority') ?? '') || null) as Priority | null,
      needs_clarification: data.get('clarification') === 'on',
    };
    update(rows => editing ? rows.map(row => row.id === item.id ? item : row) : [...rows, item]);
    setCollapsed(current => ({ ...current, [kind]: false }));
    editor.current?.close();
  }
  function remove(item: AnalysisItem) {
    setDeleted({ item, index: items.findIndex(row => row.id === item.id) });
    update(rows => rows.filter(row => row.id !== item.id));
  }
  function undo() {
    if (!deleted) return;
    const { item, index } = deleted;
    update(rows => rows.some(row => row.id === item.id) ? rows : [...rows.slice(0, index), item, ...rows.slice(index)]);
    setCollapsed(current => ({ ...current, [item.kind]: false }));
    setDeleted(null);
  }

  return <div className="analysis-board">
    {deleted && <div className="analysis-undo" role="status"><span>Удалено: {deleted.item.title}</span><button disabled={!editable} onClick={undo}>Отменить удаление</button></div>}
    <div className="analysis-containers">
      {ANALYSIS_SECTIONS.map((section) => {
        const rows = sortByPriority(items.filter(item => item.kind === section.key));
        return <section className="analysis-container" key={section.key} aria-labelledby={`analysis-${section.key}`}>
          <header className={`analysis-container-header ${collapsed[section.key] ? 'is-collapsed' : ''}`}>
            <h3 id={`analysis-${section.key}`} className="analysis-collapse-heading">
              <button type="button" className="analysis-collapse-toggle"
                aria-expanded={!collapsed[section.key]} aria-controls={`analysis-body-${section.key}`}
                onClick={() => setCollapsed(current => ({ ...current, [section.key]: !current[section.key] }))}>
                <span className="analysis-section-icon"><Icon name={section.icon} size={19}/></span>
                <span>{section.title}</span>
                <span className="analysis-item-count" aria-label={`Элементов: ${rows.length}`}>{rows.length}</span>
                <Icon name="chevron" size={17} style={{ transform: collapsed[section.key] ? 'rotate(0deg)' : 'rotate(90deg)' }}/>
              </button>
            </h3>
            <button className="analysis-add" disabled={!editable} onClick={() => edit(section.key)} aria-label={`Добавить: ${section.title}`}><Icon name="plus" size={16}/><span>Добавить</span></button>
          </header>
          <div className="analysis-container-body" id={`analysis-body-${section.key}`} hidden={!!collapsed[section.key]}>
            {!rows.length && <div className="analysis-empty"><p>Пока нет записей</p><button disabled={!editable} onClick={() => edit(section.key)}>Добавить первый пункт</button></div>}
            {rows.map(item => <article key={item.id} className={`analysis-card ${item.needs_clarification ? 'needs-clarification' : ''} ${item.resolved ? 'is-resolved' : ''}`}>
              <div className="analysis-card-header"><h4>{item.title}</h4><div className="analysis-card-actions">
                <button disabled={!editable} title="Редактировать" aria-label={`Редактировать: ${item.title}`} onClick={() => edit(section.key, item)}><Icon name="edit" size={16}/></button>
                <button disabled={!editable} className="delete-card" title="Удалить" aria-label={`Удалить: ${item.title}`} onClick={() => remove(item)}><Icon name="trash" size={16}/></button>
              </div></div>
              <span className={`priority-badge priority-${item.priority ?? 'unknown'}`}>Приоритет: {item.priority ? PRIORITY_LABELS[item.priority] : 'Не определён'}</span>
              <span className={`confidence-badge ${item.confidence == null ? 'unknown' : item.confidence < 0.6 ? 'low' : item.confidence < 0.85 ? 'medium' : 'high'}`}
                title="Оценка анализатором того, насколько пункт следует из разговора. Это не гарантия правильности. После ручного изменения формулировки оценка не применяется.">
                Уверенность: {confidenceLabel(item.confidence)}
              </span>
              {item.role && <span className="analysis-role">{participantLabel(item.role)}</span>}
              {item.description && <p className="analysis-description">{item.description}</p>}
              <div className="analysis-card-footer">
                {item.source !== null && <button className="board-source" onClick={() => onSource(item.source!)}><Icon name="clock" size={14}/>{time(item.source)}</button>}
                <button className={`analysis-clarify ${item.needs_clarification ? 'active' : ''}`} disabled={!editable}
                  aria-pressed={!!item.needs_clarification} aria-label={`${item.needs_clarification ? 'Снять отметку уточнения' : 'Требует уточнения'}: ${item.title}`}
                  onClick={() => update(rows => rows.map(row => row.id === item.id ? {...row, needs_clarification: !row.needs_clarification} : row))}>
                  <Icon name={item.needs_clarification ? 'check' : 'question'} size={16}/>{item.needs_clarification ? 'Требует уточнения' : 'Уточнить'}
                </button>
              </div>
              {section.key === 'questions' && <label className="question-resolve"><input type="checkbox" checked={!!item.resolved} disabled={!editable}
                onChange={() => update(rows => rows.map(row => row.id === item.id ? {...row, resolved: !row.resolved} : row))}/>{item.resolved ? 'Вопрос решён' : 'Отметить решённым'}</label>}
            </article>)}
          </div>
        </section>;
      })}
    </div>
    <dialog ref={editor} className="modal"><form key={version} onSubmit={save}>
      <div className="modal-heading"><div><span className="eyebrow">{ANALYSIS_SECTIONS.find(section => section.key === kind)?.title}</span><h2>{editing ? 'Редактировать пункт' : 'Новый пункт'}</h2></div><button type="button" className="icon-button" aria-label="Закрыть окно" onClick={() => editor.current?.close()}><Icon name="close"/></button></div>
      <label className="form-field">Название или формулировка<input autoFocus name="title" required maxLength={1000} defaultValue={editing?.title}/></label>
      <label className="form-field">Описание<textarea name="description" rows={4} maxLength={10000} defaultValue={editing?.description} placeholder="Подробности, условия или последовательность действий"/></label>
      <label className="form-field">Роль или участник <span className="optional-label">необязательно</span><input name="role" maxLength={100} defaultValue={editing?.role} placeholder="Менеджер или заказчик"/></label>
      <label className="form-field">Приоритет<select name="priority" defaultValue={editing?.priority ?? ''}><option value="">Не определён</option>{Object.entries(PRIORITY_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label className="question-resolve"><input type="checkbox" name="clarification" defaultChecked={editing?.needs_clarification}/>Требует уточнения</label>
      {formError && <p className="field-error" role="alert">{formError}</p>}
      <p className="form-hint">После редактирования нажмите «Сохранить» в проекте. Правки также включаются в экспорт ТЗ.</p>
      <div className="modal-actions"><button type="button" className="button secondary" onClick={() => editor.current?.close()}>Отмена</button><button className="button primary">Сохранить</button></div>
    </form></dialog>
  </div>;
}
