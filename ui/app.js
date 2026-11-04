/* Telsiz window. Plain JS; talks to telsiz.py over HTTP on this machine only. */
(function () {
  'use strict';

  const $ = (s, root) => (root || document).querySelector(s);
  const HEAT = { low: '#3D7BFF', normal: '#1FC2B0', high: '#FFB020', crit: '#FF4F6E' };
  const CELLS = 24;

  const ui = {
    s: null,
    form: null,
    openPerson: null,
    roomSig: '',
    peopleSig: '',
    toolsSig: '',
    devicesSig: '',
    audioOpen: false,
    bansOpen: false,
    modalOpen: false,
    connErr: '',
    prevConn: '',
    prevRoom: null,
    failures: 0,
    thrDrag: null,
    lastDay: '',
    firstRunHandled: false,
    bansCount: -1,
    audioDismissed: '',
    updateOpen: false,
    updateBox: null,
    updateDone: null,
    updateSig: '',
    updatedToast: false,
  };

  /* ------------------------------------------------------------ helpers */

  const ICONS = {
    sliders: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
    eyeOff: '<path d="M3 3l18 18"/><path d="M10.6 5.1A10 10 0 0 1 12 5c5 0 9 4.5 10 7a13 13 0 0 1-2.6 3.6M6.6 6.6C4.4 8 2.8 10.1 2 12c1 2.5 5 7 10 7a9.6 9.6 0 0 0 4.4-1.1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7"/>',
    micOff: '<path d="M3 3l18 18"/><path d="M9 9v2a3 3 0 0 0 5.1 2.1M15 10V6a3 3 0 0 0-5.7-1.3"/><path d="M5.5 11a6.5 6.5 0 0 0 10.6 5M18.5 11a6.4 6.4 0 0 1-.6 2.7M12 17.5V21M8.5 21h7"/>',
    phones: '<path d="M4 15v-3a8 8 0 0 1 16 0v3"/><rect x="3" y="14" width="5" height="7" rx="2"/><rect x="16" y="14" width="5" height="7" rx="2"/>',
    phonesOff: '<path d="M3 3l18 18"/><path d="M4 15v-3a8 8 0 0 1 2.3-5.7M9.5 4.4A8 8 0 0 1 20 12v3"/><rect x="3" y="14" width="5" height="7" rx="2"/><path d="M16 16v3a2 2 0 0 0 2 2h1"/>',
    wave: '<path d="M4 12h1M8 8v8M12 5v14M16 8v8M20 12h0" stroke-width="2.6"/>',
    close: '<path d="M6 6l12 12M18 6L6 18"/>',
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    screen: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
    eye: '<path d="M2 12c1-2.5 5-7 10-7s9 4.5 10 7c-1 2.5-5 7-10 7S3 14.5 2 12z"/><circle cx="12" cy="12" r="3"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    shield: '<path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6l8-3z"/>',
    alert: '<path d="M12 3l10 18H2L12 3z"/><path d="M12 10v5M12 18v.5"/>',
    download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  };
  const icon = (name) =>
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    ICONS[name] + '</svg>';

  function h(tag, props) {
    const el = document.createElement(tag);
    if (props) {
      for (const k of Object.keys(props)) {
        const v = props[k];
        if (v == null || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'html') el.innerHTML = v;
        else if (k === 'text') el.textContent = v;
        else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
        else if (k === 'style') el.style.cssText = v;
        else if (k === 'value') el.value = v;
        else el.setAttribute(k, v === true ? '' : v);
      }
    }
    for (let i = 2; i < arguments.length; i++) {
      for (const kid of [].concat(arguments[i])) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid));
    }
    return el;
  }

  function put(el, ...kids) {
    el.replaceChildren(...[].concat(...kids).filter((k) => k != null && k !== false));
  }

  async function api(name, args) {
    try {
      const r = await fetch('api/' + name, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(args || {}),
      });
      return await r.json();
    } catch (e) {
      return { error: 'Telsiz is not responding.' };
    }
  }

  async function act(name, args) {
    const r = await api(name, args);
    if (r && r.error) alertCard(r.error);
    poll(true);
    return r || {};
  }

  function debounce(fn, ms) {
    let t = null;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  }

  function throttle(fn, ms) {
    let last = 0, timer = null, pending = null;
    return (...a) => {
      pending = a;
      const now = Date.now();
      if (now - last >= ms) { last = now; fn(...pending); pending = null; }
      else if (!timer) timer = setTimeout(() => { timer = null; last = Date.now(); if (pending) fn(...pending); pending = null; }, ms - (now - last));
    };
  }

  const calm = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

  /** Colour a range input up to its thumb. */
  function fillRange(r) {
    const min = +r.min || 0, max = +r.max || 100;
    r.style.setProperty('--fill', ((+r.value - min) / (max - min)) * 100 + '%');
  }

  function ripple(el, heat) {
    if (calm() || !el || !window.FX) return;
    const r = el.getBoundingClientRect();
    if (!r.width) return;
    window.FX.ripple(r.left + Math.min(40, r.width / 2), r.top + r.height / 2, HEAT[heat] || HEAT.normal, 3);
  }

  /* --------------------------------------------------------------- toast */

  let toastTimer = null;
  function toast(text) {
    const el = $('#toast');
    put(el, h('p', { text }));
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 3000);
  }

  /* --------------------------------------------- alerts (errors, notices) */

  function alertCard(text, opts) {
    opts = opts || {};
    const box = $('#notices');
    if (Array.from(box.querySelectorAll('.a-body')).some((b) => b.textContent === text)) return;
    const card = h('div', { class: 'alert', style: '--heat:' + (opts.heat || 'var(--crit)'), 'data-kind': opts.kind || '' },
      h('div', { class: 'a-kind', html: icon('alert') + '<span></span>' }),
      h('div', { class: 'a-body', text }),
      h('div', { class: 'a-actions' },
        opts.action ? h('button', { class: 'btn small primary', text: opts.action, onclick: () => { card.remove(); opts.onAction(); } }) : null,
        h('button', { class: 'btn small ' + (opts.action ? 'ghost' : 'primary'), text: 'Got it', onclick: () => { card.remove(); if (opts.onDismiss) opts.onDismiss(); } })
      )
    );
    card.querySelector('.a-kind span').textContent = opts.kindLabel || 'Something went wrong';
    box.append(card);
  }

  /* ----------------------------------------------------------- isolines */

  function drawIsolines() {
    const svg = $('.isolines');
    const W = 400, H = 200, nx = 80, ny = 40;
    const today = new Date();
    let seed = (today.getFullYear() * 10000 + (today.getMonth() + 1) * 100 + today.getDate()) % 2147483647;
    const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
    const sources = Array.from({ length: 4 }, () => ({
      x: rnd() * W, y: rnd() * H * 1.2 - H * 0.1, a: (rnd() < 0.3 ? -1 : 1) * (0.6 + rnd()), s: 40 + rnd() * 70,
    }));
    const f = (x, y) => sources.reduce((acc, s) => acc + s.a * Math.exp(-((x - s.x) ** 2 + (y - s.y) ** 2) / (2 * s.s * s.s)), 0);
    const grid = [];
    let min = Infinity, max = -Infinity;
    for (let j = 0; j <= ny; j++) {
      grid[j] = [];
      for (let i = 0; i <= nx; i++) {
        const v = f((i / nx) * W, (j / ny) * H);
        grid[j][i] = v;
        if (v < min) min = v;
        if (v > max) max = v;
      }
    }
    const dx = W / nx, dy = H / ny;
    let d = '';
    const levels = 11;
    for (let L = 1; L < levels; L++) {
      const z = min + ((max - min) * L) / levels;
      for (let j = 0; j < ny; j++) {
        for (let i = 0; i < nx; i++) {
          const a = grid[j][i], b = grid[j][i + 1], c = grid[j + 1][i + 1], e = grid[j + 1][i];
          const pts = [];
          const edge = (v1, v2, x1, y1, x2, y2) => {
            if ((v1 < z) !== (v2 < z)) {
              const t = (z - v1) / (v2 - v1);
              pts.push([x1 + (x2 - x1) * t, y1 + (y2 - y1) * t]);
            }
          };
          const x0 = i * dx, y0 = j * dy;
          edge(a, b, x0, y0, x0 + dx, y0);
          edge(b, c, x0 + dx, y0, x0 + dx, y0 + dy);
          edge(c, e, x0 + dx, y0 + dy, x0, y0 + dy);
          edge(e, a, x0, y0 + dy, x0, y0);
          for (let k = 0; k + 1 < pts.length; k += 2) {
            d += 'M' + pts[k][0].toFixed(1) + ' ' + pts[k][1].toFixed(1) + 'L' + pts[k + 1][0].toFixed(1) + ' ' + pts[k + 1][1].toFixed(1);
          }
        }
      }
    }
    svg.innerHTML = '<path d="' + d + '"/>';
  }

  /* ---------------------------------------------- the mic meter colorbar */

  function makeMeter(root) {
    const cells = root.querySelector('.cells');
    const thr = root.querySelector('.thr');
    const list = [];
    for (let i = 0; i < CELLS; i++) {
      const c = h('div', { class: 'cell', style: '--n:' + CELLS * 100 + '%;--pos:' + (i / (CELLS - 1)) * 100 + '%' });
      list.push(c);
      cells.append(c);
    }
    let lit = -1;
    const dbAt = (clientX) => {
      const r = root.getBoundingClientRect();
      const min = ui.s ? ui.s.meter.min : -80;
      return min + Math.min(Math.max((clientX - r.left) / r.width, 0), 1) * -min;
    };
    const sendThr = throttle((db) => api('set_threshold', { db }), 60);
    const setThr = (db) => {
      db = Math.round(Math.min(0, Math.max(ui.s.meter.min, db)));
      ui.thrDrag = { db, until: Date.now() + 600 };
      sendThr(db);
      updateAllMeters();
    };
    root.addEventListener('pointerdown', (e) => {
      if (!ui.s || ui.s.meter.mode !== 'voice') return;
      root.setPointerCapture(e.pointerId);
      setThr(dbAt(e.clientX));
    });
    root.addEventListener('pointermove', (e) => {
      if (root.hasPointerCapture && root.hasPointerCapture(e.pointerId)) setThr(dbAt(e.clientX));
    });
    root.addEventListener('keydown', (e) => {
      if (!ui.s || ui.s.meter.mode !== 'voice') return;
      const step = e.shiftKey ? 5 : 1;
      if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') { e.preventDefault(); setThr(curThr() - step); }
      if (e.key === 'ArrowRight' || e.key === 'ArrowUp') { e.preventDefault(); setThr(curThr() + step); }
    });
    return function update(m, me) {
      const frac = (m.db - m.min) / -m.min;
      const n = Math.max(0, Math.min(CELLS, Math.round(frac * CELLS)));
      if (n !== lit) {
        for (let i = 0; i < CELLS; i++) list[i].classList.toggle('on', i < n);
        lit = n;
      }
      root.classList.toggle('tx', m.tx);
      root.classList.toggle('muted', me.muted || me.deafened);
      root.classList.toggle('voice', m.mode === 'voice');
      thr.hidden = m.mode !== 'voice';
      thr.style.left = ((curThr() - m.min) / -m.min) * 100 + '%';
      root.setAttribute('aria-valuetext', m.mode === 'voice' ? 'Sensitivity ' + curThr() + ' dB' : 'Input level');
    };
  }

  function curThr() {
    if (ui.thrDrag && Date.now() < ui.thrDrag.until) return ui.thrDrag.db;
    return ui.s ? ui.s.meter.thr : -45;
  }

  const meters = [];
  function updateAllMeters() {
    if (!ui.s) return;
    for (const m of meters) m(ui.s.meter, ui.s.me);
  }

  const minus = (n) => (n < 0 ? '−' + Math.abs(n) : String(n));

  function renderLegend() {
    const s = ui.s, m = s.meter, me = s.me;
    const air = $('#air');
    let cls = 'air', text;
    if (me.deafened) { cls += ' off'; text = 'Deafened'; }
    else if (me.muted) { cls += ' off'; text = 'Mic muted'; }
    else if (m.tx) { cls += ' on'; text = 'On air'; }
    else if (m.mode === 'ptt') text = s.ptt.label ? 'Hold ' + s.ptt.label + ' to talk' : 'Set a push to talk key';
    else text = 'Quiet';
    if (air.dataset.t !== cls + text) {
      air.dataset.t = cls + text;
      air.className = cls;
      put(air, h('span', { class: 'dot' }), text);
    }
    const sens = $('#sens');
    const st = m.mode === 'voice' ? 'v' + curThr() : 'p' + s.ptt.label;
    if (sens.dataset.t !== st) {
      sens.dataset.t = st;
      if (m.mode === 'voice') put(sens, 'Sensitivity ', h('b', { text: minus(curThr()) + ' dB' }));
      else put(sens, 'Push to talk', s.ptt.label ? ': ' : '', s.ptt.label ? h('b', { text: s.ptt.label }) : '');
    }
  }

  /* ------------------------------------------------------------ room card */

  function chip(text, heat, extra) {
    return h('span', { class: 'chip c-' + heat + (extra ? ' ' + extra : '') }, h('span', { class: 'dot' }), text);
  }

  function seg(options, current, onPick, disabled) {
    const box = h('div', { class: 'seg', role: 'radiogroup' });
    for (const [value, label] of options) {
      box.append(h('button', {
        type: 'button', role: 'radio', 'aria-checked': String(value === current), text: label, disabled: disabled,
        onclick: (e) => {
          box.querySelectorAll('button').forEach((b) => b.setAttribute('aria-checked', 'false'));
          e.currentTarget.setAttribute('aria-checked', 'true');
          onPick(value);
        },
      }));
    }
    return box;
  }

  function sw(on, onToggle, label, small) {
    const b = h('button', { type: 'button', class: 'switch' + (small ? ' small' : ''), role: 'switch', 'aria-checked': String(!!on), 'aria-label': label });
    b.addEventListener('click', () => {
      const v = b.getAttribute('aria-checked') !== 'true';
      b.setAttribute('aria-checked', String(v));
      onToggle(v);
    });
    return b;
  }

  const sendField = {};
  function field(key, value) {
    ui.form[key] = value;
    if (!sendField[key]) sendField[key] = debounce((v) => api('set_field', { key, value: v }), 250);
    sendField[key](value);
  }

  function input(key, opts) {
    const el = h('input', Object.assign({ class: 'input' + (opts.secret ? ' secret' : ''), type: 'text', value: ui.form[key] || '', spellcheck: 'false', autocomplete: 'off' }, opts.attrs || {}));
    el.addEventListener('input', () => field(key, el.value));
    el.addEventListener('keydown', (e) => { if (e.key === 'Enter') connect(); });
    return el;
  }

  function roomSignature(s) {
    const { info, ...c } = s.conn;  // the ping changes every second: updated in place, not by a rebuild
    const status = c.kind === 'none' || (c.kind === 'client' && c.state !== 'connected') ? s.status : '';
    return JSON.stringify([c, s.form.role, s.share || null, s.watching, status, ui.connErr, liveHere(s)]);
  }

  /** Whoever shares a screen in your room (not you): { name, uid (when you can watch) } or null. */
  function liveHere(s) {
    if (!s.people || !s.conn.room) return null;
    const here = s.people.rooms.find((r) => r.here);
    const live = here && here.members.find((m) => m.live && !m.you);
    return live ? { name: live.name, uid: live.watchable ? live.uid : null } : null;
  }

  async function connect() {
    ui.connErr = '';
    // Flush any typed field the debounce has not sent yet.
    for (const k of ['name', 'port', 'host_password', 'address', 'join_password']) {
      if (ui.form[k] !== undefined) await api('set_field', { key: k, value: ui.form[k] });
    }
    const r = await api('toggle_connection');
    if (r.error) ui.connErr = r.error;
    ui.roomSig = '';
    poll(true);
  }

  function renderRoom() {
    const s = ui.s;
    const sig = roomSignature(s);
    if (sig === ui.roomSig) {
      const el = $('#room .conn-info');
      if (el && el.textContent !== (s.conn.info || '')) el.textContent = s.conn.info || '';
      return;
    }
    const focused = document.activeElement && $('#room').contains(document.activeElement) ? document.activeElement.dataset.key : null;
    ui.roomSig = sig;
    const box = $('#room');
    const c = s.conn;
    let heat = 'low';
    const kids = [];

    if (c.kind === 'none') {
      const host = ui.form.role === 'host';
      const lastFail = s.status && s.status !== 'Not connected' ? s.status : '';
      kids.push(
        h('div', { class: 'spot-label' }, h('span', { text: 'Not connected' }), h('span', { text: host ? 'Your PC hosts' : 'A host or a server' })),
        h('div', { style: 'margin-top:10px' }, seg([['host', 'Host a room'], ['join', 'Join a room']], ui.form.role, (v) => {
          ui.form.role = v;
          api('set_field', { key: 'role', value: v }).then(() => { ui.roomSig = ''; poll(true); });
          ui.roomSig = '';
          renderRoom();
        })),
        h('label', { class: 'field' }, 'Your name', input('name', { attrs: { maxlength: '32', 'data-key': 'name' } }))
      );
      if (host) {
        kids.push(h('div', { class: 'two' },
          h('label', { class: 'field' }, 'Port', input('port', { attrs: { inputmode: 'numeric', maxlength: '5', 'data-key': 'port' } })),
          h('label', { class: 'field' }, 'Room password', input('host_password', { secret: true, attrs: { placeholder: 'Optional', 'data-key': 'host_password' } }))
        ));
      } else {
        const addr = input('address', { secret: !ui.showJoin, attrs: { placeholder: 'e.g. 85.10.20.30:50505', 'data-key': 'address' } });
        kids.push(
          h('label', { class: 'field' }, 'Address',
            h('div', { class: 'row' }, addr,
              h('button', { type: 'button', class: 'btn small ghost', text: ui.showJoin ? 'Hide' : 'Show', onclick: () => { ui.showJoin = !ui.showJoin; ui.roomSig = ''; renderRoom(); } })),
            h('span', { class: 'help', text: 'Server or host address. Hidden on screen for streaming.' })),
          h('label', { class: 'field' }, 'Password', input('join_password', { secret: true, attrs: { placeholder: 'If the room has one', 'data-key': 'join_password' } })),
          h('div', { class: 'checks' },
            h('label', { class: 'check-label' }, sw(ui.form.remember_address, (v) => { ui.form.remember_address = v; api('set_field', { key: 'remember_address', value: v }); }, 'Remember address', true), 'Remember address'),
            h('label', { class: 'check-label' }, sw(ui.form.remember_password, (v) => { ui.form.remember_password = v; api('set_field', { key: 'remember_password', value: v }); }, 'Remember password', true), 'Remember password'))
        );
      }
      kids.push(h('div', { class: 'spot-actions' },
        h('button', { class: 'btn primary', style: '--heat:var(--normal)', html: icon(host ? 'mic' : 'plus') + (host ? 'Start hosting' : 'Join'), onclick: connect })));
      const err = ui.connErr || lastFail;
      if (err) kids.push(h('p', { class: 'hint', style: 'color:var(--crit)', text: err }));
    } else if (c.kind === 'host') {
      heat = 'normal';
      const sh = s.share || {};
      kids.push(
        h('div', { class: 'spot-label' }, h('span', { text: 'Hosting' }), h('span', { class: 'conn-info', text: c.info })),
        h('p', { class: 'spot-title', text: 'Port ' + c.port }),
        h('div', { class: 'spot-meta' },
          chip(c.people === 1 ? 'Just you' : c.people + ' in the room', 'normal'),
          c.password ? chip('Password on', 'normal') : chip('No password', 'high')),
        h('div', { class: 'addr' },
          h('div', { class: 'addr-head', text: 'Friends join with' }),
          addrRow('Internet', 'internet', sh.internet, sh.internet_ok, sh.revealed && sh.revealed.internet),
          addrRow('Same network', 'lan', sh.lan, true, sh.revealed && sh.revealed.lan),
          h('p', { class: 'hint', text: 'Addresses stay hidden for streaming; Copy works without showing them. The Internet address works when your router forwards UDP ' + (sh.port || c.port) + ' to this PC.' }),
          sh.others && sh.others.length ? h('p', { class: 'hint', text: 'Other addresses on this PC (virtual LANs): ' + sh.others.join(', ') }) : null
        ),
        h('div', { class: 'spot-actions' }, h('button', { class: 'btn', text: 'Stop hosting', onclick: connect }))
      );
    } else if (c.state !== 'connected') {
      heat = 'high';
      kids.push(
        h('div', { class: 'spot-label' }, h('span', { text: 'Connecting' })),
        h('p', { class: 'spot-title', text: 'Knocking on the door' }),
        h('p', { class: 'hint', text: s.status }),
        h('div', { class: 'spot-actions' }, h('button', { class: 'btn', text: 'Cancel', onclick: connect }))
      );
    } else {
      heat = 'normal';
      const live = liveHere(s);
      const sharerName = live ? live.name : '';
      const sharerUid = live ? live.uid : null;
      let title = 'Connected';
      if (c.room) title = c.room.name;
      else if (s.people) {
        const hostRow = s.people.rooms[0].members.find((m) => m.host);
        if (hostRow) title = hostRow.name + "'s room";
      }
      const count = c.room ? c.room.people : c.people;
      kids.push(
        h('div', { class: 'spot-label' }, h('span', { text: c.room ? 'You are in' : 'Connected' }), h('span', { class: 'conn-info', text: c.info || '' })),
        h('p', { class: 'spot-title', text: title }),
        h('div', { class: 'spot-meta' },
          chip(count === 1 ? 'Just you' : count + ' here', 'normal'),
          c.admin ? chip('Admin', 'high') : null,
          c.sharing ? chip('You are live', 'crit', 'live') : null,
          sharerName ? chip(sharerName + ' is live', 'crit', 'live') : null,
          s.watching ? chip('Watching ' + s.watching.name, 'low') : null)
      );
      const actions = h('div', { class: 'spot-actions' });
      if (c.screen) {
        if (c.sharing || c.pending) actions.append(h('button', { class: 'btn danger-fill', html: icon('screen') + (c.pending && !c.sharing ? 'Cancel sharing' : 'Stop sharing'), onclick: () => act('stop_share') }));
        else actions.append(h('button', { class: 'btn primary', style: '--heat:var(--normal)', html: icon('screen') + 'Share screen', onclick: shareDialog }));
      }
      if (sharerUid !== null) actions.append(h('button', { class: 'btn', html: icon('eye') + 'Watch', onclick: () => watch(sharerUid) }));
      actions.append(h('button', { class: 'btn ghost', text: 'Leave', onclick: connect }));
      kids.push(actions);
    }
    box.style.setProperty('--heat', 'var(--' + heat + ')');
    put(box, ...kids);
    if (focused) {
      const el = box.querySelector('[data-key="' + focused + '"]');
      if (el) { el.focus(); const n = el.value.length; try { el.setSelectionRange(n, n); } catch (e) { /* number fields */ } }
    }
  }

  function addrRow(label, which, value, ok, revealed) {
    return h('div', { class: 'addr-row' },
      h('span', { class: 'lbl', text: label }),
      h('span', { class: 'val' + (value && value[0] === '•' ? ' dots' : ''), text: value || '', title: value && value[0] !== '•' ? value : null }),
      h('button', { class: 'btn small', html: icon('copy') + 'Copy', disabled: !ok, onclick: () => copy(which, label) }),
      h('button', { class: 'btn small ghost', text: revealed ? 'Hide' : 'Show', disabled: !ok, onclick: () => act('reveal', { which }) })
    );
  }

  async function copy(which, label) {
    const r = await api('copy_address', { which });
    if (r.error) { alertCard(r.error); return; }
    try {
      await navigator.clipboard.writeText(r.text);
    } catch (e) {
      const ta = h('textarea', { style: 'position:fixed;opacity:0' });
      ta.value = r.text;
      document.body.append(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    toast('Copied the ' + label.toLowerCase() + ' address.');
  }

  /* --------------------------------------------------------------- people */

  function peopleSignature(s) {
    if (!s.people) return 'none' + s.conn.kind;
    return JSON.stringify([s.people.rooms.map((r) => [r.id, r.name, r.here, r.members.map((m) =>
      [m.uid, m.name, m.you, m.host, m.live, m.muted, m.deafened, m.watchable, m.watching])]), s.moderate, s.conn.admin, ui.openPerson]);
  }

  function renderPeople() {
    const s = ui.s;
    const sig = peopleSignature(s);
    if (sig === ui.peopleSig) { applySpeaking(); return; }
    ui.peopleSig = sig;
    const box = $('#people');
    if (!s.people) {
      put(box, h('p', { class: 'empty-list', text: s.conn.kind === 'none'
        ? 'Nobody here yet. Host a room or join one, and everyone in it shows up here, lighting up while they talk.'
        : 'Waiting for the room…' }));
      return;
    }
    const admin = s.conn.kind === 'client' && s.conn.admin;
    const groups = s.people.rooms.map((r) => {
      const head = h('h2', { class: 'group-head' + (r.here ? ' here' : ''), ondblclick: () => { if (!r.here && r.id != null) joinRoom(r) } },
        h('span', { text: r.name }), h('span', { class: 'n', text: String(r.members.length) }),
        r.here && r.id != null && s.people.rooms.length > 1 ? h('span', { class: 'chip quiet', text: 'you are here' }) : null,
        h('span', { class: 'spacer' }),
        h('span', { class: 'h-actions' },
          !r.here && r.id != null ? h('button', { class: 'btn small ghost', text: 'Join', onclick: () => joinRoom(r) }) : null,
          admin && !r.lobby && r.id != null ? h('button', { class: 'icon-btn', html: icon('trash'), title: 'Delete room', 'aria-label': 'Delete room ' + r.name, onclick: () => deleteRoom(r) }) : null)
      );
      return h('section', { class: 'group' + (r.here ? '' : ' away') }, head, r.members.map((m) => personRow(m, r.here)));
    });
    put(box, ...groups);
    applySpeaking();
  }

  function personRow(m, here) {
    const s = ui.s;
    const cls = ['person'];
    if (m.you) cls.push('you');
    if (m.deafened) cls.push('deafened');
    else if (m.muted) cls.push('muted');
    if (ui.openPerson === m.uid && !m.you) cls.push('open');
    const glyph = m.deafened ? 'phonesOff' : m.muted ? 'micOff' : 'wave';
    const row = h('div', { class: cls.join(' '), 'data-uid': String(m.uid), tabindex: m.you ? null : '0',
      onclick: (e) => { if (!m.you && !e.target.closest('.panel')) togglePerson(m.uid); },
      onkeydown: (e) => { if ((e.key === 'Enter' || e.key === ' ') && e.target === row && !m.you) { e.preventDefault(); togglePerson(m.uid); } },
      oncontextmenu: (e) => { e.preventDefault(); if (!m.you) { ui.openPerson = m.uid; ui.peopleSig = ''; renderPeople(); } },
      ondblclick: () => { if (m.watchable) watch(m.uid); } },
      h('span', { class: 'ring', html: icon(glyph) }),
      h('span', { class: 'p-name', text: m.name }),
      h('span', { class: 'p-meta' },
        m.live ? chip('Live', 'crit', 'live') : null,
        m.watching ? chip('Watching', 'low') : null,
        m.deafened ? chip('Deafened', 'crit') : m.muted ? chip('Muted', 'crit') : null,
        m.host ? chip('Host', 'high') : null,
        m.you ? h('span', { class: 'chip quiet', text: 'you' }) : null)
    );
    if (ui.openPerson === m.uid && !m.you) row.append(personPanel(m, here));
    return row;
  }

  function personPanel(m, here) {
    const s = ui.s;
    const out = h('output', { text: m.vol + '%' });
    const send = throttle((v) => api('set_user_volume', { uid: m.uid, value: v }), 80);
    const range = h('input', { type: 'range', min: '0', max: '200', step: '5', value: String(m.vol), 'aria-label': 'Volume for ' + m.name });
    fillRange(range);
    range.addEventListener('input', () => { out.textContent = range.value + '%'; fillRange(range); send(+range.value); });
    const buttons = h('div', { class: 'row' },
      m.watchable ? h('button', { class: 'btn small primary', style: '--heat:var(--normal)', html: icon('eye') + 'Watch screen', onclick: () => watch(m.uid) }) : null,
      s.moderate ? h('button', { class: 'btn small ghost danger', text: 'Kick', onclick: () => kick(m, false) }) : null,
      s.moderate ? h('button', { class: 'btn small ghost danger', text: 'Ban', onclick: () => kick(m, true) }) : null);
    return h('div', { class: 'panel' },
      h('div', { class: 'lbl', text: here ? 'Volume for ' + m.name : m.name + ' is in another room. Their volume is kept for when you meet.' }),
      h('div', { class: 'vol' }, range, out),
      buttons.childNodes.length ? buttons : null);
  }

  function togglePerson(uid) {
    ui.openPerson = ui.openPerson === uid ? null : uid;
    ui.peopleSig = '';
    renderPeople();
  }

  function applySpeaking() {
    const s = ui.s;
    if (!s.people) return;
    const on = new Set();
    for (const r of s.people.rooms) for (const m of r.members) if (m.speaking) on.add(String(m.uid));
    document.querySelectorAll('#people .person').forEach((row) => row.classList.toggle('speaking', on.has(row.dataset.uid)));
  }

  async function joinRoom(r) {
    await act('join_room', { rid: r.id });
  }

  async function deleteRoom(r) {
    const n = r.members.length;
    const ok = await confirmBox({
      title: 'Delete the room ' + r.name + '?',
      body: n ? 'The ' + n + ' ' + (n === 1 ? 'person' : 'people') + ' in it move to the Lobby.' : 'Nobody is in it right now.',
      yes: 'Delete room',
    });
    if (ok) act('delete_room', { rid: r.id });
  }

  async function kick(m, ban) {
    const s = ui.s;
    const onServer = s.conn.kind === 'client' && s.conn.admin;
    let body;
    if (ban) body = "They can't join again, even after a restart, until you unban them under Bans. The ban covers their device and their Internet address.";
    else if (onServer) body = "They can't rejoin until everyone has left the room or the server restarts.";
    else body = "They can't rejoin until you stop and start hosting again.";
    const ok = await confirmBox({ title: (ban ? 'Ban ' : 'Kick ') + m.name + '?', body, yes: ban ? 'Ban' : 'Kick' });
    if (ok) { ui.openPerson = null; act('kick', { uid: m.uid, ban }); }
  }

  async function watch(uid) {
    const r = await act('watch', { uid });
    if (r.open) window.open(r.open, '_blank');
  }

  /* ---------------------------------------------------------------- tools */

  function renderTools() {
    const s = ui.s;
    const admin = s.conn.kind === 'client' && s.conn.admin && s.conn.rooms;
    const hint = s.conn.hint || '';
    const sig = JSON.stringify([s.tools, s.bans, admin, hint]);
    if (sig === ui.toolsSig) return;
    ui.toolsSig = sig;
    put($('#tools'),
      admin ? h('button', { class: 'btn small', html: icon('plus') + 'New room', onclick: newRoom }) : null,
      s.tools ? h('button', { class: 'btn small ghost', html: icon('shield') + (s.bans ? 'Bans (' + s.bans + ')' : 'Bans'), onclick: openBans }) : null,
      hint ? h('p', { class: 'hint', text: hint }) : null
    );
  }

  async function newRoom() {
    const name = await promptBox({ title: 'New room', label: 'Name', max: 24, yes: 'Make room' });
    if (name) act('new_room', { name });
  }

  /* ------------------------------------------------------------------ dock */

  function renderDock() {
    const me = ui.s.me;
    const m = $('#btn-mute'), d = $('#btn-deaf');
    const mk = (me.muted ? 1 : 0) + ':' + (ui.s.meter.tx ? 1 : 0);
    if (m.dataset.t !== mk) {
      m.dataset.t = mk;
      m.innerHTML = icon(me.muted ? 'micOff' : 'mic') + (me.muted ? 'Unmute mic' : 'Mute mic');
      m.classList.toggle('active', me.muted);
      m.classList.toggle('live', !me.muted && ui.s.meter.tx);
      m.setAttribute('aria-pressed', String(me.muted));
    }
    const dk = me.deafened ? '1' : '0';
    if (d.dataset.t !== dk) {
      d.dataset.t = dk;
      d.innerHTML = icon(me.deafened ? 'phonesOff' : 'phones') + (me.deafened ? 'Undeafen' : 'Deafen');
      d.classList.toggle('active', me.deafened);
      d.setAttribute('aria-pressed', String(me.deafened));
    }
  }

  /* ------------------------------------------------------- audio settings */

  function setting(name, control, desc, wide) {
    return h('div', { class: 'setting' },
      h('div', { class: 'name', text: name }),
      control && !wide ? control : null,
      control && wide ? h('div', { class: 'wide' }, control) : null,
      desc ? h('div', { class: 'desc', text: desc }) : null);
  }

  function slider(value, onChange, label) {
    const out = h('output', { text: value + '%' });
    const send = throttle(onChange, 80);
    const r = h('input', { type: 'range', min: '0', max: '200', step: '1', value: String(value), 'aria-label': label });
    fillRange(r);
    r.addEventListener('input', () => { out.textContent = r.value + '%'; fillRange(r); send(+r.value); });
    return h('div', { class: 'slider' }, r, out);
  }

  function openAudio() {
    ui.audioOpen = true;
    closeBans();
    renderAudio(true);
    $('#audio').hidden = false;
    $('#audio').scrollTop = 0;
  }

  function closeAudio() {
    ui.audioOpen = false;
    $('#audio').hidden = true;
    meters.splice(1);
  }

  function renderAudio(force) {
    const s = ui.s;
    const a = s.audio;
    const sig = JSON.stringify([a.mics, a.outs, a.mic, a.out, s.meter.mode, s.app.ptt]);
    if (!force && sig === ui.devicesSig) { updateAudioLive(); return; }
    ui.devicesSig = sig;
    const panel = $('#audio');
    const mic = h('select', { class: 'input', 'aria-label': 'Microphone' }, a.mics.map((d) => h('option', { value: d, text: d })));
    mic.value = a.mic;
    const out = h('select', { class: 'input', 'aria-label': 'Headphones' }, a.outs.map((d) => h('option', { value: d, text: d })));
    out.value = a.out;
    const devChanged = () => act('set_devices', { mic: mic.value, out: out.value });
    mic.addEventListener('change', devChanged);
    out.addEventListener('change', devChanged);

    const meterBox = h('div', { class: 'meter', id: 'meter2', role: 'slider', tabindex: '0', 'aria-label': 'Microphone level. Drag to set the sensitivity.' },
      h('div', { class: 'cells' }), h('div', { class: 'thr' }, h('i')));
    meters.splice(1);
    meters.push(makeMeter(meterBox));

    const voice = s.meter.mode === 'voice';
    put(panel, 
      h('div', { class: 'head' }, h('h2', { text: 'Audio' }),
        h('button', { class: 'icon-btn', html: icon('close'), 'aria-label': 'Close audio settings', onclick: closeAudio })),

      h('h3', { text: 'Devices' }),
      setting('Microphone', mic, null, true),
      setting('Headphones', out, null, true),
      setting('New headset plugged in?', h('button', { class: 'btn small', text: 'Refresh', onclick: () => act('refresh_devices').then(() => { ui.devicesSig = ''; }) }),
        'Refresh, then pick it in the lists above. On Linux, leave both on System default and choose the headset in the system sound settings.'),
      h('div', { class: 'setting', id: 'audio-err', hidden: !a.error }, h('div', { class: 'err', text: a.error })),

      h('h3', { text: 'Levels' }),
      setting('Mic volume', slider(a.in_gain, (v) => api('set_gain', { which: 'in', value: v }), 'Mic volume'), null, true),
      setting('Output volume', slider(a.out_gain, (v) => api('set_gain', { which: 'out', value: v }), 'Output volume'), null, true),

      h('h3', { text: 'Talking' }),
      setting('Talk mode', seg([['voice', 'Voice activity'], ['ptt', 'Push to talk']], s.meter.mode, (v) => act('set_mode', { mode: v }).then(() => { ui.devicesSig = ''; })), s.app.ptt ? null : 'Push to talk needs the pynput library. Start Telsiz with run_windows.bat or run_linux.sh to install it.', true),
      voice ? null : setting('Push to talk key', h('button', { class: 'btn small', id: 'ptt-key', onclick: () => act('capture_key') }), 'A keyboard key or a mouse side button. While a game runs as administrator, Telsiz must run as administrator too.'),
      setting(voice ? 'Sensitivity' : 'Input level', meterBox,
        voice ? 'Click or drag on the bar. Below the white mark you stay quiet; above it you are on air and the bar lights up fully. Keep keyboard noise below the mark.'
              : 'The bar lights up fully while you hold your key and talk.', true),

      h('h3', { text: 'Checks' }),
      setting('Hear myself (mic test)', sw(a.loopback, (v) => api('set_loopback', { on: v }), 'Hear myself')),
      setting('Push to talk sounds', sw(a.chimes, (v) => api('set_chimes', { on: v }), 'Push to talk sounds'),
        'A rising chime when you go on air and a falling one when you let go. Only you hear them.'),

      h('h3', { text: 'Updates' }),
      setting('Check for updates', sw(s.update.auto, (v) => api('update_auto', { on: v }), 'Check for updates'),
        'Once a day Telsiz asks GitHub whether a newer version exists. Nothing is installed without asking you.'),
      h('div', { class: 'setting' },
        h('div', { class: 'name', id: 'upd-line', text: updateLine(s.update) }),
        h('button', { class: 'btn small', id: 'upd-btn', text: 'Check now', onclick: () => act('update_check') }),
        h('div', { class: 'err', id: 'upd-err', hidden: s.update.state !== 'error', text: s.update.message })),

      h('div', { class: 'buttons' },
        h('button', { class: 'btn primary', style: '--heat:var(--normal)', text: 'Done', onclick: closeAudio }),
        h('button', { class: 'btn ghost danger', text: 'Quit Telsiz', onclick: quitApp })),
      h('p', { class: 'foot', text: s.app.name + ' ' + s.app.version + '. Settings are saved as you change them.' + (s.app.tray ? ' Closing the window keeps Telsiz in the tray; Exit there quits.' : '') })
    );
    updateAudioLive();
  }

  function updateAudioLive() {
    const s = ui.s;
    const line = $('#upd-line');
    if (line) {
      const t = updateLine(s.update);
      if (line.textContent !== t) line.textContent = t;
      const busy = ['checking', 'working', 'restarting'].includes(s.update.state);
      $('#upd-btn').disabled = busy;
      const ue = $('#upd-err');
      ue.hidden = s.update.state !== 'error';
      if (ue.textContent !== s.update.message) ue.textContent = s.update.message;
    }
    const key = $('#ptt-key');
    if (key) {
      const t = s.ptt.capturing ? 'Press a key or mouse side button…' : s.ptt.label ? 'Key: ' + s.ptt.label : 'Set key';
      if (key.textContent !== t) key.textContent = t;
    }
    const err = $('#audio-err');
    if (err) {
      err.hidden = !s.audio.error;
      const e = err.querySelector('.err');
      if (e.textContent !== s.audio.error) e.textContent = s.audio.error;
    }
  }

  async function quitApp() {
    const ok = await confirmBox({ title: 'Quit Telsiz?', body: 'You leave the room, and push to talk stops working until you start Telsiz again.', yes: 'Quit' });
    if (ok) {
      await api('quit');
      document.body.innerHTML = '<p style="padding:24px;color:#8D9DC2;font:14px sans-serif">Telsiz has quit. You can close this window.</p>';
    }
  }

  /* ----------------------------------------------------------------- bans */

  async function openBans() {
    closeAudio();
    ui.bansOpen = true;
    $('#bans').hidden = false;
    await renderBans();
  }

  function closeBans() {
    ui.bansOpen = false;
    $('#bans').hidden = true;
  }

  async function renderBans() {
    const b = await api('bans');
    if (b.error) { alertCard(b.error); return; }
    ui.bansCount = ui.s ? ui.s.bans : -1;
    put($('#bans'), 
      h('div', { class: 'head' }, h('h2', { text: 'Bans' }),
        h('button', { class: 'icon-btn', html: icon('close'), 'aria-label': 'Close bans', onclick: closeBans })),
      h('p', { class: 'help', style: 'font-size:12.5px;margin:0 0 6px', text: b.title + '. Banned people can’t join until you unban them. Only names and dates are shown, never addresses.' }),
      b.entries.length
        ? b.entries.map((e, i) => h('div', { class: 'ban-row' },
            h('div', null, h('div', { class: 'who', text: e.name }), h('div', { class: 'when', text: e.when ? 'banned ' + e.when : '' })),
            h('button', { class: 'btn small', text: 'Unban', onclick: async () => { await act('unban', { index: i }); setTimeout(renderBans, 300); } })))
        : h('p', { class: 'empty-list', text: 'Nobody is banned.' })
    );
  }

  /* --------------------------------------------------------------- modals */

  function modal(build, opts) {
    return new Promise((resolve) => {
      const m = $('#modal');
      ui.modalOpen = true;
      const locked = () => !!(opts && opts.locked && opts.locked());  // Esc and outside clicks do nothing then
      const done = (v) => { ui.modalOpen = false; m.hidden = true; put(m); document.removeEventListener('keydown', esc, true); resolve(v); };
      const esc = (e) => { if (e.key === 'Escape') { e.stopPropagation(); if (!locked()) done(null); } };
      document.addEventListener('keydown', esc, true);
      put(m, h('div', { class: 'dialog', role: 'dialog', 'aria-modal': 'true' }, build(done)));
      m.onclick = (e) => { if (e.target === m && !locked()) done(null); };
      m.hidden = false;
      // Inputs first; for yes/no questions the safe answer (Cancel) has focus, so Enter never kicks anyone.
      const first = m.querySelector('input, select') || m.querySelector('.actions .btn.ghost') || m.querySelector('.btn');
      if (first) first.focus();
    });
  }

  function confirmBox(o) {
    return modal((done) => [
      h('h2', { text: o.title }),
      o.body ? h('p', { text: o.body }) : null,
      h('div', { class: 'actions' },
        h('button', { class: 'btn ghost', text: 'Cancel', onclick: () => done(false) }),
        h('button', { class: 'btn danger-fill', text: o.yes, onclick: () => done(true) }))
    ]).then((v) => !!v);
  }

  function promptBox(o) {
    return modal((done) => {
      const inp = h('input', { class: 'input', type: 'text', maxlength: String(o.max || 40), 'aria-label': o.label });
      inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') done(inp.value.trim() || null); });
      return [
        h('h2', { text: o.title }),
        h('label', { class: 'field' }, o.label, inp),
        h('div', { class: 'actions' },
          h('button', { class: 'btn ghost', text: 'Cancel', onclick: () => done(null) }),
          h('button', { class: 'btn primary', style: '--heat:var(--normal)', text: o.yes, onclick: () => done(inp.value.trim() || null) }))
      ];
    });
  }

  async function shareDialog() {
    const r = await api('screens');
    if (r.error) { alertCard(r.error); return; }
    let preset = r.preset;
    const pick = h('select', { class: 'input', 'aria-label': 'Screen' }, r.screens.map((x) => h('option', { value: String(x.index), text: x.label })));
    pick.value = String(r.screens.some((x) => x.index === r.pick) ? r.pick : r.screens[0].index);
    const opts = h('div', { class: 'options', role: 'radiogroup' });
    for (const p of r.presets) {
      const [name, rest] = p.label.split(/:\s*/, 2);
      opts.append(h('button', { type: 'button', class: 'option', role: 'radio', 'aria-checked': String(p.key === preset),
        onclick: (e) => { preset = p.key; opts.querySelectorAll('.option').forEach((o) => o.setAttribute('aria-checked', 'false')); e.currentTarget.setAttribute('aria-checked', 'true'); } },
        h('b', { text: name }), h('span', { text: rest || '' })));
    }
    const go = await modal((done) => [
      h('h2', { text: 'Share your screen' }),
      h('label', { class: 'field' }, 'Screen', pick),
      opts,
      h('p', { style: 'margin-top:12px', text: 'Everyone in this room who presses Watch sees this whole screen, including pop-ups and notifications. Your microphone keeps working as usual.' }),
      h('div', { class: 'actions' },
        h('button', { class: 'btn ghost', text: 'Cancel', onclick: () => done(false) }),
        h('button', { class: 'btn primary', style: '--heat:var(--normal)', html: icon('screen') + 'Start sharing', onclick: () => done(true) }))
    ]);
    if (go) act('start_share', { monitor: +pick.value, preset });
  }

  /* -------------------------------------------------------------- updates */

  const UPDATE_HOW = {
    exe: 'Telsiz downloads the new Telsiz.exe from GitHub, swaps it in and restarts.',
    source: 'Telsiz downloads the new version from GitHub, replaces the files in its folder and restarts.',
    git: 'This folder is a git clone, so Telsiz runs git pull and restarts.',
  };
  const updateBusy = () => !!ui.s && ['working', 'restarting'].includes(ui.s.update.state);
  const updateSig = (s) => JSON.stringify([s.update.state, s.update.latest, s.update.step, s.update.progress, s.update.message, s.update.kind, s.conn.kind === 'none']);

  function updateDialog() {
    ui.updateOpen = true;
    ui.updateSig = updateSig(ui.s);
    modal((done) => {
      ui.updateDone = done;
      ui.updateBox = h('div', { class: 'update' });
      renderUpdate();
      return ui.updateBox;
    }, { locked: updateBusy }).then((v) => {
      ui.updateOpen = false;
      ui.updateBox = ui.updateDone = null;
      if (v === null) api('update_later');  // Esc or a click outside: same as Later
    });
  }

  function renderUpdate() {
    const u = ui.s.update;
    const box = ui.updateBox;
    if (!box) return;
    const inRoom = ui.s.conn.kind !== 'none';
    const close = (name) => () => { api(name); if (ui.updateDone) ui.updateDone(false); };
    if (u.state === 'working' || u.state === 'restarting') {
      const pct = u.progress == null ? null : Math.round(u.progress * 100);
      put(box,
        h('h2', { text: u.state === 'restarting' ? 'Restarting Telsiz' : 'Updating to Telsiz ' + u.latest }),
        h('div', { class: 'progress' + (pct == null ? ' busy' : ''), role: 'progressbar', 'aria-valuenow': pct == null ? null : String(pct), 'aria-valuemin': '0', 'aria-valuemax': '100' },
          h('i', { style: pct == null ? null : 'width:' + pct + '%' })),
        h('p', { class: 'step', text: (u.step || 'Working…') + (pct == null ? '' : '  ' + pct + '%') }),
        h('p', { text: u.state === 'restarting'
          ? 'Telsiz closes and opens again in a moment.' + (inRoom ? ' It rejoins the room by itself.' : '')
          : 'Keep Telsiz open. It restarts by itself when the update is in place.' }));
      return;
    }
    const failed = u.state === 'error';
    const manual = u.kind === 'manual';
    const notes = (u.notes || '').trim();
    put(box,
      h('div', { class: 'u-kind', html: icon('download') + '<span>Update</span>' }),
      h('h2', { text: failed ? 'The update didn’t finish' : 'Telsiz ' + u.latest + ' is out' }),
      h('p', { text: failed ? u.message : 'You have ' + u.version + '.' }),
      notes && !failed ? h('div', { class: 'notes', tabindex: '0', 'aria-label': 'What is new', text: notes }) : null,
      !failed ? h('p', { text: manual
        ? 'Telsiz can’t update itself here (' + (u.why || 'unknown reason') + '). Get the new version from the download page.'
        : UPDATE_HOW[u.kind] + (inRoom ? ' You drop out of the room for a few seconds and rejoin by itself.' : '') }) : null,
      h('div', { class: 'actions' },
        h('button', { class: 'btn ghost', text: failed ? 'Close' : 'Later', onclick: close('update_later') }),
        !failed && !manual ? h('button', { class: 'btn ghost', text: 'Skip this version', onclick: close('update_skip') }) : null,
        manual || failed ? h('button', { class: 'btn' + (manual ? ' primary' : ''), style: manual ? '--heat:var(--normal)' : null, text: 'Open download page', onclick: close('update_page') }) : null,
        !manual ? h('button', { class: 'btn primary', style: '--heat:var(--normal)', html: icon('download') + (failed ? 'Try again' : 'Update now'), onclick: () => act('update_install') }) : null));
  }

  function applyUpdate(s) {
    const u = s.update;
    if (!u) return;
    if (u.prompt && !ui.updateOpen && !ui.modalOpen) updateDialog();
    if (ui.updateOpen) {
      if (!u.prompt && !updateBusy()) { if (ui.updateDone) ui.updateDone(false); }
      else {
        const sig = updateSig(s);
        if (sig !== ui.updateSig) {
          ui.updateSig = sig;
          const hadFocus = ui.updateBox && ui.updateBox.contains(document.activeElement);
          renderUpdate();
          const f = hadFocus && (ui.updateBox.querySelector('.actions .btn.ghost') || ui.updateBox.querySelector('.btn'));
          if (f) f.focus();
        }
      }
    }
    if (u.just_updated && !ui.updatedToast) {
      ui.updatedToast = true;
      toast('Telsiz is now ' + u.just_updated + '.');
    }
  }

  function updateLine(u) {
    if (u.state === 'checking') return 'Checking GitHub…';
    if (u.state === 'current') return 'Telsiz ' + u.version + ' is the newest version.';
    if (u.state === 'available') return 'Telsiz ' + u.latest + ' is available.';
    if (u.state === 'working' || u.state === 'restarting') return u.step || 'Updating…';
    return 'You have Telsiz ' + u.version + '.';
  }

  /* ----------------------------------------------------------------- poll */

  let polling = false;
  async function poll(once) {
    if (polling && once) return;
    let s = null;
    try {
      const r = await fetch('state', { cache: 'no-store' });
      s = await r.json();
      ui.failures = 0;
    } catch (e) {
      ui.failures++;
    }
    if (s) apply(s);
    else if (ui.failures === 20) $('#status').textContent = 'Telsiz stopped. Start it again to reconnect.';
    if (!once) setTimeout(() => poll(false), ui.failures > 20 ? 2000 : 50);
  }

  function apply(s) {
    const first = !ui.s;
    ui.s = s;
    if (first) {
      ui.form = Object.assign({}, s.form);
      $('#btn-hide').hidden = !(s.app.tray && !s.app.browser);
    }
    const day = new Date().toDateString();
    if (day !== ui.lastDay) { ui.lastDay = day; drawIsolines(); }
    if (s.conn.kind === 'none' && ui.prevConn && ui.prevConn !== 'none') ui.form = Object.assign({}, s.form);

    const st = $('#status');
    if (st.textContent !== s.status) st.textContent = s.status;
    updateAllMeters();
    renderLegend();
    renderRoom();
    renderPeople();
    renderTools();
    renderDock();
    if (ui.audioOpen) renderAudio(false);
    if (ui.bansOpen && ui.bansCount !== s.bans) renderBans();

    const err = s.audio.error;
    const hasCard = !!$('#notices [data-kind="audio"]');
    // "Got it" hides the card until the problem changes (or goes away and comes back)
    if (err && !hasCard && !ui.audioOpen && err !== ui.audioDismissed) alertCard('Your microphone or headphones could not start. Open Audio to pick another device.', { kind: 'audio', kindLabel: 'Audio problem', action: 'Open Audio', onAction: openAudio, onDismiss: () => { ui.audioDismissed = err; } });
    if (!err) ui.audioDismissed = '';
    if (!err && hasCard) $('#notices [data-kind="audio"]').remove();

    // A small isoline ripple when you arrive somewhere: connected, hosting, or a new room.
    const connKey = s.conn.kind === 'none' ? 'none' : s.conn.kind + ':' + (s.conn.state || '');
    const roomId = s.conn.room ? s.conn.room.id : null;
    const arrived = (connKey === 'host:connected' || connKey === 'client:connected') && ui.prevConn !== connKey;
    if ((arrived || (roomId !== null && ui.prevRoom !== null && roomId !== ui.prevRoom)) && ui.prevConn) {
      requestAnimationFrame(() => ripple($('#room .spot-title'), 'normal'));
    }
    ui.prevConn = connKey;
    ui.prevRoom = roomId;

    applyUpdate(s);

    if (s.app.first_run && !ui.firstRunHandled) {
      ui.firstRunHandled = true;
      api('first_run_done');
      openAudio();
    }
  }

  /* ----------------------------------------------------------------- boot */

  meters.push(makeMeter($('#meter')));
  $('#btn-audio').innerHTML = icon('sliders');
  $('#btn-hide').innerHTML = icon('eyeOff');
  $('#btn-audio2').innerHTML = icon('sliders') + 'Audio';
  $('#btn-audio').addEventListener('click', () => (ui.audioOpen ? closeAudio() : openAudio()));
  $('#btn-audio2').addEventListener('click', () => (ui.audioOpen ? closeAudio() : openAudio()));
  $('#btn-hide').addEventListener('click', () => api('hide_window'));
  $('#btn-mute').addEventListener('click', () => act('toggle_mute'));
  $('#btn-deaf').addEventListener('click', () => act('toggle_deafen'));
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || ui.modalOpen) return;
    if (ui.audioOpen) closeAudio();
    else if (ui.bansOpen) closeBans();
    else if (ui.openPerson !== null) { ui.openPerson = null; ui.peopleSig = ''; renderPeople(); }
  });
  document.addEventListener('contextmenu', (e) => { if (!e.target.closest('input, textarea, .a-body, .title small')) e.preventDefault(); });
  poll(false);
})();
