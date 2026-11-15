'use strict';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
const ic = (n, cls = '') => {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('class', 'i ' + cls);
  s.setAttribute('aria-hidden', 'true');
  s.innerHTML = `<use href="#i-${n}"/>`;
  return s;
};
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, '');
    else e.setAttribute(k, v);
  }
  for (const c of kids.flat(9)) {
    if (c == null || c === false) continue;
    e.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return e;
}
async function api(path, body, signal) {
  const opt =
    body === void 0
      ? {}
      : {
          method: 'POST',
          headers: { 'X-ALD': '1', 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        };
  if (signal) opt.signal = signal;
  const r = await fetch(path, opt);
  if (!r.ok) throw new Error(path + ' ' + r.status);
  return r.json();
}
const thumbUrl = u => (u ? '/api/thumb?u=' + encodeURIComponent(u) : null);
const fmtSize = n => {
  if (!n) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < 4) {
    n /= 1024;
    i++;
  }
  return (n >= 100 || i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + u[i];
};
const fmtSpeed = n => (n ? fmtSize(n) + '/s' : '');
const fmtEta = s => {
  if (s == null || s < 0) return '';
  if (s < 15) return Math.max(1, Math.round(s)) + ' s left';
  if (s < 60) return Math.round(s / 5) * 5 + ' s left'; // steps of 5 so it doesnt jitter every tick
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + ' min left';
  return (s / 3600).toFixed(1) + ' h left';
};
const fmtTook = s => {
  s = Math.round(s || 0);
  if (s < 1) return '';
  const m = Math.floor(s / 60);
  return m ? `${m} min ${String(s % 60).padStart(2, '0')} s` : `${s} s`;
};
const ago = t => {
  const s = Date.now() / 1e3 - t;
  if (s < 90) return 'just now';
  if (s < 3600) return Math.round(s / 60) + ' min ago';
  if (s < 86400) return Math.round(s / 3600) + ' h ago';
  const d = Math.round(s / 86400);
  return d === 1 ? 'yesterday' : d + ' days ago';
};
// quality comes in as a height, so 2160 is 4K and 4320 is 8K
const qLabel = q => (q >= 4320 ? '8K' : q >= 2160 ? '4K' : q + 'p');
const looksLikeUrl = v => /^(https?:\/\/\S+|(www\.)?[\w-]+(\.[\w-]+)+\/\S*)$/i.test(v.trim());
const plural = (n, a, b) => (n === 1 ? a : b);
// all the ui state, jobs/settings/library get swapped whole by merge() in live.js
const S = {
  page: 'get',
  jobs: [],
  settings: {},
  health: {},
  library: [],
  resumable: 0,
  paused: false,
  folder: '',
  browsers: [],
  version: '',
  an: null,
  kind: 'video',
  quality: 'best',
  container: 'mp4',
  afmt: 'mp3',
  sel: new Set(), // picked playlist entries, stored by entry.i and not by row position
  filter: '',
  view: [], // rows after the search filter, batch mode has group headers in here too
  lastClick: -1,
  libq: '',
  lastClip: '',
  logs: [],
};
function toast(kind, text) {
  const t = h(
    'div',
    { class: 'toast ' + (kind || '') },
    kind === 'good' ? ic('check') : kind === 'warn' ? ic('alert') : ic('info'),
    text
  );
  $('#toasts').append(t);
  setTimeout(() => {
    t.classList.add('out');
    setTimeout(() => t.remove(), 320);
  }, 4200);
}
function chime() {
  if (!S.settings.sound) return;
  try {
    const a = new (window.AudioContext || window.webkitAudioContext)();
    // two notes, E5 then A5
    [659, 880].forEach((f, i) => {
      const o = a.createOscillator(),
        g = a.createGain(),
        t = a.currentTime + i * 0.13;
      o.type = 'sine';
      o.frequency.value = f;
      g.gain.setValueAtTime(0, t);
      g.gain.linearRampToValueAtTime(0.1, t + 0.02);
      g.gain.exponentialRampToValueAtTime(1e-4, t + 0.45);
      o.connect(g).connect(a.destination);
      o.start(t);
      o.stop(t + 0.5);
    });
  } catch (e) {}
}
function devThumb(url, dur, opts = {}) {
  const d = h('div', { class: 'dev' + (opts.mini ? ' mini' : '') });
  const src = thumbUrl(url);
  const ph = h('div', { class: 'ph' }, ic('film'));
  if (!src) {
    d.append(ph);
  } else {
    // same image three times, css blurs the back one and wipes bw into color as progress goes up
    const back = h('img', { class: 'back', src, alt: '', draggable: 'false', referrerpolicy: 'no-referrer' });
    const bw = h('img', { class: 'bw', src, alt: '', draggable: 'false', referrerpolicy: 'no-referrer' });
    const col = h('img', { class: 'color', src, alt: '', draggable: 'false', referrerpolicy: 'no-referrer' });
    const fail = () => {
      back.remove();
      bw.remove();
      col.remove();
      if (!ph.isConnected) d.prepend(ph);
    };
    bw.addEventListener('error', fail);
    col.addEventListener('error', fail);
    if (opts.adapt)
      bw.addEventListener('load', () => {
        // anything this close to square is a music cover, not a 16:9 thumb
        if (bw.naturalWidth / (bw.naturalHeight || 1) < 1.25) d.classList.add('square');
      });
    d.append(back, bw, col);
    if (opts.develop) d.classList.add('develop');
  }
  d.append(h('i', { class: 'scan' }));
  if (dur) d.append(h('span', { class: 'dur num' }, dur));
  return d;
}
function setProgress(dev, pct, working) {
  dev.classList.toggle('working', working);
  dev.style.setProperty('--p', (Math.max(0, Math.min(1, pct)) * 100).toFixed(1) + '%');
}
function movePill() {
  const t = $('.tab[aria-selected=true]'),
    p = $('#pill');
  if (!t) return;
  p.style.width = t.offsetWidth + 'px';
  p.style.transform = `translateX(${t.offsetLeft}px)`;
}
function go(page) {
  if (S.page === page) return;
  if (S.page === 'studio' && typeof stLeave === 'function') stLeave();
  S.page = page;
  $$('.tab').forEach(t => t.setAttribute('aria-selected', String(t.dataset.page === page)));
  movePill();
  if (page === 'saved') renderSaved();
  if (page === 'settings') renderSettings();
  $$('.page').forEach(p => {
    const on = p.id === 'p-' + page;
    p.hidden = !on;
    p.classList.remove('enter');
    if (on && !reduced) {
      void p.offsetWidth; // forces a reflow so the enter animation can play again
      p.classList.add('enter');
    }
  });
  if (page === 'settings') spySettings();
  if (page === 'studio') stShown();
}
$$('.tab').forEach(t => t.addEventListener('click', () => go(t.dataset.page)));
addEventListener('resize', movePill);
