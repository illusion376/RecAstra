'use client';

import { useEffect, useRef, useState } from 'react';
import { Icon } from '@/components/icon';
import { RequirementsBoard } from '@/components/requirements-board';
import { DEMO_MODE, getMeeting, listMeetings, uploadMeeting } from '@/lib/api';
import { demoMeeting } from '@/lib/demo';
import { Meeting, analysisItems, specification, time } from '@/lib/meeting';

type View = 'workspace' | 'projects' | 'meetings' | 'documents';
export default function Home() {
  const [meetings, setMeetings] = useState<Meeting[]>(DEMO_MODE ? [demoMeeting] : []);
  const [selected, setSelected] = useState(DEMO_MODE ? 'demo' : '');
  const [view, setView] = useState<View>('workspace');
  const [query, setQuery] = useState('');
  const [speaker, setSpeaker] = useState('all');
  const [globalQuery, setGlobalQuery] = useState('');
  const [activeSource, setActiveSource] = useState<number | null>(754);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(!DEMO_MODE);
  const [uploading, setUploading] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [uploadError, setUploadError] = useState('');
  const [dirty, setDirty] = useState(false);
  const [audioTime, setAudioTime] = useState(0);
  const [audioFailed, setAudioFailed] = useState(false);
  const audio = useRef<HTMLAudioElement>(null);
  const uploadDialog = useRef<HTMLDialogElement>(null);
  const requestController = useRef<AbortController | null>(null);
  const initialController = useRef<AbortController | null>(null);
  const meeting = meetings.find(m => m.id === selected);

  async function refresh(signal?: AbortSignal) {
    setLoading(true); setError('');
    try {
      const rows = await listMeetings(signal);
      if (signal?.aborted) return;
      setMeetings(rows); setSelected(current => rows.some(m => m.id === current) ? current : rows[0]?.id ?? '');
    } catch (e) { if (!signal?.aborted) setError(e instanceof Error ? e.message : 'Не удалось загрузить встречи.'); }
    finally { if (!signal?.aborted) setLoading(false); }
  }
  useEffect(() => {
    if (DEMO_MODE) return;
    const controller = new AbortController(); initialController.current = controller;
    void refresh(controller.signal);
    return () => { controller.abort(); requestController.current?.abort(); };
  }, []);
  useEffect(() => {
    if (!meeting || meeting.status !== 'processing' || DEMO_MODE) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await getMeeting(meeting.id, controller.signal);
        if (controller.signal.aborted) return;
        setMeetings(rows => rows.map(m => m.id === next.id ? next : m));
        if (next.status === 'processing') timer = setTimeout(poll, 3000);
      } catch (e) {
        if (!controller.signal.aborted) setError(e instanceof Error ? e.message : 'Не удалось обновить встречу.');
      }
    };
    timer = setTimeout(poll, 3000);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [meeting?.id, meeting?.status, meeting]);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(''), 5000);
    return () => clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  function updateMeeting(change: (m: Meeting) => Meeting) {
    setMeetings(rows => rows.map(m => m.id === selected ? change(m) : m)); setDirty(true);
  }
  function openMeeting(m: Meeting) {
    setSelected(m.id); setView('workspace'); setQuery(''); setSpeaker('all'); setActiveSource(null); setAudioTime(0); setAudioFailed(false);
  }
  function jump(seconds: number) {
    const nearest = meeting?.transcript.reduce<number | null>((best, s) => best === null || Math.abs(s.start - seconds) < Math.abs(best - seconds) ? s.start : best, null);
    const target = nearest ?? seconds;
    setQuery(''); setSpeaker('all'); setActiveSource(target);
    if (audio.current && Number.isFinite(audio.current.duration)) audio.current.currentTime = Math.min(seconds, audio.current.duration);
    setTimeout(() => document.getElementById(`source-${target}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 0);
  }
  function download() {
    if (!meeting) return;
    const blob = new Blob([specification(meeting)], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob); const link = document.createElement('a');
    link.href = url; link.download = `ТЗ_${meeting.title.replace(/[^\p{L}\p{N}_-]/gu, '_')}.md`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000); setNotice('Техническое задание скачано в Markdown.');
  }
  function chooseFile(next: File | null) {
    setUploadError(''); setFile(null);
    if (!next) return;
    if (!/\.(mp3|wav|m4a|ogg|webm|flac|mp4)$/i.test(next.name)) { setUploadError('Выберите MP3, WAV, M4A, OGG, WebM, FLAC или MP4.'); return; }
    if (!next.size || next.size > 200 * 1024 * 1024) { setUploadError('Выберите непустой файл размером до 200 МБ.'); return; }
    setFile(next);
  }
  async function upload(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!file || uploading) return;
    if (DEMO_MODE) { setUploadError('Загрузка доступна после подключения сервера. Сейчас открыт демонстрационный проект.'); return; }
    const title = String(new FormData(event.currentTarget).get('title') ?? '').trim();
    if (!title) { setUploadError('Укажите название проекта.'); return; }
    setUploading(true); setUploadError(''); const controller = new AbortController(); requestController.current = controller;
    // Cancel the initial list request so a late response cannot replace the new upload.
    initialController.current?.abort(); setLoading(false);
    try {
      const result = await uploadMeeting(file, title, controller.signal);
      setMeetings(rows => [result, ...rows.filter(m => m.id !== result.id)]); openMeeting(result);
      uploadDialog.current?.close(); setFile(null); setNotice(result.status === 'processing' ? 'Запись загружена. Ожидаем результат обработки.' : 'Встреча загружена.');
    } catch (e) { setUploadError(e instanceof Error ? e.message : 'Не удалось загрузить запись.'); }
    finally { setUploading(false); }
  }
  const speakers = [...new Set(meeting?.transcript.map(s => s.speaker) ?? [])];
  const segments = meeting?.transcript.filter(s => (speaker === 'all' || speaker === s.speaker) && s.text.toLocaleLowerCase('ru').includes(query.toLocaleLowerCase('ru'))) ?? [];
  const filteredMeetings = meetings.filter(m => m.title.toLocaleLowerCase('ru').includes(globalQuery.toLocaleLowerCase('ru')));

  return <div className="app-shell">
    <aside className="sidebar">
      <a href="/" className="brand" aria-label="RecAstra — главная"><span className="brand-mark">R<span>✦</span></span>RecAstra</a>
      <div className="nav-caption">РАБОЧЕЕ ПРОСТРАНСТВО</div>
      <nav aria-label="Основная навигация">{([
        ['projects','folder','Проекты'], ['meetings','calendar','Все встречи'], ['documents','file','Документы'],
      ] as const).map(([id, icon, label]) => <button key={id} className={(view === id || (id === 'projects' && view === 'workspace')) ? 'nav-item selected' : 'nav-item'} onClick={() => { setView(id); setGlobalQuery(''); }}><Icon name={icon}/><span>{label}</span>{id === 'projects' && <span className="nav-count">{meetings.length}</span>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="workflow-note"><span className="note-icon"><Icon name="file"/></span><b>От разговора к требованиям</b><p>Все важные детали встречи — в одном месте.</p></div><div className="workspace-person"><span className="avatar">R</span><div><b>Моё пространство</b><small>{DEMO_MODE ? 'Демонстрационный проект' : 'Рабочие встречи'}</small></div></div></div>
    </aside>
    <div className="main-shell">
      <header className="topbar"><div className="breadcrumbs"><button onClick={() => setView('projects')}>Проекты</button><Icon name="chevron" size={14}/><span>{view === 'workspace' ? 'Встреча с заказчиком' : view === 'documents' ? 'Документы' : view === 'meetings' ? 'Все встречи' : 'Все проекты'}</span></div><div className="topbar-right"><span className={`mode-badge ${DEMO_MODE ? '' : 'live'}`}>{DEMO_MODE ? 'Деморежим' : 'FastAPI'}</span><span className="avatar small">R</span></div></header>
      <main>
        {error && <div className="error-banner" role="alert"><span>{error}</span><button onClick={() => { if (!dirty || window.confirm('Обновление заменит правки данными сервера. Сначала экспортируйте ТЗ, если хотите сохранить изменения. Продолжить?')) { setDirty(false); void refresh(); } }}>Повторить</button></div>}
        {loading ? <div className="empty-state" role="status"><span className="spinner"/><h1>Загружаем встречи</h1><p>Получаем данные с сервера…</p></div> : view !== 'workspace' ? <>
          <div className="page-heading"><div><div className="eyebrow">ВАШЕ РАБОЧЕЕ ПРОСТРАНСТВО</div><h1>{view === 'projects' ? 'Проекты' : view === 'meetings' ? 'Все встречи' : 'Документы'}</h1><p>{view === 'documents' ? 'Требования и технические задания по вашим встречам.' : 'Разговоры, из которых рождаются продукты.'}</p></div><button className="button primary" onClick={() => uploadDialog.current?.showModal()}><Icon name="plus"/>Новая встреча</button></div>
          <label className="search list-search"><Icon name="search" size={18}/><input aria-label="Поиск проектов" placeholder="Найти проект…" value={globalQuery} onChange={e => setGlobalQuery(e.target.value)}/></label>
          <div className="project-grid">{filteredMeetings.map(m => <button className="project-card" key={m.id} onClick={() => { openMeeting(m); }}><span className="tile-icon"><Icon name={view === 'documents' ? 'file' : 'folder'} size={25}/></span><span className="card-state">{m.status === 'ready' ? 'Готово к работе' : m.status === 'processing' ? 'Обрабатывается' : 'Ошибка обработки'}</span><h2>{m.title}</h2><p>{m.filename}</p><div className="project-meta"><span><Icon name="clock" size={16}/>{time(m.duration)}</span><span>{analysisItems(m).length} пунктов</span><Icon name="chevron" size={16}/></div></button>)}</div>
          {!filteredMeetings.length && <div className="empty-state"><Icon name="folder" size={36}/><h2>{globalQuery ? 'Ничего не найдено' : 'Пока нет встреч'}</h2><p>{globalQuery ? 'Попробуйте другое название.' : 'Загрузите первую запись разговора с заказчиком.'}</p></div>}
        </> : !meeting ? <div className="empty-state"><Icon name="upload" size={40}/><h1>Начните с новой встречи</h1><p>Загрузите запись, чтобы получить расшифровку с сервера.</p><button className="button primary" onClick={() => uploadDialog.current?.showModal()}>Загрузить запись</button></div> : <>
          <div className="page-heading"><div><div className="eyebrow"><span className="project-dot"/>ПРОЕКТ · {DEMO_MODE ? 'ПРИМЕР' : 'ВСТРЕЧА'}</div><h1>{meeting.title}</h1><p className="meeting-meta">Встреча с заказчиком<span>·</span>{Math.ceil(meeting.duration / 60)} мин<span>·</span>{Number.isNaN(Date.parse(meeting.date)) ? meeting.date : new Date(meeting.date).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'Europe/Moscow' })}</p></div><div className="heading-actions"><button className="button secondary" onClick={() => uploadDialog.current?.showModal()}><Icon name="upload" size={18}/>Загрузить запись</button><button className="button primary" onClick={download} disabled={meeting.status !== 'ready'}><Icon name="download" size={18}/>Экспорт ТЗ</button></div></div>
          {meeting.status === 'processing' && <div className="info-banner" role="status"><span className="spinner"/>Сервер обрабатывает запись. Результат появится автоматически.</div>}
          {meeting.status === 'failed' && <div className="error-banner" role="alert">Сервер не смог обработать запись. Попробуйте загрузить её снова.</div>}
          <div className="workspace-grid"><section className="panel recording"><div className="panel-heading"><h2>Запись встречи</h2><span className="label-muted">АУДИО</span></div><div className="audio-file"><span className="file-icon"><Icon name="file" size={24}/></span><div><b>{meeting.filename || 'Запись встречи'}</b><small>{time(meeting.duration)}{DEMO_MODE ? ' · Демонстрационная запись' : ''}</small></div></div>
            {meeting.audio_url ? <><audio key={meeting.audio_url} ref={audio} controls preload="metadata" src={meeting.audio_url} onTimeUpdate={e => setAudioTime(e.currentTarget.currentTime)} onError={() => setAudioFailed(true)} aria-label="Аудиозапись встречи"/>{audioFailed && <p className="field-error" role="alert">Не удалось воспроизвести запись. Проверьте доступность аудиофайла на сервере.</p>}</> : <div className="audio-placeholder"><span className="muted-play"><Icon name="play"/></span><div className="waveform" aria-hidden="true">{Array.from({length: 62}, (_, i) => <i key={i} style={{height: `${7 + ((i * 19 + i * i * 7) % 33)}px`}}/>)}</div><span className="audio-note">Аудио недоступно{DEMO_MODE ? ' в деморежиме' : ''}</span></div>}
            <div className="transcript-heading"><h2>Расшифровка</h2><span className="count-badge">{meeting.transcript.length} реплик</span></div><div className="transcript-filters"><label className="search"><Icon name="search" size={17}/><input placeholder="Поиск по расшифровке…" aria-label="Поиск по расшифровке" value={query} onChange={e => setQuery(e.target.value)}/>{query && <button aria-label="Очистить поиск" onClick={() => setQuery('')}><Icon name="close" size={14}/></button>}</label><select aria-label="Фильтр по участнику" value={speaker} onChange={e => setSpeaker(e.target.value)}><option value="all">Все спикеры</option>{speakers.map(s => <option key={s}>{s}</option>)}</select></div>
            <div className="transcript-list">{segments.map(s => <button key={s.id} id={`source-${s.start}`} className={`transcript-row ${activeSource === s.start ? 'active' : ''}`} onClick={() => jump(s.start)}><span className={`speaker-avatar ${speakers.indexOf(s.speaker) % 2 ? 'mint' : ''}`}>{s.speaker.slice(0, 1)}</span><span className="utterance"><span className="utterance-meta"><b>{s.speaker}</b><span>·</span><time>{time(s.start)}</time></span><span className="utterance-text">{s.text}</span></span></button>)}{!segments.length && <div className="inline-empty">{meeting.transcript.length ? 'Реплики не найдены. Измените поиск или фильтр.' : meeting.status === 'processing' ? 'Ожидаем расшифровку…' : 'Сервер ещё не передал расшифровку.'}</div>}</div>
            <div className="transcript-footer"><Icon name="clock" size={14}/>{meeting.audio_url ? `Позиция воспроизведения: ${time(audioTime)}` : 'Нажмите на источник, чтобы найти реплику'}</div>
          </section>
          <div className="right-column"><section className="panel requirements-panel"><header className="analysis-panel-title"><Icon name="list" size={19}/><h2>Анализ разговора</h2></header>
            <RequirementsBoard key={meeting.id} meeting={meeting} onUpdate={updateMeeting} onSource={jump}/>

          </section>
          </div></div><footer className="page-footer"><span>RecAstra · Рабочее пространство требований</span><span>{dirty ? 'Правки в текущей сессии · сохраните через экспорт ТЗ' : 'Проверьте результат перед согласованием'}</span></footer>
        </>}
      </main>
    </div>
    <dialog ref={uploadDialog} className="modal" onCancel={e => { if (uploading) e.preventDefault(); }}><form onSubmit={upload}><div className="modal-heading"><div><span className="eyebrow">НОВАЯ ВСТРЕЧА</span><h2>Загрузить запись</h2></div><button type="button" className="icon-button" disabled={uploading} aria-label="Закрыть окно" onClick={() => uploadDialog.current?.close()}><Icon name="close"/></button></div><p className="modal-description">Добавьте разговор с заказчиком в рабочее пространство.</p><label className="form-field">Название проекта<input name="title" placeholder="Например, сервис онлайн-записи" required maxLength={150} disabled={uploading}/></label><label className={`dropzone ${file ? 'has-file' : ''}`} onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); if (!uploading) chooseFile(e.dataTransfer.files[0]); }}><Icon name={file ? 'check' : 'upload'} size={30}/><b>{file?.name ?? 'Выберите файл или перетащите сюда'}</b><span>{file ? `${(file.size / 1024 / 1024).toFixed(1)} МБ · нажмите, чтобы заменить` : 'MP3, WAV, M4A, OGG, WebM, FLAC, MP4 · до 200 МБ'}</span><input type="file" aria-label="Аудио или видео встречи" accept=".mp3,.wav,.m4a,.ogg,.webm,.flac,.mp4" disabled={uploading} onChange={e => chooseFile(e.target.files?.[0] ?? null)}/></label>{DEMO_MODE && <div className="info-banner">Сейчас открыт пример. Загрузка станет доступна после подключения сервера.</div>}{uploadError && <p className="field-error" role="alert">{uploadError}</p>}<div className="modal-actions"><button type="button" className="button secondary" disabled={uploading} onClick={() => uploadDialog.current?.close()}>Отмена</button><button className="button primary" disabled={!file || uploading || DEMO_MODE}>{uploading ? <><span className="spinner"/>Загружаем…</> : <><Icon name="upload" size={17}/>Загрузить запись</>}</button></div></form></dialog>
    {notice && <div className="toast" role="status"><Icon name="check"/>{notice}<button aria-label="Закрыть уведомление" onClick={() => setNotice('')}><Icon name="close" size={16}/></button></div>}
  </div>;
}
