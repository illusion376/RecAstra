'use client';

import { useState } from 'react';
import { Icon } from '@/components/icon';
import { login, register } from '@/lib/api';
import { saveSession, logoutMessage } from '@/lib/session';

type Mode = 'login' | 'register';
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export function AuthScreen() {
  const [mode, setMode] = useState<Mode>('login');
  const [error, setError] = useState(logoutMessage);
  const [busy, setBusy] = useState(false);

  function switchMode(next: Mode) { setMode(next); setError(''); }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (busy) return;
    const form = new FormData(event.currentTarget);
    const name = String(form.get('name') ?? '').trim();
    const email = String(form.get('email') ?? '').trim();
    const password = String(form.get('password') ?? '');
    const repeat = String(form.get('repeat') ?? '');

    if (mode === 'register' && !name) return setError('Укажите имя.');
    if (!EMAIL.test(email)) return setError('Введите корректный адрес почты.');
    if (mode === 'register' && password.length < 6) return setError('Пароль должен быть не короче 6 символов.');
    if (mode === 'register' && password !== repeat) return setError('Пароли не совпадают.');
    if (!password) return setError('Введите пароль.');

    setBusy(true); setError('');
    try {
      saveSession(mode === 'login' ? await login(email, password) : await register(name, email, password));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось выполнить вход.');
      setBusy(false);
    }
  }

  const registering = mode === 'register';
  return <div className="auth-shell">
    <div className="auth-card">
      <div className="brand auth-brand"><span className="brand-mark">R<span>✦</span></span>RecAstra</div>
      <div className="auth-switch" role="group" aria-label="Вход или регистрация">
        <button type="button" aria-pressed={!registering} className={registering ? '' : 'active'} onClick={() => switchMode('login')}>Вход</button>
        <button type="button" aria-pressed={registering} className={registering ? 'active' : ''} onClick={() => switchMode('register')}>Регистрация</button>
      </div>
      <h1>{registering ? 'Создайте аккаунт' : 'С возвращением'}</h1>
      <p className="auth-lead">{registering ? 'Чтобы загружать встречи и собирать из них ТЗ.' : 'Войдите, чтобы открыть свои проекты и документы.'}</p>
      {/* key: при смене режима поля и автозаполнение сбрасываются */}
      <form key={mode} onSubmit={submit} noValidate>
        {registering && <label className="form-field">Имя<input name="name" autoComplete="name" placeholder="Анна" maxLength={100} disabled={busy} autoFocus/></label>}
        <label className="form-field">Почта<input name="email" type="email" autoComplete="email" placeholder="you@example.com" maxLength={254} disabled={busy} autoFocus={!registering}/></label>
        <label className="form-field">Пароль<input name="password" type="password" autoComplete={registering ? 'new-password' : 'current-password'} placeholder={registering ? 'Не короче 6 символов' : ''} maxLength={128} disabled={busy}/></label>
        {registering && <label className="form-field">Повторите пароль<input name="repeat" type="password" autoComplete="new-password" maxLength={128} disabled={busy}/></label>}
        {error && <p className="field-error" role="alert">{error}</p>}
        <button className="button primary auth-submit" disabled={busy}>{busy ? <><span className="spinner"/>{registering ? 'Создаём аккаунт…' : 'Входим…'}</> : <><Icon name={registering ? 'plus' : 'check'} size={17}/>{registering ? 'Зарегистрироваться' : 'Войти'}</>}</button>
      </form>
      <p className="auth-footer">{registering ? 'Уже есть аккаунт?' : 'Ещё нет аккаунта?'} <button type="button" className="text-link" onClick={() => switchMode(registering ? 'login' : 'register')}>{registering ? 'Войти' : 'Зарегистрироваться'}</button></p>
    </div>
  </div>;
}
