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
      <button class="rep-btn rep-btn-primary" id="rep_zap_btn">🎯 Zap Element (Alt+Z)</button>
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
    let hoveredEl = null;

    function startZapping() {
      zapping = true;
      panel.classList.remove('open');
      pill.classList.add('active');
      pill.innerHTML = '<span>✕</span><span>Click an element (Esc)</span>';
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
      stopZapping();
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
      }
    });

    shadow.getElementById('rep_zap_btn').addEventListener('click', startZapping);

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
