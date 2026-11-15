'use strict';
function renderSaved() {
  const q = S.libq.trim().toLowerCase();
  const items = S.library.filter(e => !q || (e.title || '').toLowerCase().includes(q));
  const box = $('#libList');
  if (!items.length) {
    box.replaceChildren(
      h(
        'div',
        { class: 'empty' },
        h('h3', null, S.library.length ? 'Nothing matches' : 'Nothing saved yet'),
        S.library.length ? 'Try a different word.' : 'Videos you download show up here, ready to play.'
      )
    );
    return;
  }
  box.replaceChildren(...items.map(libRow));
}
$('#libq').addEventListener('input', e => {
  S.libq = e.target.value;
  renderSaved();
});
async function chooseFolder() {
  const r = await api('/api/choose_folder', {});
  // couldnt open the folder dialog so fall back to typing the path
  if (r.unavailable) {
    const p = prompt('Type the folder where videos should be saved:', S.folder);
    if (p) await api('/api/settings', { folder: p });
  }
  await refreshFull();
}
let savedTimer = null;
function flashSaved() {
  const n = $('#savedNote');
  if (!n) return;
  n.replaceChildren(ic('check'), 'Saved');
  n.classList.add('on');
  clearTimeout(savedTimer);
  savedTimer = setTimeout(() => n.classList.remove('on'), 1700);
}
const setSetting = (k, v) => {
  S.settings[k] = v; // local copy first, the saved note shows once the server says ok
  api('/api/settings', { [k]: v })
    .then(flashSaved)
    .catch(() => {});
  if (k === 'theme') applyTheme();
};
function toggle(label, desc, key) {
  const sw = h('button', {
    class: 'switch',
    role: 'switch',
    'aria-checked': String(!!S.settings[key]),
    'aria-label': label,
    onclick: () => {
      const v = !S.settings[key];
      sw.setAttribute('aria-checked', String(v));
      setSetting(key, v);
    },
  });
  return h('div', { class: 'set' }, h('div', { class: 'l' }, h('b', null, label), h('span', null, desc)), sw);
}
function hc(state, title, sub) {
  return h(
    'div',
    { class: 'set status ' + state },
    h('div', { class: 'l' }, h('b', null, title)),
    h('span', { class: 'st' }, h('i', { class: 'dot' }), sub)
  );
}
// the numbers in here are copied by hand from PROFILES in settings.py, change both
const SPEEDS = [
  [
    'gentle',
    'Gentle',
    'Light on the PC. Good while you work, game or stream.',
    '2 at a time · 6 connections',
    1,
  ],
  [
    'balanced',
    'Balanced',
    'Fast and smooth. The right pick for most people.',
    '3 at a time · 16 connections',
    2,
  ],
  [
    'max',
    'Maximum',
    'Takes everything your line gives. The PC may feel busy.',
    '4 at a time · 32 connections',
    3,
  ],
];
const SET_SECTIONS = [
  ['saving', 'Saving'],
  ['speed', 'Speed'],
  ['links', 'Links'],
  ['spotify', 'Spotify'],
  ['signin', 'Sign-in'],
  ['look', 'Look and sound'],
  ['updates', 'Updates and network'],
  ['checkup', 'Check-up'],
];
const grp = (id, title, desc, ...body) =>
  h(
    'section',
    { class: 'group', id },
    h('div', { class: 'gh' }, h('h2', null, title), desc ? h('p', null, desc) : null),
    h('div', { class: 'gb panel' }, ...body)
  );
const field = (label, key, ph) => {
  const i = h('input', {
    class: 'fld',
    placeholder: ph,
    'aria-label': label,
    autocomplete: 'off',
    spellcheck: 'false',
    onchange: e => setSetting(key, e.target.value.trim()),
  });
  i.value = S.settings[key] || '';
  return h('label', null, label, i);
};
function jumpTo(id) {
  const el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
}
function spySettings() {
  const pg = $('#p-settings'),
    nav = $('.setnav');
  if (!pg || !nav || !pg.clientHeight) return;
  const top = pg.getBoundingClientRect().top;
  let cur = 'saving';
  $$('.group', pg).forEach(g => {
    if (g.getBoundingClientRect().top - top <= 100) cur = g.id; // 100px under the top counts as current
  });
  // the last section is too short to ever reach the top so the bottom of the page counts for it
  if (pg.scrollTop + pg.clientHeight >= pg.scrollHeight - 4) cur = 'checkup';
  $$('button', nav).forEach(b => b.setAttribute('aria-current', String(b.dataset.id === cur)));
}
let spyBusy = false;
$('#p-settings').addEventListener(
  'scroll',
  () => {
    if (spyBusy) return; // one update per frame, scroll fires a lot
    spyBusy = true;
    requestAnimationFrame(() => {
      spyBusy = false;
      spySettings();
    });
  },
  { passive: true }
);
function renderSettings() {
  const s = S.settings,
    hl = S.health,
    body = $('#setBody');
  if (!s.folder && !S.folder) return; // no full state yet
  const out = h('div', { class: 'result-line', id: 'toolOut', 'aria-live': 'polite' });
  const logbox = h('pre', { class: 'logbox', id: 'logbox', hidden: true });
  const tool = (label, icon, fn, target) =>
    h(
      'button',
      {
        class: 'btn small soft',
        onclick: async e => {
          const b = e.currentTarget,
            o = target || out;
          b.disabled = true;
          try {
            await fn(o);
          } catch (x) {
            o.textContent = 'That did not work. Try again.';
          }
          b.disabled = false;
        },
      },
      ic(icon),
      label
    );
  const radio = (label, items, cur, key) =>
    h(
      'div',
      { class: 'seg', role: 'radiogroup', 'aria-label': label },
      ...items.map(([v, l]) =>
        h(
          'button',
          {
            role: 'radio',
            'aria-checked': String(cur === v),
            onclick: e => {
              $$('button', e.currentTarget.parentNode).forEach(b => b.setAttribute('aria-checked', 'false'));
              e.currentTarget.setAttribute('aria-checked', 'true');
              setSetting(key, v);
            },
          },
          l
        )
      )
    );
  const themeSeg = radio(
    'Theme',
    [
      ['auto', 'Match my PC'],
      ['light', 'Light'],
      ['dark', 'Dark'],
    ],
    s.theme || 'auto',
    'theme'
  );
  const browserSel = h(
    'select',
    {
      class: 'sel',
      'aria-label': 'Browser to borrow sign-in from',
      onchange: e => setSetting('browser', e.target.value),
    },
    h('option', { value: 'none', selected: (s.browser || 'none') === 'none' }, 'Not needed'),
    ...(S.browsers || []).map(b =>
      h('option', { value: b, selected: s.browser === b }, 'Use ' + b.charAt(0).toUpperCase() + b.slice(1))
    )
  );
  const engineSel = h(
    'select',
    {
      class: 'sel',
      'aria-label': 'Spotify method',
      onchange: e => setSetting('spotify_engine', e.target.value),
    },
    h(
      'option',
      { value: 'auto', selected: (s.spotify_engine || 'auto') === 'auto' },
      'spotDL, with built-in backup'
    ),
    h('option', { value: 'builtin', selected: s.spotify_engine === 'builtin' }, 'Built-in only')
  );
  const proxies = h('textarea', {
    class: 'ta',
    placeholder: 'One per line, e.g. socks5://127.0.0.1:9050',
    'aria-label': 'Proxies',
    onchange: e => setSetting('proxies', e.target.value),
  });
  proxies.value = s.proxies || '';
  const prof = s.profile || 'balanced';
  const note = h('div', { class: 'speed-note', 'aria-live': 'polite' });
  const paintNote = v => {
    const [, name, d, facts] = SPEEDS.find(x => x[0] === v) || SPEEDS[1];
    note.replaceChildren(h('b', null, name + '. '), d, h('span', { class: 'num' }, facts));
  };
  paintNote(prof);
  const speeds = h(
    'div',
    { class: 'speeds', role: 'radiogroup', 'aria-label': 'Speed' },
    ...SPEEDS.map(([v, name, d, facts, bars]) =>
      h(
        'button',
        {
          class: 'speed',
          role: 'radio',
          'aria-checked': String(prof === v),
          onclick: e => {
            $$('.speed', speeds).forEach(b => b.setAttribute('aria-checked', 'false'));
            e.currentTarget.setAttribute('aria-checked', 'true');
            setSetting('profile', v);
            paintNote(v);
          },
        },
        h(
          'span',
          { class: 'bars', 'aria-hidden': 'true' },
          ...[1, 2, 3].map(n => h('i', { class: n <= bars ? 'on' : '' }))
        ),
        name
      )
    )
  );
  const spOut = h('span', null, 'Measures your line for a few seconds.');
  const ok = hl.ffmpeg && hl.ffprobe;
  const nav = h(
    'nav',
    { class: 'setnav', 'aria-label': 'Settings sections' },
    ...SET_SECTIONS.map(([id, t], i) =>
      h('button', { 'data-id': id, 'aria-current': String(i === 0), onclick: () => jumpTo(id) }, t)
    )
  );
  body.replaceChildren(
    h(
      'div',
      { class: 'setwrap' },
      nav,
      h(
        'div',
        { class: 'setmain' },
        grp(
          'saving',
          'Saving',
          'Everything you download goes into one folder.',
          h(
            'div',
            { class: 'set' },
            h(
              'div',
              { class: 'l' },
              h('b', null, 'Save videos to'),
              h(
                'span',
                { class: 'path', id: 'folderPath', title: S.folder || s.folder },
                S.folder || s.folder
              )
            ),
            h('button', { class: 'btn small soft', onclick: chooseFolder }, ic('folder'), 'Change'),
            h('button', { class: 'btn small soft', onclick: () => api('/api/reveal', {}) }, 'Open')
          )
        ),
        grp(
          'speed',
          'Speed',
          'How hard the app works. It changes at once, even while downloads are running.',
          speeds,
          note,
          h(
            'div',
            { class: 'set' },
            h('div', { class: 'l' }, h('b', null, 'Not sure which to pick?'), spOut),
            tool(
              'Test my connection',
              'download',
              async o => {
                o.textContent = 'Testing for a few seconds...';
                const r = await api('/api/speed_test', {});
                o.textContent = r.ok
                  ? `Your connection is about ${r.mbit.toFixed(0)} Mbit/s (${r.multi.toFixed(1)} MB/s). One link alone reaches ${r.single.toFixed(1)} MB/s.`
                  : "Couldn't measure: " + r.text;
              },
              spOut
            )
          )
        ),
        grp(
          'links',
          'Links',
          'How the app reacts when you paste or copy a link.',
          toggle(
            'Fetch automatically',
            'Reads a link as soon as it is pasted. Turn off to press Fetch yourself.',
            'auto_fetch'
          ),
          toggle(
            'Notice links I copy',
            'Copy a link anywhere, come back here, and it is picked up.',
            'clip_watch'
          ),
          toggle(
            'Get around blocked sites',
            "If a site can't be reached, the app tests other routes and uses the fastest.",
            'auto_route'
          ),
          toggle(
            'Fastest link reading',
            'For YouTube, reads the link in one ordinary run, the same way yt-dlp does on its own. Turn off to use the older quick and parallel ways.',
            'plain_fetch'
          ),
          toggle(
            'Quick link reading',
            'For YouTube, asks the quickest way first, so a link is usually ready in a few seconds. Turn it off if tall versions (1440p, 4K) are missing from the list.',
            'fast_fetch'
          ),
          toggle(
            "Skip YouTube's stream list (experimental)",
            "Doesn't read the extra HLS list. In tests it was no faster, so leave it off unless you want to try.",
            'skip_m3u8'
          ),
          toggle(
            'Look for 4K and 8K (slower)',
            'Asks YouTube two more ways for the tallest versions. Most videos have none, so leave this off for the quickest link reading.',
            'find_4k'
          )
        ),
        grp(
          'spotify',
          'Spotify',
          'Spotify protects its audio, so no app can copy it. The app reads each song, cover and tags from Spotify and saves the matching audio from YouTube Music.',
          h(
            'div',
            { class: 'set' },
            h(
              'div',
              { class: 'l' },
              h('b', null, 'Method'),
              h('span', null, 'spotDL gives the best matches. The built-in matcher steps in if it fails.')
            ),
            engineSel
          ),
          h(
            'details',
            { class: 'adv' },
            h('summary', null, ic('chev'), 'Your own Spotify keys (optional)'),
            h(
              'p',
              { style: 'color:var(--ink2);font-size:13.5px' },
              'Free keys from developer.spotify.com remove the 100-song limit on big playlists.'
            ),
            h(
              'div',
              { class: 'two' },
              field('Client ID', 'spotify_client_id', 'Paste the ID'),
              field('Client secret', 'spotify_client_secret', 'Paste the secret')
            )
          )
        ),
        grp(
          'signin',
          'Sign-in',
          'Some videos (age-restricted, members-only, "prove you are human") need you signed in. The app already tries your browsers by itself when that happens.',
          h(
            'div',
            { class: 'set' },
            h(
              'div',
              { class: 'l' },
              h('b', null, 'Borrow sign-in from'),
              h('span', null, 'Only read when a site asks for it. Never leaves your PC.')
            ),
            browserSel
          )
        ),
        grp(
          'look',
          'Look and sound',
          null,
          h('div', { class: 'set' }, h('div', { class: 'l' }, h('b', null, 'Theme')), themeSeg),
          toggle('Chime when a download finishes', 'A short, soft sound.', 'sound')
        ),
        grp(
          'updates',
          'Updates and network',
          null,
          appUpdateRow(),
          otaRow(),
          toggle(
            'Keep itself up to date',
            'Sites change often. This quietly refreshes the download engine (a few MB, at most twice a day).',
            'auto_update'
          ),
          h(
            'details',
            { class: 'adv' },
            h('summary', null, ic('chev'), 'Advanced'),
            toggle('Use IPv4 only', 'Fixes odd stalls on some home routers.', 'ipv4'),
            h(
              'div',
              { class: 'set', style: 'display:block' },
              h('b', null, 'Proxies'),
              h(
                'span',
                { style: 'display:block;color:var(--ink2);font-size:13.5px;margin:2px 0 8px' },
                "Only if you use one. Tried automatically when a site can't be reached."
              ),
              proxies
            )
          )
        ),
        grp(
          'checkup',
          'Check-up',
          'What the app needs to work, and a few repair tools.',
          hc(ok ? 'ok' : 'off', 'Video joining', ok ? 'ffmpeg is ready' : 'Missing, will be fetched'),
          hc(
            hl.js ? 'ok' : 'off',
            'YouTube helper',
            hl.js ? hl.js + ' is ready' : 'Missing, will be fetched'
          ),
          hc('ok', 'Download engine', (hl.ytdlp || '?') + (hl.updated ? ' (updated)' : '')),
          hc(
            hl.impersonate ? 'ok' : 'meh',
            'Browser disguise',
            hl.impersonate ? 'Available' : 'Not available'
          ),
          h(
            'div',
            { class: 'set toolrow' },
            tool('Update now', 'refresh', async o => {
              o.textContent = 'Checking...';
              const r = await api('/api/update_engine', {});
              o.textContent = r.text;
              refreshFull();
            }),
            tool('Clean up leftovers', 'trash', async o => {
              const r = await api('/api/cleanup', {});
              if (!r.count) {
                o.textContent = 'Nothing to clean up.';
                return;
              }
              if (confirm(`Delete ${r.count} half-finished file(s) (${r.size}) from your videos folder?`)) {
                const d = await api('/api/cleanup', { do: true });
                o.textContent = `Removed ${d.count} file(s), freed ${d.size}.`;
              }
            }),
            tool('Copy report for help', 'info', async o => {
              const r = await api('/api/diagnostics');
              try {
                await navigator.clipboard.writeText(r.text);
                o.textContent = 'Copied. Paste it into a message to whoever is helping you.';
              } catch (e) {
                // clipboard can be blocked, so show the text to copy by hand
                logbox.hidden = false;
                logbox.textContent = r.text;
                o.textContent = 'Select the text below and copy it.';
              }
            }),
            out,
            logbox
          )
        ),
        h(
          'div',
          { class: 'about' },
          `Any Link Downloader ${S.version}`,
          h(
            'button',
            {
              class: 'btn small ghost',
              onclick: async () => {
                await api('/api/quit', {});
                if (window.ipc) window.ipc.postMessage('close');
                else window.close();
                // the window might not actually close (plain browser tabs refuse), so leave a message
                document.body.replaceChildren(
                  h(
                    'div',
                    { class: 'wrap', style: 'padding-top:80px' },
                    h('h1', { style: 'font:800 36px var(--display)' }, 'You can close this window.')
                  )
                );
              },
            },
            'Quit the app'
          )
        )
      )
    )
  );
  spySettings();
}
// remembers "later" for this session only, the bar comes back after a restart
function appuLater(v) {
  try {
    if (v !== void 0) sessionStorage.setItem('appu-later', v);
    return sessionStorage.getItem('appu-later');
  } catch (e) {
    return null;
  }
}
function appuMB(n) {
  return n ? ` (${Math.max(1, Math.round(n / 1048576))} MB)` : '';
}
async function appuCheck() {
  S.appu = await api('/api/app_update', { force: true });
  const r = $('#appuRow');
  if (r) fillAppU(r);
  renderUpdBar();
}
async function appuDownload() {
  S.appu = await api('/api/app_update_download', {});
  const r = $('#appuRow');
  if (r) fillAppU(r);
  renderUpdBar();
}
async function appuInstall() {
  let r = await api('/api/app_update_install', {});
  // busy means downloads are still running, ask before stopping them
  if (r.busy) {
    if (
      !confirm(
        `${r.text} They will be stopped and offered again the next time the app opens. Install the update now?`
      )
    )
      return;
    r = await api('/api/app_update_install', { confirm: true });
  }
  if (!r.ok) {
    toast('warn', r.text || 'The update could not be started.');
    if (r.view) S.appu = r.view;
    return;
  }
  toast('good', 'Updating. The app closes and opens again by itself.');
  await api('/api/quit', {});
  if (window.ipc) window.ipc.postMessage('close');
  else window.close();
}
function fillAppU(el) {
  const a = S.appu || {};
  // no repo set means self update is switched off
  if (!a.enabled) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.replaceChildren();
  const btn = (t, fn) =>
    h(
      'button',
      {
        class: 'btn small',
        onclick: async e => {
          const b = e.currentTarget;
          b.disabled = true;
          try {
            await fn();
          } finally {
            b.disabled = false;
          }
        },
      },
      t
    );
  const pct = Math.round((a.progress || 0) * 100);
  let title,
    sub,
    act = null,
    bar = null;
  if (a.state === 'available') {
    title = `Version ${a.version} is available`;
    sub = `You have ${a.current}${appuMB(a.size)}. Downloaded once, then installed when you say so.`;
    act = btn('Download update', appuDownload);
  } else if (a.state === 'downloading') {
    title = `Downloading version ${a.version}`;
    sub = `${pct}%`;
    bar = h('div', { class: 'appu-bar' }, h('i', { style: `width:${pct}%` }));
  } else if (a.state === 'ready') {
    title = `Version ${a.version} is ready`;
    sub = 'Closes the app, installs, and opens it again. Your settings and saved list stay.';
    act = a.can_install
      ? btn('Restart and install', appuInstall)
      : a.page
        ? h(
            'a',
            { class: 'btn small', href: a.page, target: '_blank', rel: 'noopener' },
            'Open the download page'
          )
        : null;
  } else if (a.state === 'installing') {
    title = 'Installing the update';
    sub = 'The app will open again in a moment.';
  } else if (a.state === 'error') {
    title = "The update didn't work";
    sub = a.error;
    act = btn('Try again', a.version ? appuDownload : appuCheck);
  } else {
    title = `Any Link Downloader ${a.current}`;
    sub = 'You have the latest version.';
    act = btn('Check now', appuCheck);
  }
  el.append(h('div', { class: 'l' }, h('b', null, title), h('span', null, sub), bar));
  if (act) el.append(act);
}
function appUpdateRow() {
  const el = h('div', { class: 'set', id: 'appuRow' });
  fillAppU(el);
  return el;
}
async function otaCheck() {
  S.ota = await api('/api/ota', { force: true });
  const r = $('#otaRow');
  if (r) fillOta(r);
}
// quick updates, the small patches. only shows when ota.py has a key and this is the installed app
function fillOta(el) {
  const o = S.ota || {};
  if (!o.enabled) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.replaceChildren();
  let title = 'Quick updates',
    sub = o.running
      ? `You are on the latest quick update${o.built ? ` (${o.built})` : ''}.`
      : 'You have the latest version.',
    act = h(
      'button',
      {
        class: 'btn small',
        onclick: async e => {
          const b = e.currentTarget;
          b.disabled = true;
          try {
            await otaCheck();
          } finally {
            b.disabled = false;
          }
        },
      },
      'Check now'
    );
  if (o.state === 'checking') {
    sub = 'Checking...';
    act = null;
  } else if (o.pending) {
    title = 'An update is downloaded';
    sub = `It switches on the next time you open the app${o.built ? ` (${o.built})` : ''}.`;
    act = null;
  } else if (o.state === 'error') sub = o.error;
  el.append(h('div', { class: 'l' }, h('b', null, title), h('span', null, sub)));
  if (act) el.append(act);
}
function otaRow() {
  const el = h('div', { class: 'set', id: 'otaRow' });
  fillOta(el);
  return el;
}
function renderUpdBar() {
  const b = $('#updbar');
  if (!b) return;
  const a = S.appu || {},
    show = a.enabled && ['available', 'downloading', 'ready'].includes(a.state) && appuLater() !== a.version;
  if (!show) {
    if (b.firstChild) b.replaceChildren();
    return;
  }
  const btn = (t, fn, cls = 'btn small') => h('button', { class: cls, onclick: fn }, t);
  const pct = Math.round((a.progress || 0) * 100);
  let text,
    acts = [];
  if (a.state === 'available') {
    text = `Version ${a.version} is available.`;
    acts = [
      btn('Download', appuDownload),
      btn(
        'Later',
        () => {
          appuLater(a.version);
          renderUpdBar();
        },
        'btn small ghost'
      ),
    ];
  } else if (a.state === 'downloading') text = `Downloading version ${a.version}... ${pct}%`;
  else {
    text = `Version ${a.version} is ready to install.`;
    acts = [
      a.can_install ? btn('Restart and install', appuInstall) : null,
      btn(
        'Later',
        () => {
          appuLater(a.version);
          renderUpdBar();
        },
        'btn small ghost'
      ),
    ].filter(Boolean);
  }
  b.replaceChildren(
    h(
      'div',
      { class: 'banner' },
      ic('refresh'),
      text,
      a.state === 'downloading' ? h('div', { class: 'appu-bar' }, h('i', { style: `width:${pct}%` })) : null,
      h('span', { class: 'sp' }, ...acts)
    )
  );
}
function renderBanner() {
  const b = $('#banner'),
    n = S.jobs.filter(j => j.status === 'queued').length;
  const need = S.paused && n > 0;
  if (!need) {
    if (b.firstChild) b.replaceChildren();
    return;
  }
  if (b.firstChild) return; // already showing, this runs on every tick
  b.append(
    h(
      'div',
      { class: 'banner' },
      ic('refresh'),
      `${n} ${plural(n, 'download was', 'downloads were')} interrupted last time.`,
      h(
        'span',
        { class: 'sp' },
        h('button', { class: 'btn small', onclick: () => api('/api/resume', { go: true }) }, 'Continue'),
        h('button', { class: 'btn small ghost', onclick: () => api('/api/resume', { go: false }) }, 'Discard')
      )
    )
  );
}
