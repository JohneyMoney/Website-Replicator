"""Export an archive as ONE .html file that opens by double-click (no server, no internet).

Every captured GET response is embedded once as base64. A small boot script turns the
assets into blob: URLs on demand, rewrites the captured page to use them and answers
fetch/XHR from the embedded copies, so pages also work from file://.
"""
from __future__ import annotations

import base64
import html
import json
from pathlib import Path

from .control import control_tag_content
from .textedit import text_runtime_tag_content
from .util import guess_mime, header_value, human_size

MAX_EMBED_BYTES = 350 * 1024 * 1024  # base64 + JSON must stay below the browser's string limit
REDIRECTS = (301, 302, 303, 307, 308)


def _json(value) -> str:
    """JSON that is safe inside a <script> element."""
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def export_single_html(archive, out_path, log=print) -> Path:
    out_path = Path(out_path)
    conn = archive.connect()
    tmp = out_path.with_name(out_path.name + ".tmp")
    try:
        meta = archive.meta(conn)
        start = meta.get("start_url")
        if not start or archive.start_document(conn) is None:
            raise ValueError("This archive has no captured start page.")
        total = conn.execute("SELECT COALESCE(SUM(body_size), 0) n FROM responses "
                             "WHERE method='GET' AND status=200 AND body IS NOT NULL").fetchone()["n"]
        if total > MAX_EMBED_BYTES:
            raise ValueError(f"The site is too large for one HTML file ({human_size(total)}, limit "
                             f"{human_size(MAX_EMBED_BYTES)}). Use 'Export package (.zip)' instead.")

        redirects = []
        for r in conn.execute("SELECT url, resp_headers, status FROM responses WHERE method='GET' "
                              f"AND status IN {REDIRECTS} ORDER BY id"):
            target = header_value(json.loads(r["resp_headers"]), "location")
            if target:
                redirects.append([r["url"], target])

        title = html.escape(str(meta.get("title") or archive.name))
        overrides = archive.get_overrides()
        count = 0
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            f.write(f'<!doctype html>\n<html><head><meta charset="utf-8"><title>{title}</title>\n'
                    '<script id="__replicator_data" type="application/json">')
            f.write(f'{{"start":{_json(start)},"redirects":{_json(redirects)},'
                    f'"css":{_json(overrides["css"])},"js":{_json(overrides["js"])},"assets":[')
            for r in conn.execute("SELECT * FROM responses WHERE method='GET' AND status=200 "
                                  "AND body IS NOT NULL ORDER BY id"):
                if archive.is_url_blocked(r["url"]):
                    continue
                mime = r["mime"] or guess_mime(r["url"])
                data = archive.apply_text_rules(r, archive.body_for(r))
                body = base64.b64encode(data).decode("ascii")
                f.write(("," if count else "") + f'[{_json(r["url"])},{_json(mime)},"{body}"]')
                count += 1
            f.write("]}</script>\n<script>" + text_runtime_tag_content(archive.get_text_rules())
                    + "</script>\n<script>" + control_tag_content(archive.get_page_edits())
                    + "</script>\n<script>" + BOOT_JS + "</script>\n</head>"
                    "<body><p>Loading offline copy...</p></body></html>\n")
        tmp.replace(out_path)
    finally:
        conn.close()
        tmp.unlink(missing_ok=True)
    log(f"Single HTML saved: {out_path} ({count} files, {human_size(out_path.stat().st_size)})")
    return out_path


BOOT_JS = r"""
(() => {
'use strict';
const D = JSON.parse(document.getElementById('__replicator_data').textContent);
const START = D.start, ALL = new Map(), BY_PATH = new Map(), BY_PATHQ = new Map();
const REDIRECT = new Map(D.redirects), IMPORTS = {};
let BASE = START, PAGE_IMPORTS = {};

for (const [url, mime, b64] of D.assets) {
  const u = new URL(url), e = {url: u.href, mime, b64};
  ALL.set(url, e); ALL.set(u.href, e);
  BY_PATH.set(u.origin + u.pathname, e); BY_PATHQ.set(u.pathname + u.search, e);
}

const SKIP = /^(data:|blob:|javascript:|mailto:|tel:|about:|#)/i;
const isCss = e => /css/i.test(e.mime), isHtml = e => /html/i.test(e.mime);
const isJs = e => /javascript|ecmascript/i.test(e.mime) || /\.m?js$/i.test(new URL(e.url).pathname);

function abs(value, base = BASE) {
  try {
    const x = new URL(value, base);
    if (x.protocol === 'file:') {
      if (x.pathname === location.pathname) return BASE;
      return new URL(x.pathname + x.search, START).href;
    }
    x.hash = '';
    return x.href;
  } catch { return null; }
}
function find(value, base = BASE) {
  let a = abs(value, base);
  if (!a || !/^https?:/.test(a)) return null;
  for (let i = 0; i < 5 && REDIRECT.has(a); i++) a = abs(REDIRECT.get(a), a);
  const u = new URL(a);
  return ALL.get(a) || BY_PATH.get(u.origin + u.pathname) || BY_PATHQ.get(u.pathname + u.search) || null;
}
function bytes(e) {
  const s = atob(e.b64), out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}
function text(e) {
  const m = /charset=["']?([\w-]+)/i.exec(e.mime);
  try { return new TextDecoder(m ? m[1] : 'utf-8').decode(bytes(e)); }
  catch { return new TextDecoder().decode(bytes(e)); }
}
const blobUrl = (data, type) => URL.createObjectURL(new Blob([data], {type}));

// Page code that checks location.protocol/origin/... should see the original site, not file://.
const originalLocation = {};
for (const key of ['protocol', 'origin', 'host', 'hostname', 'port'])
  Object.defineProperty(originalLocation, key, {get: () => new URL(BASE)[key]});
globalThis.__replicatorLocation = originalLocation;
const LOCATION_READ = /(?<![\w$.])(?:(?:window|self|globalThis|document)\.)?location\.(protocol|origin|host|hostname|port)\b(?!\s*=(?!=))/g;
const patchJs = src => src.replace(LOCATION_READ, '(globalThis.__replicatorLocation||location).$1');

function assetUrl(e) {
  if (e.blob) return e.blob;
  if (isCss(e)) {
    if (e.busy) return e.url;
    e.busy = true;
    e.blob = blobUrl(css(text(e), e.url), 'text/css');
    e.busy = false;
  } else if (isJs(e)) {
    e.blob = blobUrl(patchJs(text(e)), 'text/javascript');
  } else {
    e.blob = blobUrl(bytes(e), e.mime);
  }
  return e.blob;
}
function map(value, base = BASE) {
  const v = String(value ?? '').trim();
  if (!v || SKIP.test(v)) return value;
  const e = find(v, base);
  if (!e) return value;
  const hash = v.indexOf('#');
  return assetUrl(e) + (hash >= 0 ? v.slice(hash) : '');
}
const mapSrcset = (v, base) => String(v).split(/,\s+/).map(part => {
  const [u, ...rest] = part.trim().split(/\s+/);
  return [map(u, base), ...rest].join(' ');
}).join(', ');
function css(src, base = BASE) {
  return src
    .replace(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^)'"\s]+))\s*\)/gi,
      (m, a, b, c) => `url("${map(a ?? b ?? c, base)}")`)
    .replace(/@import\s+(["'])([^"']+)\1/gi, (m, q, u) => `@import "${map(u, base)}"`);
}

// ES modules: specifiers become absolute URLs, and the import map points those at blobs.
function resolveSpecifier(s, base) {
  if (PAGE_IMPORTS[s]) return abs(PAGE_IMPORTS[s]);
  let best = '';
  for (const k of Object.keys(PAGE_IMPORTS))
    if (k.endsWith('/') && s.startsWith(k) && k.length > best.length) best = k;
  if (best) return abs(PAGE_IMPORTS[best] + s.slice(best.length));
  return /^(\.{0,2}\/|[a-z][a-z0-9+.-]*:)/i.test(s) ? abs(s, base) : null;
}
function moduleText(src, base) {
  return patchJs(src)
    .replace(/(\bfrom\s*|\bimport\s*\(?\s*)(["'`])([^"'`\n$]+)\2/g, (m, head, q, spec) => {
      const target = resolveSpecifier(spec, base), e = target && find(target);
      if (!e || !isJs(e)) return m;
      loadModule(e);
      return head + q + e.url + q;
    })
    .replace(/\bimport\.meta\.url\b/g, JSON.stringify(base));
}
function loadModule(e) {
  if (e.module) return;
  e.module = true;
  IMPORTS[e.url] = blobUrl(moduleText(text(e), e.url), 'text/javascript');
}

const DROP_REL = /\b(preload|modulepreload|prefetch|preconnect|dns-prefetch|manifest)\b/i;
function rewrite(el) {
  const tag = el.localName;
  if (tag === 'link') {
    if (DROP_REL.test(el.rel || el.getAttribute('rel') || '')) { el.remove(); return; }
    el.removeAttribute('integrity');
    const h = el.getAttribute('href');
    if (h) setIfChanged(el, 'href', map(h));
  }
  if (tag === 'script') {
    el.removeAttribute('integrity');
    if (el.type === 'module') return;
  }
  if (tag === 'style' && /url\(|@import/i.test(el.textContent)) {
    const out = css(el.textContent);
    if (out !== el.textContent) el.textContent = out;
  }
  for (const name of ['src', 'poster', ...(tag === 'object' ? ['data'] : [])]) {
    const v = el.getAttribute(name);
    if (v) setIfChanged(el, name, map(v));
  }
  if (tag === 'image') for (const name of ['href', 'xlink:href']) {
    const v = el.getAttribute(name);
    if (v) setIfChanged(el, name, map(v));
  }
  const set = el.getAttribute('srcset');
  if (set) setIfChanged(el, 'srcset', mapSrcset(set));
  const style = el.getAttribute('style');
  if (style && /url\(/i.test(style)) setIfChanged(el, 'style', css(style));
}
function setIfChanged(el, name, value) {
  if (el.getAttribute(name) !== value) nativeSetAttribute.call(el, name, value);
}

// Runtime hooks: requests the page makes later are answered from the embedded files.
const nativeSetAttribute = Element.prototype.setAttribute;
const nativeFetch = window.fetch;
window.fetch = function (input, init) {
  const req = input instanceof Request ? input : null;
  const url = req ? req.url : String(input?.href ?? input);
  const method = String(init?.method || req?.method || 'GET').toUpperCase();
  if (SKIP.test(url)) return nativeFetch.apply(this, arguments);
  const e = (method === 'GET' || method === 'HEAD') ? find(url) : null;
  if (e) return Promise.resolve(new Response(method === 'HEAD' ? null : bytes(e),
    {status: 200, headers: {'Content-Type': e.mime}}));
  console.warn('[offline copy] not captured:', method, url);
  return Promise.resolve(new Response('', {status: 404, statusText: 'Not in offline copy'}));
};
const nativeOpen = XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open = function (method, url, ...rest) {
  const e = String(method).toUpperCase() === 'GET' && !SKIP.test(String(url)) ? find(url) : null;
  if (!e && String(method).toUpperCase() === 'GET' && !SKIP.test(String(url))) {
    console.warn('[offline copy] not captured:', method, url);  // behave like the server's 404
    Object.defineProperty(this, 'status', {configurable: true, get: () => 404});
    Object.defineProperty(this, 'statusText', {configurable: true, get: () => 'Not Found'});
    return nativeOpen.call(this, method, blobUrl('', 'text/plain'), ...rest);
  }
  return nativeOpen.call(this, method, e ? blobUrl(bytes(e), e.mime) : url, ...rest);
};
function hook(proto, prop, convert) {
  const d = proto && Object.getOwnPropertyDescriptor(proto, prop);
  if (d && d.set) Object.defineProperty(proto, prop, {...d, set(v) { d.set.call(this, convert(v)); }});
}
for (const C of [HTMLImageElement, HTMLMediaElement, HTMLSourceElement, HTMLScriptElement,
                 HTMLIFrameElement, HTMLEmbedElement, HTMLTrackElement, HTMLInputElement])
  hook(C.prototype, 'src', v => map(v));
for (const [owner, prop] of [[Node.prototype, 'textContent'], [Element.prototype, 'innerHTML']]) {
  const d = Object.getOwnPropertyDescriptor(owner, prop);
  Object.defineProperty(HTMLStyleElement.prototype, prop,
    {...d, set(v) { d.set.call(this, css(String(v ?? ''))); }});
}
// CSS, fonts and HTML added by scripts are rewritten before the browser can request anything.
const hasUrl = s => /url\(|@import/i.test(s);
for (const name of ['appendChild', 'insertBefore']) {
  const native = Node.prototype[name];
  Node.prototype[name] = function (node, ...rest) {
    if (node && node.nodeType === 3 && this.localName === 'style' && hasUrl(node.data)) node.data = css(node.data);
    else if (node && node.nodeType === 1 && node.localName === 'style') rewrite(node);
    return native.call(this, node, ...rest);
  };
}
const nativeInsertRule = CSSStyleSheet.prototype.insertRule;
CSSStyleSheet.prototype.insertRule = function (rule, ...rest) {
  return nativeInsertRule.call(this, css(String(rule)), ...rest);
};
for (const name of ['replace', 'replaceSync']) {
  const native = CSSStyleSheet.prototype[name];
  if (native) CSSStyleSheet.prototype[name] = function (text) { return native.call(this, css(String(text))); };
}
const nativeSetProperty = CSSStyleDeclaration.prototype.setProperty;
CSSStyleDeclaration.prototype.setProperty = function (name, value, ...rest) {
  return nativeSetProperty.call(this, name, /url\(/i.test(value) ? css(String(value)) : value, ...rest);
};
hook(CSSStyleDeclaration.prototype, 'cssText', v => css(String(v)));
for (const prop of ['backgroundImage', 'background', 'maskImage', 'borderImageSource', 'listStyleImage'])
  hook(CSSStyleDeclaration.prototype, prop, v => /url\(/i.test(v) ? css(String(v)) : v);
if (window.FontFace) {
  const NativeFontFace = FontFace;
  window.FontFace = function (family, source, descriptors) {
    return new NativeFontFace(family, typeof source === 'string' ? css(source) : source, descriptors);
  };
  window.FontFace.prototype = NativeFontFace.prototype;
}
{
  const d = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML');
  Object.defineProperty(Element.prototype, 'innerHTML', {...d, set(value) {
    let html = value;
    if (this.namespaceURI === 'http://www.w3.org/1999/xhtml' && this.localName !== 'template' &&
        /<(?:style|link|img|source|video|audio)\b|url\(/i.test(String(value))) {
      const t = document.createElement('template');
      d.set.call(t, String(value));
      t.content.querySelectorAll('*').forEach(rewrite);
      html = d.get.call(t);
    }
    d.set.call(this, html);
  }});
}
hook(HTMLLinkElement.prototype, 'href', v => map(v));
hook(HTMLVideoElement.prototype, 'poster', v => map(v));
hook(HTMLImageElement.prototype, 'srcset', v => mapSrcset(v));
hook(HTMLSourceElement.prototype, 'srcset', v => mapSrcset(v));
Element.prototype.setAttribute = function (name, value) {
  const n = String(name).toLowerCase(), t = this.localName;
  if (n === 'src' || n === 'poster' || (n === 'href' && (t === 'link' || t === 'image'))) value = map(value);
  else if (n === 'srcset') value = mapSrcset(value);
  else if (n === 'style' && /url\(/i.test(value)) value = css(String(value));
  return nativeSetAttribute.call(this, name, value);
};
// Workers cannot read this page's blobs from file://, so they ask the page for each file.
function workerPrelude(base, source) {
  const scripts = {};
  if (source.includes('importScripts')) for (const e of new Set(ALL.values())) {
    if (isJs(e)) { const u = new URL(e.url); scripts[e.url] = scripts[u.origin + u.pathname] = e.b64; }
  }
  return `(() => {
const BASE = ${JSON.stringify(base)}, SCRIPTS = ${JSON.stringify(scripts)}, waiting = new Map(), queue = [];
let port = null, nextId = 1;
self.__replicatorLocation = new URL(BASE);
const local = v => /^(data|blob):/i.test(v);
const abs = v => { try { const x = new URL(v, BASE); x.hash = ''; return x.href; } catch { return null; } };
const ask = url => new Promise(done => {
  const id = nextId++, msg = {id, url};
  waiting.set(id, done);
  if (port) port.postMessage(msg); else queue.push(msg);
});
self.addEventListener('message', e => {
  if (!e.data || !e.data.__replicator_port) return;
  e.stopImmediatePropagation();
  port = e.data.__replicator_port;
  port.onmessage = m => { const done = waiting.get(m.data.id); waiting.delete(m.data.id); done(m.data); };
  queue.splice(0).forEach(msg => port.postMessage(msg));
});
const nativeFetch = self.fetch;
self.fetch = async (input, init) => {
  const req = input instanceof Request ? input : null;
  const url = req ? req.url : String(input && input.href || input);
  if (local(url)) return nativeFetch(input, init);
  const method = String(init && init.method || req && req.method || 'GET').toUpperCase();
  const hit = method === 'GET' || method === 'HEAD' ? await ask(abs(url)) : {};
  if (hit.buffer) return new Response(method === 'HEAD' ? null : hit.buffer, {status: 200, headers: {'Content-Type': hit.mime}});
  console.warn('[offline copy] worker, not captured:', method, url);
  return new Response('', {status: 404});
};
if (self.XMLHttpRequest) {
  const P = XMLHttpRequest.prototype, open = P.open, send = P.send, setHeader = P.setRequestHeader;
  P.open = function (method, url, isAsync, ...rest) {
    this.__replicator = null;
    if (String(method).toUpperCase() !== 'GET' || isAsync === false || local(String(url)))
      return open.call(this, method, url, isAsync === undefined ? true : isAsync, ...rest);
    this.__replicator = {method, url: abs(url)};
  };
  P.setRequestHeader = function (...args) { if (!this.__replicator) return setHeader.apply(this, args); };
  P.send = function (body) {
    const pending = this.__replicator;
    if (!pending) return send.call(this, body);
    ask(pending.url).then(hit => {
      if (!hit.buffer) {
        console.warn('[offline copy] worker, not captured:', pending.url);
        Object.defineProperty(this, 'status', {get: () => 404});
        Object.defineProperty(this, 'statusText', {get: () => 'Not Found'});
      }
      open.call(this, 'GET', URL.createObjectURL(new Blob(hit.buffer ? [hit.buffer] : [], {type: hit.mime || ''})), true);
      send.call(this);
    });
  };
}
if (self.importScripts) {
  const load = self.importScripts;
  self.importScripts = (...urls) => load(...urls.map(u => {
    const a = local(String(u)) ? null : abs(u), x = a && new URL(a);
    const b64 = a && (SCRIPTS[a] || SCRIPTS[x.origin + x.pathname]);
    if (!b64) return u;
    const s = atob(b64), out = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return URL.createObjectURL(new Blob([out], {type: 'text/javascript'}));
  }));
}
})();\n`;
}
function workerSource(url, opts) {
  if (opts && opts.type === 'module') return null;
  if (/^blob:/i.test(url)) {
    const x = new XMLHttpRequest();
    nativeOpen.call(x, 'GET', url, false);
    x.send();
    return {src: x.responseText, base: BASE};
  }
  const e = SKIP.test(url) ? null : find(url);
  return e ? {src: patchJs(text(e)), base: e.url} : null;
}
if (window.Worker) {
  const NativeWorker = Worker;
  window.Worker = class extends NativeWorker {
    constructor(url, opts) {
      const target = String(url?.href ?? url), code = workerSource(target, opts);
      super(code ? blobUrl(workerPrelude(code.base, code.src) + code.src, 'text/javascript')
                 : (opts && opts.type === 'module' ? map(target) : target), opts);
      if (!code) return;
      const channel = new MessageChannel();
      channel.port1.onmessage = ev => {
        const e = find(ev.data.url);
        if (!e) { channel.port1.postMessage({id: ev.data.id}); return; }
        const buffer = bytes(e).buffer;
        channel.port1.postMessage({id: ev.data.id, buffer, mime: e.mime}, [buffer]);
      };
      NativeWorker.prototype.postMessage.call(this, {__replicator_port: channel.port2}, [channel.port2]);
    }
  };
}
for (const name of ['pushState', 'replaceState']) {
  const native = history[name];
  history[name] = function (state, title, url) {
    try { return native.call(this, state, title, url); }
    catch { return native.call(this, state, title); }
  };
}
if (navigator.serviceWorker) {
  navigator.serviceWorker.register = () => new Promise(() => {});
}
if (navigator.sendBeacon) navigator.sendBeacon = () => true;  // never report to the real site

function onLinkClick(ev) {
  const a = ev.target.closest && ev.target.closest('a[href]');
  if (!a || ev.defaultPrevented || ev.button || ev.ctrlKey || ev.metaKey || ev.shiftKey ||
      (a.target && a.target !== '_self')) return;
  const href = a.getAttribute('href');
  if (!href || /^(javascript:|mailto:|tel:)/i.test(href)) return;
  let target;
  try { target = new URL(href, BASE); } catch { return; }
  if (abs(target.href) === abs(BASE)) {
    if (!target.hash) return;
    ev.preventDefault();
    location.hash = target.hash;
    return;
  }
  const e = find(target.href);
  if (e && isHtml(e)) {
    ev.preventDefault();
    location.search = '?__page=' + encodeURIComponent(e.url);  // relative URLs would follow <base>
  }
}

function render(page) {
  BASE = page.url;
  const doc = new DOMParser().parseFromString(text(page), 'text/html');
  const pageBase = doc.querySelector('base[href]');
  if (pageBase) BASE = abs(pageBase.getAttribute('href')) || BASE;
  doc.querySelectorAll('base, noscript, meta[http-equiv="Content-Security-Policy" i], meta[http-equiv="refresh" i]')
    .forEach(n => n.remove());
  doc.querySelectorAll('script[type="importmap"]').forEach(n => {
    try { Object.assign(PAGE_IMPORTS, JSON.parse(n.textContent).imports || {}); } catch {}
    n.remove();
  });
  doc.querySelectorAll('script[type="module"]').forEach(s => {
    s.removeAttribute('integrity');
    const src = s.getAttribute('src');
    if (!src) { s.textContent = moduleText(s.textContent, BASE); return; }
    const e = find(src);
    if (e) { loadModule(e); s.setAttribute('src', IMPORTS[e.url]); }
  });
  doc.querySelectorAll('script:not([src])').forEach(s => {
    if (!s.type || /javascript|ecmascript/i.test(s.type)) s.textContent = patchJs(s.textContent);
  });
  doc.querySelectorAll('*').forEach(rewrite);

  const base = doc.createElement('base');
  base.href = BASE;
  const importMap = doc.createElement('script');
  importMap.type = 'importmap';
  importMap.textContent = JSON.stringify({imports: IMPORTS});
  doc.head.prepend(base, importMap);
  if (D.css.trim()) doc.head.append(Object.assign(doc.createElement('style'), {textContent: css(D.css)}));
  if (D.js.trim()) importMap.after(Object.assign(doc.createElement('script'), {textContent: patchJs(D.js)}));

  window.__replicator_page = new URL(BASE).pathname;
  document.open();
  window.__replicator_text.watch();
  window.__replicator_control.watch();
  window.addEventListener('click', onLinkClick);
  new MutationObserver(records => {
    for (const r of records) {
      if (r.type === 'attributes') { rewrite(r.target); continue; }
      for (const n of r.addedNodes) {
        if (n.nodeType !== 1) continue;
        rewrite(n);
        n.querySelectorAll('*').forEach(rewrite);
      }
    }
  }).observe(document, {subtree: true, childList: true, attributes: true, attributeFilter: ['style']});
  document.write('<!doctype html>' + doc.documentElement.outerHTML);
  document.close();
}

// document.open() is ignored while this file is still being parsed, so wait for load.
window.addEventListener('load', () => setTimeout(() => {
  try {
    const wanted = new URLSearchParams(location.search).get('__page');
    const page = (wanted && find(wanted, START)) || find(START, START);
    if (!page) throw new Error('The start page is missing from this file.');
    render(page);
  } catch (err) {
    document.body.textContent = 'Could not open this offline copy: ' + err.message;
    console.error(err);
  }
}), {once: true});
})();
"""
