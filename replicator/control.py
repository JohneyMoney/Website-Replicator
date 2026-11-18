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
  const scenes = [], cache = new Map(), views = new Map(), images = new Map();
  let camera = null, liveOp = null;
  function wrapRenderer(r) {
    if (r.__replicatorWrapped) return;
    r.__replicatorWrapped = true;
    const render = r.render;
    r.render = function (scene, cam) {
      if (scene && scene.isScene && !scenes.includes(scene)) scenes.push(scene);
      if (cam && cam.isPerspectiveCamera && (!camera || scene === scenes[0])) camera = cam;
      if (scene && scene.isScene && cam) views.set(scene, {camera: cam, renderer: this, at: performance.now()});
      apply3d();
      return render.apply(this, arguments);
    };
  }
  if (!window.__THREE_DEVTOOLS__) {
    const tools = new EventTarget();
    tools.addEventListener('observe', e => { if (e.detail && e.detail.isWebGLRenderer) wrapRenderer(e.detail); });
    window.__THREE_DEVTOOLS__ = tools;
  }
  const safeName = name => String(name).replace(/\//g, '\u2215');
  function childKey(parent, child) {
    if (!child.name) return 'i:' + parent.children.indexOf(child);
    const same = parent.children.filter(c => c.name === child.name);
    return 'n:' + safeName(child.name) + ':' + same.indexOf(child);
  }
  function resolve(path) {
    if (path === 'camera') return camera;
    const parts = path.split('/');
    let node = scenes[+parts[0]];
    for (const key of parts.slice(1)) {
      if (!node) return null;
      const m = /^n:(.*):(\d+)$/.exec(key);
      node = m ? node.children.filter(c => c.name && safeName(c.name) === m[1])[+m[2]] : node.children[+key.slice(2)];
    }
    return node || null;
  }
  function pathOf(node) {
    if (node === camera) return 'camera';
    const parts = [];
    for (; node.parent; node = node.parent) parts.unshift(childKey(node.parent, node));
    const i = scenes.indexOf(node);
    return i < 0 ? null : [i, ...parts].join('/');
  }
  const materials = o => !o.material ? [] : Array.isArray(o.material) ? o.material : [o.material];
  const SLOTS = ['map', 'emissiveMap', 'normalMap', 'roughnessMap', 'metalnessMap', 'aoMap', 'alphaMap', 'bumpMap', 'lightMap'];
  const slotTexture = (m, slot) => slot.startsWith('u:') ? (m.uniforms && m.uniforms[slot.slice(2)] || {}).value : m[slot];
  function slotsOf(m) {
    const out = SLOTS.filter(s => m[s] && m[s].isTexture);
    for (const k of Object.keys(m.uniforms || {})) {
      const v = m.uniforms[k] && m.uniforms[k].value;
      if (v && v.isTexture) out.push('u:' + k);
    }
    return out;
  }
  const drawable = i => !!i && ((window.HTMLImageElement && i instanceof HTMLImageElement) ||
    (window.HTMLCanvasElement && i instanceof HTMLCanvasElement) ||
    (window.ImageBitmap && i instanceof ImageBitmap) || (window.OffscreenCanvas && i instanceof OffscreenCanvas));
  const canSwap = t => !!t && t.isTexture && !t.isCompressedTexture && !t.isDataTexture && !t.isCubeTexture &&
    !t.isVideoTexture && !t.isRenderTargetTexture && !t.isDepthTexture && drawable(t.image);
  function setTexture(m, slot, url) {
    const tex = slotTexture(m, slot);
    if (!canSwap(tex) || tex.__replicatorSrc === url) return;
    let img = images.get(url);
    if (!img) { img = new Image(); img.src = url; images.set(url, img); }
    if (!img.complete || !img.naturalWidth) return;
    tex.__replicatorSrc = url;
    tex.image = img;
    tex.needsUpdate = true;
  }
  function setUniform(u, value) {
    if (!u) return;
    const now = u.value;
    if (typeof now === 'number') u.value = +value;
    else if (now && now.isColor) now.set(value);
    else if (now && now.fromArray && Array.isArray(value)) now.fromArray(value);
  }
  function apply3d() {
    for (const o of liveOp ? ops.concat(liveOp) : ops) {
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
      if (v.text !== undefined && typeof node.text === 'string' && node.text !== v.text) {
        node.text = v.text;
        if (node.sync) node.sync();
      }
      for (const m of materials(node)) {
        if (v.color) {
          if (m.color) m.color.set(v.color);
          else if (m.uniforms && m.uniforms.uColor && m.uniforms.uColor.value && m.uniforms.uColor.value.set)
            m.uniforms.uColor.value.set(v.color);
        }
        if (v.emissive && m.emissive) m.emissive.set(v.emissive);
        for (const k of ['roughness', 'metalness']) if (v[k] !== undefined && typeof m[k] === 'number') m[k] = v[k];
        if (v.opacity !== undefined && (m.opacity !== v.opacity || !m.transparent)) {
          m.opacity = v.opacity;
          if (!m.transparent) { m.transparent = true; m.needsUpdate = true; }
        }
        if (v.wireframe !== undefined) m.wireframe = v.wireframe;
        if (v.uniforms && m.uniforms) for (const k of Object.keys(v.uniforms)) setUniform(m.uniforms[k], v.uniforms[k]);
        if (v.textures) for (const k of Object.keys(v.textures)) setTexture(m, k, v.textures[k]);
      }
    }
  }
  function tree() {
    const out = [];
    const walk = (node, path, depth) => {
      if (out.length >= 6000) return;
      const said = typeof node.text === 'string' && node.text.trim() ? '"' + node.text.trim().slice(0, 40) + '"' : '';
      out.push({path, depth, label: node.name || said || node.type || 'Object', type: node.type || '',
                kids: (node.children || []).length, visible: node.visible !== false});
      for (const c of node.children || []) walk(c, path + '/' + childKey(node, c), depth + 1);
    };
    scenes.forEach((s, i) => walk(s, String(i), 0));
    if (camera) out.unshift({path: 'camera', depth: 0, label: 'Camera', type: camera.type, kids: 0, visible: true});
    return out;
  }
  function read3d(path) {
    const n = resolve(path);
    if (!n) return null;
    const round = v => Math.round(v * 1000) / 1000, m = materials(n)[0];
    const hexOf = c => c && c.getHexString ? '#' + c.getHexString() : undefined;
    const color = m && (m.color || (m.uniforms && m.uniforms.uColor && m.uniforms.uColor.value));
    const uniforms = [];
    for (const k of Object.keys(m && m.uniforms || {}).slice(0, 60)) {
      const v = m.uniforms[k] && m.uniforms[k].value;
      if (typeof v === 'number') uniforms.push({name: k, type: 'number', value: round(v)});
      else if (v && v.isColor) uniforms.push({name: k, type: 'color', value: hexOf(v)});
      else if (v && v.toArray && v.fromArray && !v.isTexture && (v.isVector2 || v.isVector3 || v.isVector4))
        uniforms.push({name: k, type: 'vector', value: v.toArray().map(round)});
    }
    const pos = n.geometry && n.geometry.attributes && n.geometry.attributes.position;
    return {visible: n.visible, position: n.position.toArray().map(round),
            rotation: [n.rotation.x, n.rotation.y, n.rotation.z].map(r => round(r * 180 / Math.PI)),
            scale: n.scale.toArray().map(round), fov: n.isPerspectiveCamera ? n.fov : undefined,
            color: hexOf(color), emissive: m ? hexOf(m.emissive) : undefined,
            roughness: m && typeof m.roughness === 'number' ? round(m.roughness) : undefined,
            metalness: m && typeof m.metalness === 'number' ? round(m.metalness) : undefined,
            opacity: m ? m.opacity : undefined, wireframe: m ? !!m.wireframe : undefined,
            type: n.type || 'Object', material: m ? m.type : undefined, vertices: pos ? pos.count : undefined, uniforms};
  }
  function textures3d(path) {
    const n = resolve(path), out = [];
    if (!n) return out;
    for (const m of materials(n)) for (const slot of slotsOf(m)) {
      const t = slotTexture(m, slot);
      if (!out.some(x => x.slot === slot)) out.push({slot, image: drawable(t.image) ? t.image : null, swap: canSwap(t)});
    }
    return out;
  }
  function texts3d(path) {
    const n = resolve(path), T = window.__replicator_text, drawn = new Set();
    if (!n) return {own: null, drawn: []};
    for (const m of materials(n)) for (const slot of slotsOf(m)) {
      const img = (slotTexture(m, slot) || {}).image;
      if (T && T.canvasText && img) for (const s of T.canvasText(img)) drawn.add(s);
    }
    return {own: typeof n.text === 'string' ? n.text : null, drawn: [...drawn]};
  }
  function flash(path) {
    const n = resolve(path);
    if (!n) return;
    const was = n.visible;
    let left = 6;
    const t = setInterval(() => { n.visible = !n.visible; if (--left <= 0) { clearInterval(t); n.visible = was; } }, 130);
  }
  window.__replicator_control = {
    page, watch, applyNow: applyDom, tree, read3d, textures3d, texts3d, flash, resolve, pathOf,
    views: () => views,
    preview(op) { liveOp = op; },
    get ops() { return ops; },
    setOps(list) { ops = list; liveOp = null; cache.clear(); applyDom(); }
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
  let host, root, panel, body, list, search, hoverBox, selBox, tagBox, toastBox;
  let active = false, interact = false, tab = saved.tab || 'page', side = saved.side || 'right';
  let sel = null, hover = null, drag = null, dragEnded = 0, under = [], node3d = saved.node || null, filterText = '';
  let hover3d = null, hits3d = [], drag3d = null, reveal = !!saved.node;
  const open3d = new Set(), domOpen = new WeakSet();

  function remember(open) {
    try {
      sessionStorage.setItem(STORE, JSON.stringify({past: past.slice(-40), future: future.slice(-40), side, open, tab, node: node3d}));
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
    toastBox.style.background = bad ? '#ff6b5e' : '#c6f135';
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
  function placeRect(box, r) {
    if (!r) { box.style.display = 'none'; return; }
    Object.assign(box.style, {display: 'block', left: r.left + 'px', top: r.top + 'px',
                              width: r.width + 'px', height: r.height + 'px'});
  }
  const rectOf = el => el && el.isConnected ? el.getBoundingClientRect() : null;
  function placeTag(r, text) {
    if (!r) { tagBox.style.display = 'none'; return; }
    tagBox.textContent = text + '  ' + Math.round(r.width) + ' × ' + Math.round(r.height);
    Object.assign(tagBox.style, {display: 'block', left: Math.max(4, r.left) + 'px',
                                 top: (r.top > 24 ? r.top - 22 : r.top + 3) + 'px'});
  }
  function tick() {
    if (!active) return;
    const c = C(), picked = node3d ? c.resolve(node3d) : null;
    placeRect(selBox, interact ? null : picked ? rect3d(picked) : rectOf(sel));
    let r = null, text = '';
    if (!interact && !drag && !drag3d) {
      if (hover3d && hover3d !== picked) { r = rect3d(hover3d); text = name3d(hover3d); }
      else if (!hover3d && hover && hover !== sel) { r = rectOf(hover); text = label(hover); }
    }
    placeRect(hoverBox, r);
    placeTag(r, text);
    requestAnimationFrame(tick);
  }

  // ------------------------------------------------------------ finding 3D objects under the mouse
  function inv(e) {
    const [a,b,c,d,f,g,h,i,j,k,l,m,n,o,p,q] = e;
    const s0=a*g-b*f, s1=a*h-c*f, s2=a*i-d*f, s3=b*h-c*g, s4=b*i-d*g, s5=c*i-d*h;
    const t0=j*o-k*n, t1=j*p-l*n, t2=j*q-m*n, t3=k*p-l*o, t4=k*q-m*o, t5=l*q-m*p;
    let det = s0*t5 - s1*t4 + s2*t3 + s3*t2 - s4*t1 + s5*t0;
    if (!det) return null;
    det = 1 / det;
    return [(g*t5-h*t4+i*t3)*det, (c*t4-b*t5-d*t3)*det, (o*s5-p*s4+q*s3)*det, (l*s4-k*s5-m*s3)*det,
            (h*t2-f*t5-i*t1)*det, (a*t5-c*t2+d*t1)*det, (p*s2-n*s5-q*s1)*det, (j*s5-l*s2+m*s1)*det,
            (f*t4-g*t2+i*t0)*det, (b*t2-a*t4-d*t0)*det, (n*s4-o*s2+q*s0)*det, (k*s2-j*s4-m*s0)*det,
            (g*t1-f*t3-h*t0)*det, (a*t3-b*t1+c*t0)*det, (o*s1-n*s3-p*s0)*det, (j*s3-k*s1+l*s0)*det];
  }
  function times(a, b) {
    const r = new Array(16);
    for (let c = 0; c < 4; c++) for (let w = 0; w < 4; w++)
      r[c*4+w] = a[w]*b[c*4] + a[w+4]*b[c*4+1] + a[w+8]*b[c*4+2] + a[w+12]*b[c*4+3];
    return r;
  }
  function pt(m, x, y, z) {
    const w = m[3]*x + m[7]*y + m[11]*z + m[15] || 1;
    return [(m[0]*x+m[4]*y+m[8]*z+m[12])/w, (m[1]*x+m[5]*y+m[9]*z+m[13])/w, (m[2]*x+m[6]*y+m[10]*z+m[14])/w];
  }
  const dirOf = (m, x, y, z) => [m[0]*x+m[4]*y+m[8]*z, m[1]*x+m[5]*y+m[9]*z, m[2]*x+m[6]*y+m[10]*z];
  const dot = (a, b) => a[0]*b[0] + a[1]*b[1] + a[2]*b[2];
  const posCache = new WeakMap(), boundCache = new WeakMap();
  function positions(g) {
    const p = g.attributes && g.attributes.position;
    if (!p) return null;
    if (p.array instanceof Float32Array && !p.isInterleavedBufferAttribute && p.itemSize === 3 && !p.normalized) return p.array;
    let c = posCache.get(p);
    if (c && c.length === p.count * 3) return c;
    c = new Float32Array(p.count * 3);
    for (let i = 0; i < p.count; i++) { c[i*3] = p.getX(i); c[i*3+1] = p.getY(i); c[i*3+2] = p.getZ(i); }
    posCache.set(p, c);
    return c;
  }
  function bounds(g) {
    let b = boundCache.get(g);
    if (b) return b;
    let min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
    if (g.boundingBox && isFinite(g.boundingBox.min.x)) { min = g.boundingBox.min.toArray(); max = g.boundingBox.max.toArray(); }
    else {
      const a = positions(g);
      if (!a) return null;
      for (let i = 0; i < a.length; i += 3) for (let k = 0; k < 3; k++) {
        if (a[i+k] < min[k]) min[k] = a[i+k];
        if (a[i+k] > max[k]) max[k] = a[i+k];
      }
    }
    if (!isFinite(min[0] + max[0] + min[1] + max[1] + min[2] + max[2])) return null;
    const c = [0, 1, 2].map(k => (min[k] + max[k]) / 2);
    const r = Math.hypot(max[0]-c[0], max[1]-c[1], max[2]-c[2]);
    b = {min, max, c, r};
    if (!g.isInstancedBufferGeometry) boundCache.set(g, b);  // instanced text changes its box
    return b;
  }
  function hitSphere(o, d, b) {
    const f = [o[0]-b.c[0], o[1]-b.c[1], o[2]-b.c[2]], A = dot(d, d), B = dot(f, d), D = B*B - A*(dot(f, f) - b.r*b.r);
    if (D < 0 || !A) return null;
    const far = (-B + Math.sqrt(D)) / A;
    return far < 0 ? null : Math.max(0, (-B - Math.sqrt(D)) / A);
  }
  function hitBox(o, d, b) {
    let lo = 0, hi = Infinity;
    for (let k = 0; k < 3; k++) {
      if (Math.abs(d[k]) < 1e-12) { if (o[k] < b.min[k] || o[k] > b.max[k]) return null; continue; }
      let p = (b.min[k]-o[k])/d[k], q = (b.max[k]-o[k])/d[k];
      if (p > q) [p, q] = [q, p];
      lo = Math.max(lo, p); hi = Math.min(hi, q);
      if (lo > hi) return null;
    }
    return lo;
  }
  function hitTriangles(g, o, d) {
    const a = positions(g), idx = g.index && g.index.array;
    const total = idx ? idx.length : a.length / 3, dr = g.drawRange || {start: 0, count: Infinity};
    const start = dr.start, end = Math.min(total, start + dr.count);
    let best = null;
    for (let n = start; n + 2 < end; n += 3) {
      const i = (idx ? idx[n] : n) * 3, j = (idx ? idx[n+1] : n+1) * 3, k = (idx ? idx[n+2] : n+2) * 3;
      const ex = a[j]-a[i], ey = a[j+1]-a[i+1], ez = a[j+2]-a[i+2], fx = a[k]-a[i], fy = a[k+1]-a[i+1], fz = a[k+2]-a[i+2];
      const px = d[1]*fz-d[2]*fy, py = d[2]*fx-d[0]*fz, pz = d[0]*fy-d[1]*fx, det = ex*px+ey*py+ez*pz;
      if (Math.abs(det) < 1e-14) continue;
      const tx = o[0]-a[i], ty = o[1]-a[i+1], tz = o[2]-a[i+2], u = (tx*px+ty*py+tz*pz)/det;
      if (u < 0 || u > 1) continue;
      const qx = ty*ez-tz*ey, qy = tz*ex-tx*ez, qz = tx*ey-ty*ex, v = (d[0]*qx+d[1]*qy+d[2]*qz)/det;
      if (v < 0 || u + v > 1) continue;
      const t = (fx*qx+fy*qy+fz*qz)/det;
      if (t >= 0 && (best === null || t < best)) best = t;
    }
    return best;
  }
  function worldMatrices(obj, limit) {
    const mw = obj.matrixWorld.elements;
    if (!obj.isInstancedMesh) return [mw];
    const out = [], a = obj.instanceMatrix.array, n = Math.min(obj.count, limit);
    for (let i = 0; i < n; i++) out.push(times(mw, a.subarray(i*16, i*16+16)));
    return out;
  }
  const sceneOf = obj => { while (obj.parent) obj = obj.parent; return obj; };
  const viewOf = obj => C().views().get(sceneOf(obj));
  function makeRay(view, x, y) {
    const r = view.renderer.domElement.getBoundingClientRect(), cam = view.camera;
    if (!r.width || x < r.left || x > r.right || y < r.top || y > r.bottom) return null;
    const nx = (x - r.left) / r.width * 2 - 1, ny = 1 - (y - r.top) / r.height * 2;
    const pi = cam.projectionMatrixInverse ? cam.projectionMatrixInverse.elements : inv(cam.projectionMatrix.elements);
    const w = cam.matrixWorld.elements, o = pt(w, ...pt(pi, nx, ny, -1)), f = pt(w, ...pt(pi, nx, ny, 0.9));
    return {o, d: [f[0]-o[0], f[1]-o[1], f[2]-o[2]]};
  }
  function rect3d(obj) {
    const view = viewOf(obj);
    if (!view || obj.isCamera) return null;
    const cam = view.camera, vm = cam.matrixWorldInverse.elements, pm = cam.projectionMatrix.elements;
    const box = view.renderer.domElement.getBoundingClientRect();
    let l = Infinity, t = Infinity, r = -Infinity, b = -Infinity, used = 0;
    const add = (x, y, z, m) => {
      const v = pt(vm, ...pt(m, x, y, z));
      if (cam.isPerspectiveCamera && v[2] >= 0) return;
      const n = pt(pm, ...v), sx = box.left + (n[0]+1)/2*box.width, sy = box.top + (1-n[1])/2*box.height;
      l = Math.min(l, sx); r = Math.max(r, sx); t = Math.min(t, sy); b = Math.max(b, sy);
    };
    const walk = o => {
      if (o.visible === false || used >= 300) return;
      const g = o.geometry, q = g && bounds(g);
      if (q) {
        used++;
        for (const m of worldMatrices(o, 64)) for (let c = 0; c < 8; c++)
          add(c&1 ? q.max[0] : q.min[0], c&2 ? q.max[1] : q.min[1], c&4 ? q.max[2] : q.min[2], m);
      }
      (o.children || []).forEach(walk);
    };
    walk(obj);
    if (!used) add(0, 0, 0, obj.matrixWorld.elements);
    if (l === Infinity) return null;
    if (r - l < 12) { l -= 6; r += 6; }
    if (b - t < 12) { t -= 6; b += 6; }
    l = Math.max(l, box.left); t = Math.max(t, box.top); r = Math.min(r, box.right); b = Math.min(b, box.bottom);
    return r > l && b > t ? {left: l, top: t, width: r-l, height: b-t} : null;
  }
  function testObject(obj, ray, view, hits, st) {
    const mats = obj.material ? (Array.isArray(obj.material) ? obj.material : [obj.material]) : [];
    if (mats.length && mats.every(m => m.visible === false || m.colorWrite === false || (m.transparent && m.opacity <= 0.01))) return;
    const g = obj.geometry, b = g && bounds(g);
    if (!b) return;
    let best = null, soft = false;
    for (const m of worldMatrices(obj, 3000)) {
      const im = inv(m);
      if (!im) continue;
      const o = pt(im, ...ray.o), d = dirOf(im, ...ray.d), near = hitSphere(o, d, b);
      if (near === null) continue;
      let t = near, loose = false;
      if (obj.isMesh && g.isInstancedBufferGeometry) { t = hitBox(o, d, b); loose = typeof obj.text !== 'string'; }
      else if (obj.isMesh && !obj.isSkinnedMesh && !(g.morphAttributes && g.morphAttributes.position)) {
        const count = (g.index ? g.index.count : g.attributes.position.count) / 3;
        if (st.budget >= count) { st.budget -= count; t = hitTriangles(g, o, d); } else loose = true;
      } else if (!obj.isSprite) loose = true;
      if (t !== null && (best === null || t < best)) { best = t; soft = loose; }
    }
    if (best === null) return;
    const verts = g.attributes.position ? g.attributes.position.count : 0;
    if (verts && verts <= 6) {  // post-processing quads should not win a click
      const r = rect3d(obj), c = view.renderer.domElement.getBoundingClientRect();
      if ((r && r.width * r.height >= c.width * c.height * 0.9) || (mats[0] && mats[0].isShaderMaterial && obj.frustumCulled === false)) soft = true;
    }
    hits.push({obj, t: best, soft});
  }
  function pick3d(x, y, stack) {
    const c = C(), hits = [], st = {budget: 3e6, seen: 0};
    if (!c.views().size) return hits;
    const all = [...c.views()], latest = Math.max(...all.map(v => v[1].at));
    for (const [scene, view] of all) {
      if (view.at < latest - 1500 || !stack.includes(view.renderer.domElement)) continue;
      const ray = makeRay(view, x, y);
      if (!ray) continue;
      const walk = n => {
        if (n.visible === false || st.seen++ > 30000) return;
        if ((n.isMesh || n.isSprite || n.isPoints || n.isLine) && (!n.layers || !view.camera.layers || n.layers.test(view.camera.layers)))
          testObject(n, ray, view, hits, st);
        (n.children || []).forEach(walk);
      };
      walk(scene);
    }
    return hits.sort((a, b) => a.soft - b.soft || a.t - b.t);
  }
  function hasContent(el) {
    if (el === document.body || el === document.documentElement) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.opacity === '0') return false;
    if (/^(img|video|svg|input|button|textarea|select)$/.test(el.localName)) return true;
    if ([...el.childNodes].some(n => n.nodeType === 3 && n.nodeValue.trim())) return true;
    const bg = /rgba?\(([^)]+)\)/.exec(cs.backgroundColor), parts = bg ? bg[1].split(/[, /]+/) : [];
    return /url\(/.test(cs.backgroundImage) || (parts.length > 3 ? parseFloat(parts[3]) > 0.9 : parts.length === 3);
  }
  function lookAt(x, y) {
    const clean = list => list.filter(n => !isUi(n) && n !== document.documentElement);
    const under = clean(document.elementsFromPoint(x, y));
    // 3D canvases usually ignore the mouse, so let them show up in the stack for this one lookup.
    const canvases = [...new Set([...C().views().values()].map(v => v.renderer.domElement))].filter(n => n.isConnected);
    const styles = canvases.map(n => [n.style.getPropertyValue('pointer-events'), n.style.getPropertyPriority('pointer-events')]);
    canvases.forEach(n => n.style.setProperty('pointer-events', 'auto', 'important'));
    const stack = clean(document.elementsFromPoint(x, y));
    canvases.forEach((n, i) => {
      if (styles[i][0]) n.style.setProperty('pointer-events', styles[i][0], styles[i][1]);
      else n.style.removeProperty('pointer-events');
    });
    const hits = pick3d(x, y, stack);
    let use3d = false;
    if (hits.length) {
      const at = stack.findIndex(n => canvases.includes(n));
      use3d = at >= 0 && !stack.slice(0, at).some(hasContent);
    }
    return {under, hits, use3d};
  }
  const name3d = o => o.name || (typeof o.text === 'string' && o.text.trim() ? '"' + o.text.trim().slice(0, 24) + '"' : o.type || 'Object');
  function openTo(path) {
    const parts = path.split('/');
    for (let i = 1; i < parts.length; i++) {
      const p = parts.slice(0, i).join('/');
      if (i === 1) open3d.delete(p); else open3d.add(p);
    }
  }
  function select3d(obj, hits) {
    const path = C().pathOf(obj);
    if (!path) return;
    node3d = path; sel = null; tab = '3d'; hits3d = hits || []; filterText = '';
    openTo(path);
    reveal = true;
    remember(active);
    draw();
  }
  function rayOnPlane(ray, p, n) {
    const q = dot(ray.d, n);
    if (Math.abs(q) < 1e-12) return null;
    const t = ((p[0]-ray.o[0])*n[0] + (p[1]-ray.o[1])*n[1] + (p[2]-ray.o[2])*n[2]) / q;
    return [ray.o[0]+ray.d[0]*t, ray.o[1]+ray.d[1]*t, ray.o[2]+ray.d[2]*t];
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
  function select(el, hits) {
    sel = el;
    node3d = null; hits3d = hits || []; hover3d = null;
    tab = 'page'; filterText = '';
    for (let n = el.parentElement; n; n = n.parentElement) domOpen.add(n);
    reveal = true;
    remember(active);
    draw();
  }

  function pageTab() {
    const out = [field('Page title', h('input', {value: document.title, 'data-k': 'title', onchange: e =>
      commit({kind: 'title', value: e.target.value}, o => o.kind === 'title' && o.page === C().page())}))];
    if (!sel || !sel.isConnected) {
      out.push(h('p', {class: 'hint'}, 'Click anything on the page to inspect it, also things in the 3D view. Drag what you selected to move it.'));
      return out;
    }
    const own = [...sel.childNodes].filter(n => n.nodeType === 3 && n.nodeValue.trim()).map(n => n.nodeValue.trim())[0];
    const text = sel.children.length ? own : sel.textContent;
    out.push(h('div', {class: 'crumb'}, h('b', {}, label(sel)),
      sel.parentElement && sel !== document.body ? h('button', {onclick: () => select(sel.parentElement)}, '↑ Parent') : null));
    if (under.length > 1 || hits3d.length) out.push(h('div', {class: 'chips'},
      hits3d.slice(0, 4).map(hit => h('button', {onclick: () => select3d(hit.obj, hits3d)}, '3D: ' + name3d(hit.obj).slice(0, 18))),
      under.slice(0, 7).map(el => h('button', {class: el === sel ? 'on' : '', onclick: () => select(el, hits3d)}, label(el).slice(0, 22)))));
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

  const opOf = path => C().ops.find(o => o.kind === '3d' && o.path === path && o.page === C().page());
  function vector(title, key, v, path) {
    return field(title, h('div', {class: 'three'}, v[key].map((n, i) => h('input', {type: 'number', step: 'any', value: n,
      'data-k': key + i, onchange: e => {
        const next = v[key].slice();
        next[i] = parseFloat(e.target.value) || 0;
        edit3d(path, {[key]: next});
      }}))));
  }
  function edit3d(path, patch) {
    const old = opOf(path);
    commit({kind: '3d', path, value: {...(old ? old.value : {}), ...patch}},
           o => o.kind === '3d' && o.path === path && o.page === C().page());
    setTimeout(draw, 80);
  }
  function live3d(path, patch) {
    const old = opOf(path);
    C().preview({kind: '3d', path, page: C().page(), value: {...(old ? old.value : {}), ...patch}});
  }
  const nested = (path, key, name, value) => ({...((opOf(path) || {value: {}}).value[key] || {}), [name]: value});
  async function saveText(from, to) {
    if (from === to) return;
    try {
      const r = await fetch('/__replicator/text', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                   body: JSON.stringify({from, to})});
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || 'Could not save');
      if (data.missing && data.missing.length) alert('This 3D font has no letter for: ' + data.missing.join(' '));
      remember(true);
      location.reload();
    } catch (err) { toast(err.message, true); }
  }
  function textRow(s) {
    const T = window.__replicator_text;
    return h('input', {value: T.apply(s), title: s, 'data-text': s, onchange: e => saveText(s, e.target.value)});
  }
  function textureRow(path, t) {
    const name = t.slot.replace(/^u:/, '');
    const thumb = h('canvas', {class: 'thumb', width: 44, height: 44});
    if (t.image) try { thumb.getContext('2d').drawImage(t.image, 0, 0, 44, 44); } catch (_) {}
    const control = t.swap ? filePicker(data => edit3d(path, {textures: nested(path, 'textures', t.slot, data)}))
      : h('span', {class: 'hint'}, 'Packed or generated. Use Replace Images in the Replicator menu.');
    return h('div', {class: 'tex'}, thumb, h('div', {class: 'f'}, h('span', {}, name), control));
  }
  function uniformRow(path, u) {
    const put = value => edit3d(path, {uniforms: nested(path, 'uniforms', u.name, value)});
    const peek = value => live3d(path, {uniforms: nested(path, 'uniforms', u.name, value)});
    if (u.type === 'color') return field(u.name, h('input', {type: 'color', value: u.value,
      oninput: e => peek(e.target.value), onchange: e => put(e.target.value)}));
    if (u.type === 'number') return field(u.name, h('input', {type: 'number', step: 'any', value: u.value, 'data-k': 'u-' + u.name,
      oninput: e => { if (e.target.value !== '') peek(parseFloat(e.target.value)); },
      onchange: e => put(parseFloat(e.target.value) || 0)}));
    return field(u.name, h('div', {class: 'three'}, u.value.map((n, i) => h('input', {type: 'number', step: 'any', value: n, onchange: e => {
      const next = u.value.slice();
      next[i] = parseFloat(e.target.value) || 0;
      put(next);
    }}))));
  }
  function sliderRow(title, key, value, path) {
    return field(title, h('input', {type: 'range', min: 0, max: 1, step: 0.01, value, 'data-k': key,
      oninput: e => live3d(path, {[key]: parseFloat(e.target.value)}),
      onchange: e => edit3d(path, {[key]: parseFloat(e.target.value)})}));
  }
  function sceneTab() {
    const c = C();
    if (!c.tree().length) return [h('p', {class: 'hint'}, 'No Three.js scene has rendered yet. Other 3D engines cannot be inspected here, but their text is listed under Edit Text.')];
    const out = [], v = node3d && c.read3d(node3d);
    if (!v) out.push(h('p', {class: 'hint'}, 'Click something in the 3D view, or pick it in the list above.'));
    if (v) {
      const obj = c.resolve(node3d), parent = obj.parent;
      out.push(h('div', {class: 'crumb'}, h('b', {}, name3d(obj)), h('span', {class: 'btns'},
        parent ? h('button', {onclick: () => select3d(parent, hits3d)}, '↑ Parent') : null,
        h('button', {onclick: () => c.flash(node3d)}, 'Find'))));
      out.push(h('p', {class: 'hint'}, [v.type, v.material, v.vertices ? v.vertices + ' vertices' : null].filter(Boolean).join('  ·  ')));
      if (hits3d.length > 1 || under.length) out.push(h('div', {class: 'chips'},
        hits3d.slice(0, 7).map(hit => h('button', {class: hit.obj === obj ? 'on' : '', onclick: () => select3d(hit.obj, hits3d)}, name3d(hit.obj).slice(0, 22))),
        under[0] ? h('button', {onclick: () => select(under[0])}, 'HTML: ' + label(under[0]).slice(0, 16)) : null));
      const words = c.texts3d(node3d);
      if (words.own !== null) out.push(field('Text', h('textarea', {value: (opOf(node3d) || {value: {}}).value.text ?? words.own, rows: 2, 'data-k': 'text3d',
        onchange: e => edit3d(node3d, {text: e.target.value})})));
      if (words.drawn.length) out.push(h('div', {class: 'f'}, h('span', {}, 'Text on this object'), words.drawn.slice(0, 30).map(textRow)));
      out.push(h('label', {class: 'check'}, h('input', {type: 'checkbox', checked: v.visible, 'data-k': 'visible',
        onchange: e => edit3d(node3d, {visible: e.target.checked})}), 'Visible'));
      out.push(vector('Position', 'position', v, node3d), vector('Rotation°', 'rotation', v, node3d));
      if (node3d !== 'camera') out.push(vector('Scale', 'scale', v, node3d));
      if (v.fov !== undefined) out.push(field('Field of view', h('input', {type: 'number', value: v.fov,
        onchange: e => edit3d(node3d, {fov: parseFloat(e.target.value) || v.fov})})));
      for (const [key, title] of [['color', 'Color'], ['emissive', 'Glow']]) if (v[key])
        out.push(field(title, h('input', {type: 'color', value: v[key], 'data-k': key === 'color' ? 'color3d' : key,
          oninput: e => live3d(node3d, {[key]: e.target.value}), onchange: e => edit3d(node3d, {[key]: e.target.value})})));
      if (v.opacity !== undefined) out.push(sliderRow('Opacity', 'opacity', v.opacity, node3d));
      if (v.roughness !== undefined) out.push(sliderRow('Roughness', 'roughness', v.roughness, node3d));
      if (v.metalness !== undefined) out.push(sliderRow('Metalness', 'metalness', v.metalness, node3d));
      if (v.wireframe !== undefined) out.push(h('label', {class: 'check'}, h('input', {type: 'checkbox', checked: v.wireframe,
        onchange: e => edit3d(node3d, {wireframe: e.target.checked})}), 'Wireframe'));
      const tex = c.textures3d(node3d);
      if (tex.length) out.push(h('h4', {}, 'Textures'), tex.map(t => textureRow(node3d, t)));
      if (v.uniforms.length) out.push(h('h4', {}, 'Shader values'), v.uniforms.map(u => uniformRow(node3d, u)));
    }
    const T = window.__replicator_text;
    if (T) {
      const q = filterText.toLowerCase();
      const all = [...new Set([...T.scene(), ...T.drawn()])].filter(s => !q || s.toLowerCase().includes(q));
      if (all.length) out.push(h('h4', {}, 'All text in WebGL (' + all.length + ')'), all.slice(0, 60).map(textRow));
    }
    return out;
  }

  // ------------------------------------------------------------ the tree, like the Elements panel
  function row(o) {
    return h('button', {class: o.on ? 'on' : '', style: 'padding-left:' + (4 + Math.min(o.depth, 12) * 11) + 'px',
                        'data-path': o.path, onclick: o.pick, onmouseenter: o.enter, onmouseleave: o.leave},
      h('i', {class: 'car', onclick: e => { e.stopPropagation(); if (o.toggle) o.toggle(); }}, o.kids ? (o.open ? '▾' : '▸') : ''),
      h('span', {class: 'name'}, o.text), o.note ? h('span', {class: 'dim'}, o.note) : null, o.end || null);
  }
  const skipEl = el => isUi(el) || /^(script|style|noscript|template|link|meta)$/.test(el.localName);
  const kidsOf = el => [...el.children].filter(n => !skipEl(n));
  const ownText = el => [...el.childNodes].filter(n => n.nodeType === 3 && n.nodeValue.trim()).map(n => n.nodeValue.trim()).join(' ');
  function domRow(el, depth) {
    const kids = kidsOf(el).length, isOpen = domOpen.has(el);
    return row({depth, kids, open: isOpen, on: el === sel, text: label(el), note: ownText(el).slice(0, 40),
      pick: () => { under = []; select(el); },
      enter: () => { hover = el; hover3d = null; }, leave: () => { hover = null; },
      toggle: () => { isOpen ? domOpen.delete(el) : domOpen.add(el); draw(); }});
  }
  function domRows() {
    const out = [], q = filterText.toLowerCase();
    if (q) {
      for (const el of document.body.querySelectorAll('*')) {
        if (out.length >= 300) break;
        if (!skipEl(el) && !el.closest('script,style') && (label(el) + ' ' + ownText(el)).toLowerCase().includes(q)) out.push(domRow(el, 0));
      }
      return out;
    }
    const walk = (el, depth) => {
      if (out.length >= 900) return;
      out.push(domRow(el, depth));
      if (domOpen.has(el)) kidsOf(el).forEach(k => walk(k, depth + 1));
    };
    walk(document.body, 0);
    return out;
  }
  function sceneRows() {
    const c = C(), q = filterText.toLowerCase(), out = [];
    let hide = Infinity;
    for (const n of c.tree()) {
      if (out.length >= 900) break;
      const text = n.label !== n.type ? n.type : '';
      if (q) { if (!(n.label + ' ' + n.type).toLowerCase().includes(q)) continue; }
      else {
        if (n.depth > hide) continue;
        hide = Infinity;
      }
      const isOpen = open3d.has(n.path) !== (n.depth === 0);
      if (!q && n.kids && !isOpen) hide = n.depth;
      out.push(row({depth: q ? 0 : n.depth, kids: !q && n.kids, open: isOpen, on: n.path === node3d, path: n.path, text: n.label, note: text,
        pick: () => { node3d = n.path; sel = null; hits3d = []; remember(active); draw(); },
        enter: () => { hover3d = c.resolve(n.path); hover = null; }, leave: () => { hover3d = null; },
        toggle: () => { open3d.has(n.path) ? open3d.delete(n.path) : open3d.add(n.path); draw(); },
        end: n.path === 'camera' ? null : h('i', {class: 'eye' + (n.visible ? '' : ' off'), title: 'Show or hide', onclick: e => {
          e.stopPropagation();
          edit3d(n.path, {visible: !n.visible});
        }}, n.visible ? '●' : '○')}));
    }
    return out;
  }

  function draw() {
    if (!active) return;
    panel.className = 'panel ' + side;
    const scroll = body.scrollTop, focused = root.activeElement && root.activeElement.dataset.k;
    root.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
    root.querySelector('[data-k=interact]').textContent = interact ? 'Use page: on' : 'Use page: off';
    search.placeholder = tab === 'page' ? 'Search tags and text...' : 'Search 3D objects and text...';
    if (search.value !== filterText) search.value = filterText;
    const keep = list.scrollTop;
    list.replaceChildren(...(tab === 'page' ? domRows() : sceneRows()));
    list.scrollTop = keep;
    body.replaceChildren(...(tab === 'page' ? pageTab() : sceneTab()).flat());
    body.scrollTop = scroll;
    if (focused) { const n = body.querySelector('[data-k="' + focused + '"]'); if (n) n.focus(); }
    if (reveal) {
      reveal = false;
      const on = list.querySelector('button.on');
      if (on) list.scrollTop = on.offsetTop - list.clientHeight / 2;
    }
    if (tab === '3d' && !C().tree().length && !draw.wait)
      draw.wait = setTimeout(() => { draw.wait = 0; draw(); }, 700);
  }
  function build() {
    host = h('div', {id: '__replicator_editor_host'});
    host.style.all = 'initial';
    document.documentElement.append(host);
    root = host.attachShadow({mode: 'open'});
    root.append(h('style', {}, `
      *{box-sizing:border-box;font:12px Segoe UI,system-ui,sans-serif}
      .panel{position:fixed;top:0;bottom:0;width:360px;z-index:2147483646;background:#0b0d12;color:#e8ebf0;display:flex;flex-direction:column;box-shadow:0 0 24px #0008}
      .right{right:0}.left{left:0}.bar{display:flex;gap:5px;padding:8px;border-bottom:1px solid #2a303c;flex-wrap:wrap}
      .pane{flex:none;height:36vh;min-height:90px;max-height:75vh;resize:vertical;overflow:hidden;display:flex;flex-direction:column;gap:5px;padding:8px;border-bottom:1px solid #2a303c;background:#12151c}
      .body{flex:1;min-height:0;overflow:auto;padding:10px;display:flex;flex-direction:column;gap:8px}
      button{background:#1a1e27;color:#e8ebf0;border:1px solid #2a303c;border-radius:6px;padding:6px 8px;cursor:pointer}
      button:hover{background:#2a303c}button.on{background:#c6f135;border-color:#c6f135;color:#0b0d12}.bad{background:#3a1b19;border-color:#3a1b19;color:#ff6b5e}
      input,textarea,select{width:100%;min-width:0;background:#1a1e27;color:#e8ebf0;border:1px solid #3a4250;border-radius:5px;padding:5px}
      input[type=color]{padding:1px;height:27px}input[type=checkbox]{width:auto}input[type=range]{padding:0;accent-color:#c6f135}textarea{resize:vertical;font-family:Consolas,monospace}
      .f{display:grid;gap:3px}.f>span,.hint{color:#8b94a5}.hint{margin:0}.two{display:grid;grid-template-columns:44px 1fr;gap:5px}.two:has(>button){grid-template-columns:1fr auto}
      .three{display:grid;grid-template-columns:repeat(3,1fr);gap:4px}.row{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:5px}
      .crumb{display:flex;justify-content:space-between;align-items:center;gap:8px}.crumb b{overflow-wrap:anywhere;color:#c6f135}.btns{display:flex;gap:4px;flex:none}
      .chips{display:flex;flex-wrap:wrap;gap:4px}.chips button{padding:3px 6px;font-size:11px}h4{margin:6px 0 0;color:#e8ebf0;font-weight:700}
      .check{display:flex;gap:6px;align-items:center}.wide{width:100%}.tree{position:relative;flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column}
      .tree button{flex:none;display:flex;align-items:center;gap:4px;width:100%;text-align:left;border:0;border-radius:3px;background:transparent;padding-top:2px;padding-bottom:2px;padding-right:4px;white-space:nowrap;overflow:hidden}
      .tree button:hover{background:#242a35}.tree button.on{background:#c6f135;color:#0b0d12}.tree button.on .dim,.tree button.on .car,.tree button.on .eye{color:#28330f}
      .car{flex:none;width:12px;font-style:normal;color:#8b94a5;text-align:center}.name{flex:none;max-width:70%;overflow:hidden;text-overflow:ellipsis}
      .dim{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;color:#6e7788;font-size:11px}.eye{margin-left:auto;flex:none;font-style:normal;color:#c6f135;padding:0 3px}.eye.off{color:#566072}
      .tex{display:grid;grid-template-columns:44px 1fr;gap:7px;align-items:center}.thumb{width:44px;height:44px;border-radius:4px;background:#1a1e27}
      .box{position:fixed;pointer-events:none;z-index:2147483645;display:none}
      .hover{outline:1px dashed #c6f135;background:#c6f1350d}.sel{outline:2px solid #c6f135;background:#c6f13514}
      .tag{position:fixed;pointer-events:none;z-index:2147483645;display:none;background:#c6f135;color:#0b0d12;padding:2px 6px;border-radius:3px;font-size:11px;white-space:nowrap}
      .toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:2147483647;color:#0b0d12;padding:8px 14px;border-radius:7px;display:none}`));
    hoverBox = h('div', {class: 'box hover'});
    selBox = h('div', {class: 'box sel'});
    tagBox = h('div', {class: 'tag'});
    toastBox = h('div', {class: 'toast'});
    body = h('div', {class: 'body'});
    list = h('div', {class: 'tree'});
    search = h('input', {'data-k': 'search', oninput: e => { filterText = e.target.value; draw(); }});
    panel = h('div', {class: 'panel ' + side},
      h('div', {class: 'bar'},
        h('button', {'data-tab': 'page', onclick: () => { tab = 'page'; filterText = ''; reveal = true; draw(); }}, 'Page'),
        h('button', {'data-tab': '3d', onclick: () => { tab = '3d'; filterText = ''; reveal = true; draw(); }}, '3D'),
        h('button', {'data-k': 'interact', onclick: () => { interact = !interact; draw(); }}, ''),
        h('button', {onclick: () => { side = side === 'right' ? 'left' : 'right'; remember(true); draw(); }}, '⇄'),
        h('button', {'data-k': 'undo', onclick: () => jump(past, future)}, 'Undo'),
        h('button', {'data-k': 'redo', onclick: () => jump(future, past)}, 'Redo'),
        h('button', {class: 'bad', onclick: async () => {
          if (!confirm('Remove all edits made with this editor on every page?')) return;
          past.push(C().ops); future = []; C().setOps([]); await persist(); location.reload();
        }}, 'Reset'),
        h('button', {'data-k': 'close', onclick: close}, '✕')),
      h('div', {class: 'pane'}, search, list),
      body);
    root.append(hoverBox, selBox, tagBox, panel, toastBox);
  }
  function setPill(display) {  // the editor panel would otherwise sit underneath it
    const pill = document.getElementById('__replicator_ui_host');
    if (pill) pill.style.display = display;
  }
  function open() {
    if (!host) build();
    host.style.display = '';
    setPill('none');
    active = true;
    remember(true);
    draw();
    requestAnimationFrame(tick);
  }
  function close() {
    active = false;
    host.style.display = 'none';
    C().preview(null);
    setPill('');
    remember(false);
  }

  const blocked = e => active && !interact && !e.composedPath().includes(host) && !isUi(e.composedPath()[0]) &&
                       !e.composedPath().some(n => n && n.id === '__replicator_ui_host');
  const stop = e => { e.preventDefault(); e.stopImmediatePropagation(); };
  let mouseX = 0, mouseY = 0, hoverQueued = false;
  function queueHover() {
    if (hoverQueued) return;
    hoverQueued = true;
    setTimeout(() => {
      hoverQueued = false;
      if (!active || interact || drag || drag3d) return;
      const seen = lookAt(mouseX, mouseY);
      hover3d = seen.use3d ? seen.hits[0].obj : null;
      hover = seen.under[0] || null;
    }, 45);
  }
  function moveDrag3d(e) {
    const d = drag3d, dx = e.clientX - d.startX, dy = e.clientY - d.startY;
    if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true;
    if (!d.moved) return;
    if (e.shiftKey) {
      d.patch = {rotation: [d.rotation[0] + dy * 0.5, d.rotation[1] + dx * 0.5, d.rotation[2]].map(n => Math.round(n * 10) / 10)};
    } else {
      const ray = makeRay(d.view, e.clientX, e.clientY), at = ray && rayOnPlane(ray, d.point, d.normal);
      if (!at) return;
      const world = [at[0]-d.start[0], at[1]-d.start[1], at[2]-d.start[2]];
      const local = d.parent ? dirOf(d.parent, ...world) : world;
      d.patch = {position: d.position.map((n, i) => Math.round((n + local[i]) * 10000) / 10000)};
    }
    live3d(d.path, d.patch);
  }
  function startDrag3d(e, seen) {
    if (!node3d || node3d === 'camera' || !seen.use3d) return false;
    const c = C(), obj = c.resolve(node3d);
    if (!obj) return false;
    const inside = o => { for (; o; o = o.parent) if (o === obj) return true; return false; };
    if (!seen.hits.some(hit => inside(hit.obj))) return false;
    const view = viewOf(obj), v = c.read3d(node3d);
    if (!view) return false;
    const m = obj.matrixWorld.elements, cw = view.camera.matrixWorld.elements, point = [m[12], m[13], m[14]];
    const normal = [-cw[8], -cw[9], -cw[10]], ray = makeRay(view, e.clientX, e.clientY), start = ray && rayOnPlane(ray, point, normal);
    if (!start) return false;
    drag3d = {path: node3d, view, point, normal, start, startX: e.clientX, startY: e.clientY, position: v.position, rotation: v.rotation,
              parent: obj.parent ? inv(obj.parent.matrixWorld.elements) : null};
    return true;
  }
  addEventListener('mousemove', e => {
    mouseX = e.clientX; mouseY = e.clientY;
    if (drag3d) { stop(e); moveDrag3d(e); }
    else if (drag) {
      stop(e);
      const dx = e.clientX - drag.startX, dy = e.clientY - drag.startY;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      if (drag.moved) {
        drag.x = drag.baseX + dx; drag.y = drag.baseY + dy;
        drag.el.style.setProperty('translate', drag.x + 'px ' + drag.y + 'px', 'important');
      }
    } else if (blocked(e)) queueHover();
  }, true);
  // Only hide these from the page: cancelling pointerdown would also cancel mousedown.
  for (const type of ['pointerdown', 'pointerup', 'pointermove', 'touchstart', 'touchend'])
    addEventListener(type, e => { if (blocked(e)) e.stopImmediatePropagation(); }, {capture: true, passive: true});
  for (const type of ['dblclick', 'contextmenu'])
    addEventListener(type, e => { if (blocked(e)) stop(e); }, true);
  addEventListener('mousedown', e => {
    if (!blocked(e)) return;
    stop(e);
    const seen = lookAt(e.clientX, e.clientY);
    if (startDrag3d(e, seen)) return;
    const t = seen.under[0];
    if (sel && t && (t === sel || sel.contains(t)) && sel !== document.body) {
      const s = selector(sel), old = C().ops.find(o => o.kind === 'move' && o.sel === s && o.page === C().page());
      drag = {el: sel, sel: s, startX: e.clientX, startY: e.clientY, baseX: old ? old.value.x : 0, baseY: old ? old.value.y : 0};
    }
  }, true);
  addEventListener('mouseup', e => {
    if (drag3d) {
      stop(e);
      const d = drag3d;
      drag3d = null;
      C().preview(null);
      if (d.moved && d.patch) { edit3d(d.path, d.patch); dragEnded = Date.now(); }
    } else if (drag) {
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
    const seen = lookAt(e.clientX, e.clientY);
    under = seen.under;
    if (seen.use3d) select3d(seen.hits[0].obj, seen.hits);
    else if (under[0]) select(under[0], seen.hits);
  }, true);
  addEventListener('keydown', e => {
    if (e.altKey && (e.key === 'e' || e.key === 'E')) { e.preventDefault(); active ? close() : open(); }
    else if (active && e.key === 'Escape') { sel = null; node3d = null; hits3d = []; remember(true); draw(); }
  }, true);
  window.__replicator_editor = {open, close};
  if (saved.open) {
    if (document.readyState === 'loading') addEventListener('DOMContentLoaded', () => setTimeout(open, 200));
    else setTimeout(open, 200);
  }
})();
"""
