/* Isoline ripple, from FocusDock: concentric wobbly contours spreading from a point. */
(function () {
  'use strict';
  const canvas = document.getElementById('fx');
  const ctx = canvas.getContext('2d');
  let rings = [];
  let running = false;
  let dpr = 1;

  function resize() {
    dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(innerWidth * dpr);
    canvas.height = Math.round(innerHeight * dpr);
  }
  addEventListener('resize', resize);
  resize();

  function loop(t) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, innerWidth, innerHeight);
    rings = rings.filter((r) => t - r.t0 < r.life);
    for (const r of rings) {
      if (t < r.t0) continue;
      const k = (t - r.t0) / r.life;
      const radius = r.r0 + (r.r1 - r.r0) * (1 - Math.pow(1 - k, 3));
      ctx.beginPath();
      ctx.lineWidth = 1.6 * (1 - k) + 0.4;
      ctx.strokeStyle = r.color;
      ctx.globalAlpha = (1 - k) * 0.9;
      for (let i = 0; i <= 48; i++) {
        const a = (i / 48) * Math.PI * 2;
        const wob = 1 + 0.06 * Math.sin(a * 3 + r.seed) + 0.04 * Math.sin(a * 5 + r.seed * 2);
        const x = r.x + Math.cos(a) * radius * wob;
        const y = r.y + Math.sin(a) * radius * wob * 0.92;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    if (rings.length) requestAnimationFrame(loop);
    else { running = false; ctx.clearRect(0, 0, innerWidth, innerHeight); }
  }

  function ripple(x, y, color, count) {
    const t0 = performance.now();
    const seed = Math.random() * 10;
    for (let i = 0; i < (count || 3); i++) {
      rings.push({ x, y, color, t0: t0 + i * 110, life: 900, r0: 10, r1: 70 + i * 26, seed: seed + i });
    }
    if (!running) { running = true; requestAnimationFrame(loop); }
  }

  window.FX = { ripple };
})();
