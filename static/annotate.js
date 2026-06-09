/* Persistent app-side highlights on top of the PREBUILT PDF.js viewer.
 * (This is now the default and only viewer; the old custom canvas viewer was removed.)
 *
 * The viewer is self-hosted + same-origin, so we can reach its PDFViewerApplication,
 * eventBus, and per-page viewports. Highlights are stored in PDF coordinates
 * ({pageIndex, rects}); we draw our own overlay layer into each page div and
 * redraw on 'pagerendered' (handles virtualization AND zoom, since the page
 * re-renders at the new scale and we recompute rects from PDF points). We never
 * touch PDF.js's own annotation editor. */
(() => {
  const frame = document.getElementById("pdfjs-frame");
  if (!frame) return;
  const slug = frame.dataset.slug;
  const key = frame.dataset.key;

  let win, app, eventBus, pdfViewer;
  let annotations = [];           // {id, origin, color, page, position:{pageIndex,rects}, ...}
  // The highlight scheme is configured in Settings and handed to us on the frame.
  const PALETTE = (() => {
    try {
      const s = JSON.parse(frame.dataset.scheme || "[]");
      const p = s.map((c) => [c.color, c.label]).filter((c) => c[0]);
      if (p.length) return p;
    } catch (e) { /* fall through */ }
    return [["#ffd400", "important"], ["#5fd35f", "agree"], ["#ff6666", "disagree"], ["#6fb3ff", "unclear"]];
  })();
  let defaultColor = (() => {
    try {
      const saved = localStorage.getItem("pp.hlcolor");
      if (saved && PALETTE.some((c) => c[0] === saved)) return saved;  // honor a saved pick if still valid
    } catch (e) {}
    return PALETTE[0][0];                                              // else the scheme's first color
  })();
  function setDefaultColor(c) { defaultColor = c; try { localStorage.setItem("pp.hlcolor", c); } catch (e) {} }
  let pending = null;             // current selection -> {pageIndex, rects, text}

  const toolbar = document.getElementById("beta-toolbar");

  function hexToRgba(hex, a) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
    if (!m) return `rgba(255,212,0,${a})`;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }
  function escapeHtml(s) {
    return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  // --- drawing -------------------------------------------------------------
  function ensureLayer(pageDiv) {
    let layer = pageDiv.querySelector(".app-ann-layer");
    if (!layer) {
      layer = win.document.createElement("div");
      layer.className = "app-ann-layer";
      layer.style.cssText = "position:absolute;inset:0;pointer-events:none;z-index:5";
      pageDiv.appendChild(layer);
    }
    return layer;
  }

  // Group raw line-rects into clean, non-overlapping bands. getClientRects()
  // returns one rect per line, but consecutive lines overlap vertically by a
  // pixel or two — and with mix-blend-mode:multiply those overlaps darken into
  // visible banding ("looks strange"). We cluster rects by line (vertical
  // overlap > 50%), union each line horizontally, then split any vertical
  // overlap between adjacent lines at the midpoint so nothing double-paints.
  function mergeRects(boxes) {
    const lines = [];
    boxes.slice().sort((a, b) => a.top - b.top || a.left - b.left).forEach((r) => {
      const ln = lines.find((L) => {
        const ov = Math.min(L.bottom, r.bottom) - Math.max(L.top, r.top);
        return ov > 0.5 * Math.min(L.bottom - L.top, r.bottom - r.top);
      });
      if (ln) {
        ln.left = Math.min(ln.left, r.left); ln.right = Math.max(ln.right, r.right);
        ln.top = Math.min(ln.top, r.top); ln.bottom = Math.max(ln.bottom, r.bottom);
      } else lines.push({ ...r });
    });
    lines.sort((a, b) => a.top - b.top);
    for (let i = 0; i < lines.length - 1; i++) {
      if (lines[i].bottom > lines[i + 1].top) {
        const m = (lines[i].bottom + lines[i + 1].top) / 2;
        lines[i].bottom = m; lines[i + 1].top = m;
      }
    }
    return lines;
  }

  function drawPage(pageNumber) {
    const pv = pdfViewer.getPageView(pageNumber - 1);
    if (!pv || !pv.div || !pv.viewport) return;
    const layer = ensureLayer(pv.div);
    layer.innerHTML = "";
    const vp = pv.viewport;
    const mine = annotations.filter((a) => a.page === pageNumber - 1);
    mine.forEach((a) => {
      const boxes = (a.position && a.position.rects || []).map((rect) => {
        const [x1, y1, x2, y2] = vp.convertToViewportRectangle(rect);
        return { left: Math.min(x1, x2), top: Math.min(y1, y2), right: Math.max(x1, x2), bottom: Math.max(y1, y2) };
      });
      mergeRects(boxes).forEach((b) => {
        const d = win.document.createElement("div");
        d.style.cssText =
          `position:absolute;left:${b.left}px;top:${b.top}px;` +
          `width:${b.right - b.left}px;height:${b.bottom - b.top}px;` +
          `background:${hexToRgba(a.color || defaultColor, 0.32)};` +
          `border-radius:1px;pointer-events:auto;` +
          (a.origin === "app" ? "cursor:pointer;" : "cursor:default;") +
          (a.origin === "zotero" ? "outline:1px dashed rgba(0,0,0,.35);" : "");
        d.title = a.origin === "app"
          ? (a.note_text || a.selected_text || "click to recolor / delete")
          : (a.note_text || a.selected_text || "");
        if (a.origin === "app") {
          d.dataset.annId = a.id;
          d.addEventListener("click", (ev) => { ev.stopPropagation(); openEditPopup(a, ev); });
        }
        layer.appendChild(d);
      });
    });
  }
  function redrawAll() {
    if (!pdfViewer) return;
    for (let i = 1; i <= pdfViewer.pagesCount; i++) {
      const pv = pdfViewer.getPageView(i - 1);
      if (pv && pv.div && pv.canvas) drawPage(i);
    }
  }

  // Coalesce redraws: PDF.js fires pagerendered AND textlayerrendered per page
  // (and rapidly during scroll/zoom). Batch them so each page is drawn at most
  // once per animation frame instead of synchronously on every event.
  const _pendingPages = new Set();
  let _drawScheduled = false;
  function scheduleDraw(pageNumber) {
    _pendingPages.add(pageNumber);
    if (_drawScheduled) return;
    _drawScheduled = true;
    (win || window).requestAnimationFrame(() => {
      _drawScheduled = false;
      const pages = [..._pendingPages];
      _pendingPages.clear();
      pages.forEach(drawPage);
    });
  }

  async function loadAnnotations() {
    try {
      const r = await fetch(`/c/${slug}/p/${key}/annotations`);
      annotations = (await r.json()).annotations;
    } catch (e) { annotations = []; }
  }

  // --- selection -> toolbar ------------------------------------------------
  function onMouseUp() {
    const sel = win.getSelection();
    const t = (sel && sel.toString() || "").trim();
    if (!t) { hideToolbar(); return; }
    const node = sel.anchorNode;
    const el = node && (node.nodeType === 1 ? node : node.parentElement);
    const pageEl = el && el.closest(".page");
    if (!pageEl) { hideToolbar(); return; }
    const pageNumber = parseInt(pageEl.dataset.pageNumber, 10);
    const pv = pdfViewer.getPageView(pageNumber - 1);
    if (!pv) { hideToolbar(); return; }
    const vp = pv.viewport;
    // Measure from the CONTENT origin (canvas), not the page's border-box: the
    // .page div has a border so its top-left is offset from the canvas/text by
    // the border width. Our overlay draws at the content origin, so we must too.
    const originEl = pv.canvas || pageEl.querySelector(".canvasWrapper") ||
                     pageEl.querySelector(".textLayer") || pageEl;
    const pageRect = originEl.getBoundingClientRect();
    const rects = [];
    for (const cr of sel.getRangeAt(0).getClientRects()) {
      if (cr.width < 1 || cr.height < 1) continue;
      const x1 = cr.left - pageRect.left, y1 = cr.top - pageRect.top;
      const p1 = vp.convertToPdfPoint(x1, y1);
      const p2 = vp.convertToPdfPoint(x1 + cr.width, y1 + cr.height);
      rects.push([Math.min(p1[0], p2[0]), Math.min(p1[1], p2[1]),
                  Math.max(p1[0], p2[0]), Math.max(p1[1], p2[1])]);
    }
    if (!rects.length) { hideToolbar(); return; }
    pending = { pageIndex: pageNumber - 1, rects, text: t };
    // position the parent-side toolbar at the selection (iframe offset + rect)
    const last = sel.getRangeAt(0).getClientRects();
    const r = last[last.length - 1];
    const ifr = frame.getBoundingClientRect();
    toolbar.style.left = Math.max(8, ifr.left + r.left) + "px";
    toolbar.style.top = Math.max(8, ifr.top + r.bottom + 6) + "px";
    toolbar.classList.remove("hidden");
  }
  function hideToolbar() { toolbar.classList.add("hidden"); pending = null; }

  // --- recolor/delete an EXISTING highlight (click the highlight in the PDF) -
  async function patchAnn(a, body) {
    await fetch(`/annotations/${a.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    Object.assign(a, body);
    drawPage(a.page + 1);
  }
  async function deleteAnn(a) {
    await fetch(`/annotations/${a.id}`, { method: "DELETE" });
    annotations = annotations.filter((x) => String(x.id) !== String(a.id));
    drawPage(a.page + 1);
  }

  let editPop = null;
  function ensureEditPop() {
    if (editPop) return editPop;
    editPop = document.createElement("div");
    editPop.className = "hidden fixed z-50 rounded shadow bg-white border border-slate-200 px-1.5 py-1 flex items-center gap-1";
    editPop.innerHTML =
      PALETTE.map(([hex, name]) => `<button data-color="${hex}" title="${name}" style="background:${hex}" class="w-4 h-4 rounded-sm border border-slate-300"></button>`).join("") +
      `<button data-act="ask" title="ask the chat about this highlight" class="px-1 text-sm text-slate-600 hover:text-violet-700">💬</button>` +
      `<button data-act="note" title="note" class="px-1 text-sm text-slate-600 hover:text-slate-900">✎</button>` +
      `<button data-act="del" title="delete" class="px-1 text-sm text-rose-600 hover:text-rose-800">🗑</button>`;
    document.body.appendChild(editPop);
    editPop.addEventListener("click", async (e) => {
      const a = editPop._ann; if (!a) return;
      const t = e.target;
      if (t.dataset.color) { await patchAnn(a, { color: t.dataset.color }); hideEditPop(); return; }
      else if (t.dataset.act === "ask") { askAboutHighlight(a); hideEditPop(); return; }
      else if (t.dataset.act === "del") { await deleteAnn(a); hideEditPop(); return; }
      else if (t.dataset.act === "note") {
        const r = editPop.getBoundingClientRect();
        openNoteEditor({ x: r.left, y: r.bottom + 4, text: a.note_text || "", color: a.color, withColor: false,
          onSave: async (text) => { await patchAnn(a, { note_text: text }); if (window.__renderHighlights) window.__renderHighlights(); } });
        hideEditPop(); return;
      }
      hideEditPop();
    });
    document.addEventListener("click", hideEditPop);
    document.addEventListener("scroll", hideEditPop, true);
    return editPop;
  }
  function hideEditPop() { if (editPop) { editPop.classList.add("hidden"); editPop._ann = null; } }

  // --- reusable inline note editor (replaces the old prompt() calls) ---------
  let noteEd = null;
  function ensureNoteEditor() {
    if (noteEd) return noteEd;
    noteEd = document.createElement("div");
    noteEd.className = "hidden fixed z-50 w-64 rounded-lg shadow-lg bg-white border border-slate-200 p-2";
    noteEd.innerHTML =
      `<textarea data-note rows="3" placeholder="Note… (Enter to save · Shift+Enter = newline · Esc to cancel)"
                 class="w-full resize-none rounded border border-slate-300 px-2 py-1 text-sm"></textarea>
       <div data-sw class="hidden mt-1.5 flex items-center gap-1">
         <span class="text-xs text-slate-400 mr-1">color</span>
         ${PALETTE.map(([hex, name]) => `<button type="button" data-color="${hex}" title="${name}" style="background:${hex}" class="w-4 h-4 rounded-sm border border-slate-300"></button>`).join("")}
       </div>
       <div class="mt-2 flex justify-end gap-1 text-sm">
         <button type="button" data-act="cancel" class="px-2 py-0.5 rounded text-slate-500 hover:bg-slate-100">Cancel</button>
         <button type="button" data-act="save" class="px-2 py-0.5 rounded bg-slate-900 text-white hover:bg-slate-700">Save</button>
       </div>`;
    document.body.appendChild(noteEd);
    const ta = noteEd.querySelector("[data-note]");
    const swWrap = noteEd.querySelector("[data-sw]");
    function paintSwatches() {
      swWrap.querySelectorAll("[data-color]").forEach((b) => {
        const on = (b.dataset.color || "").toLowerCase() === (noteEd._color || "").toLowerCase();
        b.style.outline = on ? "2px solid #334155" : ""; b.style.outlineOffset = "1px";
      });
    }
    function close() { noteEd.classList.add("hidden"); noteEd._onSave = null; }
    function save() { const cb = noteEd._onSave; close(); if (cb) cb(ta.value.trim(), noteEd._color); }
    noteEd.querySelector('[data-act="cancel"]').addEventListener("click", close);
    noteEd.querySelector('[data-act="save"]').addEventListener("click", save);
    swWrap.addEventListener("click", (e) => { const c = e.target.dataset.color; if (c) { noteEd._color = c; paintSwatches(); ta.focus(); } });
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); save(); }
      else if (e.key === "Escape") { e.preventDefault(); close(); }
    });
    noteEd._open = (opts) => {
      noteEd._onSave = opts.onSave;
      noteEd._color = opts.color || defaultColor;
      ta.value = opts.text || "";
      swWrap.classList.toggle("hidden", !opts.withColor);
      paintSwatches();
      noteEd.classList.remove("hidden");                 // show first so we can measure
      const w = noteEd.offsetWidth, h = noteEd.offsetHeight;
      noteEd.style.left = Math.max(8, Math.min(opts.x, window.innerWidth - w - 8)) + "px";
      noteEd.style.top = Math.max(8, Math.min(opts.y, window.innerHeight - h - 8)) + "px";
      setTimeout(() => ta.focus(), 0);
    };
    return noteEd;
  }
  function openNoteEditor(opts) { ensureNoteEditor()._open(opts); }
  function openEditPopup(a, ev) {
    const p = ensureEditPop();
    p._ann = a;
    const ifr = frame.getBoundingClientRect();   // ev coords are iframe-relative
    p.style.left = Math.max(8, ifr.left + ev.clientX) + "px";
    p.style.top = Math.max(8, ifr.top + ev.clientY + 8) + "px";
    p.classList.remove("hidden");
  }

  async function createHighlight(color, noteText) {
    if (!pending) return null;
    const body = {
      kind: "highlight", color: color || defaultColor,
      position: { pageIndex: pending.pageIndex, rects: pending.rects },
      selected_text: pending.text, note_text: noteText || "",
    };
    const r = await fetch(`/c/${slug}/p/${key}/annotations`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const a = await r.json();
    annotations.push(a);
    drawPage(pending.pageIndex + 1);
    if (window.__renderHighlights) window.__renderHighlights();   // reflect it in the Highlights tab
    try { win.getSelection().removeAllRanges(); } catch (e) {}
    return a;
  }

  function askInChat() {
    if (window.__showChat) window.__showChat();   // switch the right pane to the Chat tab
    const ta = document.querySelector('textarea[name="message"]');
    if (ta && pending) {
      // Anchor the quote to its page so the agent can read that exact page for context
      // instead of searching the whole paper (and risking a similar passage elsewhere).
      const pg = (pending.pageIndex | 0) + 1;
      ta.value = "> (p. " + pg + ") " + pending.text.replace(/\s+/g, " ") + "\n\n" + ta.value;
      ta.focus();
    }
    try { win.getSelection().removeAllRanges(); } catch (e) {}
  }

  // Ask the chat about an EXISTING highlight (from its edit popup): quote its text + page
  // into the message box, same page-anchored format as a fresh selection.
  function askAboutHighlight(a) {
    if (window.__showChat) window.__showChat();
    const ta = document.querySelector('textarea[name="message"]');
    if (ta && a) {
      const pg = (a.page | 0) + 1;
      const txt = (a.selected_text || a.note_text || "").replace(/\s+/g, " ").trim();
      if (txt) ta.value = "> (p. " + pg + ") " + txt + "\n\n" + ta.value;
      ta.focus();
    }
  }

  // --- selection toolbar wiring (parent-side) ------------------------------
  // Swatch = highlight in that color instantly. Ask = quote into chat.
  // ✎ Note = inline note editor (with its own color picker) that creates a noted highlight.
  toolbar.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-act]"); if (!b) return;
    const act = b.dataset.act;
    if (act === "hl") {
      const color = b.dataset.color || defaultColor;
      setDefaultColor(color);
      await createHighlight(color); hideToolbar();
    } else if (act === "ask") {
      askInChat(); hideToolbar();
    } else if (act === "note") {
      const r = toolbar.getBoundingClientRect();
      const cap = pending;                       // hold the selection across the async save
      toolbar.classList.add("hidden");           // hide the bar but keep `pending`
      openNoteEditor({ x: r.left, y: r.bottom + 4, text: "", color: defaultColor, withColor: true,
        onSave: async (text, color) => {
          pending = cap; setDefaultColor(color);
          await createHighlight(color, text); pending = null;
        } });
    }
  });
  // (The header highlight legend is display-only — color is chosen from the selection popup.)

  // --- highlight manager (rendered into the Highlights tab) ----------------
  function wireManager() {
    const list = document.getElementById("hl-list");
    if (!list) return;

    let filterColor = null;          // hex string, or null = show all
    const selected = new Set();      // String(id) of checked highlights
    const sameColor = (a, hex) => (a.color || "#ffd400").toLowerCase() === hex.toLowerCase();

    async function patch(id, body) {
      await fetch(`/annotations/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    }
    function redrawPages(pages) { pages.forEach((p) => drawPage(p + 1)); }

    function render() {
      const app_ = annotations.filter((a) => a.origin === "app");
      const shown = filterColor ? app_.filter((a) => sameColor(a, filterColor)) : app_;

      // prune stale selections (deleted items)
      for (const id of [...selected]) if (!app_.some((a) => String(a.id) === id)) selected.delete(id);

      // filter chips
      const chip = (active, label, color, val) =>
        `<button data-act="filter" data-color="${val}" class="px-2 py-0.5 rounded text-xs border ${active ? 'border-slate-800 bg-slate-100 text-slate-800' : 'border-slate-200 text-slate-500'} hover:bg-slate-50">` +
        (color ? `<span class="inline-block w-2.5 h-2.5 rounded-sm align-middle mr-1" style="background:${color}"></span>` : '') + `${label}</button>`;
      let header = `<div class="flex items-center gap-1 flex-wrap mb-3">
        <span class="text-xs text-slate-400 mr-1">filter:</span>
        ${chip(!filterColor, 'all', '', '')}
        ${PALETTE.map(([hex, name]) => chip(filterColor && sameColor({ color: filterColor }, hex), name, hex, hex)).join('')}
      </div>`;

      // batch action bar (only when something is selected)
      let batch = '';
      if (selected.size) {
        const sw = PALETTE.map(([hex, name]) =>
          `<button data-act="batch-color" data-color="${hex}" title="recolor → ${name}" style="background:${hex}" class="inline-block w-4 h-4 rounded-sm border border-slate-300"></button>`).join('');
        batch = `<div class="flex items-center gap-2 mb-3 p-2 rounded bg-slate-50 border border-slate-200 text-xs">
          <span class="text-slate-700">${selected.size} selected</span>
          <span class="text-slate-400 ml-1">recolor:</span>${sw}
          <button data-act="batch-delete" class="text-rose-600 hover:text-rose-800 ml-1">delete</button>
          <button data-act="select-clear" class="ml-auto text-slate-500 hover:text-slate-800">clear</button>
        </div>`;
      }

      if (!shown.length) {
        const empty = filterColor ? 'No highlights of this color.' : 'No highlights yet. Select text in the PDF to add one.';
        list.innerHTML = header + batch + `<p class="text-sm text-slate-400">${empty}</p>`;
        return;
      }
      const selectAllBar = `<label class="flex items-center gap-2 text-xs text-slate-500 mb-2 cursor-pointer select-none">
        <input type="checkbox" data-act="select-all" class="cursor-pointer"> Select all${filterColor ? ' (filtered)' : ''}</label>`;
      const rows = shown.map((a) => {
        const sw = PALETTE.map(([hex, name]) => `<button data-act="color" data-color="${hex}" title="${name}" style="background:${hex}" class="inline-block w-3 h-3 rounded-sm border border-slate-300"></button>`).join("");
        const checked = selected.has(String(a.id)) ? 'checked' : '';
        return `<div class="border-b border-slate-100 py-2" data-id="${a.id}">
          <div class="flex items-start gap-2">
            <input type="checkbox" data-act="select" class="mt-1 shrink-0" ${checked}>
            <span class="mt-1 inline-block w-3 h-3 rounded-sm shrink-0" style="background:${a.color || '#ffd400'}"></span>
            <div class="flex-1 min-w-0">
              <div class="text-sm text-slate-700">${escapeHtml((a.selected_text || '').slice(0, 180)) || '(no text)'}</div>
              ${a.note_text ? `<div class="text-sm text-slate-500 italic">${escapeHtml(a.note_text)}</div>` : ''}
              <div class="mt-1 flex items-center gap-3 text-xs text-slate-500">
                <span>p.${(a.page || 0) + 1}</span>
                <button data-act="jump" class="hover:text-slate-800">jump</button>
                <button data-act="note" class="hover:text-slate-800">note</button>
                <button data-act="delete" class="text-rose-600 hover:text-rose-800">delete</button>
                <span class="flex gap-1 ml-auto">${sw}</span>
              </div>
            </div>
          </div></div>`;
      }).join("");
      list.innerHTML = header + batch + selectAllBar + rows;
      const selAll = list.querySelector('[data-act="select-all"]');
      if (selAll) {
        const sc = shown.filter((a) => selected.has(String(a.id))).length;
        selAll.checked = shown.length > 0 && sc === shown.length;
        selAll.indeterminate = sc > 0 && sc < shown.length;
      }
    }

    // The Highlights tab calls this when it becomes active (and once at boot).
    window.__refreshHighlights = async () => { await loadAnnotations(); selected.clear(); render(); };
    // Lighter re-render from the in-memory list (e.g. after adding a highlight).
    window.__renderHighlights = render;
    render();   // initial paint (empty until annotations load)

    list.addEventListener("click", async (e) => {
      const act = e.target.dataset.act;

      // toolbar-level actions (no specific row)
      if (act === "filter") { filterColor = e.target.dataset.color || null; render(); return; }
      if (act === "select-clear") { selected.clear(); render(); return; }
      if (act === "select-all") {
        const app2 = annotations.filter((x) => x.origin === "app");
        const shown2 = filterColor ? app2.filter((x) => sameColor(x, filterColor)) : app2;
        const allSel = shown2.length && shown2.every((x) => selected.has(String(x.id)));
        shown2.forEach((x) => allSel ? selected.delete(String(x.id)) : selected.add(String(x.id)));
        render(); return;
      }
      if (act === "batch-color") {
        const color = e.target.dataset.color;
        const pages = new Set();
        for (const id of selected) {
          const a = annotations.find((x) => String(x.id) === id);
          if (!a) continue;
          await patch(id, { color }); a.color = color; pages.add(a.page);
        }
        redrawPages(pages); render(); return;
      }
      if (act === "batch-delete") {
        if (!confirm(`Delete ${selected.size} highlight(s)?`)) return;
        const pages = new Set();
        for (const id of selected) {
          const a = annotations.find((x) => String(x.id) === id);
          if (a) pages.add(a.page);
          await fetch(`/annotations/${id}`, { method: "DELETE" });
          annotations = annotations.filter((x) => String(x.id) !== id);
        }
        selected.clear(); redrawPages(pages); render(); return;
      }

      // per-row actions
      const item = e.target.closest("[data-id]"); if (!item) return;
      const id = item.dataset.id;
      const a = annotations.find((x) => String(x.id) === String(id));
      if (act === "select") {
        if (selected.has(id)) selected.delete(id); else selected.add(id);
        render();
      } else if (act === "delete") {
        await fetch(`/annotations/${id}`, { method: "DELETE" });
        annotations = annotations.filter((x) => String(x.id) !== String(id));
        if (a) drawPage(a.page + 1); render();
      } else if (act === "jump") {
        if (a) pdfViewer.scrollPageIntoView({ pageNumber: a.page + 1 });
      } else if (act === "color") {
        const color = e.target.dataset.color;
        await patch(id, { color });
        if (a) { a.color = color; drawPage(a.page + 1); } render();
      } else if (act === "note") {
        const r = e.target.getBoundingClientRect();
        openNoteEditor({ x: r.left, y: r.bottom + 4, text: (a && a.note_text) || "", color: a && a.color, withColor: false,
          onSave: async (text) => { await patch(id, { note_text: text }); if (a) a.note_text = text; render(); } });
      }
    });
  }

  // --- boot: wait for the viewer, then hook events -------------------------
  async function waitForApp() {
    for (let i = 0; i < 100; i++) {
      const a = win && win.PDFViewerApplication;
      if (a && a.initializedPromise) { await a.initializedPromise; return a; }
      await new Promise((r) => setTimeout(r, 100));
    }
    return null;
  }

  // --- citation lookup: click an in-text reference → resolve + offer to add -------
  const _esc = (s) => (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const _CITE_RX = /(\[\d+(?:\s*[,–-]\s*\d+)*\])|([A-Z][A-Za-z'’.\-]+(?:\s+(?:et al\.?|and|&)\s*[A-Za-z'’.\-]*)*,?\s*\(?(?:19|20)\d{2}[a-z]?\)?)/g;
  function extractCitation(clickedText, windowText) {
    const matches = (windowText.match(_CITE_RX) || []).map((s) => s.trim());
    if (!matches.length) return null;
    const ct = (clickedText || "").trim();
    const hit = matches.find((x) => ct && (x.includes(ct) || ct.includes(x) ||
      (ct.length >= 3 && x.includes(ct.slice(0, 3)))));
    return (hit || matches[0]).trim();
  }

  let citePop = null;
  function ensureCitePop() {
    if (citePop) return citePop;
    citePop = document.createElement("div");
    citePop.className = "hidden fixed z-[60] w-72 rounded-lg shadow-xl bg-white border border-slate-200 p-3";
    document.body.appendChild(citePop);
    document.addEventListener("click", (e) => { if (citePop && !citePop.contains(e.target)) citePop.classList.add("hidden"); }, true);
    return citePop;
  }
  async function openCitePopup(cite, px, py) {
    const p = ensureCitePop();
    p.innerHTML = `<div class="text-xs text-slate-500">Looking up <b>${_esc(cite)}</b>…</div>`;
    p.style.left = Math.min(px, window.innerWidth - 300) + "px";
    p.style.top = Math.min(py + 8, window.innerHeight - 220) + "px";
    p.classList.remove("hidden");
    let d;
    try {
      const r = await fetch(`/c/${slug}/p/${key}/cite-lookup`, {
        method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ cite }) });
      d = await r.json();
    } catch (e) { p.innerHTML = `<div class="text-xs text-rose-600">Lookup failed.</div>`; return; }
    if (!d.found) { p.innerHTML = `<div class="text-xs text-slate-500">Couldn't resolve “${_esc(d.cite || cite)}” to a paper.</div>`; return; }
    let html = `<div class="text-[10px] uppercase tracking-wide text-slate-400 mb-1">Citation · ${_esc(d.cite)}</div>`
      + `<div class="text-sm font-semibold text-slate-900 leading-snug">${_esc(d.title)}</div>`
      + `<div class="text-xs text-slate-500 mt-0.5">${_esc(d.authors || "")}${d.year ? " · " + _esc(d.year) : ""}</div>`;
    if (d.arxiv_id) {
      html += `<div class="text-[11px] text-emerald-700 mt-1.5">✓ on arXiv: ${_esc(d.arxiv_id)}</div>`
        + `<div class="mt-2 flex items-center gap-2"><button id="cite-add" class="rounded bg-violet-600 text-white px-2.5 py-1 text-xs hover:bg-violet-700">+ Add to collection</button>`
        + `<a href="https://arxiv.org/abs/${encodeURIComponent(d.arxiv_id)}" target="_blank" class="text-[11px] text-slate-500 hover:underline">open ↗</a></div>`;
    } else {
      html += `<div class="text-[11px] text-slate-400 mt-1.5">Not found on arXiv.</div>`;
    }
    p.innerHTML = html;
    const btn = p.querySelector("#cite-add");
    if (btn) btn.addEventListener("click", async () => {
      btn.disabled = true; btn.textContent = "Adding…";
      try {
        const r = await fetch(`/c/${slug}/p/${key}/cite-add`, {
          method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ arxiv_id: d.arxiv_id, title: d.arxiv_title || d.title,
            authors: d.authors || "", year: d.year || "", abstract: d.abstract || "" }) });
        const j = await r.json();
        btn.textContent = j.ok ? "✓ Added" : "Failed";
        btn.className = "rounded bg-emerald-100 text-emerald-700 px-2.5 py-1 text-xs";
      } catch (e) { btn.textContent = "Failed"; }
    });
  }
  function setupCiteClicks() {
    win.document.addEventListener("click", (e) => {
      if (citePop && citePop.contains && citePop.contains(e.target)) return;
      let els = [];
      try { els = win.document.elementsFromPoint(e.clientX, e.clientY) || []; } catch (_) { return; }
      if (els.some((el) => el.dataset && el.dataset.annId)) return;     // a highlight — let its handler run
      try { if (win.getSelection && String(win.getSelection()).trim()) return; } catch (_) {}  // selecting
      const span = els.find((el) => el.tagName === "SPAN" && el.closest && el.closest(".textLayer"));
      if (!span) return;
      const sibs = [span.previousElementSibling, span, span.nextElementSibling,
                    span.nextElementSibling && span.nextElementSibling.nextElementSibling];
      const windowText = sibs.filter(Boolean).map((s) => s.textContent).join(" ");
      const cite = extractCitation(span.textContent, windowText);
      if (!cite) return;
      e.preventDefault(); e.stopPropagation();                         // beat the internal-link jump
      const fr = frame.getBoundingClientRect();
      openCitePopup(cite, fr.left + e.clientX, fr.top + e.clientY);
    }, true);
  }

  frame.addEventListener("load", async () => {
    try { win = frame.contentWindow; void win.document; }
    catch (e) { console.warn("beta viewer not same-origin", e); return; }
    app = await waitForApp();
    if (!app) { console.warn("PDFViewerApplication not ready"); return; }
    eventBus = app.eventBus;
    pdfViewer = app.pdfViewer;
    await loadAnnotations();
    window.__beta = { get annotations() { return annotations; }, pdfViewer, redrawAll, drawPage };
    eventBus.on("pagerendered", (e) => scheduleDraw(e.pageNumber));
    eventBus.on("textlayerrendered", (e) => scheduleDraw(e.pageNumber));
    eventBus.on("pagesloaded", redrawAll);
    win.document.addEventListener("mouseup", () => setTimeout(onMouseUp, 0));
    win.document.addEventListener("scroll", hideToolbar, true);
    setupCiteClicks();
    redrawAll();
  });

  wireManager();
})();
