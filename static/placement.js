// Adjust control: move and scale one image inside its art box, saved per
// image. Shared by the pane (index.html) and the review page (review.html).
//
// The picture is always the server's: api/animal-preview.png draws the image
// through the dashboard's own code at the panel's size and layout. While a
// drag is in progress the last preview is nudged with a CSS transform so the
// hand has something to follow; on release the server redraws it, and the
// placement it reports back (X-Placement, clamped to the box; null for the
// automatic fit) becomes the draft. All URLs are relative: the pages are
// also served under a prefix.
(function () {
  const DEFAULT = {x: 0, y: 0, scale: 1};
  const NUDGE = 0.01;   // arrow keys move by 1% of the box

  const css = `
  .adj { display:flex; flex-direction:column; gap:10px; }
  .adj-stage { position:relative; overflow:hidden; touch-action:none; cursor:grab;
    background:#fff; border:1px solid var(--bix-border-strong, #45475a); border-radius:8px;
    aspect-ratio:4/3; user-select:none; -webkit-user-select:none; }
  .adj-stage:focus-visible { outline:2px solid var(--bix-accent, #fab387); outline-offset:2px; }
  .adj-stage.dragging { cursor:grabbing; }
  .adj-stage img { position:absolute; inset:0; width:100%; height:100%; display:block;
    pointer-events:none; transform-origin:50% 50%; }
  .adj-stage.busy img { opacity:.7; }
  .adj-row { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .adj-scale { display:flex; align-items:center; gap:10px; flex:1; min-width:220px;
    color:var(--bix-text-soft, #a6adc8); font-size:14px; }
  .adj-scale input { flex:1; min-height:44px; font-size:16px;
    accent-color:var(--bix-accent, #fab387); }
  .adj-pct { font-family:var(--bix-mono, ui-monospace, Menlo, monospace); min-width:4.5ch;
    text-align:right; color:var(--bix-text, #cdd6f4); font-variant-numeric:tabular-nums; }
  .adj button { min-height:44px; padding:0 14px; border-radius:8px; font:inherit;
    font-size:14px; cursor:pointer; background:none; color:var(--bix-text, #cdd6f4);
    border:1px solid var(--bix-border-strong, #45475a); }
  .adj button.adj-save { background:var(--bix-accent, #fab387);
    color:var(--bix-on-accent, #11111b); border-color:var(--bix-accent, #fab387); font-weight:600; }
  .adj button:disabled { opacity:.45; cursor:default; }
  .adj-status { font-size:13px; color:var(--bix-text-soft, #a6adc8); }
  .adj-status.err { color:var(--bix-danger, #f38ba8); }
  .adj-pos { font-family:var(--bix-mono, ui-monospace, Menlo, monospace); font-size:12px;
    color:var(--bix-text-muted, #7f849c); }`;
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

  // mount(el, {file, placement, onSaved}) → {reload(), destroy()}
  // placement null means the automatic fit. onSaved(animal) gets the
  // server's summary of the image after a save.
  function mount(el, opts) {
    const file = opts.file;
    let saved = {...DEFAULT, ...(opts.placement || {})};
    let draft = {...saved};
    let seq = 0, objectUrl = null, drag = null;

    el.innerHTML = `
      <div class="adj">
        <div class="adj-stage" tabindex="0" role="img"
             aria-label="The print in its art box. Drag, or use the arrow keys, to move it.">
          <img alt="">
        </div>
        <div class="adj-row">
          <label class="adj-scale">Scale
            <input type="range" min="50" max="250" step="5" aria-label="Scale">
            <span class="adj-pct"></span></label>
        </div>
        <div class="adj-row">
          <button type="button" data-act="reset">Reset to automatic</button>
          <button type="button" class="adj-save" data-act="save">Save</button>
          <span class="adj-status" role="status"></span>
        </div>
        <div class="adj-pos"></div>
      </div>`;
    const stage = el.querySelector('.adj-stage');
    const img = stage.querySelector('img');
    const range = el.querySelector('input[type=range]');
    const pctEl = el.querySelector('.adj-pct');
    const status = el.querySelector('.adj-status');
    const pos = el.querySelector('.adj-pos');
    const saveBtn = el.querySelector('[data-act=save]');

    function say(text, err) {
      status.textContent = text;
      status.className = 'adj-status' + (err ? ' err' : '');
    }
    function sync() {
      range.value = pct(draft.scale);
      pctEl.textContent = pct(draft.scale) + '%';
      pos.textContent = same(draft, DEFAULT) ? 'automatic fit'
        : `across ${signed(draft.x)} · down ${signed(draft.y)} · scale ${pct(draft.scale)}%`;
      const dirty = !same(draft, saved);
      saveBtn.disabled = !dirty;
      if (dirty) say('Unsaved changes');
      else if (status.textContent === 'Unsaved changes') say('');
    }

    // Latest request wins: a slow render must not overwrite a newer one.
    async function render() {
      const mine = ++seq;
      stage.classList.add('busy');
      const q = new URLSearchParams({file, x: draft.x, y: draft.y, scale: draft.scale,
                                     t: Date.now()});
      try {
        const r = await fetch('api/animal-preview.png?' + q);
        if (!r.ok) {
          const j = await r.json().catch(() => ({}));
          throw new Error(j.detail || ('HTTP ' + r.status));
        }
        const placed = JSON.parse(r.headers.get('X-Placement') || 'null') || DEFAULT;
        const blob = await r.blob();
        if (mine !== seq) return;
        if (objectUrl) URL.revokeObjectURL(objectUrl);
        objectUrl = URL.createObjectURL(blob);
        await new Promise((ok, bad) => {
          img.onload = ok;
          img.onerror = () => bad(new Error('preview image did not decode'));
          img.src = objectUrl;
        });
        stage.style.aspectRatio = img.naturalWidth + ' / ' + img.naturalHeight;
        img.style.transform = '';
        draft = {...draft, x: placed.x, y: placed.y};
        sync();
      } catch (e) {
        if (mine === seq) say('Preview failed: ' + e.message, true);
      } finally {
        if (mine === seq) stage.classList.remove('busy');
      }
    }

    stage.addEventListener('pointerdown', e => {
      if (e.button !== 0) return;
      stage.setPointerCapture(e.pointerId);
      stage.classList.add('dragging');
      drag = {x: e.clientX, y: e.clientY, dx: 0, dy: 0};
    });
    stage.addEventListener('pointermove', e => {
      if (!drag) return;
      drag.dx = e.clientX - drag.x; drag.dy = e.clientY - drag.y;
      img.style.transform = `translate(${drag.dx}px, ${drag.dy}px)`;
    });
    function endDrag() {
      if (!drag) return;
      const {dx, dy} = drag;
      drag = null;
      stage.classList.remove('dragging');
      if (!dx && !dy) return;
      draft = {...draft, x: draft.x + dx / stage.clientWidth,
                         y: draft.y + dy / stage.clientHeight};
      sync(); render();
    }
    stage.addEventListener('pointerup', endDrag);
    stage.addEventListener('pointercancel', endDrag);
    stage.addEventListener('keydown', e => {
      const step = {ArrowLeft: [-NUDGE, 0], ArrowRight: [NUDGE, 0],
                    ArrowUp: [0, -NUDGE], ArrowDown: [0, NUDGE]}[e.key];
      if (!step) return;
      e.preventDefault();
      draft = {...draft, x: draft.x + step[0], y: draft.y + step[1]};
      sync(); render();
    });

    range.addEventListener('input', () => {
      pctEl.textContent = range.value + '%';
      // Feedback only; the server draws it on release.
      img.style.transform = `scale(${range.value / 100 / draft.scale})`;
    });
    range.addEventListener('change', () => {
      draft = {...draft, scale: range.value / 100};
      sync(); render();
    });

    el.querySelector('[data-act=reset]').addEventListener('click', () => {
      draft = {...DEFAULT};
      sync(); render();
    });
    saveBtn.addEventListener('click', async () => {
      saveBtn.disabled = true;
      say('Saving…');
      try {
        const body = same(draft, DEFAULT) ? {file, reset: true}
                                          : {file, x: draft.x, y: draft.y, scale: draft.scale};
        const j = await postJSON('api/placement', body);
        saved = {...DEFAULT, ...j.animal.placement};
        draft = {...saved};
        sync();
        say('Saved');
        if (opts.onSaved) opts.onSaved(j.animal);
      } catch (e) {
        say('Save failed: ' + e.message, true);
        saveBtn.disabled = false;
      }
    });

    sync();
    say('');
    render();
    return {
      reload: render,   // e.g. after the image was rotated
      destroy() { seq++; if (objectUrl) URL.revokeObjectURL(objectUrl); el.innerHTML = ''; },
    };
  }

  window.Placement = {mount, postJSON};
})();
