'use strict';
// state merging, the sse connection itself is in studio.js (connect)
function applyTheme() {
  const t = S.settings.theme;
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}
let clipBusy = false;
async function checkClip() {
  // only when the get page is idle, dont want to clobber what theyre typing
  if (clipBusy || S.many || !S.settings.clip_watch || S.page !== 'get' || S.an || input.value.trim()) return;
  clipBusy = true;
  try {
    const { text } = await api('/api/clipboard');
    const m = (text || '').trim().match(/^https?:\/\/\S+$/);
    if (m && m[0] !== S.lastClip) {
      S.lastClip = m[0];
      input.value = m[0];
      syncSlot();
      if (autoFetch()) analyze(m[0]);
      toast(
        '',
        autoFetch() ? 'Picked up the link you copied.' : 'Picked up the link you copied. Press Fetch.'
      );
    }
  } catch (e) {
  } finally {
    clipBusy = false;
  }
}
// no polling, we only look when the window gets focus again
addEventListener('focus', () => setTimeout(checkClip, 60));
function merge(s) {
  if (s.app_update) S.appu = s.app_update;
  if (s.ota) S.ota = s.ota;
  const prev = new Map(S.jobs.map(j => [j.id, j.status]));
  S.jobs = s.jobs;
  S.paused = s.paused;
  S.folder = s.folder;
  // the light ticks dont carry settings or library, only the full snapshots do
  if (s.settings) {
    S.settings = s.settings;
    S.health = s.health;
    S.library = s.library;
    S.resumable = s.resumable;
    S.version = s.version;
    S.browsers = s.browsers;
    applyTheme();
    // saved kind and format are only taken once, after that the ui owns them
    if (!S.prefsApplied) {
      S.kind = s.settings.kind || 'video';
      S.quality = 'best';
      S.container = s.settings.container || 'mp4';
      S.afmt = s.settings.audio_format || 'mp3';
      S.prefsApplied = true;
    }
  }
  // a job flipped to done since the last tick -> chime and pull the full state for the library
  let finished = false;
  for (const j of S.jobs)
    if (j.status === 'done' && prev.has(j.id) && prev.get(j.id) !== 'done') finished = true;
  renderTray();
  renderBanner();
  renderUpdBar();
  {
    const row = $('#appuRow');
    if (row) fillAppU(row);
    const orow = $('#otaRow');
    if (orow) fillOta(orow);
  }
  if (finished) {
    chime();
    refreshFull();
  }
  // dont rebuild the settings page while they are typing in it
  if (S.page === 'settings' && s.settings && !$('#setBody').contains(document.activeElement))
    renderSettings();
  syncSlot();
  const fp = $('#folderPath');
  if (fp) fp.textContent = S.folder;
}
let fullTimer = null;
function refreshFull() {
  clearTimeout(fullTimer);
  return new Promise(res => {
    fullTimer = setTimeout(async () => {
      try {
        merge(await api('/api/state'));
        if (S.page === 'saved') renderSaved();
        updateRecent();
      } catch (e) {}
      res();
    }, 120); // debounce so a few jobs finishing together make one request
  });
}
