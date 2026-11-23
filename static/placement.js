// The print, grabbed and dragged on the page preview itself: move it with a
// finger or the pointer, resize it with a pinch, ctrl/cmd + scroll (a
// trackpad pinch), the Smaller/Larger buttons or + and -. It moves live:
// the browser composes the page from two layers the server splits it into
// (api/page-layers: the plate, and everything over it), moves the plate
// with no request per move, and clamps it the way the server does. Release
// saves the placement (per image and art layout, api/placement), then the
// server's real dithered picture quietly replaces the live view.
// Shared by the pane (index.html) and the review page (review.html). All
// URLs are relative: the pages are also served under a prefix.
(function () {
  const DEFAULT = {x: 0, y: 0, scale: 1};
  const NUDGE = 0.01;          // arrow keys move by 1% of the box
  const STEP = 0.1;            // Smaller/Larger and +/- change the scale by 10 points
  const PAUSE_SAVE_MS = 500;   // keys, buttons and the wheel save once they pause

  const css = `
  .pl-stage { position:relative; width:100%; }
  .pl-stage.fit { container-type:size; height:100%;
    display:flex; align-items:center; justify-content:center; }
  .pl-frame { position:relative; aspect-ratio:var(--ar, 1.333); margin:0 auto;
    width:min(100%, calc(var(--pl-maxh, 100000px) * var(--ar, 1.333)));
    background:#fff; border:1px solid var(--bix-border-strong, #45475a); border-radius:8px;
    overflow:hidden; user-select:none; -webkit-user-select:none; }
  .pl-stage.fit .pl-frame { width:min(100cqw, calc(100cqh * var(--ar, 1.333))); }
  .pl-frame img { display:block; max-width:none; pointer-events:none; -webkit-user-drag:none; }
  .pl-rest, .pl-page { position:absolute; inset:0; width:100%; height:100%; }
  .pl-live { position:absolute; inset:0; background:#fff; }
  .pl-live[hidden] { display:none; }
  .pl-box { position:absolute; overflow:hidden; }
  .pl-art { position:absolute; }
  .pl-grab { position:absolute; touch-action:none; cursor:grab; }
  .pl-grab.dragging { cursor:grabbing; }
  .pl-grab:hover, .pl-grab.dragging, .pl-stage.outline .pl-grab {
    outline:1.5px dashed var(--bix-border-strong, #45475a); outline-offset:-1.5px; }
  .pl-grab:focus-visible { outline:2px solid var(--bix-accent, #fab387); outline-offset:-2px; }
  .pl-ctl { display:flex; flex-direction:column; gap:6px; }
  .pl-row { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .pl-ctl button { min-height:44px; min-width:44px; padding:0 14px; border-radius:8px;
    font:inherit; font-size:14px; cursor:pointer; background:none;
    color:var(--bix-text, #cdd6f4); border:1px solid var(--bix-border-strong, #45475a); }
  .pl-ctl button:disabled { opacity:.45; cursor:default; }
  .pl-pct { font-family:var(--bix-mono, ui-monospace, Menlo, monospace); min-width:4.5ch;
    text-align:center; font-variant-numeric:tabular-nums; color:var(--bix-text, #cdd6f4); }
  /* Holds its line when empty, so a status coming and going never resizes the picture. */
  .pl-status { font-size:13px; line-height:18px; min-height:18px; color:var(--bix-text-soft, #a6adc8); }
  .pl-status.err { color:var(--bix-danger, #f38ba8); }
  .pl-hint { font-size:12px; color:var(--bix-text-muted, #7f849c); }`;
  if (!document.getElementById('pl-css')) {
    const s = document.createElement('style');
    s.id = 'pl-css'; s.textContent = css;
    document.head.appendChild(s);
  }

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const round4 = v => Math.round(v * 1e4) / 1e4;
  const same = (a, b) => Math.abs(a.x - b.x) < 1e-4 && Math.abs(a.y - b.y) < 1e-4 &&
                         Math.abs(a.scale - b.scale) < 1e-4;

  async function postJSON(url, body) {
    const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify(body)});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
    return j;
  }
  async function fetchOk(url) {
    const r = await fetch(url);
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || ('HTTP ' + r.status));
    }
    return r;
  }

  // mount(target, opts) → {refresh(), reload(), settle(), destroy()}
  // target: one element for the picture and its controls, or {stage,
  //   controls} to put them in two places.
  // opts.restUrl(): the picture shown at rest (the panel simulation).
  // opts.file: the print to drag; null shows the picture alone (a test
  //   pattern, or no print).
  // opts.screen: the print screen the picture is (default: the panel's).
  // opts.layoutName: the art layout the placement is for, in words.
  // opts.aspect: the picture's width / height until it has loaded.
  // opts.fit: size the picture to fit its stage both ways.
  // opts.plainWheel: the wheel resizes without ctrl/cmd (a page that never
  //   scrolls); opts.outline: always outline the print's area.
  // opts.onSaved(animal): the server's summary after each save.
  // refresh() reloads the picture at rest; reload() the layers too (the
  //   image was rotated). settle() resolves once every change is saved:
  //   true, or false when the last save failed (the status says why).
  function mount(target, opts) {
    const file = opts.file || null;
    const stageHost = target.stage || target;
    const ctlHost = target.controls || target;
    stageHost.innerHTML = `
      <div class="pl-stage${opts.fit ? ' fit' : ''}${opts.outline ? ' outline' : ''}">
        <div class="pl-frame">
          <img class="pl-rest" alt="${opts.alt || 'The page as the panel will show it'}">
          <div class="pl-live" hidden>
            <div class="pl-box"><img class="pl-art" alt=""></div>
            <img class="pl-page" alt="">
          </div>
        </div>
      </div>`;
    const frame = stageHost.querySelector('.pl-frame');
    const rest = frame.querySelector('.pl-rest');
    const live = frame.querySelector('.pl-live');
    const boxEl = frame.querySelector('.pl-box');
    const artEl = frame.querySelector('.pl-art');
    const pageEl = frame.querySelector('.pl-page');
    if (opts.aspect) frame.style.setProperty('--ar', opts.aspect);

    let gone = false, restSeq = 0, restUrl = null, layerSeq = 0;
    let L = null;             // the layers: geometry in frame px, and the images
    let draft = {...DEFAULT}; // the placement shown, already clamped
    let saved = true, saveSeq = 0, saving = Promise.resolve(), saveTimer = null;
    let changes = 0;          // bumps on every change, so a stale reload can't hide a newer one
    let grab = null, status = null, pctEl = null, ctl = null;

    async function loadRest() {
      const mine = ++restSeq;
      try {
        const blob = await (await fetchOk(opts.restUrl())).blob();
        if (mine !== restSeq || gone) return false;
        const url = URL.createObjectURL(blob);
        await new Promise((ok, bad) => {
          rest.onload = ok;
          rest.onerror = () => bad(new Error('the picture did not decode'));
          rest.src = url;
        });
        if (restUrl) URL.revokeObjectURL(restUrl);
        restUrl = url;
        frame.style.setProperty('--ar', rest.naturalWidth / rest.naturalHeight);
        return mine === restSeq;
      } catch (e) {
        if (mine === restSeq && !gone) say('Preview failed: ' + e.message, true);
        return false;
      }
    }

    const api = {refresh: loadRest, reload: loadRest, settle: async () => true,
                 destroy() { gone = true; if (restUrl) URL.revokeObjectURL(restUrl);
                             stageHost.innerHTML = ''; if (ctlHost !== stageHost) ctlHost.innerHTML = ''; }};
    function say(text, err) {
      if (!status) return;
      status.textContent = text;
      status.className = 'pl-status' + (err ? ' err' : '');
    }
    if (!file) {
      if (ctlHost !== stageHost) ctlHost.innerHTML = '';
      loadRest();
      return api;
    }

    // ── The print, and its controls ──
    frame.insertAdjacentHTML('beforeend',
      `<div class="pl-grab" tabindex="0" role="img" hidden
            aria-label="The print. Drag to move it, pinch or ctrl + scroll to resize it; arrow keys move it, plus and minus resize it."></div>`);
    grab = frame.querySelector('.pl-grab');
    const ctlHtml = `
      <div class="pl-ctl">
        <div class="pl-row">
          <button type="button" data-act="smaller" aria-label="Smaller">Smaller</button>
          <span class="pl-pct" aria-label="Scale"></span>
          <button type="button" data-act="larger" aria-label="Larger">Larger</button>
          <button type="button" data-act="reset">Reset to automatic</button>
        </div>
        <div class="pl-row"><span class="pl-status" role="status"></span></div>
        <div class="pl-hint">Drag the print to move it; pinch or ctrl + scroll to resize.
          For the ${opts.layoutName || 'current layout'}.</div>
      </div>`;
    if (ctlHost === stageHost) stageHost.insertAdjacentHTML('beforeend', ctlHtml);
    else ctlHost.innerHTML = ctlHtml;
    ctl = ctlHost.querySelector('.pl-ctl');
    status = ctl.querySelector('.pl-status');
    pctEl = ctl.querySelector('.pl-pct');
    const btns = [...ctl.querySelectorAll('button')];
    const setReady = on => { grab.hidden = !on; for (const b of btns) b.disabled = !on; };
    setReady(false);

    // The server's clamp (screens.normalize_placement, then _placed_centre:
    // the subject's centre stays inside the box), so what shows is what saves.
    function clampP(p) {
      const [bx0, by0, bx1, by1] = L.box;
      const bw = bx1 - bx0, bh = by1 - by0;
      const sh = L.limits.shift, [s0, s1] = L.limits.scale;
      const x = clamp(p.x, -sh, sh), y = clamp(p.y, -sh, sh);
      const cx = clamp(L.auto[0] + x * bw, bx0, bx1), cy = clamp(L.auto[1] + y * bh, by0, by1);
      return {x: round4((cx - L.auto[0]) / bw), y: round4((cy - L.auto[1]) / bh),
              scale: round4(clamp(p.scale, s0, s1))};
    }
    const centreOf = p => [L.auto[0] + p.x * (L.box[2] - L.box[0]),
                           L.auto[1] + p.y * (L.box[3] - L.box[1])];
    const pct = (v, of) => (v / of * 100) + '%';

    function placeBox() {
      const [W, H] = L.size, [x0, y0, x1, y1] = L.box;
      for (const el of [boxEl, grab]) {
        Object.assign(el.style, {left: pct(x0, W), top: pct(y0, H),
                                 width: pct(x1 - x0, W), height: pct(y1 - y0, H)});
      }
      // The fades the art gets where the box cuts it, as CSS masks.
      const f = L.fades, bw = x1 - x0, bh = y1 - y0, g = [];
      if (f.left) g.push(`linear-gradient(to right, transparent, #000 ${pct(f.left, bw)})`);
      if (f.right) g.push(`linear-gradient(to left, transparent, #000 ${pct(f.right, bw)})`);
      if (f.top) g.push(`linear-gradient(to bottom, transparent, #000 ${pct(f.top, bh)})`);
      if (f.bottom) g.push(`linear-gradient(to top, transparent, #000 ${pct(f.bottom, bh)})`);
      const m = g.join(', ') || 'none';
      boxEl.style.setProperty('mask-image', m);
      boxEl.style.setProperty('-webkit-mask-image', m);
      boxEl.style.setProperty('mask-composite', 'intersect');
      boxEl.style.setProperty('-webkit-mask-composite', 'source-in');
    }

    // The plate where `draft` puts it: its layer, drawn at its own
    // placement, scaled about that centre and moved to the draft's.
    function draw() {
      pctEl.textContent = Math.round(draft.scale * 100) + '%';
      if (!L || !L.art) return;
      const a = L.art, [x0, y0, x1, y1] = L.box, bw = x1 - x0, bh = y1 - y0;
      const k = draft.scale / a.scale, [cx, cy] = centreOf(draft);
      const r = a.rect.map((v, i) => (i % 2 ? cy + (v - a.centre[1]) * k : cx + (v - a.centre[0]) * k));
      Object.assign(artEl.style, {left: pct(r[0] - x0, bw), top: pct(r[1] - y0, bh),
                                  width: pct(r[2] - r[0], bw), height: pct(r[3] - r[1], bh)});
    }

    async function loadLayers() {
      const mine = ++layerSeq;
      const q = new URLSearchParams({file});
      if (opts.screen) q.set('screen', opts.screen);
      try {
        const j = await (await fetchOk('api/page-layers?' + q)).json();
        if (mine !== layerSeq || gone) return;
        // Decoded first, then swapped in with their geometry in one go.
        const srcs = [j.page, j.art ? j.art.image : null].filter(Boolean);
        await Promise.all(srcs.map(src => new Promise((ok, bad) => {
          const im = new Image();
          im.onload = ok; im.onerror = () => bad(new Error('a layer did not decode'));
          im.src = src;
        })));
        if (mine !== layerSeq || gone) return;
        pageEl.src = j.page;
        if (j.art) artEl.src = j.art.image;
        L = j;
        live.style.background = j.paper;
        if (!L.art) { setReady(false); return; }   // no print could be drawn
        // Nothing waiting to save and no finger down: the server's word.
        if (saved && !saveTimer && !gesture) draft = {...L.placement};
        placeBox(); draw(); setReady(true);
      } catch (e) {
        if (mine === layerSeq && !gone) say('Could not load the print to drag: ' + e.message, true);
      }
    }

    function showLive() { changes++; draw(); live.hidden = false; }
    // Back to the server's picture once it shows what was saved.
    async function settleView() {
      const at = changes;
      const ok = await loadRest();
      loadLayers();
      if (ok && at === changes && !gesture && saved && !saveTimer) live.hidden = true;
    }

    // Saves run one after another so the last change is the one stored;
    // only the latest reports its outcome.
    function save() {
      clearTimeout(saveTimer);
      saveTimer = null;
      const mine = ++saveSeq;
      const body = same(draft, DEFAULT) ? {file, layout: L.layout, reset: true}
        : {file, layout: L.layout, x: draft.x, y: draft.y, scale: draft.scale};
      say('Saving…');
      saved = false;
      saving = saving.then(() => postJSON('api/placement', body)).then(j => {
        if (mine !== saveSeq) return;
        saved = true;
        if (gone) return;
        say('Saved');
        if (opts.onSaved) opts.onSaved(j.animal);
        settleView();
      }, e => {
        if (mine !== saveSeq) return;
        saved = false;
        if (!gone) say('Save failed: ' + e.message, true);
      });
    }
    function saveSoon(ms) {
      say('Saving…');
      clearTimeout(saveTimer);
      saveTimer = setTimeout(save, ms);
    }
    function set(p) { draft = clampP(p); showLive(); }

    // ── Pointers: one moves the print, two pinch it ──
    const pointers = new Map();   // pointerId → [x, y] in frame px
    let gesture = null;           // {prev: {m, d}, moved}
    const toFrame = e => {
      const r = frame.getBoundingClientRect();
      return [(e.clientX - r.left) * L.size[0] / r.width, (e.clientY - r.top) * L.size[1] / r.height];
    };
    function spread() {
      const ps = [...pointers.values()];
      const m = [ps.reduce((s, p) => s + p[0], 0) / ps.length, ps.reduce((s, p) => s + p[1], 0) / ps.length];
      const d = ps.length < 2 ? 0 : ps.reduce((s, p) => s + Math.hypot(p[0] - m[0], p[1] - m[1]), 0) / ps.length;
      return {m, d};
    }
    // Scale by k about the frame point `at`, and move by (dx, dy): the
    // draft that puts the print there, clamped.
    function moved(p, k, at, dx, dy) {
      const [cx, cy] = centreOf(p), [bx0, by0, bx1, by1] = L.box;
      const s = clamp(p.scale * k, L.limits.scale[0], L.limits.scale[1]);
      const kk = s / p.scale;
      const nx = at[0] + (cx - at[0]) * kk + dx, ny = at[1] + (cy - at[1]) * kk + dy;
      return {x: (nx - L.auto[0]) / (bx1 - bx0), y: (ny - L.auto[1]) / (by1 - by0), scale: s};
    }
    grab.addEventListener('pointerdown', e => {
      if (!L || (e.pointerType === 'mouse' && e.button !== 0)) return;
      e.preventDefault();
      grab.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId, toFrame(e));
      if (!gesture) { gesture = {moved: false}; grab.classList.add('dragging'); clearTimeout(saveTimer); saveTimer = null; }
      gesture.prev = spread();
    });
    grab.addEventListener('pointermove', e => {
      if (!gesture || !pointers.has(e.pointerId)) return;
      pointers.set(e.pointerId, toFrame(e));
      const now = spread(), prev = gesture.prev;
      const k = prev.d && now.d ? now.d / prev.d : 1;
      set(moved(draft, k, prev.m, now.m[0] - prev.m[0], now.m[1] - prev.m[1]));
      gesture.moved = true;
      gesture.prev = now;
    });
    function lift(e) {
      if (!pointers.delete(e.pointerId) || !gesture) return;
      if (pointers.size) { gesture.prev = spread(); return; }
      const did = gesture.moved;
      gesture = null;
      grab.classList.remove('dragging');
      if (did) save();
    }
    grab.addEventListener('pointerup', lift);
    grab.addEventListener('pointercancel', lift);

    grab.addEventListener('wheel', e => {
      if (!L || !(opts.plainWheel || e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      const unit = e.deltaMode === 1 ? 0.05 : e.deltaMode === 2 ? 1 : 0.002;
      set(moved(draft, Math.exp(-e.deltaY * unit), toFrame(e), 0, 0));
      saveSoon(PAUSE_SAVE_MS);
    }, {passive: false});

    function rescale(delta) {
      // Steps land on whole tens of percent, about the print's own centre.
      const s = Math.round((draft.scale + delta) * 10) / 10;
      set(moved(draft, s / draft.scale, centreOf(draft), 0, 0));
      saveSoon(PAUSE_SAVE_MS);
    }
    grab.addEventListener('keydown', e => {
      const step = {ArrowLeft: [-NUDGE, 0], ArrowRight: [NUDGE, 0],
                    ArrowUp: [0, -NUDGE], ArrowDown: [0, NUDGE]}[e.key];
      if (step) {
        e.preventDefault();
        set({...draft, x: draft.x + step[0], y: draft.y + step[1]});
        saveSoon(PAUSE_SAVE_MS);
      } else if (e.key === '+' || e.key === '=' || e.key === '-') {
        e.preventDefault();
        rescale(e.key === '-' ? -STEP : STEP);
      }
    });
    ctl.querySelector('[data-act=smaller]').addEventListener('click', () => rescale(-STEP));
    ctl.querySelector('[data-act=larger]').addEventListener('click', () => rescale(STEP));
    ctl.querySelector('[data-act=reset]').addEventListener('click', () => {
      set({...DEFAULT});
      save();
    });

    say('');
    draw();
    loadRest();
    loadLayers();
    return {
      refresh: loadRest,
      async reload() { await Promise.all([loadRest(), loadLayers()]); },
      async settle() {
        if (saveTimer) save();
        await saving;
        return saved;
      },
      destroy() {
        // A change still waiting to save is saved, not lost.
        if (saveTimer) save();
        api.destroy();
        layerSeq++;
      },
    };
  }

  window.Placement = {mount, postJSON};
})();
