// Adjust control: move and scale one image inside one layout's art box,
// saved per image and layout, automatically when a change is released.
// Shared by the pane (index.html: the art box alone) and the review page
// (review.html: the whole page the print will hang on, dragged inside its
// art box). The picture is always the server's render, never a CSS
// imitation (CSS only nudges it while a finger is down). All URLs are
// relative: the pages are also served under a prefix.
(function () {
  const DEFAULT = {x: 0, y: 0, scale: 1};
  const WHOLE = [0, 0, 1, 1];   // the art box, as shares of the picture
  const NUDGE = 0.01;   // arrow keys move by 1% of the box
  const NUDGE_SAVE_MS = 500;   // arrow-key nudges save once they pause

  const css = `
  .adj { display:flex; flex-direction:column; gap:10px; }
  .adj-stage { position:relative; }
  .adj-frame { position:relative; width:100%; aspect-ratio:var(--ar, 1.333); background:#fff;
    border:1px solid var(--bix-border-strong, #45475a); border-radius:8px; overflow:hidden;
    user-select:none; -webkit-user-select:none; }
  /* fit: the whole picture fits its stage both ways, so it never scrolls. */
  .adj-stage.fit { container-type:size; width:100%; height:100%;
    display:flex; align-items:center; justify-content:center; }
  .adj-stage.fit .adj-frame { width:min(100cqw, calc(100cqh * var(--ar, 1.333))); }
  .adj-frame > img { position:absolute; inset:0; width:100%; height:100%; display:block;
    pointer-events:none; }
  .adj-stage.busy .adj-frame > img { opacity:.7; }
  .adj-box { position:absolute; touch-action:none; cursor:grab; overflow:hidden;
    left:calc(var(--bx0) * 100%); top:calc(var(--by0) * 100%);
    width:calc(var(--bw) * 100%); height:calc(var(--bh) * 100%); }
  .adj-frame.page .adj-box { outline:1.5px dashed var(--bix-border-strong, #45475a);
    outline-offset:-1.5px; }
  .adj-box:focus-visible { outline:2px solid var(--bix-accent, #fab387); outline-offset:-2px; }
  .adj-box.dragging { cursor:grabbing; }
  /* While a finger is down: a copy of the picture, clipped to the box,
     moved or scaled over it. */
  .adj-move { position:absolute; inset:0; display:none; background:#fff;
    transform-origin:50% 50%; pointer-events:none; }
  .adj-box.moving .adj-move { display:block; }
  .adj-move img { position:absolute; display:block; max-width:none;
    width:calc(100% / var(--bw)); height:calc(100% / var(--bh));
    left:calc(-100% * var(--bx0) / var(--bw)); top:calc(-100% * var(--by0) / var(--bh)); }
  .adj-row { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .adj-scale { display:flex; align-items:center; gap:10px; flex:1; min-width:200px;
    color:var(--bix-text-soft, #a6adc8); font-size:14px; }
  .adj-scale input { flex:1; min-height:44px; font-size:16px;
    accent-color:var(--bix-accent, #fab387); }
  .adj-pct { font-family:var(--bix-mono, ui-monospace, Menlo, monospace); min-width:4.5ch;
    text-align:right; color:var(--bix-text, #cdd6f4); font-variant-numeric:tabular-nums; }
  .adj button { min-height:44px; padding:0 14px; border-radius:8px; font:inherit;
    font-size:14px; cursor:pointer; background:none; color:var(--bix-text, #cdd6f4);
    border:1px solid var(--bix-border-strong, #45475a); }
  .adj button:disabled { opacity:.45; cursor:default; }
  .adj-status { font-size:13px; color:var(--bix-text-soft, #a6adc8); }
  .adj-status.err { color:var(--bix-danger, #f38ba8); }
  .adj-pos { font-family:var(--bix-mono, ui-monospace, Menlo, monospace); font-size:12px;
    color:var(--bix-text-muted, #7f849c); }
  .adj-for { font-size:12px; color:var(--bix-text-muted, #7f849c); }`;
  if (!document.getElementById('adj-css')) {
    const s = document.createElement('style');
    s.id = 'adj-css'; s.textContent = css;
    document.head.appendChild(s);
  }

  const same = (a, b) => Math.abs(a.x - b.x) < 1e-4 && Math.abs(a.y - b.y) < 1e-4 &&
                         Math.abs(a.scale - b.scale) < 1e-4;
  const pct = v => Math.round(v * 100);
  const signed = v => (v > 0 ? '+' : '') + pct(v) + '%';

  async function postJSON(url, body) {
    const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify(body)});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
    return j;
  }

  // mount(target, opts) → {reload(), settle(), destroy()}
  // target: one element for the picture and its controls, or {stage,
  //   controls} to put them in two places.
  // opts.file, opts.layout: the image and the art layout key the placement
  //   is for ("hero", ...); opts.layoutName its words ("portrait hero layout").
  // opts.placement: the saved placement, null for the automatic fit.
  // opts.screen: when given, the picture is the whole page that screen will
  //   hang (api/page-preview.png) and the print is dragged inside its art
  //   box; otherwise it is the art box alone (api/animal-preview.png).
  // opts.fit: size the picture to fit its stage both ways.
  // opts.onSaved(animal): the server's summary after each save.
  // settle() resolves once every change is saved: true, or false when the
  //   last save failed (the status says why).
  function mount(target, opts) {
    const {file, layout} = opts;
    const stageHost = target.stage || target;
    const ctlHost = target.controls || target;
    let draft = {...DEFAULT, ...(opts.placement || {})};
    let seq = 0, saveSeq = 0, saving = Promise.resolve(), committing = Promise.resolve();
    let saved = true, objectUrl = null, drag = null, nudgeTimer = null, gone = false;

    const stageHtml = `
      <div class="adj-stage${opts.fit ? ' fit' : ''}">
        <div class="adj-frame${opts.screen ? ' page' : ''}">
          <img alt="${opts.screen ? 'The whole page with this print, as the panel will show it' : ''}">
          <div class="adj-box" tabindex="0" role="img"
               aria-label="The print in its area. Drag, or use the arrow keys, to move it.">
            <div class="adj-move"><img alt=""></div>
          </div>
        </div>
      </div>`;
    const ctlHtml = `
        <div class="adj-row">
          <label class="adj-scale">Scale
            <input type="range" min="50" max="250" step="5" aria-label="Scale">
            <span class="adj-pct"></span></label>
        </div>
        <div class="adj-row">
          <button type="button" data-act="reset">Reset to automatic</button>
          <span class="adj-status" role="status"></span>
        </div>
        <div class="adj-pos"></div>
        <div class="adj-for"></div>`;
    if (stageHost === ctlHost) {
      stageHost.innerHTML = `<div class="adj">${stageHtml}${ctlHtml}</div>`;
    } else {
      stageHost.innerHTML = stageHtml;
      ctlHost.innerHTML = `<div class="adj">${ctlHtml}</div>`;
    }
    const stage = stageHost.querySelector('.adj-stage');
    const frame = stage.querySelector('.adj-frame');
    const page = frame.querySelector(':scope > img');
    const box = frame.querySelector('.adj-box');
    const move = box.querySelector('.adj-move');
    const copy = move.querySelector('img');
    const range = ctlHost.querySelector('input[type=range]');
    const pctEl = ctlHost.querySelector('.adj-pct');
    const status = ctlHost.querySelector('.adj-status');
    const pos = ctlHost.querySelector('.adj-pos');
    ctlHost.querySelector('.adj-for').textContent = 'for the ' + (opts.layoutName || layout);

    function setBox(b) {
      const [x0, y0, x1, y1] = b;
      for (const [k, v] of [['--bx0', x0], ['--by0', y0], ['--bw', x1 - x0], ['--bh', y1 - y0]]) {
        frame.style.setProperty(k, v);
      }
    }
    setBox(WHOLE);

    function say(text, err) {
      status.textContent = text;
      status.className = 'adj-status' + (err ? ' err' : '');
    }
    function sync() {
      range.value = pct(draft.scale);
      pctEl.textContent = pct(draft.scale) + '%';
      pos.textContent = same(draft, DEFAULT) ? 'automatic fit'
        : `across ${signed(draft.x)} · down ${signed(draft.y)} · scale ${pct(draft.scale)}%`;
    }
    function feedback(transform) {
      box.classList.toggle('moving', !!transform);
      move.style.transform = transform || '';
    }

    // Latest request wins: a slow render must not overwrite a newer one.
    // Resolves true when this render is still the latest.
    async function render() {
      const mine = ++seq;
      stage.classList.add('busy');
      const q = new URLSearchParams({file, x: draft.x, y: draft.y, scale: draft.scale,
                                     t: Date.now()});
      if (opts.screen) q.set('screen', opts.screen); else q.set('layout', layout);
      const url = (opts.screen ? 'api/page-preview.png?' : 'api/animal-preview.png?') + q;
      try {
        const r = await fetch(url);
        if (!r.ok) {
          const j = await r.json().catch(() => ({}));
          throw new Error(j.detail || ('HTTP ' + r.status));
        }
        const placed = JSON.parse(r.headers.get('X-Placement') || 'null') || DEFAULT;
        const artBox = JSON.parse(r.headers.get('X-Art-Box') || 'null') || WHOLE;
        const blob = await r.blob();
        if (mine !== seq) return false;
        const next = URL.createObjectURL(blob);
        await new Promise((ok, bad) => {
          page.onload = ok;
          page.onerror = () => bad(new Error('preview image did not decode'));
          page.src = next;
        });
        if (objectUrl) URL.revokeObjectURL(objectUrl);
        objectUrl = next;
        copy.src = next;
        frame.style.setProperty('--ar', page.naturalWidth / page.naturalHeight);
        setBox(artBox);
        feedback(null);
        draft = {...draft, x: placed.x, y: placed.y};
        sync();
      } catch (e) {
        if (mine === seq) say('Preview failed: ' + e.message, true);
      } finally {
        if (mine === seq) stage.classList.remove('busy');
      }
      return mine === seq;
    }

    // Saves run one after another so the last change is the one stored;
    // only the latest reports its outcome.
    function save() {
      clearTimeout(nudgeTimer);
      nudgeTimer = null;
      const mine = ++saveSeq;
      const body = same(draft, DEFAULT) ? {file, layout, reset: true}
        : {file, layout, x: draft.x, y: draft.y, scale: draft.scale};
      say('Saving…');
      saved = false;
      saving = saving.then(() => postJSON('api/placement', body)).then(j => {
        if (mine !== saveSeq) return;
        saved = true;
        if (gone) return;
        say('Saved');
        if (opts.onSaved) opts.onSaved(j.animal);
      }, e => {
        if (mine !== saveSeq) return;
        saved = false;
        if (!gone) say('Save failed: ' + e.message, true);
      });
    }
    // A released drag or slider: draw it (which also clamps it to the box
    // the way the server will), then save what was drawn.
    function commit() {
      say('Saving…');
      committing = render().then(latest => { if (latest) save(); });
    }

    box.addEventListener('pointerdown', e => {
      if (e.button !== 0) return;
      box.setPointerCapture(e.pointerId);
      box.classList.add('dragging');
      drag = {x: e.clientX, y: e.clientY, dx: 0, dy: 0};
    });
    box.addEventListener('pointermove', e => {
      if (!drag) return;
      drag.dx = e.clientX - drag.x; drag.dy = e.clientY - drag.y;
      feedback(`translate(${drag.dx}px, ${drag.dy}px)`);
    });
    function endDrag() {
      if (!drag) return;
      const {dx, dy} = drag;
      drag = null;
      box.classList.remove('dragging');
      if (!dx && !dy) { feedback(null); return; }
      draft = {...draft, x: draft.x + dx / box.clientWidth,
                         y: draft.y + dy / box.clientHeight};
      sync(); commit();
    }
    box.addEventListener('pointerup', endDrag);
    box.addEventListener('pointercancel', endDrag);
    box.addEventListener('keydown', e => {
      const step = {ArrowLeft: [-NUDGE, 0], ArrowRight: [NUDGE, 0],
                    ArrowUp: [0, -NUDGE], ArrowDown: [0, NUDGE]}[e.key];
      if (!step) return;
      e.preventDefault();
      draft = {...draft, x: draft.x + step[0], y: draft.y + step[1]};
      sync(); render();
      clearTimeout(nudgeTimer);
      nudgeTimer = setTimeout(save, NUDGE_SAVE_MS);
    });

    range.addEventListener('input', () => {
      pctEl.textContent = range.value + '%';
      // Feedback only; the server draws it on release.
      feedback(`scale(${range.value / 100 / draft.scale})`);
    });
    range.addEventListener('change', () => {
      draft = {...draft, scale: range.value / 100};
      sync(); commit();
    });

    ctlHost.querySelector('[data-act=reset]').addEventListener('click', () => {
      draft = {...DEFAULT};
      sync(); save(); render();
    });

    sync();
    say('');
    render();
    return {
      reload: render,   // e.g. after the image was rotated
      async settle() {
        await committing;
        if (nudgeTimer) save();
        await saving;
        return saved;
      },
      destroy() {
        // A nudge still waiting to save is saved, not lost.
        if (nudgeTimer) save();
        gone = true; seq++;
        if (objectUrl) URL.revokeObjectURL(objectUrl);
        stageHost.innerHTML = '';
        if (ctlHost !== stageHost) ctlHost.innerHTML = '';
      },
    };
  }

  window.Placement = {mount, postJSON};
})();
