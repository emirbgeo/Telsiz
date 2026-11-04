/* Shows the shared screen: long-polls JPEG pictures and draws them to fit the window. */
(function () {
  'use strict';
  const canvas = document.getElementById('screen');
  const ctx = canvas.getContext('2d');
  const wait = document.getElementById('wait');
  let seq = 0;
  let alive = true;
  let full = false;
  let last = null;

  const post = (name, args) => fetch('api/' + name, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(args || {}),
  }).then((r) => r.json()).catch(() => ({}));

  function size() {
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(innerWidth * dpr);
    canvas.height = Math.round(innerHeight * dpr);
    post('viewer_resize', { w: canvas.width, h: canvas.height });
    if (last) draw(last);
  }

  function draw(bmp) {
    const cw = canvas.width, ch = canvas.height;
    const k = Math.min(cw / bmp.width, ch / bmp.height);
    const w = Math.round(bmp.width * k), h = Math.round(bmp.height * k);
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, cw, ch);
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(bmp, (cw - w) >> 1, (ch - h) >> 1, w, h);
  }

  async function frames() {
    while (alive) {
      try {
        const r = await fetch('frame/' + seq, { cache: 'no-store' });
        if (r.status === 200) {
          seq = +(r.headers.get('X-Seq') || seq + 1);
          const bmp = await createImageBitmap(await r.blob());
          if (last && last.close) last.close();
          last = bmp;
          draw(bmp);
          wait.hidden = true;
        } else if (r.status === 404) {
          await new Promise((res) => setTimeout(res, 500));
        }
      } catch (e) {
        await new Promise((res) => setTimeout(res, 500));
      }
    }
  }

  async function watchState() {
    while (alive) {
      try {
        const s = await (await fetch('state', { cache: 'no-store' })).json();
        if (s.watching) {
          document.title = s.watching.name + "'s screen";
          document.getElementById('who').textContent = 'Waiting for ' + s.watching.name + "'s screen";
        } else if (seq > 0 || Date.now() - started > 4000) {
          alive = false;
          wait.hidden = false;
          document.getElementById('who').textContent = 'The screen share ended';
          document.getElementById('sub').textContent = 'You can close this window.';
          setTimeout(() => window.close(), 1500);
        }
      } catch (e) { /* Telsiz is gone */ }
      await new Promise((res) => setTimeout(res, 1000));
    }
  }

  async function setFull(on) {
    full = on;
    const r = await post('viewer_fullscreen', { on });
    if (!r.handled) {  // in a browser tab: use the page's own full screen
      try {
        if (on && !document.fullscreenElement) await document.documentElement.requestFullscreen();
        if (!on && document.fullscreenElement) await document.exitFullscreen();
      } catch (e) { /* not allowed here */ }
    }
  }

  const started = Date.now();
  addEventListener('resize', size);
  addEventListener('dblclick', () => setFull(!full));
  addEventListener('keydown', (e) => { if (e.key === 'Escape' && full) setFull(false); });
  const uid = location.hash.slice(1);
  addEventListener('beforeunload', () => {
    if (navigator.sendBeacon) navigator.sendBeacon('api/viewer_close', JSON.stringify({ uid }));
  });
  setTimeout(() => { document.getElementById('hint').style.opacity = '0'; }, 4000);
  size();
  frames();
  watchState();
})();
