'use strict';
const input = $('#url'),
  goBtn = $('#go'),
  clearBtn = $('#clear');
const padBox = $('#many'),
  padCnt = $('#manyCnt'),
  padGo = $('#manyGo');
let anTok = 0,
  anTimer = null;
const autoFetch = () => S.settings.auto_fetch !== false;
const MAX_BATCH = 40; // batch mode only reads the first 40 links
const extractLinks = text => {
  const seen = new Set(),
    out = [];
  for (const tok of String(text || '').split(/[\s,;]+/)) {
    // links pasted out of a sentence or markdown come with trailing punctuation
    const t = tok.replace(/[)\].,;]+$/, '');
    if (t && looksLikeUrl(t) && !seen.has(t)) {
      seen.add(t);
      out.push(t);
    }
  }
  return out;
};
function syncSlot() {
  const has = input.value.trim().length > 0;
  clearBtn.hidden = !has;
  goBtn.textContent = has ? 'Fetch' : 'Paste';
  const loading = S.an && S.an.status === 'loading';
  const old = $('.slot .spin');
  if (loading && !old) $('#slot').insertBefore(h('span', { class: 'spin' }), clearBtn);
  if (!loading && old) old.remove();
  $('#tip').textContent = S.many
    ? 'One link on each line, videos or playlists. Press Ctrl+Enter or the button to fetch them all.'
    : autoFetch()
      ? 'Tip: copy a link anywhere, then come back here. The app picks it up for you.'
      : 'Paste a link, then press Fetch.';
}
function resetResult() {
  anTok++;
  S.an = null;
  S.sel.clear();
  renderResult();
}
function onInput() {
  clearTimeout(anTimer);
  syncSlot();
  const v = input.value.trim();
  if (!v) {
    resetResult();
    return;
  }
  if (autoFetch() && looksLikeUrl(v)) anTimer = setTimeout(() => analyze(v), 300); // let them finish typing first
}
input.addEventListener('input', onInput);
input.addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    clearTimeout(anTimer);
    const v = input.value.trim();
    if (v) analyze(v);
  }
});
input.addEventListener('paste', e => {
  const t = (e.clipboardData || window.clipboardData).getData('text').trim();
  if (!t) return;
  e.preventDefault();
  // more than one link in the paste flips to the multi link box
  const links = extractLinks(t);
  if (links.length > 1) {
    setMany(true, links.join('\n'));
    return;
  }
  input.value = t;
  syncSlot();
  clearTimeout(anTimer);
  if (autoFetch()) analyze(t);
});
clearBtn.addEventListener('click', () => {
  input.value = '';
  syncSlot();
  resetResult();
  input.focus();
});
goBtn.addEventListener('click', async () => {
  const v = input.value.trim();
  if (v) {
    analyze(v);
    return;
  }
  try {
    const { text } = await api('/api/clipboard');
    const m = (text || '').trim();
    if (!m) {
      toast('', 'Copy a link first, then press Paste.');
      return;
    }
    const links = extractLinks(m);
    if (links.length > 1) {
      setMany(true, links.join('\n'));
      return;
    }
    input.value = m.split(/\s+/)[0];
    syncSlot();
    if (autoFetch()) analyze(input.value);
  } catch (e) {
    input.focus();
  }
});
// paste anywhere on the get page, not just inside the box
document.addEventListener('paste', e => {
  if (S.page !== 'get' || S.many || /input|textarea/i.test(e.target.tagName)) return;
  const t = (e.clipboardData || window.clipboardData).getData('text').trim();
  if (!t) return;
  const links = extractLinks(t);
  if (links.length > 1) {
    setMany(true, links.join('\n'));
    return;
  }
  input.value = t;
  syncSlot();
  if (autoFetch()) analyze(t);
});
addEventListener('dragover', e => e.preventDefault()); // drop never fires without this
addEventListener('drop', e => {
  e.preventDefault();
  const raw = (e.dataTransfer.getData('text/uri-list') || e.dataTransfer.getData('text') || '').trim();
  const links = extractLinks(raw);
  if (!links.length) return;
  go('get');
  if (S.many || links.length > 1) {
    setMany(
      true,
      (S.many && padBox.value.trim() ? padBox.value.replace(/\s*$/, '\n') : '') + links.join('\n')
    );
    return;
  }
  input.value = links[0];
  syncSlot();
  if (autoFetch()) analyze(links[0]);
});
function manyCount() {
  const n = Math.min(extractLinks(padBox.value).length, MAX_BATCH);
  const over = extractLinks(padBox.value).length > MAX_BATCH;
  padCnt.replaceChildren(
    h('b', null, n),
    ' ' + plural(n, 'link', 'links') + (over ? ` (first ${MAX_BATCH} only)` : '')
  );
  padGo.disabled = n === 0;
  padGo.textContent = n ? `Fetch ${n} ${plural(n, 'link', 'links')}` : 'Fetch links';
  // reset to auto first or scrollHeight never gets smaller again
  padBox.style.height = 'auto';
  padBox.style.height = Math.min(308, Math.max(140, padBox.scrollHeight)) + 'px';
}
function setMany(on, text) {
  if (on === !!S.many && text == null) return;
  if (on) {
    clearTimeout(anTimer);
    const seed = text != null ? text : input.value.trim();
    S.many = true;
    if (seed) padBox.value = seed.replace(/\s*$/, '\n');
    resetResult();
  } else {
    S.many = false;
    const one = extractLinks(padBox.value);
    if (one.length === 1 && !input.value.trim()) input.value = one[0];
    resetResult();
  }
  $('#pad').classList.remove('done');
  $('#slotFold').classList.toggle('open', !S.many);
  $('#slotFold').inert = !!S.many;
  $('#padFold').classList.toggle('open', !!S.many);
  $('#padFold').inert = !S.many;
  $('#getwrap').classList.toggle('manymode', !!S.many);
  manyCount();
  syncSlot();
  // focus after the fold animation
  setTimeout(() => (S.many ? padBox : input).focus(), reduced ? 0 : 220);
  if (S.many) {
    if (padBox.scrollHeight > padBox.clientHeight) padBox.scrollTop = padBox.scrollHeight;
    padBox.setSelectionRange(padBox.value.length, padBox.value.length);
  }
}
$('#manyOn').addEventListener('click', e => {
  e.preventDefault();
  setMany(true);
});
$('#manyOff').addEventListener('click', () => setMany(false));
padBox.addEventListener('input', manyCount);
padBox.addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    fetchMany();
  }
});
padBox.addEventListener('paste', e => {
  const t = (e.clipboardData || window.clipboardData).getData('text');
  if (!t) return;
  e.preventDefault();
  const a = padBox.selectionStart,
    b = padBox.selectionEnd,
    before = padBox.value.slice(0, a);
  const lead = before && !before.endsWith('\n') ? '\n' : '';
  padBox.setRangeText(lead + t.trim() + '\n', a, b, 'end');
  manyCount();
});
$('#manyClip').addEventListener('click', async () => {
  try {
    const { text } = await api('/api/clipboard');
    const links = extractLinks(text);
    if (!links.length) {
      toast('', 'Copy a link first, then press this.');
      return;
    }
    padBox.value =
      padBox.value.replace(/\s*$/, '') + (padBox.value.trim() ? '\n' : '') + links.join('\n') + '\n';
    manyCount();
    padBox.focus();
  } catch (e) {
    padBox.focus();
  }
});
padGo.addEventListener('click', () => fetchMany());
$('#manyEdit').addEventListener('click', () => {
  $('#pad').classList.remove('done');
  setTimeout(() => {
    padBox.focus();
    fitPlaylist();
  }, 380);
});
async function analyze(raw) {
  const tok = ++anTok; // so a slow older fetch cant overwrite a newer one
  S.an = { status: 'loading' };
  S.sel.clear();
  S.filter = '';
  renderResult();
  // timings only go to the log, to see where the slow links spend their time
  const tm = (S.tm = { t0: performance.now() });
  const t0 = Date.now(),
    ac = new AbortController(),
    cap = setTimeout(() => ac.abort(), 11e4); // 110 s then give up
  // the still looking line only shows after 10s so quick links dont flash it
  const wait = setInterval(() => {
    const el = $('#anWait');
    if (!el || tok !== anTok) return;
    const n = Math.round((Date.now() - t0) / 1e3);
    el.hidden = n < 10;
    el.firstChild.textContent =
      n < 25
        ? `Still looking... ${n}s. Some sites are slow.`
        : `Still trying... ${n}s. This site may be blocked or very slow.`;
  }, 1e3);
  // preview is the quick title and thumb, shows while the full analyze is still running
  api('/api/preview', { url: raw })
    .then(p => {
      if (tok !== anTok || !S.an || S.an.status !== 'loading' || !p || !p.title) return;
      tm.prev = performance.now() - tm.t0;
      S.an = { status: 'loading', preview: p };
      renderResult();
    })
    .catch(() => {});
  try {
    const d = await api('/api/analyze', { url: raw }, ac.signal);
    clearTimeout(cap);
    clearInterval(wait);
    tm.card = performance.now() - tm.t0;
    if (tok !== anTok) return;
    if (d.type === 'error') S.an = { status: 'error', data: d };
    else {
      S.an = { status: 'ok', data: d };
      if (d.type === 'playlist') initPlaylist(d);
    }
  } catch (e) {
    clearTimeout(cap);
    clearInterval(wait);
    if (tok !== anTok) return;
    S.an =
      e && e.name === 'AbortError'
        ? {
            status: 'error',
            data: {
              title: 'That took too long',
              text: "The site didn't answer in time. It may be blocked on your network or very slow right now. Try again, or try the link in your browser first.",
              action: 'retry',
            },
          }
        : {
            status: 'error',
            data: {
              title: 'The app stopped answering',
              text: 'Close this window and open the app again.',
              action: null,
            },
          };
  }
  clearTimeout(cap);
  clearInterval(wait);
  renderResult();
}
let renderedFor = null;
function renderResult() {
  syncSlot();
  const an = S.an,
    box = $('#result'),
    fold = $('#fold');
  $('#getwrap').classList.toggle('compact', !!an || S.jobs.length > 0);
  if (an === renderedFor) return; // same object, nothing to redraw
  renderedFor = an;
  if (!an) {
    fold.classList.remove('open');
    $('#getwrap').classList.remove('plmode');
    // empty it after the fold has closed
    setTimeout(() => {
      if (!S.an) box.replaceChildren();
    }, 520);
    updateRecent();
    return;
  }
  let node;
  if (an.status === 'loading' && an.preview) {
    const p = an.preview;
    node = h(
      'div',
      null,
      h(
        'div',
        { class: 'vid' },
        h(
          'div',
          { style: 'aspect-ratio:16/9;border-radius:14px;overflow:hidden;background:var(--sunk)' },
          p.thumb
            ? h('img', {
                src: thumbUrl(p.thumb),
                alt: '',
                draggable: 'false',
                referrerpolicy: 'no-referrer',
                decoding: 'async',
                style: 'width:100%;height:100%;object-fit:cover;display:block',
              })
            : null
        ),
        h(
          'div',
          { class: 'info' },
          h('h2', null, p.title),
          h('div', { class: 'meta' }, p.uploader || ''),
          h('div', { class: 'meta' }, h('span', { class: 'dots' }, 'Reading the qualities'))
        )
      ),
      h(
        'div',
        { class: 'anwait', id: 'anWait', hidden: true },
        h('span'),
        h('button', { class: 'btn small ghost', onclick: () => resetResult() }, 'Cancel')
      )
    );
  } else if (an.status === 'loading')
    node = h(
      'div',
      null,
      h(
        'div',
        { class: 'skel', 'aria-label': 'Reading the link' },
        h('i', { class: 't' }),
        h(
          'div',
          null,
          h('i', { class: 'l', style: 'width:80%' }),
          h('i', { class: 'l', style: 'width:55%' }),
          h('i', { class: 'l', style: 'width:35%;margin-top:40px' })
        )
      ),
      h(
        'div',
        { class: 'anwait', id: 'anWait', hidden: true },
        h('span'),
        h('button', { class: 'btn small ghost', onclick: () => resetResult() }, 'Cancel')
      )
    );
  else if (an.status === 'error') node = errorCard(an.data);
  else if (an.data.type === 'playlist') node = playlistPanel(an.data);
  else node = videoCard(an.data);
  box.replaceChildren(node);
  fold.classList.add('open');
  $('#getwrap').classList.toggle('plmode', an.status === 'ok' && an.data.type === 'playlist');
  fitPlaylist();
  setTimeout(fitPlaylist, 560); // again once the fold animation is done
  updateRecent();
}
function fitPlaylist() {
  const pl = $('.pl');
  if (!pl) return;
  const top = pl.getBoundingClientRect().top + $('#p-get').scrollTop;
  // whatever is left of the window, but never under 340
  const room = innerHeight - pl.getBoundingClientRect().top - 22;
  pl.style.setProperty('--pl-h', Math.max(340, room) + 'px');
  vlRender();
}
addEventListener('resize', fitPlaylist);
function errorCard(d) {
  const acts = h('div', { class: 'acts' });
  if (d.action === 'cookies')
    acts.append(
      h(
        'button',
        {
          class: 'btn small',
          onclick: () => {
            go('settings');
            setTimeout(() => $('#signin')?.scrollIntoView({ behavior: 'smooth' }), 400);
          },
        },
        'Set up sign-in'
      )
    );
  if (d.action === 'update')
    acts.append(
      h(
        'button',
        {
          class: 'btn small',
          onclick: async e => {
            e.target.disabled = true;
            const r = await api('/api/update_engine', {});
            toast(r.ok ? 'good' : 'warn', r.text);
            analyze(input.value.trim());
          },
        },
        'Update and try again'
      )
    );
  if (d.action === 'folder')
    acts.append(h('button', { class: 'btn small', onclick: chooseFolder }, 'Choose another folder'));
  acts.append(
    h(
      'button',
      { class: 'btn small ghost', onclick: () => analyze(input.value.trim()) },
      ic('refresh'),
      'Try again'
    )
  );
  return h(
    'div',
    { class: 'bad-card', role: 'alert' },
    ic('alert'),
    h('div', null, h('h3', null, d.title), h('p', null, d.text), acts)
  );
}
function kindSeg(onchange) {
  const mk = (k, label, icon) =>
    h(
      'button',
      {
        role: 'radio',
        'aria-checked': String(S.kind === k),
        onclick: e => {
          S.kind = k;
          saveChoice();
          $$('[data-kind]').forEach(b => b.setAttribute('aria-checked', String(b.dataset.kind === k)));
          onchange && onchange();
        },
      },
      ic(icon),
      label
    );
  const seg = h(
    'div',
    { class: 'seg', role: 'radiogroup', 'aria-label': 'Save as' },
    mk('video', 'Video', 'film'),
    mk('audio', 'Audio', 'music')
  );
  seg.children[0].dataset.kind = 'video';
  seg.children[1].dataset.kind = 'audio';
  return seg;
}
const saveChoice = () =>
  api('/api/settings', { kind: S.kind, quality: 'best', container: S.container, audio_format: S.afmt }).catch(
    () => {}
  );
const VFMT = [
  ['mp4', 'MP4'],
  ['mkv', 'MKV'],
  ['webm', 'WebM'],
  ['mov', 'MOV'],
  ['avi', 'AVI'],
];
const AFMT = [
  ['mp3', 'MP3'],
  ['m4a', 'M4A'],
  ['opus', 'Opus'],
  ['flac', 'FLAC'],
  ['wav', 'WAV'],
  ['ogg', 'OGG'],
];
const SPFMT = AFMT.filter(([v]) => ['mp3', 'm4a', 'flac'].includes(v));
function formatSelect(opts) {
  opts = opts || {};
  const sel = h('select', {
    class: 'sel fmt',
    'aria-label': 'File type',
    title: 'File type',
    onchange: e => {
      if (isAudio()) S.afmt = e.target.value;
      else S.container = e.target.value;
      saveChoice();
      sel.refresh(true);
    },
  });
  const isAudio = () => opts.audioOnly || S.kind === 'audio';
  sel.refresh = keep => {
    const list = isAudio() ? (opts.audioOnly ? SPFMT : AFMT) : VFMT,
      cur = isAudio() ? S.afmt : S.container;
    const val = list.some(([v]) => v === cur) ? cur : list[0][0];
    if (isAudio()) S.afmt = val;
    else S.container = val;
    const sz = opts.sizes ? opts.sizes(isAudio()) || {} : {};
    sel.replaceChildren(
      ...list.map(([v, l]) => {
        const n = sz[v];
        return h('option', { value: v, selected: v === val }, l + (n ? '  ·  ~' + fmtSize(n) : ''));
      })
    );
    sel.value = val;
    if (opts.onchange) opts.onchange();
  };
  if (opts.onopen)
    ['mouseenter', 'focus', 'touchstart', 'mousedown'].forEach(ev =>
      sel.addEventListener(ev, () => opts.onopen(), { passive: true })
    );
  sel.refresh();
  return sel;
}
function videoCard(d) {
  const isPage = d.type === 'page';
  const qwrap = h('div', { class: 'chips', role: 'radiogroup', 'aria-label': 'Quality' });
  const heights = (d.heights || []).slice(0, 7);
  // the quality we remembered might not exist for this video
  if (S.quality !== 'best' && !heights.includes(+S.quality)) S.quality = 'best';
  // sizes are per container since mp4, mkv and webm come out different
  const vsizes = () => (d.sizes_by && d.sizes_by[S.container]) || d.sizes || {};
  const mkChip = (val, label) => {
    const b = h(
      'button',
      {
        class: 'chip',
        role: 'radio',
        'aria-checked': String(S.quality === val),
        onclick: () => {
          S.quality = val;
          saveChoice();
          $$('.chip', qwrap).forEach(c => c.setAttribute('aria-checked', String(c.dataset.q === val)));
          paintTag();
        },
      },
      label,
      h('small', null)
    );
    b.dataset.q = val;
    return b;
  };
  qwrap.append(mkChip('best', heights.length ? 'Best · ' + qLabel(heights[0]) : 'Best'));
  heights.forEach(q => qwrap.append(mkChip(String(q), qLabel(q))));
  const tag = h('span', { class: 'sz num', title: 'Estimated size of this download' });
  const paintTag = () => {
    const n = S.kind === 'audio' ? (d.audio_sizes || {})[S.afmt] : vsizes()[S.quality] || vsizes().best;
    tag.textContent = n ? '~' + fmtSize(n) : '';
  };
  const paintSizes = () => {
    const z = vsizes();
    $$('.chip', qwrap).forEach(c => {
      const n = z[c.dataset.q];
      c.lastChild.textContent = n ? '~' + fmtSize(n) : '';
    });
    paintTag();
  };
  const showQ = () => {
    qwrap.hidden = S.kind === 'audio' || isPage || heights.length === 0;
    fsel && fsel.refresh();
  };
  const more = { go: () => {} };
  const fsel = formatSelect({
    sizes: au =>
      au
        ? d.audio_sizes
        : Object.fromEntries(VFMT.map(([v]) => [v, ((d.sizes_by || {})[v] || {})[S.quality]])),
    onchange: paintSizes,
    onopen: () => more.go(),
  });
  const seg = isPage ? null : kindSeg(showQ);
  showQ();
  // sizes arrive after the card, a quick first pass and then a full one if that was partial
  if (d.sizes_pending) {
    const tm = S.tm,
      ts = performance.now(),
      take = r => {
        d.sizes = r.sizes || {};
        d.sizes_by = r.sizes_by;
        d.audio_sizes = r.audio_sizes || {};
        paintSizes();
        fsel.refresh();
      };
    api('/api/sizes', { url: d.src })
      .then(r => {
        if (!r || !r.sizes_by) return;
        take(r);
        const t1 = performance.now();
        if (tm)
          api('/api/timing', {
            msg: `preview +${tm.prev != null ? (tm.prev / 1e3).toFixed(1) : 'none'} s | card +${tm.card != null ? (tm.card / 1e3).toFixed(1) : '?'} s | sizes shown +${((t1 - tm.t0) / 1e3).toFixed(1)} s (asked ${((ts - tm.t0) / 1e3).toFixed(1)} s, answered after ${((t1 - ts) / 1e3).toFixed(2)} s)`,
          }).catch(() => {});
        // the full pass only gets asked for once they open the format dropdown
        let asked = false;
        more.go = () => {
          if (asked || !r.partial) return;
          asked = true;
          api('/api/sizes', { url: d.src, full: true })
            .then(r2 => {
              if (r2 && r2.sizes_by) {
                take(r2);
                d.sizes_pending = false;
              }
            })
            .catch(() => {});
        };
        if (!r.partial) d.sizes_pending = false;
      })
      .catch(() => {});
  }
  const dl = h(
    'button',
    { class: 'btn big', onclick: () => downloadSingle(d, false) },
    ic('download'),
    'Download'
  );
  const info = h(
    'div',
    { class: 'info' },
    h('h2', null, d.title),
    h(
      'div',
      { class: 'meta' },
      [
        d.uploader,
        isPage
          ? `${d.count} video link${d.count === 1 ? '' : 's'} found on this page`
          : d.live
            ? 'Live now'
            : '',
      ]
        .filter(Boolean)
        .join('  ·  ')
    ),
    h('div', { class: 'opts' }, seg, qwrap, h('div', { class: 'row' }, fsel, dl, tag))
  );
  const card = h(
    'div',
    { class: 'vid' },
    devThumb(d.thumb, d.duration_text, { develop: true, adapt: true }),
    info
  );
  const wrap = h('div', null, card);
  if (d.existing || d.existing_audio) {
    const have = d.existing || d.existing_audio;
    wrap.append(
      h(
        'div',
        { class: 'note' },
        ic('check'),
        'You already saved this one.',
        h(
          'span',
          { class: 'links' },
          h('a', { onclick: () => api('/api/open', { path: have }) }, 'Play it'),
          h('a', { onclick: () => api('/api/reveal', { path: have }) }, 'Show in folder'),
          h('a', { onclick: () => downloadSingle(d, true) }, 'Download again')
        )
      )
    );
  }
  if (d.in_playlist) {
    wrap.append(
      h(
        'div',
        { class: 'note', style: 'background:var(--sunk)' },
        ic('list'),
        'This video is part of a playlist.',
        h(
          'span',
          { class: 'links' },
          h(
            'a',
            {
              onclick: () => {
                input.value = d.in_playlist.url;
                syncSlot();
                analyze(d.in_playlist.url);
              },
            },
            'See the whole playlist'
          )
        )
      )
    );
  }
  return wrap;
}
async function downloadSingle(d, force) {
  const item = {
    url: d.type === 'page' ? d.url : d.src,
    id: d.id,
    title: d.title,
    thumb: d.thumb,
    uploader: d.uploader,
    duration: d.duration || 0,
  };
  await api('/api/enqueue', {
    items: [item],
    kind: S.kind,
    container: S.container,
    audio_format: S.afmt,
    height: S.quality === 'best' ? null : +S.quality,
    force: !!force,
  });
  afterEnqueue();
}
function afterEnqueue() {
  input.value = '';
  padBox.value = '';
  $('#pad').classList.remove('done');
  manyCount();
  syncSlot();
  resetResult();
  if (S.many) setMany(false);
  const sc = $('#p-get');
  sc.scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' });
}
const jobRecs = new Map(); // one dom record per job id, reused between ticks
const isActive = j => ['queued', 'analyzing', 'downloading', 'processing', 'waiting'].includes(j.status);
function makeJobRec(j) {
  const dev = devThumb(j.thumb, '', { mini: true });
  const name = h('div', { class: 'name' }, j.title || j.url);
  const st = h('div', { class: 'st num' });
  const err = h('div');
  const acts = h('div', { class: 'acts' });
  const root = h(
    'div',
    { class: 'job' + (reduced ? '' : ' enter'), 'data-id': j.id },
    dev,
    h('div', { class: 't' }, name, st, err),
    acts
  );
  return { root, dev, name, st, err, acts, last: {} };
}
// state comes in constantly while downloading so only touch the dom when a value really changed
function setIf(rec, key, val, fn) {
  const k = JSON.stringify(val);
  if (rec.last[key] === k) return;
  rec.last[key] = k;
  fn();
}
function updateJobRec(rec, j) {
  const act = isActive(j);
  setIf(rec, 'name', j.title, () => (rec.name.textContent = j.title || j.url));
  // half percent steps, no point repainting more often than that
  setIf(rec, 'dev', [j.status, Math.round(j.progress * 200)], () => {
    if (j.status === 'done' || j.status === 'skipped') setProgress(rec.dev, 1, false);
    else if (j.status === 'downloading' || j.status === 'processing') setProgress(rec.dev, j.progress, true);
    else setProgress(rec.dev, 0, j.status !== 'failed' && j.status !== 'cancelled' ? true : false);
    rec.dev.classList.toggle('working', j.status === 'downloading' || j.status === 'processing');
    rec.dev.style.filter = j.status === 'failed' || j.status === 'cancelled' ? 'grayscale(1)' : '';
  });
  const pct = Math.round(j.progress * 100);
  // the ~ in front of the total means its a guess, j.exact is only true when every stream size is known
  const bits = [];
  if (j.message === 'Stopping' && isActive(j)) {
    bits.push(h('span', { class: 'dots' }, 'Stopping'));
  } else if (j.status === 'downloading' && j.message === 'Waiting for data') {
    bits.push(
      h('span', { style: 'color:var(--warn);font-weight:650' }, 'Waiting for data'),
      j.total ? fmtSize(j.downloaded) + ' of ' + (j.exact ? '' : '~') + fmtSize(j.total) : ''
    );
  } else if (j.status === 'downloading') {
    bits.push(
      h('b', null, pct + '%'),
      j.total ? fmtSize(j.downloaded) + ' of ' + (j.exact ? '' : '~') + fmtSize(j.total) : '',
      fmtSpeed(j.speed),
      fmtEta(j.eta)
    );
    if (j.attempt) bits.push(h('span', { style: 'color:var(--ink3)' }, j.attempt));
  } else if (j.status === 'processing') {
    bits.push(
      h('b', null, pct + '%'),
      h('span', { class: 'dots' }, (j.message || 'Finishing up').replace(/\.+$/, '')),
      fmtEta(j.eta)
    );
  } else if (j.status === 'done') {
    bits.push(
      h('span', { class: 'ok' }, 'Saved'),
      fmtSize(j.size),
      fmtTook(j.took) ? 'in ' + fmtTook(j.took) : ''
    );
    if (j.note) bits.push(h('span', { style: 'color:var(--warn);font-weight:600' }, j.note));
  } else if (j.status === 'skipped')
    bits.push(h('span', { class: 'ok' }, 'Already saved'), 'Nothing to download');
  else if (j.status === 'failed') bits.push(h('span', { class: 'no' }, "Didn't work"));
  else if (j.status === 'cancelled') bits.push('Cancelled');
  else
    bits.push(
      h(
        'span',
        { class: j.status === 'queued' ? '' : 'dots' },
        (j.message || 'Waiting in line').replace(/\.+$/, '').replace('...', '')
      )
    );
  const out = bits.filter(Boolean).map(b => (b.nodeType ? b : h('span', null, b)));
  setIf(rec, 'st', out.map(n => n.textContent).join('|') + '|' + j.status, () =>
    rec.st.replaceChildren(...out)
  );
  setIf(rec, 'err', [j.status, j.error && j.error.title], () => {
    rec.err.replaceChildren();
    if (j.status !== 'failed' || !j.error) return;
    const e = j.error,
      a = h('div', { class: 'acts' });
    if (e.action === 'cookies')
      a.append(
        h(
          'button',
          {
            class: 'btn small',
            onclick: () => {
              go('settings');
              setTimeout(() => $('#signin')?.scrollIntoView({ behavior: 'smooth' }), 400);
            },
          },
          'Set up sign-in'
        )
      );
    if (e.action === 'folder')
      a.append(
        h(
          'button',
          {
            class: 'btn small',
            onclick: async () => {
              await chooseFolder();
              api('/api/retry', { id: j.id });
            },
          },
          'Choose another folder'
        )
      );
    if (e.action === 'update')
      a.append(
        h(
          'button',
          {
            class: 'btn small',
            onclick: async ev => {
              ev.target.disabled = true;
              const r = await api('/api/update_engine', {});
              toast(r.ok ? 'good' : 'warn', r.text);
              api('/api/retry', { id: j.id });
            },
          },
          'Update and try again'
        )
      );
    a.append(
      h(
        'button',
        { class: 'btn small ghost', onclick: () => api('/api/retry', { id: j.id }) },
        ic('refresh'),
        'Try again'
      )
    );
    rec.err.append(h('div', { class: 'err' }, h('b', null, e.title), h('p', null, e.text), a));
  });
  setIf(rec, 'acts', [j.status, j.path], () => {
    rec.acts.replaceChildren();
    if (act)
      rec.acts.append(
        h(
          'button',
          {
            class: 'icon-btn',
            'aria-label': 'Cancel',
            title: 'Cancel',
            onclick: () => api('/api/cancel', { id: j.id }),
          },
          ic('x')
        )
      );
    else if (j.status === 'done' || j.status === 'skipped') {
      rec.acts.append(
        h(
          'button',
          { class: 'btn small', onclick: () => api('/api/open', { path: j.path }) },
          ic('play'),
          'Play'
        ),
        h(
          'button',
          {
            class: 'icon-btn',
            'aria-label': 'Show in folder',
            title: 'Show in folder',
            onclick: () => api('/api/reveal', { path: j.path }),
          },
          ic('folder')
        )
      );
      if (j.status === 'skipped')
        rec.acts.append(
          h(
            'button',
            {
              class: 'icon-btn',
              'aria-label': 'Download again',
              title: 'Download again',
              onclick: () => api('/api/retry', { id: j.id }),
            },
            ic('refresh')
          )
        );
    } else if (j.status === 'cancelled')
      rec.acts.append(
        h(
          'button',
          {
            class: 'icon-btn',
            'aria-label': 'Try again',
            title: 'Try again',
            onclick: () => api('/api/retry', { id: j.id }),
          },
          ic('refresh')
        )
      );
    if (!act)
      rec.acts.append(
        h(
          'button',
          {
            class: 'icon-btn',
            'aria-label': 'Remove from list',
            title: 'Remove from list',
            onclick: () => api('/api/remove', { id: j.id }),
          },
          ic('x')
        )
      );
  });
}
function renderTray() {
  const active = S.jobs.filter(isActive).sort((a, b2) => a.created - b2.created);
  const done = S.jobs
    .filter(j => !isActive(j))
    .sort((a, b2) => (b2.finished || 0) - (a.finished || 0))
    .slice(0, 12); // only the last 12 finished ones stay in the list
  const order = [...active, ...done];
  const list = $('#jobs');
  const keep = new Set(order.map(j => j.id));
  for (const [id, rec] of jobRecs)
    if (!keep.has(id)) {
      rec.root.remove();
      jobRecs.delete(id);
    }
  order.forEach((j, i) => {
    let rec = jobRecs.get(j.id);
    if (!rec) {
      rec = makeJobRec(j);
      jobRecs.set(j.id, rec);
    }
    updateJobRec(rec, j);
    // keep the same nodes and just reorder them
    if (list.children[i] !== rec.root) list.insertBefore(rec.root, list.children[i] || null);
  });
  $('#tray').hidden = order.length === 0;
  $('#getwrap').classList.toggle('compact', !!S.an || order.length > 0);
  const n = active.length;
  $('#trayTitle').textContent = n ? `Downloading ${n} ${plural(n, 'video', 'videos')}` : 'Finished';
  $('#cancelAll').hidden = n < 2;
  $('#clearDone').hidden = done.length === 0;
  const b = $('#badge');
  const was = b.hidden;
  b.hidden = n === 0;
  b.textContent = n;
  if (was !== b.hidden) requestAnimationFrame(movePill); // the badge changes the tab width
  updateRecent();
}
$('#cancelAll').addEventListener('click', () => api('/api/cancel', {}));
$('#clearDone').addEventListener('click', () => api('/api/remove', {}));
function updateRecent() {
  const show = !S.an && S.jobs.length === 0 && S.library.some(e => e.exists);
  $('#recent').hidden = !show;
  if (!show) return;
  const items = S.library.filter(e => e.exists).slice(0, 3);
  const key = items.map(e => e.path).join('|');
  if ($('#recentList').dataset.key === key) return; // same items, skip the rebuild
  $('#recentList').dataset.key = key;
  $('#recentList').replaceChildren(...items.map(libRow));
}
function libRow(e) {
  const dev = devThumb(e.thumb, '', { mini: true });
  return h(
    'div',
    { class: 'lib' + (e.exists ? '' : ' missing') },
    dev,
    h(
      'div',
      null,
      h('div', { class: 'name' }, e.title || e.path),
      h(
        'div',
        { class: 'sub' },
        [
          e.exists ? fmtSize(e.size) : 'File was moved or deleted',
          e.kind === 'audio' ? ((e.path || '').split('.').pop() || 'mp3').toUpperCase() : '',
          ago(e.t),
        ]
          .filter(Boolean)
          .join('  ·  ')
      )
    ),
    h(
      'div',
      { class: 'acts' },
      e.exists
        ? [
            h(
              'button',
              { class: 'btn small', onclick: () => api('/api/open', { path: e.path }) },
              ic('play'),
              'Play'
            ),
            h(
              'button',
              {
                class: 'icon-btn',
                title: 'Show in folder',
                'aria-label': 'Show in folder',
                onclick: () => api('/api/reveal', { path: e.path }),
              },
              ic('folder')
            ),
            h(
              'button',
              {
                class: 'icon-btn',
                title: 'Edit in Studio',
                'aria-label': 'Edit in Studio',
                onclick: () => openInStudio(e.path),
              },
              ic('scissors')
            ),
          ]
        : null,
      h(
        'button',
        {
          class: 'icon-btn',
          title: 'Remove from list',
          'aria-label': 'Remove from list',
          onclick: async () => {
            await api('/api/forget', { path: e.path });
            refreshFull();
          },
        },
        ic('x')
      )
    )
  );
}
