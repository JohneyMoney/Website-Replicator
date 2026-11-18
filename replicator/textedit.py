"""Runtime half of text editing: applies saved text rules to canvas and DOM text.

WebGL text normally reaches the GPU through a 2D canvas (fillText -> texture) or through
strings in JS/JSON (MSDF and bitmap fonts). The second kind is handled when files are
served (Archive.apply_text_rules); this script covers the first kind and dynamic DOM text.
It must run before any page script.
"""
from __future__ import annotations

import json


def text_runtime_tag_content(rules: list[dict]) -> str:
    data = json.dumps(rules, ensure_ascii=False).replace("<", "\\u003c")
    return f"window.__replicator_text_rules={data};\n{TEXT_RUNTIME_JS}"


TEXT_RUNTIME_JS = r"""
(function () {
  if (window.__replicator_text) return;
  const escape = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const rules = (window.__replicator_text_rules || [])
    .slice().sort((a, b) => b.from.length - a.from.length)
    .map(r => ({from: r.from, to: r.to,
      re: new RegExp('(?<![\\p{L}\\p{N}_])' + escape(r.from) + '(?![\\p{L}\\p{N}_])', 'gu')}));
  function apply(value) {
    let s = String(value);
    for (const r of rules) {
      if (r.to.includes(r.from) && s.includes(r.to)) continue;  // already applied in the source
      if (s.includes(r.from)) s = s.replace(r.re, () => r.to);
    }
    return s;
  }

  const drawn = new Map();
  function record(value) {
    const s = String(value).trim();
    if (s.length >= 2 && s.length < 400 && drawn.size < 800 && /\p{L}/u.test(s)) drawn.set(s, 1);
  }
  for (const C of [window.CanvasRenderingContext2D, window.OffscreenCanvasRenderingContext2D]) {
    if (!C) continue;
    for (const name of ['fillText', 'strokeText', 'measureText']) {
      const native = C.prototype[name];
      C.prototype[name] = function (text, ...rest) {
        if (name !== 'measureText') record(text);
        return native.call(this, rules.length ? apply(text) : text, ...rest);
      };
    }
  }

  const written = new WeakMap(), SKIP = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'NOSCRIPT']);
  function fixNode(n) {
    if (!n.parentNode || SKIP.has(n.parentNode.nodeName) || written.get(n) === n.nodeValue) return;
    const out = apply(n.nodeValue);
    if (out !== n.nodeValue) n.nodeValue = out;
    written.set(n, n.nodeValue);
  }
  function fixTree(root) {
    if (root.nodeType === 3) return fixNode(root);
    if (root.nodeType !== 1 && root.nodeType !== 9) return;
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (w.nextNode()) fixNode(w.currentNode);
  }
  function watch() {
    if (!rules.length) return;
    new MutationObserver(list => {
      for (const m of list) {
        if (m.type === 'characterData') fixNode(m.target);
        else m.addedNodes.forEach(fixTree);
      }
    }).observe(document, {subtree: true, childList: true, characterData: true});
    document.addEventListener('DOMContentLoaded', () => fixTree(document));
    if (document.readyState !== 'loading') fixTree(document);
  }
  window.__replicator_text = {apply, watch, drawn: () => [...drawn.keys()]};
  watch();
})();
"""
