"""In-page inspector, visual element zapper, and DevTools helpers injected into offline pages.

Features:
- Visual Element Zapper (point-and-click to permanently remove/hide any element).
- Unlock Scroll (fixes overflow:hidden on body/html after modals/overlays).
- DevTools Console integration: inspect an element in DevTools ($0) and type `zap($0)`.
- Custom JS function mocks and script URL blocking.
- Clean Shadow DOM container so host page CSS cannot break the toolbar.
"""
from __future__ import annotations

INPAGE_TOOLS_JS = r"""
(function() {
  if (window.__replicator_tools_active) return;
  window.__replicator_tools_active = true;

  // ------------------------------------------------------------- Console API
  window.replicator = {
    zap: function(target) {
      const el = (typeof target === 'string') ? document.querySelector(target) : target;
      if (!el || !(el instanceof Element)) {
        console.warn('[Replicator] Invalid element for zap:', target);
        return;
      }
      const sel = getBestSelector(el);
      hideElement(el, sel);
    },
    unlockScroll: function() {
      applyRule('html, body { overflow: auto !important; height: auto !important; position: static !important; }');
      console.log('[Replicator] Page scroll unlocked.');
    },
    blockUrl: function(urlPattern) {
      if (!urlPattern) return;
      fetch('/__replicator/block_url', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ pattern: urlPattern })
      }).then(r => r.json()).then(d => {
        console.log('[Replicator] Blocked URL pattern:', urlPattern);
      });
    },
    undo: function() {
      fetch('/__replicator/undo_zap', { method: 'POST' })
        .then(r => r.json())
        .then(d => {
          console.log('[Replicator] Undid last override:', d.removed || 'none');
          location.reload();
        });
    }
  };
  window.zap = window.replicator.zap;
  window.unlockScroll = window.replicator.unlockScroll;

  // Log friendly guide in DevTools Console
  try {
    console.log(
      '%c⚡ Replicator Tools Active%c\n' +
      '• Select an element in DevTools Elements tab and run: %czap($0)%c\n' +
      '• Or run: %cunlockScroll()%c to fix frozen page scrolling\n' +
      '• Press %cAlt+Z%c on the page to start Point-and-Click Zapper',
      'background:#2563eb;color:#fff;font-weight:bold;padding:2px 6px;border-radius:4px', '',
      'color:#38bdf8;font-weight:bold', '',
      'color:#38bdf8;font-weight:bold', '',
      'background:#374151;color:#fff;padding:1px 4px;border-radius:3px', ''
    );
  } catch(_) {}

  // -------------------------------------------------------- Selector Generator
  function getBestSelector(el) {
    if (!el || el.nodeType !== 1) return '';
    if (el.id && !/^\d|[^\w-]/.test(el.id)) {
      return '#' + CSS.escape(el.id);
    }
    const tag = el.tagName.toLowerCase();
    if (el.classList && el.classList.length) {
      const classes = Array.from(el.classList).filter(c => !c.startsWith('__rep_') && !/^\d|[^\w-]/.test(c));
      if (classes.length) {
        const sel = tag + '.' + classes.map(c => CSS.escape(c)).join('.');
        if (document.querySelectorAll(sel).length === 1) return sel;
      }
    }
    // Walk up to 3 parents for unique path
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.body && cur !== document.documentElement && parts.length < 3) {
      let part = cur.tagName.toLowerCase();
      if (cur.id && !/^\d|[^\w-]/.test(cur.id)) {
        parts.unshift('#' + CSS.escape(cur.id));
        break;
      }
      if (cur.classList && cur.classList.length) {
        const cls = Array.from(cur.classList).filter(c => !c.startsWith('__rep_') && !/^\d|[^\w-]/.test(c))[0];
        if (cls) part += '.' + CSS.escape(cls);
      }
      parts.unshift(part);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  }

  function hideElement(el, selector) {
    const sel = selector || getBestSelector(el);
    if (!sel) return;
    el.style.display = 'none';
    const rule = `${sel} { display: none !important; }`;
    applyRule(rule);
  }

  function applyRule(rule) {
    let s = document.getElementById('__replicator_live_overrides');
    if (!s) {
      s = document.createElement('style');
      s.id = '__replicator_live_overrides';
      document.head.appendChild(s);
    }
    s.appendChild(document.createTextNode(rule + '\n'));
    fetch('/__replicator/zap', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ rule: rule })
    }).catch(e => console.warn('[Replicator] Failed to persist zap:', e));
  }

  // ------------------------------------------------------------- UI Host
  function initUI() {
    if (!document.body) return;
    const host = document.createElement('div');
    host.id = '__replicator_ui_host';
    host.style.all = 'initial';
    document.documentElement.appendChild(host);
    const shadow = host.attachShadow({ mode: 'open' });

    const style = document.createElement('style');
    style.textContent = `
      * { box-sizing: border-box; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
      .rep-pill {
        position: fixed; bottom: 18px; right: 18px; z-index: 2147483647;
        background: #181b22; color: #f1f5f9; border: 1px solid #334155;
        border-radius: 20px; padding: 7px 14px; font-size: 13px; font-weight: 600;
        cursor: pointer; box-shadow: 0 4px 14px rgba(0,0,0,0.4);
        display: flex; align-items: center; gap: 6px; user-select: none;
        transition: transform 0.15s, background 0.15s;
      }
      .rep-pill:hover { background: #2563eb; transform: translateY(-1px); }
      .rep-pill.active { background: #dc2626; border-color: #ef4444; }

      .rep-panel {
        position: fixed; bottom: 58px; right: 18px; z-index: 2147483647;
        width: 320px; background: #0f172a; color: #e2e8f0;
        border: 1px solid #334155; border-radius: 12px; padding: 14px;
        box-shadow: 0 10px 25px rgba(0,0,0,0.5); display: none; flex-direction: column; gap: 10px;
        font-size: 13px;
      }
      .rep-panel.open { display: flex; }
      .rep-panel-header {
        display: flex; justify-content: space-between; align-items: center;
        font-weight: 700; font-size: 14px; color: #f8fafc; border-bottom: 1px solid #1e293b; padding-bottom: 8px;
      }
      .rep-btn {
        background: #1e293b; color: #f1f5f9; border: 1px solid #334155;
        border-radius: 6px; padding: 8px 10px; font-size: 12px; font-weight: 600;
        cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px;
        transition: background 0.12s;
      }
      .rep-btn:hover { background: #334155; }
      .rep-btn-primary { background: #2563eb; border-color: #3b82f6; }
      .rep-btn-primary:hover { background: #1d4ed8; }
      .rep-btn-danger { background: #991b1b; border-color: #dc2626; }
      .rep-btn-danger:hover { background: #7f1d1d; }

      /* Highlighter Overlay */
      .rep-hl {
        position: fixed; pointer-events: none; z-index: 2147483640;
        border: 2px solid #ef4444; background: rgba(239, 68, 68, 0.2);
        transition: top 0.05s, left 0.05s, width 0.05s, height 0.05s;
        display: none; border-radius: 2px;
      }
      .rep-hl-tag {
        position: absolute; top: -22px; left: 0; background: #ef4444; color: #fff;
        font-size: 11px; font-weight: 700; padding: 2px 6px; border-radius: 3px;
        white-space: nowrap; pointer-events: none;
      }

      /* Modal Dialog */
      .rep-modal {
        position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
        background: rgba(0,0,0,0.6); z-index: 2147483647;
        display: none; place-items: center; padding: 20px;
      }
      .rep-modal.open { display: grid; }
      .rep-modal-box {
        background: #0f172a; border: 1px solid #334155; border-radius: 12px;
        width: 100%; max-width: 520px; padding: 18px; color: #e2e8f0;
        box-shadow: 0 15px 30px rgba(0,0,0,0.6); display: flex; flex-direction: column; gap: 12px;
      }
      textarea, input[type="text"] {
        width: 100%; background: #1e293b; color: #f8fafc; border: 1px solid #475569;
        border-radius: 6px; padding: 8px; font-family: Consolas, monospace; font-size: 12px;
      }
      textarea:focus, input[type="text"]:focus { outline: 2px solid #2563eb; }
      .rep-row { display: flex; gap: 8px; justify-content: flex-end; }
      .rep-wide { max-width: 680px; max-height: 86vh; }
      .rep-list { overflow: auto; display: flex; flex-direction: column; gap: 8px; min-height: 80px; }
      .rep-item { display: grid; grid-template-columns: 1fr auto auto; gap: 6px; align-items: center; }
      .rep-item small { grid-column: 1 / -1; color: #94a3b8; font-size: 11px; overflow-wrap: anywhere; }
      .rep-add { display: grid; grid-template-columns: 1fr 1fr auto; gap: 6px; }
      .rep-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px; }
      .rep-card { display: flex; flex-direction: column; gap: 5px; font-size: 11px; color: #94a3b8; min-width: 0; }
      .rep-card img { width: 100%; height: 100px; object-fit: contain; background: #1e293b; border-radius: 6px; }
      .rep-card span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    `;
    shadow.appendChild(style);

    // Pill
    const pill = document.createElement('div');
    pill.className = 'rep-pill';
    pill.innerHTML = '<span>⚡</span><span>Replicator</span>';
    shadow.appendChild(pill);

    // Panel
    const panel = document.createElement('div');
    panel.className = 'rep-panel';
    panel.innerHTML = `
      <div class="rep-panel-header">
        <span>⚡ Replicator Tools</span>
        <span id="rep_close" style="cursor:pointer;color:#94a3b8">&times;</span>
      </div>
      <button class="rep-btn rep-btn-primary" id="rep_text_btn">✏️ Edit Text, also 3D/WebGL (Alt+T)</button>
      <button class="rep-btn" id="rep_images_btn">🖼 Replace Images &amp; Textures</button>
      <button class="rep-btn" id="rep_zap_btn">🎯 Zap Element (Alt+Z)</button>
      <button class="rep-btn" id="rep_unlock_btn">🔓 Unlock Page Scroll</button>
      <button class="rep-btn" id="rep_edit_css_btn">📋 Edit CSS Overrides</button>
      <button class="rep-btn" id="rep_edit_js_btn">⚙ JS Function Mocks</button>
      <button class="rep-btn" id="rep_block_url_btn">🚫 Block Script / URL</button>
      <button class="rep-btn" id="rep_undo_btn">↺ Undo Last Zap</button>
      <div style="font-size:11px;color:#94a3b8;line-height:1.4">
        Tip: In DevTools Console type <code>zap($0)</code> on any inspected element!
      </div>
    `;
    shadow.appendChild(panel);

    // Highlighter
    const hl = document.createElement('div');
    hl.className = 'rep-hl';
    const hlTag = document.createElement('div');
    hlTag.className = 'rep-hl-tag';
    hl.appendChild(hlTag);
    shadow.appendChild(hl);

    // Modal
    const modal = document.createElement('div');
    modal.className = 'rep-modal';
    modal.innerHTML = `
      <div class="rep-modal-box">
        <div style="font-weight:700;font-size:15px" id="rep_modal_title">Modal Title</div>
        <div id="rep_modal_desc" style="font-size:12px;color:#94a3b8"></div>
        <textarea id="rep_modal_text" rows="8"></textarea>
        <div class="rep-row">
          <button class="rep-btn" id="rep_modal_cancel">Cancel</button>
          <button class="rep-btn rep-btn-primary" id="rep_modal_save">Save & Apply</button>
        </div>
      </div>
    `;
    shadow.appendChild(modal);

    // Toast
    const toast = document.createElement('div');
    toast.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#10b981;color:#fff;padding:8px 16px;border-radius:8px;font-size:13px;font-weight:600;z-index:2147483647;display:none;box-shadow:0 4px 12px rgba(0,0,0,0.3);';
    shadow.appendChild(toast);

    function showToast(msg, isErr) {
      toast.textContent = msg;
      toast.style.background = isErr ? '#ef4444' : '#10b981';
      toast.style.display = 'block';
      setTimeout(() => { toast.style.display = 'none'; }, 2400);
    }

    // Toggle Panel
    pill.addEventListener('click', () => {
      panel.classList.toggle('open');
    });
    shadow.getElementById('rep_close').addEventListener('click', () => {
      panel.classList.remove('open');
    });

    // ------------------------------------------------------------- Zapper Logic
    let zapping = false;
    let pickingText = false;
    let hoveredEl = null;

    function startZapping(forText) {
      zapping = true;
      pickingText = forText === true;
      panel.classList.remove('open');
      pill.classList.add('active');
      pill.innerHTML = pickingText ? '<span>✏️</span><span>Click text or 3D (Esc)</span>'
                                   : '<span>✕</span><span>Click an element (Esc)</span>';
      hl.style.display = 'block';
      document.body.style.cursor = 'crosshair';
    }

    function stopZapping() {
      zapping = false;
      pill.classList.remove('active');
      pill.innerHTML = '<span>⚡</span><span>Replicator</span>';
      hl.style.display = 'none';
      document.body.style.cursor = '';
      hoveredEl = null;
    }

    document.addEventListener('mousemove', e => {
      if (!zapping) return;
      const target = document.elementFromPoint(e.clientX, e.clientY);
      if (!target || target === host || host.contains(target)) return;
      hoveredEl = target;
      const r = target.getBoundingClientRect();
      hl.style.top = r.top + 'px';
      hl.style.left = r.left + 'px';
      hl.style.width = r.width + 'px';
      hl.style.height = r.height + 'px';
      const sel = getBestSelector(target);
      hlTag.textContent = sel.length > 36 ? sel.slice(0, 34) + '...' : sel;
    }, true);

    document.addEventListener('click', e => {
      if (!zapping) return;
      if (host.contains(e.target)) return;
      e.preventDefault();
      e.stopPropagation();
      const target = hoveredEl || e.target;
      const forText = pickingText;
      stopZapping();
      if (forText) { pickText(e, target); return; }
      if (!target || target === document.body || target === document.documentElement) return;

      const sel = getBestSelector(target);
      if (confirm('Hide this element permanently?\n\nSelector: ' + sel)) {
        hideElement(target, sel);
        showToast('Element hidden permanently!');
      }
    }, true);

    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && zapping) {
        stopZapping();
      } else if (e.altKey && (e.key === 'z' || e.key === 'Z')) {
        e.preventDefault();
        if (zapping) stopZapping(); else startZapping();
      } else if (e.altKey && (e.key === 't' || e.key === 'T')) {
        e.preventDefault();
        if (zapping) stopZapping(); else startZapping(true);
      }
    });

    shadow.getElementById('rep_zap_btn').addEventListener('click', () => startZapping());
    shadow.getElementById('rep_text_btn').addEventListener('click', () => openTextEditor());
    shadow.getElementById('rep_images_btn').addEventListener('click', () => openImageEditor());

    // Unlock Scroll
    shadow.getElementById('rep_unlock_btn').addEventListener('click', () => {
      window.replicator.unlockScroll();
      panel.classList.remove('open');
      showToast('Page scroll unlocked!');
    });

    // Undo
    shadow.getElementById('rep_undo_btn').addEventListener('click', () => {
      fetch('/__replicator/undo_zap', { method: 'POST' })
        .then(r => r.json())
        .then(d => {
          showToast('Undid: ' + (d.removed || 'none'));
          setTimeout(() => location.reload(), 600);
        });
    });

    // Modal Actions
    const mBox = shadow.getElementById('rep_modal_text');
    const mTitle = shadow.getElementById('rep_modal_title');
    const mDesc = shadow.getElementById('rep_modal_desc');
    let mSaveHandler = null;

    function openModal(title, desc, initialVal, onSave) {
      mTitle.textContent = title;
      mDesc.textContent = desc;
      mBox.value = initialVal;
      mSaveHandler = onSave;
      panel.classList.remove('open');
      modal.classList.add('open');
      mBox.focus();
    }

    shadow.getElementById('rep_modal_cancel').addEventListener('click', () => {
      modal.classList.remove('open');
    });
    shadow.getElementById('rep_modal_save').addEventListener('click', () => {
      if (mSaveHandler) mSaveHandler(mBox.value);
      modal.classList.remove('open');
    });

    // Edit CSS
    shadow.getElementById('rep_edit_css_btn').addEventListener('click', () => {
      fetch('/__replicator/overrides').then(r => r.json()).then(d => {
        openModal(
          'Persistent CSS Overrides',
          'CSS rules injected into every page (e.g. #banner { display: none !important; })',
          d.css || '',
          val => {
            fetch('/__replicator/overrides', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({ css: val })
            }).then(() => {
              showToast('CSS saved! Reloading...');
              setTimeout(() => location.reload(), 600);
            });
          }
        );
      });
    });

    // Edit JS
    shadow.getElementById('rep_edit_js_btn').addEventListener('click', () => {
      fetch('/__replicator/overrides').then(r => r.json()).then(d => {
        openModal(
          'Persistent JavaScript Overrides & Function Mocks',
          'Code executed BEFORE any page scripts load. Use to mock or stub out functions (e.g. window.myFunc = () => {};)',
          d.js || '// Run before page scripts:\n// window.someTracker = () => {};\n// window.annoyingPopup = () => {};\n',
          val => {
            fetch('/__replicator/overrides', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({ js: val })
            }).then(() => {
              showToast('JS saved! Reloading...');
              setTimeout(() => location.reload(), 600);
            });
          }
        );
      });
    });

    // ------------------------------------------------- Text and image editors
    const mk = (tag, cls, text) => {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text !== undefined) n.textContent = text;
      return n;
    };
    const button = (label, onClick, cls) => {
      const b = mk('button', 'rep-btn' + (cls ? ' ' + cls : ''), label);
      b.addEventListener('click', onClick);
      return b;
    };
    const textInput = (value, placeholder) => {
      const i = mk('input');
      i.type = 'text';
      i.value = value || '';
      i.placeholder = placeholder || '';
      return i;
    };
    async function api(name, body) {
      const r = await fetch('/__replicator/' + name, body === undefined ? {} : {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || 'Request failed');
      return data;
    }
    const reloadSoon = ms => setTimeout(() => location.reload(), ms);
    const drawnText = () => window.__replicator_text ? window.__replicator_text.drawn() : [];
    const sceneText = () => window.__replicator_text ? window.__replicator_text.scene() : [];

    const editor = mk('div', 'rep-modal');
    const editorBox = mk('div', 'rep-modal-box rep-wide');
    const editorTitle = mk('div');
    editorTitle.style.cssText = 'font-weight:700;font-size:15px';
    const editorDesc = mk('div');
    editorDesc.style.cssText = 'font-size:12px;color:#94a3b8;line-height:1.4';
    const editorTools = mk('div');
    editorTools.style.cssText = 'display:flex;flex-direction:column;gap:8px';
    const editorList = mk('div', 'rep-list');
    const editorFoot = mk('div', 'rep-row');
    editorFoot.appendChild(button('Close', () => editor.classList.remove('open')));
    editorBox.append(editorTitle, editorDesc, editorTools, editorList, editorFoot);
    editor.appendChild(editorBox);
    shadow.insertBefore(editor, toast);

    function openEditor(title, desc) {
      editorTitle.textContent = title;
      editorDesc.textContent = desc;
      editorTools.replaceChildren();
      editorList.replaceChildren();
      panel.classList.remove('open');
      modal.classList.remove('open');
      editor.classList.add('open');
    }

    // CSS can show "Shop" as "SHOP", so find the spelling the page really contains.
    function realSpelling(text) {
      const lower = text.toLowerCase();
      const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let loose = null;
      while (w.nextNode()) {
        if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(w.currentNode.parentNode.nodeName)) continue;
        const v = w.currentNode.nodeValue;
        if (v.includes(text)) return text;
        const at = loose ? -1 : v.toLowerCase().indexOf(lower);
        if (at >= 0) loose = v.slice(at, at + text.length);
      }
      return loose || text;
    }

    async function saveText(from, to) {
      if (!from || from === to) return;
      try {
        const drawn = drawnText(), spelling = realSpelling(from);
        const inCanvas = drawn.includes(from);
        if (!inCanvas) from = spelling;
        const onPage = inCanvas || sceneText().includes(from) ||
                       (document.body.innerText || '').includes(from) || spelling !== from;
        const d = await api('text', {from, to});
        if (spelling !== from) await api('text', {from: spelling, to});  // same text on the page itself
        if (!d.source_files && !onPage && !d.was_edit) {
          await api('text', {from: d.from, to: d.from});
          showToast('Could not find that exact text. Check spelling and capitals.', true);
          return;
        }
        editor.classList.remove('open');
        if (d.missing.length && !inCanvas) {
          showToast('The 3D font has no glyph for: ' + d.missing.join(' '), true);
          reloadSoon(2600);
        } else {
          showToast('Text saved! Reloading...');
          reloadSoon(500);
        }
      } catch (err) { showToast(err.message, true); }
    }

    function pickText(e, target) {
      const caret = document.caretRangeFromPoint && document.caretRangeFromPoint(e.clientX, e.clientY);
      const node = caret && caret.startContainer;
      const text = node && node.nodeType === 3 ? node.nodeValue.trim() : '';
      if (!text || !target || target.closest('canvas')) { openTextEditor(); return; }
      openModal('Edit text', 'Changes this text everywhere on the site.', text, val => saveText(text, val.trim()));
    }

    async function openTextEditor() {
      openEditor('Edit text', 'Text drawn into canvas and WebGL is listed below. For other 3D text, ' +
                 'type it exactly as shown on screen in "Text on the page".');
      const from = textInput('', 'Text on the page'), to = textInput('', 'New text');
      const add = mk('div', 'rep-add');
      add.append(from, to, button('Replace', () => saveText(from.value.trim(), to.value.trim()), 'rep-btn-primary'));
      const filter = textInput('', 'Filter list...');
      editorTools.append(add, filter);
      let rules = [];
      try { rules = (await api('text')).rules; } catch (err) { showToast(err.message, true); }
      const known = new Set(rules.flatMap(r => [r.from, r.to]));
      const edited = new Map(rules.map(r => [r.to, r.from]));  // one row per visible text
      const rows = [...edited].map(([current, original]) => ({current, original}))
        .concat([...new Set([...sceneText(), ...drawnText()])].filter(t => !known.has(t)).map(t => ({current: t})));
      const draw = () => {
        const q = filter.value.toLowerCase();
        editorList.replaceChildren(...rows.filter(r => r.current.toLowerCase().includes(q)).map(r => {
          const row = mk('div', 'rep-item'), input = textInput(r.current);
          row.append(input, button('Save', () => saveText(r.current, input.value.trim()), 'rep-btn-primary'));
          row.append(r.original ? button('Reset', () => saveText(r.current, r.original)) : mk('span'));
          row.append(mk('small', '', r.original ? 'Edited. Original: ' + (r.original) : 'Canvas / WebGL'));
          return row;
        }));
        if (!editorList.children.length) editorList.append(mk('small', '', 'No canvas text found on this page yet.'));
      };
      filter.addEventListener('input', draw);
      draw();
    }

    function resized(file, width, height, mime) {
      return new Promise((resolve, reject) => {
        const img = new Image(), src = URL.createObjectURL(file);
        img.onerror = () => reject(new Error('That file is not a readable image.'));
        img.onload = () => {
          const c = document.createElement('canvas');
          c.width = width; c.height = height;
          const k = Math.max(width / img.naturalWidth, height / img.naturalHeight);
          const w = img.naturalWidth * k, h = img.naturalHeight * k;
          c.getContext('2d').drawImage(img, (width - w) / 2, (height - h) / 2, w, h);
          URL.revokeObjectURL(src);
          c.toBlob(b => b ? resolve(b) : reject(new Error('Could not encode the image.')), mime, 0.92);
        };
        img.src = src;
      });
    }

    // Logos stored as distance-field textures are redrawn from typed text.
    function distance1d(f, n, d, v, z) {
      let k = 0;
      v[0] = 0; z[0] = -1e20; z[1] = 1e20;
      for (let q = 1; q < n; q++) {
        let s;
        do {
          const r = v[k];
          s = (f[q] + q * q - f[r] - r * r) / (2 * q - 2 * r);
        } while (s <= z[k] && --k > -1);
        k++;
        v[k] = q; z[k] = s; z[k + 1] = 1e20;
      }
      k = 0;
      for (let q = 0; q < n; q++) {
        while (z[k + 1] < q) k++;
        const r = v[k];
        d[q] = (q - r) * (q - r) + f[r];
      }
    }
    function distanceField(grid, w, h) {
      const n = Math.max(w, h), f = new Float64Array(n), d = new Float64Array(n);
      const v = new Uint32Array(n), z = new Float64Array(n + 1);
      for (let x = 0; x < w; x++) {
        for (let y = 0; y < h; y++) f[y] = grid[y * w + x];
        distance1d(f, h, d, v, z);
        for (let y = 0; y < h; y++) grid[y * w + x] = d[y];
      }
      for (let y = 0; y < h; y++) {
        for (let x = 0; x < w; x++) f[x] = grid[y * w + x];
        distance1d(f, w, d, v, z);
        for (let x = 0; x < w; x++) grid[y * w + x] = d[x];
      }
      return grid;
    }
    function textTexture(text, item) {
      const S = 6, W = item.width * S, H = item.height * S, [x0, y0, x1, y1] = item.bbox;
      const c = document.createElement('canvas');
      c.width = W; c.height = H;
      const g = c.getContext('2d', {willReadFrequently: true}), native = window.__replicator_text.nativeCanvas;
      g.font = '900 200px "Arial Black", Impact, Arial, sans-serif';
      g.textBaseline = 'alphabetic';
      const m = native.measureText.call(g, text);
      const left = m.actualBoundingBoxLeft, right = m.actualBoundingBoxRight;
      const up = m.actualBoundingBoxAscent, down = m.actualBoundingBoxDescent;
      const tw = left + right, th = up + down;
      if (!(tw > 0 && th > 0)) throw new Error('Type some visible text first.');
      const k = Math.min((x1 - x0) * S / tw, (y1 - y0) * S / th);
      g.setTransform(k, 0, 0, k, (x0 + x1) * S / 2 - k * (right - left) / 2, (y0 + y1) * S / 2 + k * (up - down) / 2);
      native.fillText.call(g, text, 0, 0);
      const alpha = g.getImageData(0, 0, W, H).data;
      const outside = new Float64Array(W * H), inside = new Float64Array(W * H);
      for (let i = 0; i < W * H; i++) {
        const on = alpha[i * 4 + 3] > 127;
        outside[i] = on ? 0 : 1e20;
        inside[i] = on ? 1e20 : 0;
      }
      distanceField(outside, W, H);
      distanceField(inside, W, H);
      const out = new Uint8Array(item.width * item.height * 4);
      for (let y = 0; y < item.height; y++) {
        const row = item.height - 1 - y;  // data textures are stored bottom row first
        for (let x = 0; x < item.width; x++) {
          const i = Math.floor((y + 0.5) * S) * W + Math.floor((x + 0.5) * S);
          const dist = (Math.sqrt(inside[i]) - Math.sqrt(outside[i])) / S;
          const value = Math.round(Math.max(0, Math.min(1, 0.5 + dist / 4)) * 255), p = (row * item.width + x) * 4;
          out[p] = out[p + 1] = out[p + 2] = value;
        }
      }
      return out;
    }
    async function replaceWithText(item, text) {
      try {
        const r = await fetch('/__replicator/texture?url=' + encodeURIComponent(item.url),
                              {method: 'POST', body: textTexture(text, item)});
        if (!r.ok) throw new Error((await r.json()).error);
        showToast('Logo replaced! Reloading...');
        reloadSoon(500);
      } catch (err) { showToast(err.message, true); }
    }
    function sdfCard(item) {
      const card = mk('div', 'rep-card'), img = mk('img');
      img.src = item.preview;
      card.append(img, mk('span', '', item.url.split('?')[0].split('/').pop()), button('Replace with text', () => {
        editor.classList.remove('open');
        openModal('Replace logo with text', 'The new text is drawn in a heavy font and fitted to the original logo.',
                  '', val => { if (val.trim()) replaceWithText(item, val.trim()); });
      }));
      if (item.edited) card.append(button('Restore', async () => {
        await api('image_restore', {url: item.url});
        location.reload();
      }));
      return card;
    }

    async function openImageEditor() {
      openEditor('Replace images & textures', 'The new image is fitted to the original size, so 3D textures keep working.');
      const grid = mk('div', 'rep-grid');
      editorList.append(grid);
      let data;
      try { data = await api('images'); } catch (err) { showToast(err.message, true); return; }
      if (data.compressed) editorDesc.textContent += ' ' + data.compressed +
        ' compressed GPU textures (.ktx2/.basis) cannot be replaced here.';
      data.images.sort((a, b) => (b.kind === 'sdf') - (a.kind === 'sdf'));
      for (const item of data.images) {
        if (item.kind === 'sdf') { grid.append(sdfCard(item)); continue; }
        const card = mk('div', 'rep-card'), img = mk('img');
        img.loading = 'lazy';
        img.src = item.local;
        const file = mk('input');
        file.type = 'file';
        file.accept = 'image/*';
        file.addEventListener('change', async () => {
          try {
            if (!img.naturalWidth) throw new Error('The original image could not be read.');
            const blob = await resized(file.files[0], img.naturalWidth, img.naturalHeight, item.mime);
            const r = await fetch('/__replicator/image?url=' + encodeURIComponent(item.url), {method: 'POST', body: blob});
            if (!r.ok) throw new Error((await r.json()).error);
            editor.classList.remove('open');
            showToast('Image replaced! Reloading...');
            reloadSoon(500);
          } catch (err) { showToast(err.message, true); }
        });
        card.append(img, mk('span', '', item.url.split('?')[0].split('/').pop()),
                    button('Replace', () => file.click()));
        if (item.edited) card.append(button('Restore', async () => {
          await api('image_restore', {url: item.url});
          location.reload();
        }));
        grid.append(card);
      }
      if (!data.images.length) editorList.append(mk('small', '', 'No PNG, JPEG or WebP files in this archive.'));
    }

    // Block URL
    shadow.getElementById('rep_block_url_btn').addEventListener('click', () => {
      fetch('/__replicator/overrides').then(r => r.json()).then(d => {
        openModal(
          'Block Script / Endpoint URLs',
          'Enter URL substrings to block (one per line). ReplayServer will return empty 200 OK.',
          (d.blocked || []).join('\n'),
          val => {
            const list = val.split('\n').map(s => s.trim()).filter(Boolean);
            fetch('/__replicator/overrides', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({ blocked: list })
            }).then(() => {
              showToast('Blocked URLs updated! Reloading...');
              setTimeout(() => location.reload(), 600);
            });
          }
        );
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initUI);
  } else {
    initUI();
  }
})();
"""
