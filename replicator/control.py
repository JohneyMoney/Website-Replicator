"""Full visual control of a captured page: element edits and Three.js scene edits.

CONTROL_RUNTIME_JS replays saved edits (also inside single HTML exports).
EDITOR_JS is the point-and-click editor shown during "Play Offline".
"""
from __future__ import annotations

import json


def control_tag_content(ops: list[dict]) -> str:
    data = json.dumps(ops, ensure_ascii=False).replace("<", "\\u003c")
    return f"window.__replicator_edits={data};\n{CONTROL_RUNTIME_JS}"


CONTROL_RUNTIME_JS = r"""
(function () {
  if (window.__replicator_control) return;
  let ops = window.__replicator_edits || [];
  const page = () => window.__replicator_page || location.pathname;
  const current = () => ops.filter(o => !o.page || o.page === page());
  const pick = sel => { try { return document.querySelector(sel); } catch (_) { return null; } };
  const html = new WeakMap();

  function setText(el, value) {
    const nodes = [...el.childNodes].filter(n => n.nodeType === 3 && n.nodeValue.trim());
    if (el.children.length && nodes.length) {
      if (nodes[0].nodeValue !== value) nodes[0].nodeValue = value;
    } else if (el.textContent !== value) el.textContent = value;
  }
  function setStyle(el, prop, value) {
    if (value === '' || value == null) el.style.removeProperty(prop);
    else if (el.style.getPropertyValue(prop) !== value || el.style.getPropertyPriority(prop) !== 'important')
      el.style.setProperty(prop, value, 'important');
  }
  function addNode(o, target) {
    if (document.querySelector('[data-rep-op="' + o.id + '"]')) return;
    let node;
    if (o.kind === 'clone') {
      node = target.cloneNode(true);
      node.removeAttribute('id');
      node.querySelectorAll('[data-rep-op]').forEach(n => n.removeAttribute('data-rep-op'));
    } else {
      const t = document.createElement('template');
      t.innerHTML = o.value;
      node = t.content.firstElementChild;
    }
    if (!node) return;
    node.setAttribute('data-rep-op', o.id);
    target.insertAdjacentElement('afterend', node);
  }
  function applyDom() {
    for (const o of current()) {
      if (o.kind === '3d') continue;
      if (o.kind === 'title') { if (document.title !== o.value) document.title = o.value; continue; }
      const el = pick(o.sel);
      if (!el) continue;
      if (o.kind === 'style') for (const p of Object.keys(o.value)) setStyle(el, p, o.value[p]);
      else if (o.kind === 'text') setText(el, o.value);
      else if (o.kind === 'remove') setStyle(el, 'display', 'none');
      else if (o.kind === 'move') setStyle(el, 'translate', o.value.x + 'px ' + o.value.y + 'px');
      else if (o.kind === 'attr') {
        if (el.getAttribute(o.name) !== o.value) el.setAttribute(o.name, o.value);
        if (o.name === 'src' && el.hasAttribute('srcset')) el.removeAttribute('srcset');
      } else if (o.kind === 'html') {
        if (html.get(el) !== el.innerHTML) { el.innerHTML = o.value; html.set(el, el.innerHTML); }
      } else if (o.kind === 'clone' || o.kind === 'insert') addNode(o, el);
    }
  }
  let queued = false;
  function schedule() {
    if (queued) return;
    queued = true;
    setTimeout(() => { queued = false; applyDom(); }, 60);
  }
  function watch() {
    new MutationObserver(schedule).observe(document, {subtree: true, childList: true, characterData: true});
    document.addEventListener('DOMContentLoaded', applyDom);
    if (document.readyState !== 'loading') applyDom();
  }

  // ------------------------------------------------------------ Three.js scenes
  const scenes = [], cache = new Map();
  let camera = null;
  function wrapRenderer(r) {
    if (r.__replicatorWrapped) return;
    r.__replicatorWrapped = true;
    const render = r.render;
    r.render = function (scene, cam) {
      if (scene && scene.isScene && !scenes.includes(scene)) scenes.push(scene);
      if (cam && cam.isPerspectiveCamera && (!camera || scene === scenes[0])) camera = cam;
      apply3d();
      return render.apply(this, arguments);
    };
  }
  if (!window.__THREE_DEVTOOLS__) {
    const tools = new EventTarget();
    tools.addEventListener('observe', e => { if (e.detail && e.detail.isWebGLRenderer) wrapRenderer(e.detail); });
    window.__THREE_DEVTOOLS__ = tools;
  }
  function childKey(parent, child) {
    if (!child.name) return 'i:' + parent.children.indexOf(child);
    const same = parent.children.filter(c => c.name === child.name);
    return 'n:' + child.name + ':' + same.indexOf(child);
  }
  function resolve(path) {
    if (path === 'camera') return camera;
    const parts = path.split('/');
    let node = scenes[+parts[0]];
    for (const key of parts.slice(1)) {
      if (!node) return null;
      const m = /^n:(.*):(\d+)$/.exec(key);
      node = m ? node.children.filter(c => c.name === m[1])[+m[2]] : node.children[+key.slice(2)];
    }
    return node || null;
  }
  const materials = o => !o.material ? [] : Array.isArray(o.material) ? o.material : [o.material];
  function apply3d() {
    for (const o of ops) {
      if (o.kind !== '3d' || (o.page && o.page !== page())) continue;
      let node = cache.get(o.path);
      if (!node || (o.path !== 'camera' && !node.parent && !node.isScene)) {
        node = resolve(o.path);
        if (!node) continue;
        cache.set(o.path, node);
      }
      const v = o.value;
      if (v.visible !== undefined) node.visible = v.visible;
      if (v.position) node.position.set(...v.position);
      if (v.rotation) node.rotation.set(...v.rotation.map(d => d * Math.PI / 180));
      if (v.scale && node !== camera) node.scale.set(...v.scale);
      if (v.fov !== undefined && node.isCamera && node.fov !== v.fov) { node.fov = v.fov; node.updateProjectionMatrix(); }
      for (const m of materials(node)) {
        if (v.color) {
          if (m.color) m.color.set(v.color);
          else if (m.uniforms && m.uniforms.uColor && m.uniforms.uColor.value && m.uniforms.uColor.value.set)
            m.uniforms.uColor.value.set(v.color);
        }
        if (v.opacity !== undefined && (m.opacity !== v.opacity || !m.transparent)) {
          m.opacity = v.opacity;
          if (!m.transparent) { m.transparent = true; m.needsUpdate = true; }
        }
        if (v.wireframe !== undefined) m.wireframe = v.wireframe;
      }
    }
  }
  function tree() {
    const out = [];
    const walk = (node, path, depth) => {
      if (out.length >= 1500) return;
      out.push({path, depth, label: node.name || node.type || 'Object', type: node.type || ''});
      for (const c of node.children || []) walk(c, path + '/' + childKey(node, c), depth + 1);
    };
    scenes.forEach((s, i) => walk(s, String(i), 0));
    if (camera) out.unshift({path: 'camera', depth: 0, label: 'Camera', type: camera.type});
    return out;
  }
  function read3d(path) {
    const n = resolve(path);
    if (!n) return null;
    const round = v => Math.round(v * 1000) / 1000, m = materials(n)[0];
    const color = m && (m.color || (m.uniforms && m.uniforms.uColor && m.uniforms.uColor.value));
    return {visible: n.visible, position: n.position.toArray().map(round),
            rotation: [n.rotation.x, n.rotation.y, n.rotation.z].map(r => round(r * 180 / Math.PI)),
            scale: n.scale.toArray().map(round), fov: n.isPerspectiveCamera ? n.fov : undefined,
            color: color && color.getHexString ? '#' + color.getHexString() : undefined,
            opacity: m ? m.opacity : undefined, wireframe: m ? !!m.wireframe : undefined};
  }
  function flash(path) {
    const n = resolve(path);
    if (!n) return;
    const was = n.visible;
    let left = 6;
    const t = setInterval(() => { n.visible = !n.visible; if (--left <= 0) { clearInterval(t); n.visible = was; } }, 130);
  }
  window.__replicator_control = {
    page, watch, applyNow: applyDom, tree, read3d, flash,
    get ops() { return ops; },
    setOps(list) { ops = list; cache.clear(); applyDom(); }
  };
  watch();
})();
"""


EDITOR_JS = r"""
(function () {
  if (window.__replicator_editor) return;
  const C = () => window.__replicator_control;
  const STORE = '__replicator_editor';
  let saved = {};
  try { saved = JSON.parse(sessionStorage.getItem(STORE) || '{}'); } catch (_) {}
  let past = saved.past || [], future = saved.future || [];
  let host, root, panel, body, hoverBox, selBox, toastBox;
  let active = false, interact = false, tab = 'page', side = saved.side || 'right';
  let sel = null, hover = null, drag = null, dragEnded = 0, under = [], node3d = null, filter3d = '';

  function remember(open) {
    try {
      sessionStorage.setItem(STORE, JSON.stringify({past: past.slice(-40), future: future.slice(-40), side, open}));
    } catch (_) {}
  }
  function h(tag, props, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else if (k === 'class') n.className = v;
      else if (k in n && k !== 'list') n[k] = v;
      else n.setAttribute(k, v);
    }
    for (const c of kids.flat()) if (c != null) n.append(c);
    return n;
  }
  function toast(msg, bad) {
    toastBox.textContent = msg;
    toastBox.style.background = bad ? '#dc2626' : '#059669';
    toastBox.style.display = 'block';
    clearTimeout(toast.t);
    toast.t = setTimeout(() => { toastBox.style.display = 'none'; }, 2200);
  }
  async function persist() {
    remember(active);
    try {
      const r = await fetch('/__replicator/edits', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                    body: JSON.stringify({ops: C().ops})});
      if (!r.ok) throw new Error('Could not save');
    } catch (err) { toast(err.message, true); }
  }
  const newId = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  function commit(op, sameAs) {
    past.push(C().ops);
    future = [];
    const next = C().ops.filter(o => !(sameAs && sameAs(o)));
    next.push({id: newId(), page: C().page(), ...op});
    C().setOps(next);
    persist();
    return next[next.length - 1];
  }
  async function jump(from, to) {
    if (!from.length) return toast('Nothing to change');
    to.push(C().ops);
    C().setOps(from.pop());
    await persist();
    location.reload();
  }

  function selector(el) {
    if (el === document.body) return 'body';
    if (el === document.documentElement) return 'html';
    if (el.dataset && el.dataset.repOp) return '[data-rep-op="' + el.dataset.repOp + '"]';
    const parts = [];
    for (let n = el; n && n !== document.body && n.nodeType === 1; n = n.parentElement) {
      if (n.dataset.repOp) { parts.unshift('[data-rep-op="' + n.dataset.repOp + '"]'); return parts.join(' > '); }
      if (n.id && document.querySelectorAll('#' + CSS.escape(n.id)).length === 1) {
        parts.unshift('#' + CSS.escape(n.id));
        return parts.join(' > ');
      }
      const tag = CSS.escape(n.localName);
      const same = n.parentElement ? [...n.parentElement.children].filter(c => c.localName === n.localName) : [n];
      parts.unshift(tag + ':nth-of-type(' + (same.indexOf(n) + 1) + ')');
    }
    return 'body > ' + parts.join(' > ');
  }
  const label = el => el.localName + (el.id ? '#' + el.id : el.classList.length ? '.' + el.classList[0] : '');
  const isUi = t => t === host || (t && t.id === '__replicator_ui_host');
  const pointTarget = (x, y) => { const t = document.elementFromPoint(x, y); return isUi(t) ? null : t; };
  function hex(value) {
    const m = /rgba?\((\d+)[, ]+(\d+)[, ]+(\d+)/.exec(value || '');
    return m ? '#' + [m[1], m[2], m[3]].map(v => (+v).toString(16).padStart(2, '0')).join('') : '#000000';
  }
  function place(box, el) {
    if (!el || !el.isConnected) { box.style.display = 'none'; return; }
    const r = el.getBoundingClientRect();
    Object.assign(box.style, {display: 'block', left: r.left + 'px', top: r.top + 'px',
                              width: r.width + 'px', height: r.height + 'px'});
  }
  function tick() {
    if (!active) return;
    place(hoverBox, interact || hover === sel ? null : hover);
    place(selBox, interact ? null : sel);
    requestAnimationFrame(tick);
  }

  const styleOp = (prop, value) => {
    const s = selector(sel), old = C().ops.find(o => o.kind === 'style' && o.sel === s && o.page === C().page());
    commit({kind: 'style', sel: s, value: {...(old ? old.value : {}), [prop]: value}},
           o => o.kind === 'style' && o.sel === s && o.page === C().page());
  };
  const replaceOp = (kind, value, extra) => {
    const s = selector(sel);
    commit({kind, sel: s, value, ...extra}, o => o.kind === kind && o.sel === s && o.page === C().page() &&
                                                 (!extra || o.name === extra.name));
  };
  function field(title, control) { return h('label', {class: 'f'}, h('span', {}, title), control); }
  function styleRow(prop, title, type) {
    const cs = getComputedStyle(sel), value = cs.getPropertyValue(prop).trim();
    const input = h('input', {value, 'data-style': prop, onchange: e => styleOp(prop, e.target.value.trim())});
    if (type !== 'color') return field(title, input);
    const pick = h('input', {type: 'color', value: hex(value), oninput: e => { input.value = e.target.value; },
                             onchange: e => styleOp(prop, e.target.value)});
    return field(title, h('div', {class: 'two'}, pick, input));
  }
  function readImage(file) {
    return new Promise((resolve, reject) => {
      if (file.size > 12 * 1024 * 1024) return reject(new Error('Choose an image under 12 MB.'));
      const r = new FileReader();
      r.onload = () => resolve(r.result);
      r.onerror = () => reject(new Error('Could not read that file.'));
      r.readAsDataURL(file);
    });
  }
  const filePicker = onData => h('input', {type: 'file', accept: 'image/*', onchange: e => {
    readImage(e.target.files[0]).then(onData).catch(err => toast(err.message, true));
  }});
  function insertAfter(markup) {
    const op = commit({kind: 'insert', sel: selector(sel), value: markup});
    C().applyNow();
    const made = document.querySelector('[data-rep-op="' + op.id + '"]');
    if (made) select(made);
  }
  function select(el) {
    sel = el;
    tab = 'page';
    draw();
  }

  function pageTab() {
    const out = [field('Page title', h('input', {value: document.title, 'data-k': 'title', onchange: e =>
      commit({kind: 'title', value: e.target.value}, o => o.kind === 'title' && o.page === C().page())}))];
    if (!sel || !sel.isConnected) {
      out.push(h('p', {class: 'hint'}, 'Click anything on the page to edit it. Drag a selected element to move it.'));
      return out;
    }
    const own = [...sel.childNodes].filter(n => n.nodeType === 3 && n.nodeValue.trim()).map(n => n.nodeValue.trim())[0];
    const text = sel.children.length ? own : sel.textContent;
    out.push(h('div', {class: 'crumb'}, h('b', {}, label(sel)),
      sel.parentElement && sel !== document.body ? h('button', {onclick: () => select(sel.parentElement)}, '↑ Parent') : null));
    if (under.length > 1) out.push(h('div', {class: 'chips'}, under.slice(0, 7).map(el =>
      h('button', {class: el === sel ? 'on' : '', onclick: () => select(el)}, label(el).slice(0, 22)))));
    const isRoot = sel === document.body || sel === document.documentElement;
    if (!isRoot) out.push(h('div', {class: 'row'},
      h('button', {'data-k': 'duplicate', onclick: () => { commit({kind: 'clone', sel: selector(sel)}); toast('Duplicated'); }}, 'Duplicate'),
      h('button', {class: 'bad', 'data-k': 'delete', onclick: () => {
        commit({kind: 'remove', sel: selector(sel)}); sel = null; draw(); }}, 'Delete')));
    if (text != null && sel.localName !== 'canvas') out.push(field('Text', h('textarea', {value: text, rows: 3, 'data-k': 'text',
      onchange: e => replaceOp('text', e.target.value)})));
    if (sel.localName === 'a') out.push(field('Link', h('input', {value: sel.getAttribute('href') || '', 'data-k': 'href',
      onchange: e => replaceOp('attr', e.target.value, {name: 'href'})})));
    if (sel.localName === 'img') {
      out.push(field('Image address', h('input', {value: sel.getAttribute('src') || '',
        onchange: e => replaceOp('attr', e.target.value, {name: 'src'})})));
      out.push(field('New image', filePicker(data => replaceOp('attr', data, {name: 'src'}))));
    }
    if (sel.localName === 'canvas') out.push(h('button', {class: 'wide', onclick: () => { tab = '3d'; draw(); }}, 'Open 3D scene controls'));
    out.push(h('h4', {}, 'Style'));
    for (const [p, t, k] of [['color', 'Text color', 'color'], ['background-color', 'Background', 'color'],
      ['font-size', 'Font size'], ['font-weight', 'Weight'], ['font-family', 'Font'], ['text-align', 'Align'],
      ['opacity', 'Opacity'], ['width', 'Width'], ['height', 'Height'], ['margin', 'Margin'], ['padding', 'Padding'],
      ['border', 'Border'], ['border-radius', 'Corners'], ['z-index', 'Layer']]) out.push(styleRow(p, t, k));
    const extra = h('input', {placeholder: 'property: value'});
    out.push(field('Other CSS', h('div', {class: 'two'}, extra, h('button', {onclick: () => {
      const m = /^\s*([-\w]+)\s*:\s*(.+?);?\s*$/.exec(extra.value);
      if (m) styleOp(m[1], m[2]); else toast('Write it as property: value', true);
    }}, 'Add'))));
    if (!isRoot) out.push(h('h4', {}, 'Add after this'), h('div', {class: 'row'},
      h('button', {'data-k': 'add-text', onclick: () => insertAfter('<p>New text</p>')}, 'Text'),
      h('button', {onclick: () => insertAfter('<button type="button">Button</button>')}, 'Button'),
      h('button', {onclick: () => insertAfter('<a href="#">New link</a>')}, 'Link')));
    if (!isRoot) out.push(field('Image', filePicker(data => {
      const img = document.createElement('img');
      img.src = data; img.style.maxWidth = '100%';
      insertAfter(img.outerHTML);
    })));
    const code = h('textarea', {value: sel.innerHTML.slice(0, 200000), rows: 5, spellcheck: false});
    out.push(h('h4', {}, 'HTML inside'), code, h('button', {class: 'wide', onclick: () => replaceOp('html', code.value)}, 'Apply HTML'));
    return out;
  }

  function vector(title, key, v, path) {
    return field(title, h('div', {class: 'three'}, v[key].map((n, i) => h('input', {type: 'number', step: 'any', value: n,
      'data-k': key + i, onchange: e => {
        const next = v[key].slice();
        next[i] = parseFloat(e.target.value) || 0;
        edit3d(path, {[key]: next});
      }}))));
  }
  function edit3d(path, patch) {
    const old = C().ops.find(o => o.kind === '3d' && o.path === path && o.page === C().page());
    commit({kind: '3d', path, value: {...(old ? old.value : {}), ...patch}},
           o => o.kind === '3d' && o.path === path && o.page === C().page());
    setTimeout(draw, 80);
  }
  function sceneTab() {
    const all = C().tree();
    if (!all.length) return [h('p', {class: 'hint'}, 'No Three.js scene has rendered yet. Other 3D engines are not supported here.')];
    const out = [];
    const v = node3d && C().read3d(node3d);
    if (v) {
      out.push(h('div', {class: 'crumb'}, h('b', {}, (all.find(n => n.path === node3d) || {}).label || 'Object'),
        h('button', {onclick: () => C().flash(node3d)}, 'Find')));
      out.push(h('label', {class: 'check'}, h('input', {type: 'checkbox', checked: v.visible, 'data-k': 'visible',
        onchange: e => edit3d(node3d, {visible: e.target.checked})}), 'Visible'));
      out.push(vector('Position', 'position', v, node3d), vector('Rotation°', 'rotation', v, node3d));
      if (node3d !== 'camera') out.push(vector('Scale', 'scale', v, node3d));
      if (v.fov !== undefined) out.push(field('Field of view', h('input', {type: 'number', value: v.fov,
        onchange: e => edit3d(node3d, {fov: parseFloat(e.target.value) || v.fov})})));
      if (v.color) out.push(field('Color', h('input', {type: 'color', value: v.color, 'data-k': 'color3d',
        onchange: e => edit3d(node3d, {color: e.target.value})})));
      if (v.opacity !== undefined) out.push(field('Opacity', h('input', {type: 'range', min: 0, max: 1, step: 0.01, value: v.opacity,
        onchange: e => edit3d(node3d, {opacity: parseFloat(e.target.value)})})));
      if (v.wireframe !== undefined) out.push(h('label', {class: 'check'}, h('input', {type: 'checkbox', checked: v.wireframe,
        onchange: e => edit3d(node3d, {wireframe: e.target.checked})}), 'Wireframe'));
    }
    const list = h('div', {class: 'tree'});
    const fill = () => {
      const q = filter3d.toLowerCase();
      list.replaceChildren(...all.filter(n => (n.label + ' ' + n.type).toLowerCase().includes(q)).slice(0, 400).map(n =>
        h('button', {class: n.path === node3d ? 'on' : '', style: 'padding-left:' + (6 + Math.min(n.depth, 8) * 10) + 'px',
                     'data-path': n.path, onclick: () => { node3d = n.path; draw(); }}, n.label)));
    };
    fill();
    out.push(h('h4', {}, all.length + ' objects'), h('input', {placeholder: 'Search objects...', value: filter3d,
      oninput: e => { filter3d = e.target.value; fill(); }}), list);
    return out;
  }

  function draw() {
    if (!active) return;
    panel.className = 'panel ' + side;
    const scroll = body.scrollTop;
    root.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
    root.querySelector('[data-k=interact]').textContent = interact ? 'Use page: on' : 'Use page: off';
    body.replaceChildren(...(tab === 'page' ? pageTab() : sceneTab()));
    body.scrollTop = scroll;
  }
  function build() {
    host = h('div', {id: '__replicator_editor_host'});
    host.style.all = 'initial';
    document.documentElement.append(host);
    root = host.attachShadow({mode: 'open'});
    root.append(h('style', {}, `
      *{box-sizing:border-box;font:12px system-ui,Segoe UI,sans-serif}
      .panel{position:fixed;top:0;bottom:0;width:320px;z-index:2147483646;background:#0f172a;color:#e2e8f0;display:flex;flex-direction:column;box-shadow:0 0 24px #0008}
      .right{right:0}.left{left:0}.bar{display:flex;gap:5px;padding:8px;border-bottom:1px solid #334155;flex-wrap:wrap}
      .body{flex:1;overflow:auto;padding:10px;display:flex;flex-direction:column;gap:8px}
      button{background:#1e293b;color:#f1f5f9;border:1px solid #334155;border-radius:6px;padding:6px 8px;cursor:pointer}
      button:hover{background:#334155}button.on{background:#2563eb;border-color:#3b82f6}.bad{background:#7f1d1d;border-color:#b91c1c}
      input,textarea,select{width:100%;min-width:0;background:#1e293b;color:#f8fafc;border:1px solid #475569;border-radius:5px;padding:5px}
      input[type=color]{padding:1px;height:27px}input[type=checkbox]{width:auto}textarea{resize:vertical;font-family:Consolas,monospace}
      .f{display:grid;gap:3px}.f>span,.hint{color:#94a3b8}.two{display:grid;grid-template-columns:44px 1fr;gap:5px}.two:has(>button){grid-template-columns:1fr auto}
      .three{display:grid;grid-template-columns:repeat(3,1fr);gap:4px}.row{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:5px}
      .crumb{display:flex;justify-content:space-between;align-items:center;gap:8px}.crumb b{overflow-wrap:anywhere;color:#7dd3fc}
      .chips{display:flex;flex-wrap:wrap;gap:4px}.chips button{padding:3px 6px;font-size:11px}h4{margin:6px 0 0;color:#f8fafc;font-weight:700}
      .check{display:flex;gap:6px;align-items:center}.wide{width:100%}.tree{display:flex;flex-direction:column;gap:2px}
      .tree button{text-align:left;border:0;background:transparent;padding-top:3px;padding-bottom:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .tree button.on{background:#2563eb}.box{position:fixed;pointer-events:none;z-index:2147483645;display:none}
      .hover{outline:1px dashed #38bdf8}.sel{outline:2px solid #f59e0b;background:#f59e0b14}
      .toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:2147483647;color:#fff;padding:8px 14px;border-radius:7px;display:none}`));
    hoverBox = h('div', {class: 'box hover'});
    selBox = h('div', {class: 'box sel'});
    toastBox = h('div', {class: 'toast'});
    body = h('div', {class: 'body'});
    panel = h('div', {class: 'panel ' + side},
      h('div', {class: 'bar'},
        h('button', {'data-tab': 'page', onclick: () => { tab = 'page'; draw(); }}, 'Page'),
        h('button', {'data-tab': '3d', onclick: () => { tab = '3d'; draw(); }}, '3D'),
        h('button', {'data-k': 'interact', onclick: () => { interact = !interact; draw(); }}, ''),
        h('button', {onclick: () => { side = side === 'right' ? 'left' : 'right'; remember(true); draw(); }}, '⇄'),
        h('button', {'data-k': 'undo', onclick: () => jump(past, future)}, 'Undo'),
        h('button', {'data-k': 'redo', onclick: () => jump(future, past)}, 'Redo'),
        h('button', {class: 'bad', onclick: async () => {
          if (!confirm('Remove all edits made with this editor on every page?')) return;
          past.push(C().ops); future = []; C().setOps([]); await persist(); location.reload();
        }}, 'Reset'),
        h('button', {'data-k': 'close', onclick: close}, '✕')),
      body);
    root.append(hoverBox, selBox, panel, toastBox);
  }
  function open() {
    if (!host) build();
    host.style.display = '';
    active = true;
    remember(true);
    draw();
    requestAnimationFrame(tick);
  }
  function close() {
    active = false;
    host.style.display = 'none';
    remember(false);
  }

  const blocked = e => active && !interact && !e.composedPath().includes(host) && !isUi(e.composedPath()[0]) &&
                       !e.composedPath().some(n => n && n.id === '__replicator_ui_host');
  const stop = e => { e.preventDefault(); e.stopImmediatePropagation(); };
  addEventListener('mousemove', e => {
    if (drag) {
      stop(e);
      const dx = e.clientX - drag.startX, dy = e.clientY - drag.startY;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      if (drag.moved) {
        drag.x = drag.baseX + dx; drag.y = drag.baseY + dy;
        drag.el.style.setProperty('translate', drag.x + 'px ' + drag.y + 'px', 'important');
      }
    } else if (blocked(e)) hover = pointTarget(e.clientX, e.clientY);
  }, true);
  // Only hide these from the page: cancelling pointerdown would also cancel mousedown.
  for (const type of ['pointerdown', 'pointerup', 'touchstart', 'touchend'])
    addEventListener(type, e => { if (blocked(e)) e.stopImmediatePropagation(); }, {capture: true, passive: true});
  for (const type of ['dblclick', 'contextmenu'])
    addEventListener(type, e => { if (blocked(e)) stop(e); }, true);
  addEventListener('mousedown', e => {
    if (!blocked(e)) return;
    stop(e);
    const t = pointTarget(e.clientX, e.clientY);
    if (sel && t && (t === sel || sel.contains(t)) && sel !== document.body) {
      const s = selector(sel), old = C().ops.find(o => o.kind === 'move' && o.sel === s && o.page === C().page());
      drag = {el: sel, sel: s, startX: e.clientX, startY: e.clientY, baseX: old ? old.value.x : 0, baseY: old ? old.value.y : 0};
    }
  }, true);
  addEventListener('mouseup', e => {
    if (drag) {
      stop(e);
      const d = drag;
      drag = null;
      if (d.moved) {
        commit({kind: 'move', sel: d.sel, value: {x: Math.round(d.x), y: Math.round(d.y)}},
               o => o.kind === 'move' && o.sel === d.sel && o.page === C().page());
        dragEnded = Date.now();
      }
    } else if (blocked(e)) stop(e);
  }, true);
  addEventListener('click', e => {
    if (!blocked(e)) return;
    stop(e);
    if (Date.now() - dragEnded < 120) return;  // the click that ends a drag
    under = document.elementsFromPoint(e.clientX, e.clientY).filter(n => !isUi(n) && n !== document.documentElement);
    const t = under[0];
    if (t) select(t);
  }, true);
  addEventListener('keydown', e => {
    if (e.altKey && (e.key === 'e' || e.key === 'E')) { e.preventDefault(); active ? close() : open(); }
    else if (active && e.key === 'Escape') { sel = null; draw(); }
  }, true);
  window.__replicator_editor = {open, close};
  if (saved.open) {
    if (document.readyState === 'loading') addEventListener('DOMContentLoaded', () => setTimeout(open, 200));
    else setTimeout(open, 200);
  }
})();
"""
