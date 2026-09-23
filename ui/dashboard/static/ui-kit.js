// ui-kit.js — shared presentation helpers for Mission Control.
//
// Every helper that renders text escapes it. Agent output, memory content,
// task instructions and account names are untrusted: never interpolate them
// into innerHTML without going through ui.esc().
(function () {
  'use strict';

  function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function target(el) {
    return typeof el === 'string' ? document.getElementById(el) : el;
  }

  // Status vocabulary used across agents, tasks, jobs and accounts, mapped to
  // one of five tones so the same word always looks the same everywhere.
  const TONES = {
    ok: ['online', 'ready', 'healthy', 'completed', 'complete', 'success', 'succeeded', 'done', 'active', 'enabled', 'passed', 'approved', 'applied'],
    busy: ['working', 'running', 'in_progress', 'in-progress', 'executing', 'dispatched', 'authenticating', 'pending_approval', 'queued', 'pending', 'planned'],
    warn: ['degraded', 'blocked', 'rate_limited', 'quota', 'stale', 'warning', 'escalated', 'discovered', 'paused'],
    bad: ['failed', 'error', 'offline', 'unhealthy', 'rejected', 'cancelled', 'canceled', 'disabled', 'expired'],
  };
  const TONE_OF = {};
  Object.entries(TONES).forEach(([tone, words]) => words.forEach((w) => { TONE_OF[w] = tone; }));

  function toneOf(status) {
    return TONE_OF[String(status || '').trim().toLowerCase()] || 'idle';
  }

  // <span class="ui-badge ui-badge-ok">Online</span>
  function badge(status, label) {
    const text = label !== undefined ? label : String(status || 'unknown').replace(/_/g, ' ');
    return `<span class="ui-badge ui-badge-${toneOf(status)}"><span class="ui-dot"></span>${esc(text)}</span>`;
  }

  function relTime(value) {
    if (!value) return '—';
    const t = typeof value === 'number' ? (value < 1e12 ? value * 1000 : value) : Date.parse(value);
    if (Number.isNaN(t)) return esc(value);
    const s = Math.round((Date.now() - t) / 1000);
    const abs = Math.abs(s);
    const fmt = (n, unit) => `${n} ${unit}${n === 1 ? '' : 's'}`;
    let out;
    if (abs < 45) out = 'just now';
    else if (abs < 3600) out = fmt(Math.round(abs / 60), 'min');
    else if (abs < 86400) out = fmt(Math.round(abs / 3600), 'hour');
    else out = fmt(Math.round(abs / 86400), 'day');
    if (out === 'just now') return out;
    return s >= 0 ? `${out} ago` : `in ${out}`;
  }

  // <time> with the relative text visible and the exact timestamp on hover.
  function timeTag(value) {
    if (!value) return '<span class="ui-muted">—</span>';
    const exact = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
    const iso = Number.isNaN(exact.getTime()) ? String(value) : exact.toLocaleString();
    return `<time title="${esc(iso)}">${esc(relTime(value))}</time>`;
  }

  function loading(el, message) {
    const node = target(el);
    if (!node) return;
    node.innerHTML = `<div class="ui-state" role="status" aria-live="polite">
      <span class="ui-spinner" aria-hidden="true"></span>
      <span>${esc(message || 'Loading…')}</span></div>`;
  }

  // empty(el, {icon: 'fa-inbox', title: 'No tasks yet', hint: 'Dispatch one above.'})
  function empty(el, opts) {
    const node = target(el);
    if (!node) return;
    const o = opts || {};
    node.innerHTML = `<div class="ui-state">
      <i class="fa-solid ${esc(o.icon || 'fa-inbox')} ui-state-icon" aria-hidden="true"></i>
      <div class="ui-state-title">${esc(o.title || 'Nothing here yet')}</div>
      ${o.hint ? `<div class="ui-state-hint">${esc(o.hint)}</div>` : ''}</div>`;
  }

  // error(el, err, retry) — shows what failed and, when given, a Retry button.
  function error(el, err, retry) {
    const node = target(el);
    if (!node) return;
    const message = err && err.message ? err.message : String(err || 'Something went wrong');
    node.innerHTML = `<div class="ui-state ui-state-error" role="alert">
      <i class="fa-solid fa-triangle-exclamation ui-state-icon" aria-hidden="true"></i>
      <div class="ui-state-title">Couldn't load this section</div>
      <div class="ui-state-hint">${esc(message)}</div></div>`;
    if (typeof retry === 'function') {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ui-btn ui-btn-secondary';
      btn.textContent = 'Retry';
      btn.addEventListener('click', retry);
      node.firstElementChild.appendChild(btn);
    }
  }

  // Disable a button and show a spinner while an async action runs.
  async function busy(button, fn) {
    const btn = target(button);
    if (!btn || btn.disabled) return undefined;
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    btn.innerHTML = `<span class="ui-spinner ui-spinner-sm" aria-hidden="true"></span>${original}`;
    try {
      return await fn();
    } finally {
      btn.disabled = false;
      btn.removeAttribute('aria-busy');
      btn.innerHTML = original;
    }
  }

  window.ui = { esc, badge, toneOf, relTime, timeTag, loading, empty, error, busy };
})();
