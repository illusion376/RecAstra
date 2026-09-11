import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseMeeting, specification, time, analysisItems, ANALYSIS_SECTIONS, sortByPriority } from '../lib/meeting.ts';
const base = {id:'1',title:'Проект',date:'2026-09-10',filename:'a.mp3',duration:30,status:'ready',transcript:[{id:'s1',speaker:'Заказчик',start:12,text:'Текст'}]};
test('transcript-only responses do not invent requirements', () => {
  const meeting = parseMeeting(base);
  assert.deepEqual(meeting.requirements, []);
  assert.deepEqual(meeting.questions, []);
  assert.match(specification(meeting), /Не указаны/);
});
test('malformed external data is rejected', () => {
  for (const patch of [{duration:-1},{transcript:[{id:'s',speaker:'A',text:'B',start:'12'}]},{status:'unknown'},{audio_url:'javascript:alert(1)'},{requirements:[{id:'r',title:'A',description:'B',category:'functional',source:-1}]},{transcript:[base.transcript[0],base.transcript[0]]}]) {
    assert.throws(() => parseMeeting({...base,...patch}), /неподдерживаемом формате/);
  }
});
test('export includes every category, source, question state and constraint', () => {
  const m = parseMeeting({...base,requirements:[{id:'r1',title:'Выбор даты',description:'Свободное время',category:'functional',source:754},{id:'r2',title:'Мобильная версия',description:'Экран смартфона',category:'nonfunctional',source:null}],questions:[{id:'q1',text:'Срок отмены?',source:12,resolved:true}],constraints:['Без оплаты']});
  const md = specification(m);
  for (const value of ['Выбор даты','12:34','Мобильная версия','[x] Срок отмены?','Без оплаты','черновик']) assert.ok(md.includes(value));
  assert.equal(time(2537), '42:17');
});
test('roles and clarification flags survive validation and export', () => {
  const data = {...base, requirements:[{id:'r1',title:'Запись',description:'Выбор даты',category:'functional',source:12,role:'Пользователь',needs_clarification:true}], questions:[{id:'q1',text:'Уточнить срок',source:null,resolved:false,needs_clarification:true,constraint_index:0}], constraints:['Три месяца']};
  const parsed = parseMeeting(data);
  assert.match(specification(parsed), /Роль: Пользователь/);
  assert.match(specification(parsed), /Требуется уточнение/);
  assert.equal(parsed.questions[0].constraint_index, 0);
  for (const patch of [{needs_clarification:'yes'},{constraint_index:-1},{constraint_index:0.5},{constraint_index:1}]) {
    assert.throws(() => parseMeeting({...data,questions:[{...data.questions[0],...patch}]}));
  }
  assert.throws(() => parseMeeting({...data,requirements:[{...data.requirements[0],role:123}]}));
  assert.throws(() => parseMeeting({...data,requirements:[{...data.requirements[0],needs_clarification:'true'}]}));
});

test('all visible sections are exported and an empty edited analysis stays empty', () => {
  const legacy = parseMeeting({...base, requirements:[{id:'r',title:'Старое требование',description:'Описание',category:'functional',source:null}], constraints:['Старое ограничение']});
  assert.equal(analysisItems(legacy).length, 2);
  const deleted = parseMeeting({...legacy, analysis:[]});
  assert.deepEqual(analysisItems(deleted), []);
  assert.ok(!specification(deleted).includes('Старое требование'));
  assert.ok(!specification(deleted).includes('Старое ограничение'));
  const analysis = ANALYSIS_SECTIONS.map((section, i) => ({id:String(i),kind:section.key,title:`Пункт ${i}`,description:'Изменённое описание',source:12,needs_clarification:true}));
  const edited = parseMeeting({...legacy,analysis});
  for (const section of ANALYSIS_SECTIONS) assert.ok(specification(edited).includes(`## ${section.title}`));
  assert.equal(analysisItems(edited).length, ANALYSIS_SECTIONS.length);
  assert.ok(!specification(edited).includes('Старое требование'));
});
test('invalid analysis items and duplicate identities are rejected', () => {
  const item = {id:'1',kind:'roles',title:'Клиент',description:'',source:null};
  for (const patch of [{kind:'unknown'},{title:' '},{id:''},{source:-1},{needs_clarification:'yes'},{resolved:1}]) {
    assert.throws(() => parseMeeting({...base,analysis:[{...item,...patch}]}));
  }
  assert.throws(() => parseMeeting({...base,analysis:[item,item]}));
});

test('priority sorting is stable, non-mutating and reflected in export', () => {
  const priorities = ['could', null, 'must', 'should', 'must', 'wont'];
  const rows = priorities.map((priority, i) => ({id: String(i), kind:'functional', title:`Пункт ${i}`, description:'', source:null, priority}));
  assert.deepEqual(sortByPriority(rows).map(r => r.id), ['2','4','3','0','5','1']);
  assert.equal(rows[0].id, '0');
  const md = specification(parseMeeting({...base, analysis: rows}));
  assert.ok(md.indexOf('Пункт 2') < md.indexOf('Пункт 0'));
  assert.match(md, /Приоритет: Высокий/);
  assert.throws(() => parseMeeting({...base, analysis:[{...rows[0],priority:'invalid'}]}));
});

test('confidence validates bounds and survives export including zero', () => {
  const item = {id:'c',kind:'functional',title:'Форма',description:'',source:null};
  for (const confidence of [0, 0.82, 1, null, undefined]) {
    const m = parseMeeting({...base, analysis:[{...item, confidence}]});
    assert.ok(specification(m).includes(confidence == null ? 'Не оценена' : `${Math.round(confidence * 100)}%`));
  }
  for (const confidence of [-0.1, 1.1, NaN, Infinity, '0.8']) {
    assert.throws(() => parseMeeting({...base, analysis:[{...item, confidence}]}));
  }
});
