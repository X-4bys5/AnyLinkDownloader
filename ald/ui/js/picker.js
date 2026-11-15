'use strict';
const RH = 72; // row height in px, has to match .prow and .phd in the css
let vl = null; // the virtual list of the panel thats on screen right now
const CHECK = '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
function initPlaylist(d) {
  d.entries.forEach(e => {
    e.aonly = !!d.audio_only;
  });
  S.sel = new Set(d.entries.filter(e => !e.saved && !e.gone).map(e => e.i));
  S.filter = '';
  S.lastClick = -1;
  recomputeView(d);
}
function recomputeView(d) {
  const f = S.filter.trim().toLowerCase();
  S.view = f
    ? d.entries.filter(e => (e.title + ' ' + e.uploader).toLowerCase().includes(f))
    : d.rows || d.entries;
}
const hostOf = u => {
  try {
    const x = new URL(/^https?:/i.test(u) ? u : 'https://' + u);
    return x.hostname.replace(/^www\./, '') + (x.pathname.length > 1 ? x.pathname : '');
  } catch (e) {
    return u;
  }
};
function buildBatch(d) {
  const B = d.B;
  d.entries = [];
  d.rows = [];
  B.items.forEach((it, gi) => {
    const hdr = { hdr: true, gi, status: it.status, url: it.url };
    if (it.status === 'loading') {
      d.rows.push({ ...hdr, title: hostOf(it.url), sub: 'Reading this link' });
      return;
    }
    if (it.status === 'error') {
      d.rows.push({
        ...hdr,
        title: it.data.title || "Couldn't read this link",
        sub: hostOf(it.url) + '  ·  ' + (it.data.text || ''),
      });
      return;
    }
    // entries are built once, buildBatch runs again after every link finishes and the ids cant move
    if (!it.entries) {
      const r = it.data;
      // i is unique over the whole batch, n is the number inside its own playlist
      if (r.type === 'playlist')
        it.entries = r.entries.map(e => ({ ...e, n: e.i, i: ++B.n, aonly: !!r.audio_only, group: gi }));
      else
        it.entries = [
          {
            url: r.type === 'page' ? r.url : r.src,
            id: r.id,
            title: r.title,
            thumb: r.thumb,
            uploader: r.uploader,
            duration: r.duration || 0,
            duration_text: r.duration_text,
            saved: !!(r.existing || r.existing_audio),
            n: '',
            i: ++B.n,
            aonly: false,
            group: gi,
          },
        ];
      it.entries.forEach(e => {
        if (!e.saved && !e.gone) S.sel.add(e.i);
      });
    }
    if (it.data.type === 'playlist')
      d.rows.push({
        ...hdr,
        title: it.data.title,
        thumb: it.data.thumb,
        sub: [
          `${it.entries.length} ${plural(it.entries.length, ...(it.data.noun || ['video', 'videos']))}`,
          it.data.uploader,
        ]
          .filter(Boolean)
          .join('  ·  '),
        ids: it.entries.map(e => e.i),
      });
    d.rows.push(...it.entries);
    d.entries.push(...it.entries);
  });
  d.count = d.entries.length;
  const n = B.items.length,
    ok = B.items.filter(i => i.status === 'ok').length,
    bad = B.items.filter(i => i.status === 'error').length,
    wait = n - ok - bad;
  d.meta = [
    `${ok} of ${n} ${plural(n, 'link', 'links')} read`,
    wait ? `${wait} still loading` : '',
    bad ? `${bad} couldn't be read` : '',
  ]
    .filter(Boolean)
    .join('  ·  ');
}
async function fetchMany() {
  const links = extractLinks(padBox.value).slice(0, MAX_BATCH);
  if (!links.length) return;
  const tok = ++anTok;
  $('#pad').classList.add('done');
  const d = {
    type: 'playlist',
    batch: true,
    title: `${links.length} ${plural(links.length, 'link', 'links')}`,
    uploader: '',
    thumb: null,
    count: 0,
    entries: [],
    rows: [],
    B: { n: 0, items: links.map(u => ({ url: u, status: 'loading', data: null })) },
  };
  S.sel.clear();
  S.filter = '';
  S.lastClick = -1;
  buildBatch(d);
  recomputeView(d);
  S.an = { status: 'ok', data: d };
  renderResult();
  let k = 0;
  // 4 workers pulling links off the same list
  const lane = async () => {
    while (k < d.B.items.length) {
      const it = d.B.items[k++];
      try {
        const r = await api('/api/analyze', { url: it.url });
        if (tok !== anTok) return; // they cleared or started something else
        it.status = r.type === 'error' ? 'error' : 'ok';
        it.data = r;
      } catch (e) {
        if (tok !== anTok) return;
        it.status = 'error';
        it.data = { title: "The app didn't answer", text: 'Try again in a moment.' };
      }
      buildBatch(d);
      recomputeView(d);
      vlReset();
      updateCount();
      batchHead(d);
    }
  };
  await Promise.all([lane(), lane(), lane(), lane()]);
}
function batchHead(d) {
  const m = $('#plMeta');
  if (m) m.textContent = d.meta || '';
}
function retryGroup(d, gi) {
  const it = d.B.items[gi];
  it.status = 'loading';
  it.entries = null;
  buildBatch(d);
  recomputeView(d);
  vlReset();
  batchHead(d);
  api('/api/analyze', { url: it.url })
    .then(r => {
      it.status = r.type === 'error' ? 'error' : 'ok';
      it.data = r;
    })
    .catch(() => {
      it.status = 'error';
      it.data = { title: "The app didn't answer", text: 'Try again in a moment.' };
    })
    .finally(() => {
      buildBatch(d);
      recomputeView(d);
      vlReset();
      updateCount();
      batchHead(d);
    });
}
function playlistPanel(d) {
  const spacer = h('div', { class: 'pl-spacer' });
  const scroller = h('div', { class: 'pl-list', tabindex: '-1' }, spacer);
  vl = { d, spacer, scroller, rows: new Map() };
  scroller.addEventListener('scroll', () => requestAnimationFrame(vlRender), { passive: true });
  const q = h('input', {
    placeholder: d.batch ? 'Find in these links' : 'Find in this playlist',
    'aria-label': 'Find',
    oninput: e => {
      S.filter = e.target.value;
      recomputeView(d);
      vlReset();
      scroller.scrollTop = 0;
      updateCount();
    },
  });
  const count = h('div', { class: 'count num', id: 'plCount' });
  const dlBtn = h(
    'button',
    { class: 'btn big', id: 'plGo', onclick: downloadSelected },
    ic('download'),
    'Download'
  );
  const qsel = h(
    'select',
    {
      class: 'sel',
      'aria-label': 'Quality',
      onchange: e => {
        S.quality = e.target.value;
        saveChoice();
      },
    },
    ...['best', '2160', '1440', '1080', '720', '480', '360'].map(v =>
      h(
        'option',
        { value: v, selected: S.quality === v },
        v === 'best' ? 'Best quality' : qLabel(+v) + ' or lower'
      )
    )
  );
  const audioOnly = !!d.audio_only,
    noun = d.noun || ['video', 'videos'];
  const fsel = formatSelect({ audioOnly, onchange: () => updateCount() });
  const showQ = () => {
    qsel.hidden = audioOnly || S.kind === 'audio';
    fsel.refresh();
  };
  const head = d.batch
    ? h(
        'div',
        { class: 'pl-head' },
        h(
          'div',
          null,
          h('h2', { id: 'plTitle' }, d.title),
          h('div', { class: 'meta', id: 'plMeta' }, d.meta || '')
        ),
        h('div', { class: 'grow' }),
        count
      )
    : h(
        'div',
        { class: 'pl-head' },
        devThumb(d.thumb, '', { mini: true, develop: true }),
        h(
          'div',
          null,
          h('h2', null, d.title),
          h(
            'div',
            { class: 'meta' },
            [
              `${d.count} ${plural(d.count, noun[0], noun[1])}`,
              d.uploader,
              d.capped ? 'showing the first ' + d.count : '',
            ]
              .filter(Boolean)
              .join('  ·  ')
          ),
          d.note ? h('div', { class: 'meta' }, d.note) : null
        ),
        h('div', { class: 'grow' }),
        count
      );
  const pick = on => () => {
    S.view.forEach(e => {
      if (e.hdr || (on && (e.gone || e.saved))) return;
      on ? S.sel.add(e.i) : S.sel.delete(e.i);
    });
    vlRefreshSel();
  };
  const panel = h(
    'div',
    { class: 'pl' },
    head,
    h(
      'div',
      { class: 'pl-bar' },
      h('label', { class: 'search' }, ic('search'), q),
      h('button', { class: 'btn small soft', onclick: pick(true) }, 'Select all'),
      h('button', { class: 'btn small soft', onclick: pick(false) }, 'Select none')
    ),
    scroller,
    h(
      'div',
      { class: 'pl-foot' },
      audioOnly ? null : kindSeg(showQ),
      qsel,
      fsel,
      h('div', { class: 'grow' }),
      dlBtn
    )
  );
  qsel.hidden = audioOnly || S.kind === 'audio';
  // needs to be in the dom first or clientHeight is 0
  requestAnimationFrame(() => {
    vlReset();
    updateCount();
  });
  return panel;
}
function vlReset() {
  if (!vl) return;
  for (const el of vl.rows.values()) el.remove();
  vl.rows.clear();
  for (const x of [...vl.spacer.querySelectorAll('.empty')]) x.remove();
  // rows are absolutely positioned in the spacer and only the visible ones exist
  vl.spacer.style.height = S.view.length * RH + 'px';
  vlRender();
  if (!S.view.length)
    vl.spacer.replaceChildren(
      h(
        'div',
        { class: 'empty', style: 'padding:30px 24px' },
        S.filter ? 'Nothing matches that search.' : 'Nothing here yet.'
      )
    );
}
function vlRender() {
  if (!vl || !vl.scroller.isConnected) return;
  const st = vl.scroller.scrollTop,
    hh = vl.scroller.clientHeight || 500; // not laid out yet
  // 6 extra rows above and below so fast scrolling doesnt show gaps
  const first = Math.max(0, Math.floor(st / RH) - 6),
    last = Math.min(S.view.length - 1, Math.ceil((st + hh) / RH) + 6);
  for (const [i, el] of vl.rows)
    if (i < first || i > last) {
      el.remove();
      vl.rows.delete(i);
    }
  for (let i = first; i <= last; i++)
    if (!vl.rows.has(i)) {
      const el = S.view[i].hdr ? makeHeader(S.view[i], i) : makeRow(S.view[i], i);
      vl.rows.set(i, el);
      vl.spacer.append(el);
    }
}
function makeHeader(e, idx) {
  const d = vl.d,
    dev = devThumb(e.thumb, '', { mini: true });
  if (e.status === 'loading') dev.classList.add('load');
  const acts = h('div', { class: 'acts' });
  if (e.status === 'error')
    acts.append(
      h('button', { class: 'btn small soft', onclick: () => retryGroup(d, e.gi) }, ic('refresh'), 'Try again')
    );
  if (e.ids) {
    const all = () => e.ids.every(i => S.sel.has(i));
    const b = h('button', { class: 'btn small soft' }, all() ? 'Clear these' : 'Pick these');
    b.addEventListener('click', () => {
      const on = !all();
      const it = d.entries.filter(x => e.ids.includes(x.i));
      it.forEach(x => {
        if (on ? !x.gone && !x.saved : true) on ? S.sel.add(x.i) : S.sel.delete(x.i);
      });
      vlRefreshSel();
    });
    acts.append(b);
  }
  return h(
    'div',
    { class: 'phd' + (e.status === 'error' ? ' err' : ''), style: `top:${idx * RH}px`, 'data-gi': e.gi },
    e.status === 'error' ? h('div', { class: 'dev' }, h('div', { class: 'ph' }, ic('alert'))) : dev,
    h('div', { style: 'min-width:0' }, h('div', { class: 'gt' }, e.title), h('div', { class: 'sub' }, e.sub)),
    acts
  );
}
function makeRow(e, idx) {
  const sel = S.sel.has(e.i);
  const right = e.gone
    ? h('span', { class: 'tag off' }, 'Unavailable')
    : e.saved
      ? h('span', { class: 'tag' }, 'Saved')
      : h('span', { class: 'sub num' }, e.duration_text || '');
  const dev = devThumb(e.thumb, '', { mini: true });
  const pick = h('span', { class: 'pick' });
  pick.innerHTML = CHECK;
  dev.append(pick);
  const row = h(
    'div',
    {
      class: 'prow' + (sel ? ' sel' : '') + (e.gone ? ' gone' : ''),
      style: `top:${idx * RH}px`,
      role: 'checkbox',
      'aria-checked': String(sel),
      tabindex: '0',
    },
    h('span', { class: 'n num' }, e.n ?? e.i),
    dev,
    h(
      'div',
      { class: 'tt' },
      h('div', { class: 'title' }, e.title),
      h(
        'div',
        { class: 'sub' },
        [e.uploader, e.saved ? 'tick to download again' : ''].filter(Boolean).join('  ·  ')
      )
    ),
    right
  );
  const click = ev => {
    if (e.gone && !(ev.shiftKey && S.lastClick >= 0)) return;
    // shift click does the whole range, headers and unavailable rows get skipped
    if (ev.shiftKey && S.lastClick >= 0) {
      const [a, b] = [Math.min(S.lastClick, idx), Math.max(S.lastClick, idx)],
        on = e.gone ? true : !S.sel.has(e.i);
      for (let k = a; k <= b; k++) {
        const x = S.view[k];
        if (x.hdr || x.gone) continue;
        on ? S.sel.add(x.i) : S.sel.delete(x.i);
      }
    } else S.sel.has(e.i) ? S.sel.delete(e.i) : S.sel.add(e.i);
    S.lastClick = idx;
    vlRefreshSel();
  };
  row.addEventListener('click', click);
  row.addEventListener('keydown', ev => {
    if (ev.key === ' ' || ev.key === 'Enter') {
      ev.preventDefault();
      click(ev);
    } else if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      let k = idx + (ev.key === 'ArrowDown' ? 1 : -1);
      // arrow keys jump over the group headers
      while (S.view[k] && S.view[k].hdr) k += ev.key === 'ArrowDown' ? 1 : -1;
      const t = vl.rows.get(k);
      t && t.focus();
    }
  });
  return row;
}
function vlRefreshSel() {
  if (vl)
    for (const [i, el] of vl.rows) {
      const v = S.view[i];
      if (!v) continue;
      if (v.hdr) {
        const b = el.querySelector('button');
        if (b && v.ids) b.textContent = v.ids.every(x => S.sel.has(x)) ? 'Clear these' : 'Pick these';
        continue;
      }
      const s = S.sel.has(v.i);
      el.classList.toggle('sel', s);
      el.setAttribute('aria-checked', String(s));
    }
  updateCount();
}
// playlist entries only have a duration, so the size is a guess from rough bitrates (kbit/s)
const AKBPS = { mp3: 245, m4a: 256, ogg: 192, opus: 160, flac: 900, wav: 1411 };
const VKBPS = { best: 6e3, 2160: 16e3, 1440: 9e3, 1080: 5e3, 720: 2800, 480: 1300, 360: 700 };
// webm usually comes out a bit smaller and avi a lot bigger
const CFACTOR = { mp4: 1, mkv: 1, mov: 1, webm: 0.85, avi: 1.6 };
// kbit/s * 125 is bytes per second
const estBytes = (secs, audio) =>
  audio
    ? secs * (AKBPS[S.afmt] || 245) * 125
    : secs * (VKBPS[S.quality] || VKBPS.best) * 125 * (CFACTOR[S.container] || 1);
const isAudioEntry = e => !!e.aonly || S.kind === 'audio';
function updateCount() {
  const c = $('#plCount'),
    g = $('#plGo');
  if (!c || !vl) return;
  const d = vl.d,
    n = S.sel.size,
    total = d.entries.filter(e => !e.gone).length;
  const bytes = d.entries
    .filter(e => S.sel.has(e.i))
    .reduce((a, e) => a + (e.duration ? estBytes(e.duration, isAudioEntry(e)) : 0), 0);
  c.replaceChildren(
    h('b', null, n),
    h('span', null, ` of ${total} selected` + (bytes ? '  ·  ~' + fmtSize(bytes) : ''))
  );
  const nn = d.noun || ['video', 'videos'],
    allAudio = !!d.audio_only || S.kind === 'audio';
  g.replaceChildren(
    ic('download'),
    n === 0
      ? `Pick some ${d.batch ? 'items' : nn[1]}`
      : allAudio
        ? `Download ${n} as ${S.afmt.toUpperCase()}`
        : `Download ${n} ${plural(n, nn[0], nn[1])}`
  );
  g.disabled = n === 0;
}
async function downloadSelected() {
  const d = vl.d,
    chosen = d.entries.filter(e => S.sel.has(e.i));
  if (!chosen.length) return;
  const asItem = e => ({
    url: e.url,
    id: e.id,
    title: e.title,
    thumb: e.thumb,
    uploader: e.uploader,
    duration: e.duration,
    i: e.n || e.i,
    force: e.saved,
  });
  const base = {
    container: S.container,
    audio_format: S.afmt,
    height: S.quality === 'best' ? null : +S.quality,
  };
  let added = 0,
    skipped = 0;
  // audio only entries (spotify songs) go in their own enqueue since they always save as audio
  for (const songs of [true, false]) {
    const items = chosen.filter(e => !!e.aonly === songs).map(asItem);
    if (!items.length) continue;
    const r = await api('/api/enqueue', {
      items,
      kind: songs ? 'audio' : S.kind,
      ...base,
      ...(d.batch ? {} : { playlist: { title: d.title } }),
    });
    added += r.added || 0;
    skipped += r.skipped || 0;
  }
  afterEnqueue();
  toast(
    'good',
    skipped ? `Added ${added}. ${skipped} were already in your list.` : `Added ${added} to your downloads.`
  );
}
