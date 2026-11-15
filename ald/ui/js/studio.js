'use strict';
// studio tab, the editor. the backend side is ald/studio.py
const ST = {
  src: {},
  segs: [],
  sel: null,
  T: 0,
  pps: 80, // pixels per second, this is the zoom
  fit: true,
  playing: false,
  ci: 0,
  undo: [],
  redo: [],
  vids: {},
  vol: true,
  use: {},
  useN: 0,
  away: true, // true while another tab is open, nothing loads in the background then
  fmt: 'same',
  q: 'original',
  maxh: 0,
  mode: 'exact',
  noaudio: false,
  name: '',
  dir: '',
  plan: null,
  planErr: '',
  planTok: 0, // goes up on every plan request so old answers can be ignored
  job: null,
  caps: null,
  ready: false,
};
const MIN_LEN = 0.04; // shortest clip allowed, about one frame
const uid = () => Math.random().toString(36).slice(2, 9);
const stSrc = s => ST.src[s.sid];
const sLen = s => s.out - s.in;
const stOffs = () => {
  let o = 0;
  return ST.segs.map(s => {
    const r = o;
    o += sLen(s);
    return r;
  });
};
const stTotal = () => ST.segs.reduce((a, s) => a + sLen(s), 0);
const stFps = s => (stSrc(s) && stSrc(s).fps) || 25;
const quant = (t, fps) => Math.round(t * fps) / fps;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const isAudioProject = () => ST.segs.length > 0 && stSrc(ST.segs[0]).kind === 'audio';
function tcs(t, withMs = true) {
  t = Math.max(0, t);
  const h2 = Math.floor(t / 3600),
    m = Math.floor(t / 60) % 60,
    s = t - h2 * 3600 - m * 60;
  const ss = withMs ? s.toFixed(3).padStart(6, '0') : String(Math.floor(s)).padStart(2, '0');
  return h2 ? `${h2}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}
function parseTc(str) {
  const p = String(str).trim().replace(',', '.').split(':');
  if (!p.length || p.length > 3) return NaN;
  let v = 0;
  for (const x of p) {
    if (!/^\d*\.?\d*$/.test(x) || x === '') return NaN;
    v = v * 60 + parseFloat(x);
  }
  return v;
}
function locate(T) {
  const o = stOffs();
  for (let i = ST.segs.length - 1; i >= 0; i--)
    if (T >= o[i] - 1e-6) {
      const s = ST.segs[i];
      return { i, seg: s, off: o[i], t: s.in + Math.min(T - o[i], sLen(s)) };
    }
  return ST.segs.length ? { i: 0, seg: ST.segs[0], off: 0, t: ST.segs[0].in } : null;
}
// undo only keeps the clip list and the selection, the files themselves never change
const snapState = () => JSON.stringify({ segs: ST.segs, sel: ST.sel });
function pushUndo(before) {
  ST.undo.push(before || snapState());
  if (ST.undo.length > 150) ST.undo.shift(); // dont let the undo list grow forever
  ST.redo = [];
  syncBar();
}
function restore(js) {
  const o = JSON.parse(js);
  ST.segs = o.segs;
  ST.sel = o.sel;
  ST.T = clamp(ST.T, 0, stTotal());
  renderAll();
}
function stUndo() {
  if (!ST.undo.length) return;
  ST.redo.push(snapState());
  restore(ST.undo.pop());
}
function stRedo() {
  if (!ST.redo.length) return;
  ST.undo.push(snapState());
  restore(ST.redo.pop());
}
function edit(fn) {
  const b = snapState();
  fn();
  pushUndo(b);
  renderAll();
}
async function stCaps() {
  if (ST.caps) return;
  try {
    ST.caps = (await api('/api/studio/caps', {})).formats;
  } catch (e) {
    ST.caps = [];
  }
  buildFormats();
}
function addSources(list) {
  let added = 0;
  for (const info of list) {
    // audio and video clips cant be mixed in one project
    if (ST.segs.length && (ST.segs[0] && stSrc(ST.segs[0]).kind) !== info.kind) {
      toast('warn', "Audio-only files and videos can't be joined. Start over to open this one.");
      api('/api/studio/close', { id: info.id }).catch(() => {});
      continue;
    }
    ST.src[info.id] = Object.assign(ST.src[info.id] || {}, info);
    const seg = {
      id: uid(),
      sid: info.id,
      in: 0,
      out: info.duration,
      rot: 0,
      hf: false,
      vf: false,
      mute: false,
    };
    if (!added) pushUndo();
    ST.segs.push(seg);
    ST.sel = seg.id;
    added++;
    // files the browser cant play directly get a small preview copy made by the server
    if (info.mode !== 'direct' && info.prep !== 'ready')
      ST.src[info.id].prep = ST.src[info.id].prep || 'working';
    if (info.hdr)
      toast(
        'warn',
        info.name + ' looks like HDR video. It plays fine, but converting can make colors look flat.'
      );
  }
  if (!added) return;
  ST.fit = true;
  if (ST.segs.length === added) ST.T = 0;
  else ST.T = stOffs()[ST.segs.length - added];
  showEditor();
  renderAll();
  stSeek(ST.T);
}
async function stPick() {
  try {
    const r = await api('/api/studio/pick', {});
    if (r.unavailable) {
      // no native file dialog here, use the normal file input instead
      $('#stFile').click();
      return;
    }
    (r.errors || []).forEach(m => toast('warn', m));
    if (r.sources && r.sources.length) addSources(r.sources);
  } catch (e) {
    toast('warn', "Couldn't open the file picker.");
  }
}
async function openInStudio(path) {
  go('studio');
  try {
    const r = await api('/api/studio/open', { path });
    if (!r.ok) {
      toast('warn', r.error || "Couldn't open that file.");
      return;
    }
    addSources([r.source]);
  } catch (e) {
    toast('warn', "Couldn't open that file.");
  }
}
let stUpBusy = false;
async function stUpload(files) {
  files = [...files];
  if (!files.length) return;
  go('studio');
  if (stUpBusy) {
    toast('', 'Still copying the last file. Try again in a moment.');
    return;
  }
  stUpBusy = true;
  const box = $('#stUpl');
  try {
    for (const f of files) {
      box.classList.add('on');
      $('#stUplT').textContent = 'Opening ' + f.name;
      $('#stUplB').style.width = '0%';
      const info = await new Promise((res, rej) => {
        const x = new XMLHttpRequest();
        // xhr instead of fetch because fetch has no upload progress
        x.open('PUT', '/api/studio/upload?name=' + encodeURIComponent(f.name));
        x.setRequestHeader('X-ALD', '1');
        x.upload.onprogress = e => {
          if (e.lengthComputable) $('#stUplB').style.width = (e.loaded / e.total) * 100 + '%';
        };
        x.onload = () => {
          try {
            res(JSON.parse(x.responseText));
          } catch (e) {
            rej(e);
          }
        };
        x.onerror = () => rej(new Error('upload'));
        x.send(f);
      });
      if (info.ok) addSources([info.source]);
      else toast('warn', info.error || "Couldn't open " + f.name);
    }
  } catch (e) {
    toast('warn', "Couldn't copy that file in.");
  }
  box.classList.remove('on');
  stUpBusy = false;
}
function showEditor() {
  $('#stEmpty').hidden = true;
  $('#stApp').hidden = false;
}
function showEmpty() {
  $('#stApp').hidden = true;
  $('#stEmpty').hidden = false;
  renderEmpty();
}
function renderEmpty() {
  const items = (S.library || []).filter(e => e.exists).slice(0, 4);
  $('#stRecent').hidden = !items.length;
  $('#stRecentList').replaceChildren(
    ...items.map(e =>
      h(
        'div',
        { class: 'lib' },
        devThumb(e.thumb, '', { mini: true }),
        h(
          'div',
          null,
          h('div', { class: 'name' }, e.title || e.path),
          h('div', { class: 'sub' }, [fmtSize(e.size), ago(e.t)].filter(Boolean).join('  ·  '))
        ),
        h(
          'div',
          { class: 'acts' },
          h('button', { class: 'btn small', onclick: () => openInStudio(e.path) }, ic('scissors'), 'Edit')
        )
      )
    )
  );
}
async function stReset() {
  stPause();
  tqAbort();
  for (const id of Object.keys(ST.src)) api('/api/studio/close', { id }).catch(() => {});
  Object.values(ST.vids).forEach(v => {
    v.pause();
    v.removeAttribute('src');
    v.remove();
  });
  Object.assign(ST, {
    src: {},
    segs: [],
    sel: null,
    T: 0,
    undo: [],
    redo: [],
    vids: {},
    use: {},
    plan: null,
    planErr: '',
    job: null,
    name: '',
    dir: '',
    fmt: 'same',
    q: 'original',
    maxh: 0,
    mode: 'exact',
    noaudio: false,
  });
  showEmpty();
  syncBar();
}
const MAXVID = 2; // video elements kept loaded at once, more than that eats memory
function vidFor(sid) {
  let v = ST.vids[sid];
  if (!v) {
    v = h('video', { preload: 'auto', playsinline: '', 'aria-hidden': 'true' });
    v.disablePictureInPicture = true;
    ST.vids[sid] = v;
    $('#stStage').insertBefore(v, $('#stTag'));
    v.addEventListener('error', () => {
      const s = ST.src[sid];
      if (!s || !v.dataset.set) return;
      // playing the original failed, switch to the preview copy once
      if (s.mode === 'direct' && !s.fell) {
        s.fell = true;
        s.mode = 'proxy';
        s.prep = 'working';
        s.prepPct = 0;
        s.prepFail = false;
        delete v.dataset.set;
        v.removeAttribute('src');
        v.load();
        syncOverlay();
        api('/api/studio/proxy', { id: sid }).catch(() => {
          s.prep = 'failed';
          s.prepFail = true;
          syncOverlay();
        });
      } else {
        s.prepFail = true;
        syncOverlay();
      }
    });
  }
  ST.use[sid] = ++ST.useN;
  attachSrc(sid);
  trimVids();
  return v;
}
function srcReady(s) {
  return s && (s.mode === 'direct' || s.prep === 'ready');
}
function attachSrc(sid) {
  const s = ST.src[sid],
    v = ST.vids[sid];
  if (!s || !v || !srcReady(s) || v.dataset.set || ST.away) return;
  v.dataset.set = '1';
  v.src = '/api/studio/media?id=' + sid;
  v.load();
}
function releaseVid(sid) {
  const v = ST.vids[sid];
  if (!v || !v.dataset.set) return;
  v.pause();
  delete v.dataset.set;
  delete v.dataset.pre;
  v.removeAttribute('src');
  v.load();
}
function trimVids() {
  Object.keys(ST.vids)
    .filter(id => ST.vids[id].dataset.set)
    .sort((a, b) => (ST.use[b] || 0) - (ST.use[a] || 0))
    .slice(MAXVID)
    .forEach(releaseVid);
}
// thumbnail queue, only 2 load at a time and only the ones that are on screen
const TQ = { act: new Set(), q: [], max: 2, io: null, fresh: [] };
function tqPump() {
  while (!ST.away && TQ.act.size < TQ.max) {
    const i = TQ.q.findIndex(j => j.vis);
    if (i < 0) return;
    const job = TQ.q.splice(i, 1)[0];
    TQ.act.add(job);
    job.run(() => {
      if (job.dead) return;
      TQ.act.delete(job);
      tqPump();
    });
  }
}
function tqAbort() {
  if (TQ.io) {
    TQ.io.disconnect();
    TQ.io = null;
  }
  TQ.q.length = 0;
  TQ.fresh.length = 0;
  for (const j of TQ.act) {
    j.dead = true;
    j.abort();
  }
  TQ.act.clear();
}
function tqStart() {
  // no observer support, treat every thumbnail as visible
  if (!('IntersectionObserver' in window))
    TQ.fresh.forEach(j => {
      j.vis = true;
    });
  else {
    // only load thumbnails near the visible part of the timeline
    TQ.io = new IntersectionObserver(
      es2 => {
        for (const e of es2) {
          const j = e.target._job;
          if (!j) continue;
          j.vis = e.isIntersecting;
          if (j.vis && !j.queued) {
            j.queued = true;
            TQ.q.push(j);
          }
        }
        tqPump();
      },
      { root: $('#tlScroll'), rootMargin: '0px 400px' }
    );
    TQ.fresh.forEach(j => {
      if (j.el) TQ.io.observe(j.el);
    });
  }
  TQ.fresh.forEach(j => {
    if (!j.el || !TQ.io) {
      j.queued = true;
      TQ.q.push(j);
    }
  });
  TQ.fresh.length = 0;
  tqPump();
}
function tqAdd(job) {
  TQ.fresh.push(job);
}
function stLeave() {
  stPause();
  ST.away = true;
  tqAbort();
  Object.keys(ST.vids).forEach(releaseVid);
  if (planAC) {
    planAC.abort();
    planAC = null;
  }
}
function layoutVideo(v, seg) {
  const box = $('#stStage'),
    W = box.clientWidth,
    H = box.clientHeight,
    s = stSrc(seg);
  if (!s || !W || !H) return;
  // rotated by 90 or 270, so width and height trade places when fitting
  const swap = seg.rot % 180 !== 0,
    dw = swap ? s.height : s.width,
    dh = swap ? s.width : s.height,
    k = Math.min(W / dw, H / dh);
  v.style.width = s.width * k + 'px';
  v.style.height = s.height * k + 'px';
  v.style.transform = `translate(-50%,-50%) rotate(${seg.rot}deg) scale(${seg.hf ? -1 : 1},${seg.vf ? -1 : 1})`;
}
function activeSeg() {
  return ST.segs[ST.ci] || null;
}
function showAt(T, fromPlay) {
  const loc = locate(T);
  if (!loc) return;
  ST.ci = loc.i;
  const seg = loc.seg,
    s = stSrc(seg),
    v = vidFor(seg.sid);
  for (const [id, el] of Object.entries(ST.vids))
    el.classList.toggle('on', id === seg.sid && s.kind === 'video');
  $('#stWaveCard').classList.toggle('on', s.kind === 'audio');
  $('#stWaveName').textContent = s.name;
  layoutVideo(v, seg);
  v.muted = seg.mute || !ST.vol;
  const fps = s.fps || 25,
    t = Math.min(loc.t, seg.out - 0.5 / fps); // stay half a frame before the end of the clip
  // only seek when its actually off, constant tiny seeks make scrubbing choppy
  if (!fromPlay && srcReady(s) && Math.abs(v.currentTime - t) > 0.012) {
    try {
      v.currentTime = Math.max(0, t);
    } catch (e) {}
  }
  const tag = $('#stTag');
  tag.hidden = ST.segs.length < 2;
  tag.textContent = `Clip ${loc.i + 1} of ${ST.segs.length}`;
  syncOverlay();
  setPH();
}
function syncOverlay() {
  const seg = activeSeg(),
    ov = $('#stOvl');
  if (!seg) return;
  const s = stSrc(seg);
  if (s.prep === 'failed' || s.prepFail) {
    ov.className = 'ovl on';
    ov.replaceChildren(
      h(
        'div',
        null,
        "This file can't be previewed here",
        h('small', null, 'You can still cut and export it. The timeline still shows the right times.')
      )
    );
    return;
  }
  if (srcReady(s)) {
    ov.className = 'ovl';
    return;
  }
  const pct = Math.round((s.prepPct || 0) * 100);
  ov.className = 'ovl on';
  ov.replaceChildren(
    h(
      'div',
      null,
      'Making a smooth preview...',
      h('div', { class: 'bar' }, h('i', { style: `width:${pct}%` })),
      h(
        'small',
        null,
        "Your browser can't play this kind of file directly, so a small preview copy is made. The export always uses the original."
      )
    )
  );
}
function setPH() {
  const x = ST.T * ST.pps;
  $('#tlPh').style.left = x + 'px';
  $('#tTc').innerHTML = `${tcs(ST.T)} <small>/ ${tcs(stTotal())}</small>`;
  if (ST.playing) {
    const sc = $('#tlScroll');
    // keep the playhead in view while playing
    if (x > sc.scrollLeft + sc.clientWidth - 50) sc.scrollLeft = x - 60;
    else if (x < sc.scrollLeft) sc.scrollLeft = Math.max(0, x - 60);
  }
}
function stSeek(T, keepPlay) {
  const was = ST.playing;
  if (ST.playing) stPause();
  ST.T = clamp(T, 0, stTotal());
  showAt(ST.T);
  if (keepPlay && was) stPlay();
}
function stPlay() {
  if (!ST.segs.length) return;
  if (ST.T >= stTotal() - 0.02) ST.T = 0; // already at the end, start over
  showAt(ST.T);
  const seg = activeSeg(),
    v = vidFor(seg.sid),
    loc = locate(ST.T);
  if (!srcReady(stSrc(seg))) {
    toast('', 'The preview is still being made.');
    return;
  }
  v.muted = seg.mute || !ST.vol;
  try {
    v.currentTime = loc.t;
  } catch (e) {}
  const p = v.play();
  if (p && p.catch) p.catch(() => {});
  ST.playing = true;
  syncPlayBtn();
  requestAnimationFrame(tick);
}
function stPause() {
  ST.playing = false;
  Object.values(ST.vids).forEach(v => v.pause());
  syncPlayBtn();
}
function syncPlayBtn() {
  const b = $('#tPlay');
  b.replaceChildren(ic(ST.playing ? 'pause' : 'play'));
  b.setAttribute('aria-label', ST.playing ? 'Pause' : 'Play');
}
function tick() {
  if (!ST.playing) return;
  const seg = activeSeg();
  if (!seg) {
    stPause();
    return;
  }
  const v = vidFor(seg.sid),
    offs = stOffs(),
    fps = stFps(seg);
  const up = ST.segs[ST.ci + 1];
  // next clip is from another file, seek its video to the start 1.5s early so the switch is instant
  if (up && up.sid !== seg.sid && seg.out - v.currentTime < 1.5) {
    const pv = vidFor(up.sid);
    if (pv.dataset.pre !== up.id) {
      pv.dataset.pre = up.id;
      try {
        pv.currentTime = up.in;
      } catch (e) {}
    }
    vidFor(seg.sid);
  }
  if (v.currentTime >= seg.out - 0.5 / fps || v.ended) {
    const next = ST.segs[ST.ci + 1];
    if (!next) {
      ST.T = stTotal();
      stPause();
      showAt(ST.T);
      return;
    }
    ST.ci++;
    // same file and it starts where this one ends, then just keep playing without a seek
    const nv = vidFor(next.sid),
      contiguous = next.sid === seg.sid && Math.abs(next.in - seg.out) < 0.06;
    if (!contiguous) {
      v.pause();
      try {
        nv.currentTime = next.in;
      } catch (e) {}
      nv.muted = next.mute || !ST.vol;
      const p = nv.play();
      if (p && p.catch) p.catch(() => {});
    } else v.muted = next.mute || !ST.vol;
    for (const [id, el] of Object.entries(ST.vids))
      el.classList.toggle('on', id === next.sid && stSrc(next).kind === 'video');
    layoutVideo(nv, next);
    $('#stWaveCard').classList.toggle('on', stSrc(next).kind === 'audio');
    $('#stWaveName').textContent = stSrc(next).name;
    $('#stTag').textContent = `Clip ${ST.ci + 1} of ${ST.segs.length}`;
    selectSeg(next.id, true);
  }
  const cur = activeSeg();
  const cv = vidFor(cur.sid);
  ST.T = clamp(offs[ST.ci] + (cv.currentTime - cur.in), 0, stTotal());
  setPH();
  requestAnimationFrame(tick);
}
function stepFrame(n) {
  const loc = locate(ST.T);
  if (!loc) return;
  const fps = loc.seg ? stFps(loc.seg) : 25;
  stSeek(quant(ST.T + n / fps, fps));
}
const selSeg = () => ST.segs.find(s => s.id === ST.sel) || null;
function selectSeg(id, quiet) {
  ST.sel = id;
  $$('.tclip').forEach(el => el.classList.toggle('sel', el.dataset.id === id));
  syncClip();
  syncBar();
}
function stSplit() {
  const loc = locate(ST.T);
  if (!loc) return;
  const seg = loc.seg,
    fps = stFps(seg),
    t = quant(loc.t, fps);
  if (t - seg.in < MIN_LEN || seg.out - t < MIN_LEN) {
    // playhead is at the edge of the clip, nothing to split
    toast('', 'Move the playhead inside a clip, then split.');
    return;
  }
  const was = ST.playing;
  if (was) stPause();
  edit(() => {
    const b = { ...seg, id: uid(), in: t };
    seg.out = t;
    ST.segs.splice(ST.segs.indexOf(seg) + 1, 0, b);
    ST.sel = b.id;
  });
  ST.T = Math.min(ST.T, stTotal());
  showAt(ST.T);
}
function stDelete() {
  const seg = selSeg();
  if (!seg) return;
  if (ST.playing) stPause();
  const i = ST.segs.indexOf(seg),
    b = snapState();
  ST.segs.splice(i, 1);
  pushUndo(b);
  if (!ST.segs.length) {
    ST.sel = null;
    stPause();
    tqAbort();
    Object.keys(ST.vids).forEach(releaseVid);
    ST.plan = null;
    ST.planErr = '';
    showEmpty();
    schedulePlan();
    return;
  }
  ST.sel = ST.segs[Math.min(i, ST.segs.length - 1)].id;
  ST.T = clamp(stOffs()[Math.min(i, ST.segs.length - 1)], 0, stTotal());
  renderAll();
  showAt(ST.T);
}
function stSetEdge(side) {
  const loc = locate(ST.T),
    seg = selSeg();
  if (!loc || !seg || loc.seg !== seg) {
    toast('', 'Put the playhead inside the selected clip first.');
    return;
  }
  const t = quant(loc.t, stFps(seg));
  if (side === 'in') {
    if (seg.out - t < MIN_LEN) return;
    edit(() => {
      seg.in = Math.max(0, t);
    });
    ST.T = stOffs()[ST.segs.indexOf(seg)];
  } else {
    if (t - seg.in < MIN_LEN) return;
    edit(() => {
      seg.out = Math.min(stSrc(seg).duration, t);
    });
    ST.T = stOffs()[ST.segs.indexOf(seg)] + sLen(seg);
  }
  showAt(ST.T);
}
function stMove(dir) {
  const seg = selSeg();
  if (!seg) return;
  const i = ST.segs.indexOf(seg),
    j = i + dir;
  if (j < 0 || j >= ST.segs.length) return;
  edit(() => {
    ST.segs.splice(i, 1);
    ST.segs.splice(j, 0, seg);
  });
  ST.T = stOffs()[j];
  showAt(ST.T);
}
function xform(fn, all) {
  const seg = selSeg();
  if (!seg) return;
  edit(() => {
    (all ? ST.segs : [seg]).forEach(s => fn(s, seg));
  });
  showAt(ST.T);
}
const rotBy = d => s => {
  s.rot = (s.rot + d + 360) % 360;
};
// picks the ruler spacing so the labels stay about 84px apart
function niceStep(pps) {
  for (const s of [0.04, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600])
    if (s * pps >= 84) return s;
  return 7200;
}
function drawRuler() {
  const el = $('#tlRuler'),
    w = Math.max($('#tlScroll').clientWidth, stTotal() * ST.pps + 120),
    step = niceStep(ST.pps),
    kids = [];
  for (let t = 0; t * ST.pps < w && kids.length < 600; t += step / 5) {
    // 5 small ticks per label, capped at 600 so a long file doesnt make thousands of elements
    const maj = Math.abs(Math.round(t / step) * step - t) < step / 50;
    kids.push(
      h(
        'i',
        { class: 'tk' + (maj ? ' maj' : ''), style: `left:${t * ST.pps}px` },
        maj ? h('span', null, tcs(t, step < 1)) : null
      )
    );
  }
  el.replaceChildren(...kids);
}
function layoutTL() {
  const o = stOffs(),
    tot = stTotal();
  $('#tlInner').style.width = Math.max($('#tlScroll').clientWidth, tot * ST.pps + 120) + 'px';
  $$('.tclip').forEach(el => {
    const i = ST.segs.findIndex(s => s.id === el.dataset.id);
    if (i < 0) return;
    el.style.left = o[i] * ST.pps + 'px';
    el.style.width = Math.max(6, sLen(ST.segs[i]) * ST.pps) + 'px';
  });
  setPH();
}
function buildClip(seg) {
  const s = stSrc(seg),
    w = Math.max(6, sLen(seg) * ST.pps);
  const strip = h('div', { class: 'strip' + (s.kind === 'audio' ? ' aud' : '') });
  if (s.kind === 'audio') {
    // audio clips draw the waveform image as a mask over the strip
    const u = `url(/api/studio/wave?id=${s.id}&w=2400&h=58)`;
    strip.style.cssText = `-webkit-mask-size:${s.duration * ST.pps}px 100%;mask-size:${s.duration * ST.pps}px 100%;-webkit-mask-position:${-seg.in * ST.pps}px 0;mask-position:${-seg.in * ST.pps}px 0`;
    let im = null;
    tqAdd({
      vis: true,
      run(done) {
        im = new Image();
        const fin = () => {
          strip.style.webkitMaskImage = strip.style.maskImage = u;
          strip.classList.add('ld');
          done();
        };
        im.onload = fin;
        im.onerror = fin;
        im.src = `/api/studio/wave?id=${s.id}&w=2400&h=58`;
      },
      abort() {
        if (im) {
          im.onload = im.onerror = null;
          im.src = '';
        }
      },
    });
  } else {
    // one thumbnail every tw pixels along the clip, 500 at most
    const asp = clamp((s.width || 16) / (s.height || 9), 0.5, 2.2),
      tw = Math.round(58 * asp),
      n = Math.min(500, Math.ceil(w / tw));
    for (let k = 0; k < n; k++) {
      const t = clamp(seg.in + ((k + 0.5) * tw) / ST.pps, seg.in, seg.out - 0.02);
      const img = h('img', { width: tw, height: 58, alt: '', draggable: 'false' });
      img.style.transform = `${seg.rot === 180 ? 'rotate(180deg) ' : ''}${seg.hf ? 'scaleX(-1) ' : ''}${seg.vf ? 'scaleY(-1)' : ''}`;
      const src = `/api/studio/frame?id=${s.id}&t=${t.toFixed(1)}&h=116`; // 116 is twice the strip height
      const job = {
        vis: false,
        el: img,
        run(done) {
          const fin = () => {
            img.classList.add('ld');
            done();
          };
          img.onload = fin;
          img.onerror = fin;
          img.src = src;
        },
        abort() {
          img.onload = img.onerror = null;
          img.removeAttribute('src');
        },
      };
      img._job = job;
      tqAdd(job);
      strip.append(img);
    }
  }
  const badges = [
    seg.rot ? [ic('rotr'), seg.rot + '°'] : null,
    seg.hf ? ic('fliph') : null,
    seg.vf ? ic('flipv') : null,
    seg.mute ? ic('mute') : null,
  ];
  const el = h(
    'div',
    { class: 'tclip' + (seg.id === ST.sel ? ' sel' : ''), 'data-id': seg.id, title: s.name },
    strip,
    h(
      'div',
      { class: 'lbl' },
      h('span', null, s.name.replace(/\.[^.]+$/, '')),
      h('small', { class: 'num' }, tcs(sLen(seg), sLen(seg) < 60)),
      badges
    ),
    h('i', { class: 'hdl l' }),
    h('i', { class: 'hdl r' })
  );
  el.addEventListener('pointerdown', e => clipDown(e, seg, el));
  return el;
}
function renderTL() {
  tqAbort();
  drawRuler();
  $('#tlTrack').replaceChildren(...ST.segs.map(buildClip));
  layoutTL();
  if (!ST.away) tqStart();
}
function fitZoom() {
  const t = stTotal() || 1,
    w = $('#tlScroll').clientWidth - 70;
  ST.pps = clamp(w / t, 1.2, 900);
}
function setZoom(p, anchorX) {
  const sc = $('#tlScroll'),
    ax = anchorX == null ? sc.clientWidth / 2 : anchorX,
    tAt = (sc.scrollLeft + ax) / ST.pps;
  ST.pps = clamp(p, 1.2, 900);
  ST.fit = false;
  renderTL();
  sc.scrollLeft = tAt * ST.pps - ax;
}
function renderAll() {
  // cancel the snap animation left over from a trim
  clearTimeout(trimSnap);
  if (!ST.segs.length) return;
  if (ST.fit) fitZoom();
  renderTL();
  syncClip();
  syncBar();
  syncExport();
  schedulePlan();
  if (ST.T > stTotal()) ST.T = stTotal();
  showAt(ST.T);
}
const xToT = x => (x - $('#tlInner').getBoundingClientRect().left) / ST.pps;
function clipDown(e, seg, el) {
  if (e.button !== 0) return;
  e.preventDefault();
  const was = ST.playing;
  if (was) stPause();
  selectSeg(seg.id);
  const hd = e.target.closest('.hdl');
  dragBegin(e, hd ? 'd-resize' : null);
  if (hd) return startTrim(e, seg, el, hd.classList.contains('l') ? 'l' : 'r', was);
  startPress(e, seg, el, was);
}
let trimSnap = 0;
// dragging a clip handle. only styles move while dragging, the real redraw happens on release
function startTrim(e, seg, el, side, was) {
  const s = stSrc(seg),
    fps = s.fps || 25,
    x0 = e.clientX,
    o0 = { in: seg.in, out: seg.out },
    before = snapState();
  const idx = ST.segs.indexOf(seg),
    offs0 = stOffs(),
    off = offs0[idx],
    w0 = parseFloat($('#tlInner').style.width) || 0;
  el.setPointerCapture(e.pointerId);
  const elOf = sg => $$('.tclip').find(c => c.dataset.id === sg.id);
  clearTimeout(trimSnap);
  const mv = ev => {
    const dt = (ev.clientX - x0) / ST.pps;
    if (side === 'l') seg.in = clamp(quant(o0.in + dt, fps), 0, seg.out - MIN_LEN);
    else seg.out = clamp(quant(o0.out + dt, fps), seg.in + MIN_LEN, s.duration);
    layoutTL();
    const lbl = el.querySelector('.lbl small');
    if (lbl) lbl.textContent = tcs(sLen(seg), sLen(seg) < 60);
    let phX = null;
    if (side === 'l') {
      const dIn = seg.in - o0.in,
        d = dIn * ST.pps,
        grow = dIn < 0 ? -dIn : 0;
      // right edge stays put while the left handle moves, the clips after it only shift when this one grows
      el.style.left = (off + Math.max(dIn, 0)) * ST.pps + 'px';
      ST.segs.forEach((sg, i) => {
        if (i > idx) {
          const c = elOf(sg);
          if (c) c.style.left = (offs0[i] + grow) * ST.pps + 'px';
        }
      });
      $('#tlInner').style.width = Math.max(w0, parseFloat($('#tlInner').style.width) || 0) + 'px';
      const strip = el.querySelector('.strip');
      if (strip && s.kind === 'audio')
        strip.style.webkitMaskPosition = strip.style.maskPosition = `${-seg.in * ST.pps}px 0`;
      else if (strip) {
        strip.style.paddingLeft = d < 0 ? -d + 'px' : '';
        strip.scrollLeft = d > 0 ? d : 0;
      }
      phX = (off + Math.max(dIn, 0)) * ST.pps;
    }
    const t = side === 'l' ? seg.in : seg.out - 1 / fps;
    ST.T = side === 'l' ? off : off + sLen(seg);
    const v = vidFor(seg.sid);
    ST.ci = idx;
    for (const [id, vv] of Object.entries(ST.vids))
      vv.classList.toggle('on', id === seg.sid && s.kind === 'video');
    layoutVideo(v, seg);
    try {
      v.currentTime = Math.max(0, t);
    } catch (er) {}
    setPH();
    syncClip();
    if (phX != null) $('#tlPh').style.left = phX + 'px';
  };
  const up = () => {
    el.removeEventListener('pointermove', mv);
    el.removeEventListener('pointerup', up);
    el.removeEventListener('pointercancel', up);
    if (seg.in === o0.in && seg.out === o0.out) {
      if (side === 'l') layoutTL();
      return;
    }
    pushUndo(before);
    // shortened from the left, so slide everything over into the gap (no animation with reduced motion)
    if (side === 'l' && seg.in > o0.in && !reduced) {
      const o = stOffs(),
        ease = 'left .2s cubic-bezier(.2,.8,.2,1)';
      ST.segs.forEach((sg, i) => {
        if (i >= idx) {
          const c = elOf(sg);
          if (c) {
            c.style.transition = ease;
            c.style.left = o[i] * ST.pps + 'px';
          }
        }
      });
      const ph = $('#tlPh');
      ph.style.transition = ease;
      ph.style.left = ST.T * ST.pps + 'px';
      trimSnap = setTimeout(() => {
        ph.style.transition = '';
        renderAll();
      }, 210); // a bit longer than the .2s transition
    } else renderAll();
  };
  el.addEventListener('pointermove', mv);
  el.addEventListener('pointerup', up);
  el.addEventListener('pointercancel', up);
}
function startPress(e, seg, el, was) {
  const x0 = e.clientX;
  let drag = false;
  el.setPointerCapture(e.pointerId);
  const clips = $$('.tclip'),
    ins = $('#tlIns'),
    idx = ST.segs.indexOf(seg);
  let target = idx;
  const mids = () => stOffs().map((o, i) => (o + sLen(ST.segs[i]) / 2) * ST.pps);
  const mv = ev => {
    const dx = ev.clientX - x0;
    // has to move 7px before it counts as a drag, otherwise its a click that seeks
    if (!drag && Math.abs(dx) > 7 && ST.segs.length > 1) {
      drag = true;
      el.classList.add('drag');
      document.body.classList.add('d-grab');
    }
    if (!drag) return;
    const o = stOffs();
    el.style.transform = `translateX(${dx}px)`;
    const cx = o[idx] * ST.pps + (sLen(seg) * ST.pps) / 2 + dx,
      m = mids();
    target = 0;
    m.forEach((mm, i) => {
      if (i !== idx && cx > mm) target = i + (i < idx ? 1 : 0);
    });
    const stops = o
      .map((v, i) => v * ST.pps)
      .concat([stOffs().reduce((a, v, i) => v + sLen(ST.segs[i]), 0) * ST.pps]);
    const slot = target > idx ? target + 1 : target;
    ins.style.display = 'block';
    ins.style.left = (stops[slot] != null ? stops[slot] : stops[stops.length - 1]) + 'px';
    ins.style.top = '30px';
  };
  const up = ev => {
    el.removeEventListener('pointermove', mv);
    el.removeEventListener('pointerup', up);
    el.removeEventListener('pointercancel', up);
    ins.style.display = 'none';
    el.classList.remove('drag');
    el.style.transform = '';
    if (drag) {
      if (target !== idx && ev.type === 'pointerup') {
        edit(() => {
          ST.segs.splice(idx, 1);
          ST.segs.splice(target, 0, seg);
        });
        ST.T = stOffs()[target];
        showAt(ST.T);
      }
    } else {
      const T = clamp(xToT(ev.clientX), 0, stTotal());
      stSeek(quant(T, stFps(seg)), was);
    }
  };
  el.addEventListener('pointermove', mv);
  el.addEventListener('pointerup', up);
  el.addEventListener('pointercancel', up);
}
function dragBegin(e, cursor) {
  try {
    if (e.cancelable) e.preventDefault();
  } catch (er) {}
  const f = document.activeElement;
  // blur any input so a half typed timecode gets committed before the drag
  if (f && f !== document.body && /^(INPUT|TEXTAREA|SELECT)$/.test(f.tagName)) f.blur();
  try {
    getSelection().removeAllRanges();
  } catch (er) {}
  document.body.classList.add('dragging');
  if (cursor) document.body.classList.add(cursor);
  const end = () => {
    document.body.classList.remove('dragging', 'd-resize', 'd-grab');
    removeEventListener('pointerup', end, true);
    removeEventListener('pointercancel', end, true);
  };
  addEventListener('pointerup', end, true);
  addEventListener('pointercancel', end, true);
}
function startScrub(e) {
  if (e.button !== 0 || e.target.closest('.tclip')) return; // clicks on a clip are handled by the clip itself
  dragBegin(e, 'd-resize');
  const was = ST.playing;
  if (was) stPause();
  const tgt = e.currentTarget;
  tgt.setPointerCapture(e.pointerId);
  const at = ev => {
    ST.T = clamp(xToT(ev.clientX), 0, stTotal());
    const loc = locate(ST.T);
    ST.T = quant(ST.T, loc ? stFps(loc.seg) : 25);
    showAt(ST.T);
  };
  at(e);
  const up = () => {
    tgt.removeEventListener('pointermove', at);
    tgt.removeEventListener('pointerup', up);
    tgt.removeEventListener('pointercancel', up);
    if (was) stPlay();
  };
  tgt.addEventListener('pointermove', at);
  tgt.addEventListener('pointerup', up);
  tgt.addEventListener('pointercancel', up);
}
function syncBar() {
  const has = ST.segs.length > 0,
    seg = selSeg();
  $('#bUndo').disabled = !ST.undo.length;
  $('#bRedo').disabled = !ST.redo.length;
  $('#bSplit').disabled = !has;
  $('#bDel').disabled = !seg;
  $('#tPrev').disabled = $('#tNext').disabled = $('#tPlay').disabled = !has;
}
function syncClip() {
  const seg = selSeg(),
    box = $('#stClip');
  box.style.opacity = seg ? 1 : 0.5;
  if (!seg) return;
  const s = stSrc(seg);
  $('#clipInfo').textContent = s.name;
  const f = document.activeElement;
  // dont overwrite a timecode box while someone is typing in it
  if (f !== $('#inTc')) $('#inTc').value = tcs(seg.in);
  if (f !== $('#outTc')) $('#outTc').value = tcs(seg.out);
  $('#clipLen').textContent =
    `Length ${tcs(sLen(seg))}  ·  ${s.kind === 'video' ? `${s.width}×${s.height}  ·  ${+(+s.fps).toFixed(2)} fps  ·  ` : ''}${(s.vcodec || '').toUpperCase()}${s.vcodec && s.acodec ? ' / ' : ''}${(s.acodec || '').toUpperCase()}`;
  const aud = s.kind === 'audio';
  $('#xRotL').hidden = $('#xRotR').hidden = $('#xFlipH').hidden = $('#xFlipV').hidden = aud;
  $('#xAll').parentElement.hidden = aud;
  $('#xMute').disabled = !s.has_audio;
  $('#xMute').setAttribute('aria-pressed', seg.mute);
  $('#xFlipH').setAttribute('aria-pressed', seg.hf);
  $('#xFlipV').setAttribute('aria-pressed', seg.vf);
}
function commitTc(which) {
  const seg = selSeg(),
    el = $(which === 'in' ? '#inTc' : '#outTc');
  if (!seg) return;
  const s = stSrc(seg),
    fps = s.fps || 25;
  let v = parseTc(el.value);
  if (isNaN(v)) {
    syncClip();
    return;
  }
  v = quant(clamp(v, 0, s.duration), fps);
  if (which === 'in') v = Math.min(v, seg.out - MIN_LEN);
  else v = Math.max(v, seg.in + MIN_LEN);
  if (which === 'in' ? v !== seg.in : v !== seg.out)
    edit(() => {
      if (which === 'in') seg.in = v;
      else seg.out = v;
    });
  syncClip();
}
function buildFormats() {
  const caps = ST.caps || [];
  const mk = f =>
    h(
      'button',
      { type: 'button', class: 'chip', role: 'radio', 'data-f': f.id, title: f.hint, disabled: !f.ok },
      f.label
    );
  $('#fmV').replaceChildren(
    h(
      'button',
      { type: 'button', class: 'chip', role: 'radio', 'data-f': 'same', title: 'Keep the same kind of file' },
      'Same as source'
    ),
    ...caps.filter(f => f.kind === 'video').map(mk)
  );
  $('#fmA').replaceChildren(...caps.filter(f => f.kind === 'audio').map(mk));
  $$('#fmV .chip, #fmA .chip').forEach(b =>
    b.addEventListener('click', () => {
      ST.fmt = b.dataset.f;
      syncExport();
      schedulePlan();
    })
  );
  syncExport();
}
function syncExport() {
  const audio = isAudioProject(),
    fmtAudio = ['mp3', 'm4a', 'flac', 'wav', 'opus', 'ogg'].includes(ST.fmt);
  if (audio && !['same', ...['mp3', 'm4a', 'flac', 'wav', 'opus', 'ogg']].includes(ST.fmt)) ST.fmt = 'same';
  $('#fmV').parentElement.hidden = false;
  $('#fmV').parentElement.querySelector('.lab').textContent = audio ? 'File type' : 'Video file type';
  $$('#fmV .chip').forEach(b => {
    b.hidden = audio && b.dataset.f !== 'same';
    b.setAttribute('aria-checked', String(b.dataset.f === ST.fmt));
  });
  $('#fmAWrap').hidden = false;
  $('#fmALab').textContent = audio ? 'Convert to' : 'Or just the sound';
  $$('#fmA .chip').forEach(b => b.setAttribute('aria-checked', String(b.dataset.f === ST.fmt)));
  const lossless = ['flac', 'wav'].includes(ST.fmt),
    gif = ST.fmt === 'gif';
  $$('#segQ button').forEach(b => b.setAttribute('aria-checked', String(b.dataset.v === ST.q)));
  $$('#segM button').forEach(b => b.setAttribute('aria-checked', String(b.dataset.v === ST.mode)));
  $('#rowQ').hidden = lossless;
  $('#rowH').hidden = audio || fmtAudio || gif;
  $('#selH').value = String(ST.maxh);
  $('#rowS').hidden = gif || (!audio && false); // TODO: pointless, rowS gets set again a few lines down
  $('#swS').setAttribute('aria-checked', String(ST.noaudio));
  $('#rowS').hidden = gif || fmtAudio || audio;
  $('#mHint').textContent =
    ST.mode === 'exact'
      ? "Frame-accurate. Cuts are copied untouched when they land on a keyframe, and re-encoded only where they don't."
      : 'Always lossless and instant. Each cut snaps back to the nearest keyframe, so a clip can start a little early.';
  $('#exSub').textContent = ST.segs.length
    ? `${ST.segs.length} ${plural(ST.segs.length, 'clip', 'clips')} · ${tcs(stTotal(), false)}`
    : '';
  const j = ST.job,
    busy = j && j.state === 'running';
  $('#exOpts').style.pointerEvents = busy ? 'none' : '';
  $('#exOpts').style.opacity = busy ? 0.5 : 1;
  $('#exGo').hidden = !!busy;
  $('#exGo').disabled = !ST.segs.length || !!ST.planErr;
  const p = ST.plan;
  $('#exGo').textContent = p && p.lossless ? 'Export (lossless)' : 'Export';
  if (p) {
    const n = $('#exName');
    n.placeholder = p.name.replace(/\.[^.]+$/, '');
    $('#exDir').textContent = p.dir;
    $('#exDir').title = p.dir;
  }
  renderPlan();
  renderJob();
}
function renderPlan() {
  const box = $('#exPlan'),
    p = ST.plan;
  if (ST.planErr) {
    box.replaceChildren(h('div', { class: 'err' }, ST.planErr));
    return;
  }
  if (!p) {
    box.replaceChildren(h('div', null, 'Working out the plan...'));
    return;
  }
  if (p.pending) {
    box.replaceChildren(
      h('div', { class: 'head work' }, ic('refresh'), "Reading the file's cut points..."),
      h(
        'div',
        { style: 'margin-top:4px' },
        'You can export now: it picks the fastest safe way by itself (lossless when the cuts allow it).'
      )
    );
    return;
  }
  // offer fast mode when the only reason for re-encoding is a cut that isnt on a keyframe
  const wantFast = p.why && p.why[0] && /keyframe/.test(p.why[0]) && ST.mode === 'exact';
  box.replaceChildren(
    ...[
      h(
        'div',
        { class: 'head ' + (p.lossless ? 'good' : 'work') },
        ic(p.lossless ? 'bolt' : 'refresh'),
        p.lossless ? 'Lossless. Nothing is re-encoded.' : 'Re-encoded, frame-accurate'
      ),
      h(
        'div',
        { style: 'margin-top:4px' },
        [p.label, tcs(p.duration, p.duration < 60), p.est_text].filter(Boolean).join('  ·  '),
        p.canvas ? `  ·  ${p.canvas}` : ''
      ),
      p.notes.length || (!p.lossless && p.why.length)
        ? h(
            'ul',
            null,
            !p.lossless && p.why.length ? h('li', null, 'Re-encoding because ' + p.why[0] + '.') : null,
            p.notes.filter(n => !/^Lossless: nothing/.test(n)).map(n => h('li', null, n))
          )
        : null,
      wantFast
        ? h(
            'div',
            { style: 'margin-top:8px' },
            h(
              'a',
              {
                class: 'link',
                href: '#',
                role: 'button',
                onclick: ev => {
                  ev.preventDefault();
                  ST.mode = 'fast';
                  syncExport();
                  schedulePlan();
                },
              },
              'Use Fast to keep it lossless'
            )
          )
        : null,
    ].filter(Boolean)
  );
}
let planTimer = null,
  planAC = null,
  planTries = 0;
function schedulePlan() {
  clearTimeout(planTimer);
  if (planAC) {
    planAC.abort();
    planAC = null;
  }
  if (!ST.segs.length) return;
  const tok = ++ST.planTok;
  ST.plan = ST.plan || null;
  // wait 260ms so a burst of edits only asks the server once
  planTimer = setTimeout(async () => {
    // tab isnt open, redo the plan when its shown again (see stShown)
    if (ST.away) {
      ST.planStale = true;
      return;
    }
    const ac = (planAC = new AbortController());
    try {
      const r = await api('/api/studio/plan', exportSpec(), ac.signal);
      if (tok !== ST.planTok) return;
      if (r.ok) {
        ST.plan = r.plan;
        ST.planErr = '';
        // plan isnt final until the keyframes are read, ask again every 1.5s and give up after 40 tries
        if (r.plan.pending && planTries++ < 40)
          setTimeout(() => {
            if (tok === ST.planTok) schedulePlan();
          }, 1500);
        else if (!r.plan.pending) planTries = 0;
      } else {
        ST.planErr = r.error || "Can't export this yet.";
      }
    } catch (e) {
      if (e && e.name === 'AbortError') return;
      ST.planErr = "Can't reach the app.";
    } finally {
      if (planAC === ac) planAC = null;
    }
    syncExport();
  }, 260);
}
function exportSpec() {
  return {
    segments: ST.segs.map(s => ({
      src: s.sid,
      in: s.in,
      out: s.out,
      rot: s.rot,
      hflip: s.hf,
      vflip: s.vf,
      mute: s.mute,
    })),
    format: ST.fmt,
    quality: ST.q,
    maxh: ST.maxh,
    mode: ST.mode,
    noaudio: ST.noaudio,
    name: ST.name,
    dir: ST.dir,
  };
}
async function stExport() {
  // pauses playback, bails if already exporting
  if ((stPause(), ST.job && ST.job.state === 'running')) return;
  ST.job = { state: 'running', pct: 0, stage: 'Starting', id: null };
  syncExport();
  try {
    const r = await api('/api/studio/export', exportSpec());
    if (!r.ok) {
      ST.job = { state: 'error', message: r.error || "Couldn't start." };
      syncExport();
      return;
    }
    ST.job.id = ST.job.id || r.id;
  } catch (e) {
    ST.job = { state: 'error', message: "Couldn't reach the app." };
    syncExport();
  }
}
function renderJob() {
  const j = ST.job,
    box = $('#exJob');
  if (!j) {
    box.replaceChildren();
    return;
  }
  if (j.state === 'running') {
    const pct = Math.round((j.pct || 0) * 100);
    box.replaceChildren(
      h(
        'div',
        { class: 'prog' },
        h('div', { class: 'bar' }, h('i', { style: `width:${pct}%` })),
        h(
          'div',
          { class: 'meta num' },
          h('b', null, pct + '%'),
          h('span', null, j.stage || ''),
          h('span', { class: 'sp' }),
          j.speed ? h('span', null, (+j.speed).toFixed(1) + '×') : null,
          j.eta != null ? h('span', null, fmtEta(j.eta)) : null
        )
      ),
      h(
        'button',
        { class: 'btn ghost small', style: 'margin-top:12px', onclick: () => api('/api/studio/cancel', {}) },
        'Cancel'
      )
    );
  } else if (j.state === 'done') {
    box.replaceChildren(
      h(
        'div',
        { class: 'done' },
        h('div', { class: 't' }, ic('check'), 'Saved'),
        h(
          'div',
          { class: 'n' },
          j.name +
            '  ·  ' +
            j.size_text +
            (j.lossless ? '  ·  lossless' : '') +
            (j.seconds ? '  ·  ' + fmtTook(j.seconds) : '')
        ),
        h(
          'div',
          { class: 'acts' },
          h(
            'button',
            { class: 'btn small', onclick: () => api('/api/open', { path: j.path }) },
            ic('play'),
            'Open'
          ),
          h(
            'button',
            { class: 'btn small soft', onclick: () => api('/api/reveal', { path: j.path }) },
            ic('folder'),
            'Show in folder'
          ),
          h(
            'button',
            {
              class: 'btn small ghost',
              onclick: () => {
                ST.job = null;
                syncExport();
              },
            },
            'Done'
          )
        )
      )
    );
  } else if (j.state === 'error') {
    box.replaceChildren(
      h('div', { class: 'failed' }, j.message),
      h(
        'button',
        {
          class: 'btn ghost small',
          style: 'margin-top:10px',
          onclick: () => {
            ST.job = null;
            syncExport();
          },
        },
        'Dismiss'
      )
    );
  } else if (j.state === 'cancelled') {
    box.replaceChildren(h('div', { class: 'hint' }, 'Cancelled. Nothing was saved.'));
    setTimeout(() => {
      if (ST.job && ST.job.state === 'cancelled') {
        ST.job = null;
        renderJob();
      }
    }, 3e3);
  }
}
async function stSync() {
  try {
    const r = await api('/api/studio/status', {}),
      e = r.event;
    // the event stream can drop during an export, so ask the server where it is and catch up
    if (!ST.job || ST.job.state !== 'running' || !ST.job.id) return;
    if (e && e.job === ST.job.id) stEvent(e);
    else if (!r.running) {
      // server says nothing is running but the done event never came
      ST.job = {
        state: 'error',
        message: 'The export stopped unexpectedly. Your original files are untouched.',
      };
      syncExport();
    }
  } catch (er) {}
}
function stEvent(e) {
  if (e.t === 'kf') {
    // keyframes finished reading, so the plan can be more exact now
    if (ST.src[e.id]) schedulePlan();
    return;
  }
  if (e.t === 'prep') {
    const s = ST.src[e.id];
    if (!s) return;
    s.prep = e.state;
    s.prepPct = e.pct || 0;
    if (e.state === 'ready') {
      if (ST.segs.length && !ST.away) showAt(ST.T);
    } else if (ST.segs.length) syncOverlay();
  } else if (e.t === 'export') {
    if (!ST.job && e.state === 'running') ST.job = {}; // export was already running when the page loaded
    if (!ST.job || (ST.job.id && ST.job.id !== e.job)) return;
    ST.job.id = e.job;
    const wasDone = ST.job.state;
    ST.job = Object.assign(ST.job, e, { id: e.job });
    if (e.state === 'done' && wasDone !== 'done') {
      chime();
      toast('good', 'Saved ' + e.name);
    }
    syncExport();
  }
}
function stInit() {
  $('#stOpenBtn').addEventListener('click', stPick);
  $('#bAdd').addEventListener('click', stPick);
  const dz = $('#stDropZone');
  dz.addEventListener('click', e => {
    if (!e.target.closest('button')) stPick();
  });
  dz.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      stPick();
    }
  });
  $('#stFile').addEventListener('change', e => {
    stUpload(e.target.files);
    e.target.value = '';
  });
  $('#tPlay').addEventListener('click', () => (ST.playing ? stPause() : stPlay()));
  $('#tPrev').addEventListener('click', () => {
    stPause();
    stepFrame(-1);
  });
  $('#tNext').addEventListener('click', () => {
    stPause();
    stepFrame(1);
  });
  $('#tVol').addEventListener('click', () => {
    ST.vol = !ST.vol;
    $('#tVol').setAttribute('aria-pressed', String(!ST.vol));
    $('#tVol').classList.toggle('on', !ST.vol);
    $('#tVol').replaceChildren(ic(ST.vol ? 'vol' : 'mute'));
    showAt(ST.T, ST.playing);
  });
  $('#bSplit').addEventListener('click', stSplit);
  $('#bDel').addEventListener('click', stDelete);
  $('#bUndo').addEventListener('click', stUndo);
  $('#bRedo').addEventListener('click', stRedo);
  $('#bZin').addEventListener('click', () => setZoom(ST.pps * 1.6));
  $('#bZout').addEventListener('click', () => setZoom(ST.pps / 1.6));
  $('#bFit').addEventListener('click', () => {
    ST.fit = true;
    renderAll();
    $('#tlScroll').scrollLeft = 0;
  });
  // start over needs two clicks within 3s so the project doesnt get wiped by accident
  let sure = 0;
  $('#bNew').addEventListener('click', e => {
    const b = e.currentTarget,
      sp = b.querySelector('span');
    if (Date.now() - sure < 3e3) {
      sure = 0;
      sp.textContent = 'Start over';
      stReset();
    } else {
      sure = Date.now();
      sp.textContent = 'Sure?';
      b.querySelector('span').style.display = 'inline';
      setTimeout(() => {
        sp.textContent = 'Start over';
        sp.style.display = '';
      }, 3e3);
    }
  });
  $('#xRotL').addEventListener('click', () => xform(rotBy(-90)));
  $('#xRotR').addEventListener('click', () => xform(rotBy(90)));
  $('#xFlipH').addEventListener('click', () =>
    xform(s => {
      s.hf = !s.hf;
    })
  );
  $('#xFlipV').addEventListener('click', () =>
    xform(s => {
      s.vf = !s.vf;
    })
  );
  $('#xMute').addEventListener('click', () =>
    xform(s => {
      s.mute = !s.mute;
    })
  );
  $('#xAll').addEventListener('click', e => {
    e.preventDefault();
    const seg = selSeg();
    if (!seg) return;
    edit(() =>
      ST.segs.forEach(s => {
        s.rot = seg.rot;
        s.hf = seg.hf;
        s.vf = seg.vf;
      })
    );
    showAt(ST.T);
  });
  for (const w of ['in', 'out']) {
    const el = $(w === 'in' ? '#inTc' : '#outTc');
    el.addEventListener('change', () => commitTc(w));
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter') el.blur();
    });
  }
  $$('#segQ button').forEach(b =>
    b.addEventListener('click', () => {
      ST.q = b.dataset.v;
      syncExport();
      schedulePlan();
    })
  );
  $$('#segM button').forEach(b =>
    b.addEventListener('click', () => {
      ST.mode = b.dataset.v;
      syncExport();
      schedulePlan();
    })
  );
  $('#selH').addEventListener('change', e => {
    ST.maxh = +e.target.value;
    schedulePlan();
  });
  $('#swS').addEventListener('click', () => {
    ST.noaudio = !ST.noaudio;
    syncExport();
    schedulePlan();
  });
  $('#exName').addEventListener('input', e => {
    ST.name = e.target.value;
    schedulePlan();
  });
  $('#exPick').addEventListener('click', async () => {
    try {
      const r = await api('/api/studio/pick_dir', {});
      if (r.ok && r.dir) {
        ST.dir = r.dir;
        schedulePlan();
      }
    } catch (e) {}
  });
  $('#exGo').addEventListener('click', stExport);
  $('#tlRuler').addEventListener('pointerdown', startScrub);
  $('#tlTrack').addEventListener('pointerdown', startScrub);
  // ctrl + wheel zooms around the mouse, a plain wheel scrolls sideways
  $('#tlScroll').addEventListener(
    'wheel',
    e => {
      if (e.ctrlKey) {
        e.preventDefault();
        setZoom(
          ST.pps * (e.deltaY < 0 ? 1.18 : 1 / 1.18),
          e.clientX - $('#tlScroll').getBoundingClientRect().left
        );
      } else if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        $('#tlScroll').scrollLeft += e.deltaY;
        e.preventDefault();
      }
    },
    { passive: false }
  );
  new ResizeObserver(() => {
    if (S.page !== 'studio' || !ST.segs.length) return;
    if (ST.fit) {
      fitZoom();
      renderTL();
    } else layoutTL();
    showAt(ST.T, ST.playing);
  }).observe($('#tlScroll'));
  new ResizeObserver(() => {
    if (ST.segs.length && S.page === 'studio') showAt(ST.T, true);
  }).observe($('#stStage'));
  document.addEventListener('keydown', e => {
    if (S.page !== 'studio' || $('#stApp').hidden) return;
    const t = e.target,
      typing = /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable;
    if (typing) return; // dont steal keys from text boxes
    const k = e.key,
      ctrl = e.ctrlKey || e.metaKey;
    let used = true;
    if (ctrl && (k === 'z' || k === 'Z')) e.shiftKey ? stRedo() : stUndo();
    else if (ctrl && (k === 'y' || k === 'Y')) stRedo();
    else if (ctrl)
      used = false; // other ctrl shortcuts go to the browser
    else if (k === ' ') ST.playing ? stPause() : stPlay();
    else if (k === 'ArrowLeft' && e.altKey) stMove(-1);
    else if (k === 'ArrowRight' && e.altKey) stMove(1);
    else if (k === 'ArrowLeft') {
      stPause();
      e.shiftKey ? stSeek(ST.T - 1) : stepFrame(-1);
    } else if (k === 'ArrowRight') {
      stPause();
      e.shiftKey ? stSeek(ST.T + 1) : stepFrame(1);
    } else if (k === 'Home') stSeek(0);
    else if (k === 'End') stSeek(stTotal());
    else if (k === 's' || k === 'S') stSplit();
    else if (k === 'Delete' || k === 'Backspace') stDelete();
    else if (k === 'i' || k === 'I') stSetEdge('in');
    else if (k === 'o' || k === 'O') stSetEdge('out');
    else if (k === 'r' || k === 'R') xform(rotBy(90));
    else if (k === 'm' || k === 'M')
      xform(s => {
        s.mute = !s.mute;
      });
    else if (k === '+' || k === '=') setZoom(ST.pps * 1.4);
    else if (k === '-') setZoom(ST.pps / 1.4);
    else used = false;
    if (used) e.preventDefault();
  });
  // dragenter and dragleave fire for every child element,
  // so count them to know when the file really left the window
  let depth = 0;
  const veil = $('#stVeil');
  const hasFiles = e => e.dataTransfer && [...(e.dataTransfer.types || [])].includes('Files');
  addEventListener('dragenter', e => {
    if (hasFiles(e)) {
      depth++;
      veil.classList.add('on');
    }
  });
  addEventListener('dragleave', e => {
    if (hasFiles(e) && --depth <= 0) {
      depth = 0;
      veil.classList.remove('on');
    }
  });
  addEventListener(
    'drop',
    e => {
      depth = 0;
      veil.classList.remove('on');
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
        e.preventDefault();
        e.stopImmediatePropagation(); // get.js has its own drop handler for links, dont let it grab files
        stUpload(e.dataTransfer.files);
      }
    },
    true
  );
  syncPlayBtn();
  syncBar();
}
function stShown() {
  ST.away = false;
  stCaps();
  if (!ST.segs.length) renderEmpty();
  else {
    renderAll();
  }
  if (ST.planStale) {
    ST.planStale = false;
    schedulePlan();
  }
}
let es,
  lastMsg = Date.now();
function connect() {
  es = new EventSource('/api/events');
  es.addEventListener('state', e => {
    lastMsg = Date.now();
    $('#conn').hidden = true;
    const first = !S.settings.folder;
    merge(JSON.parse(e.data));
    if (first) {
      movePill();
      checkClip();
    }
  });
  es.addEventListener('studio', e => {
    try {
      stEvent(JSON.parse(e.data));
    } catch (er) {}
  });
  es.addEventListener('toast', e => {
    const t = JSON.parse(e.data);
    toast(t.kind, t.text);
  });
  es.addEventListener('log', e => {
    S.logs.push(JSON.parse(e.data));
    if (S.logs.length > 300) S.logs.shift();
  });
  es.onopen = () => {
    if (typeof ST !== 'undefined' && ST.job && ST.job.state === 'running') stSync();
  };
  // only show the offline banner when nothing came in for 6s, quick reconnects stay quiet
  es.onerror = () => {
    setTimeout(() => {
      if (Date.now() - lastMsg > 6e3) $('#conn').hidden = false;
    }, 1500);
  };
}
// the server uses the ping to know the window is still open
setInterval(() => {
  api('/api/ping', {}).catch(() => {});
  if (typeof ST !== 'undefined' && ST.job && ST.job.state === 'running') stSync();
}, 4e3);
// warn before closing the page while something is downloading or exporting
addEventListener('beforeunload', e => {
  if (S.jobs.some(isActive) || (typeof ST !== 'undefined' && ST.job && ST.job.state === 'running')) {
    e.preventDefault();
    e.returnValue = '';
  }
});
