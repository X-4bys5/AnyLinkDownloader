'use strict';
window.__aldClose = () => {
  const busy = S.jobs.some(isActive) || (typeof ST !== 'undefined' && ST.job && ST.job.state === 'running');
  if (busy && !confirm('Downloads or an export are still running. Close the app and stop them?')) return;
  // native window gets told over ipc, in a normal browser tab its just window.close
  if (window.ipc) window.ipc.postMessage('close');
  else window.close();
};
// fonts change the tab widths so the pill has to move again once they load
if (document.fonts && document.fonts.ready) document.fonts.ready.then(movePill);
movePill();
syncSlot();
manyCount();
stInit();
connect();
setTimeout(() => input.focus(), 200);
