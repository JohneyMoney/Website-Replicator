"""Safe text-only replacement across HTML, JS, JSON, SVG/XML and plain text.

Protects filenames (igloo.png), URLs (https://igloo.inc/...), paths (/assets/igloo/...),
CSS selectors/classes, HTML tags/attributes, and JS code identifiers so replacing visible
brand/UI text across a captured site does not break assets or scripts.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

_ASSET_PROPS = frozenset({
    "src", "href", "url", "uri", "path", "file", "filename", "image", "img",
    "icon", "poster", "srcset", "font", "shader", "model", "texture", "audio",
    "video", "id", "class", "classname", "key", "slug", "route", "endpoint",
    "host", "domain", "origin", "base", "baseurl", "rel", "type", "as",
})

_PROTECTED_INLINE_RE = re.compile(
    r"<[^>]+>"                                  # HTML/SVG tags & attributes inside strings
    r"|https?://[^\s\"'<>`\\]+"                 # full URLs
    r"|//[^\s\"'<>`\\]+"                        # protocol-relative URLs
    r"|[^\s\"'<>`]*[/\\][^\s\"'<>`]*"           # paths (/assets/foo, ./bar)
    r"|[\w.-]+\.(?:png|jpe?g|gif|webp|avif|svg|ico|bmp|tiff?"
    r"|woff2?|ttf|otf|eot|mp4|webm|mp3|ogg|wav|m4a"
    r"|glb|gltf|ktx2|bin|wasm|js|mjs|cjs|css|html?|json|xml|pdf|exr|hdr"
    r"|inc|com|org|net|io|dev|app)\b",          # filenames & common domain names
    re.IGNORECASE,
)

_SINGLE_TOKEN_SKIP_RE = re.compile(
    r"[/\\:]"                                   # paths, URLs, schemes
    r"|\.[a-zA-Z0-9]{1,8}$"                     # filename.ext or domain.tld
    r"|[-_]"                                    # CSS classes, slugs, snake_case ids
    r"|[a-z][A-Z]",                             # camelCase identifiers (case-sensitive!)
)

_JS_ESCAPE_RE = re.compile(r"\\(?:u[0-9a-fA-F]{4}|u\{[0-9a-fA-F]+\}|x[0-9a-fA-F]{2}|.)")


def _compile_find(find_text: str, case_sensitive: bool) -> re.Pattern[str]:
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(re.escape(find_text), flags)


def _replace_prose(
    text: str, find_re: re.Pattern[str], replace_text: str, *, skip_escapes: bool = False
) -> tuple[str, int]:
    """Replace `find_re` in `text`, skipping URLs, paths, filenames, and <tags>."""
    protected = [(m.start(), m.end()) for m in _PROTECTED_INLINE_RE.finditer(text)]
    if skip_escapes:
        protected.extend((m.start(), m.end()) for m in _JS_ESCAPE_RE.finditer(text))
        protected.sort()
    out: list[str] = []
    pos = 0
    count = 0
    p_idx = 0
    n_prot = len(protected)
    for m in find_re.finditer(text):
        s, e = m.start(), m.end()
        while p_idx < n_prot and protected[p_idx][1] <= s:
            p_idx += 1
        blocked = False
        j = p_idx
        while j < n_prot and protected[j][0] < e:
            if protected[j][1] > s:
                blocked = True
                break
            j += 1
        if blocked:
            continue
        out.append(text[pos:s])
        out.append(replace_text)
        pos = e
        count += 1
    if not count:
        return text, 0
    out.append(text[pos:])
    return "".join(out), count


class _HtmlTextSpanFinder(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=False)
        self._text = text
        self._offsets = [0]
        for ln in text.splitlines(keepends=True):
            self._offsets.append(self._offsets[-1] + len(ln))
        self._raw_depth = 0
        self.spans: list[tuple[int, int]] = []

    def _idx(self) -> int:
        line, col = self.getpos()
        if line - 1 < len(self._offsets):
            return self._offsets[line - 1] + col
        return len(self._text)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in ("script", "style"):
            self._raw_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("script", "style") and self._raw_depth > 0:
            self._raw_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._raw_depth == 0 and data and data.strip():
            start = self._idx()
            end = start + len(data)
            if self._text[start:end] == data:
                self.spans.append((start, end))


def _replace_html_text(text: str, find_re: re.Pattern[str], replace_text: str) -> tuple[str, int]:
    parser = _HtmlTextSpanFinder(text)
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        return text, 0
    if not parser.spans:
        return text, 0
    out: list[str] = []
    pos = 0
    total = 0
    for s, e in parser.spans:
        if s < pos:
            continue
        chunk = text[s:e]
        new_chunk, cnt = _replace_prose(chunk, find_re, replace_text)
        if cnt:
            out.append(text[pos:s])
            out.append(new_chunk)
            pos = e
            total += cnt
    if not total:
        return text, 0
    out.append(text[pos:])
    return "".join(out), total


_REGEX_PREV_CHARS = frozenset("=(,[!&|?:;{}~^+-*/%<>")
_REGEX_PREV_WORDS = frozenset({"return", "case", "throw", "typeof", "void", "delete", "in", "of", "else", "await", "yield"})


def _scan_js_strings(src: str) -> list[tuple[int, int, str, bool, str]]:
    """Return list of (content_start, content_end, quote, is_key, prop_name) for JS/JSON strings."""
    n = len(src)
    i = 0
    spans: list[tuple[int, int, str, bool, str]] = []
    brace_stack: list[str] = []  # 'tpl' inside `${...}` interpolation
    prev_word = ""
    prev_char = ""

    def _prev_ident_before(pos: int) -> str:
        j = pos - 1
        while j >= 0 and src[j].isspace():
            j -= 1
        if j < 0 or src[j] not in (":", "="):
            return ""
        j -= 1
        while j >= 0 and src[j].isspace():
            j -= 1
        if j >= 0 and src[j] in ("'", '"'):
            q = src[j]
            k = j - 1
            while k >= 0 and src[k] != q:
                k -= 1
            return src[k + 1:j].lower() if k >= 0 else ""
        k = j
        while k >= 0 and (src[k].isalnum() or src[k] in ("_", "$")):
            k -= 1
        return src[k + 1:j + 1].lower()

    while i < n:
        ch = src[i]
        # Skip whitespace
        if ch.isspace():
            i += 1
            continue
        # Line comment
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            nl = src.find("\n", i + 2)
            i = n if nl < 0 else nl + 1
            continue
        # Block comment
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        # Regex literal heuristic
        if ch == "/" and (prev_char == "" or prev_char in _REGEX_PREV_CHARS or prev_word in _REGEX_PREV_WORDS):
            j = i + 1
            in_cls = False
            while j < n:
                rc = src[j]
                if rc == "\\":
                    j += 2
                    continue
                if rc == "[":
                    in_cls = True
                elif rc == "]" and in_cls:
                    in_cls = False
                elif rc == "/" and not in_cls:
                    j += 1
                    while j < n and src[j].isalpha():
                        j += 1
                    break
                elif rc == "\n":
                    j = i + 1  # not a regex literal
                    break
                j += 1
            if j > i + 1:
                i = j
                prev_char = "/"
                prev_word = ""
                continue
        # Template literal close -> resume template text
        if ch == "}" and brace_stack and brace_stack[-1] == "tpl":
            brace_stack.pop()
            i = _consume_template_chunk(src, i + 1, spans, brace_stack)
            prev_char = "`"
            prev_word = ""
            continue
        if ch == "{":
            brace_stack.append("{")
            prev_char = "{"
            prev_word = ""
            i += 1
            continue
        if ch == "}":
            if brace_stack:
                brace_stack.pop()
            prev_char = "}"
            prev_word = ""
            i += 1
            continue
        # Quoted string '...' or "..."
        if ch in ("'", '"'):
            prop = _prev_ident_before(i)
            j = i + 1
            while j < n:
                sc = src[j]
                if sc == "\\":
                    j += 2
                    continue
                if sc == ch:
                    break
                if sc == "\n":
                    break
                j += 1
            if j < n and src[j] == ch:
                k = j + 1
                while k < n and src[k].isspace():
                    k += 1
                is_key = (prev_char in ("{", ",", "") and k < n and src[k] == ":")
                spans.append((i + 1, j, ch, is_key, prop))
                i = j + 1
                prev_char = ch
                prev_word = ""
                continue
        # Template literal start `...`
        if ch == "`":
            i = _consume_template_chunk(src, i + 1, spans, brace_stack)
            prev_char = "`"
            prev_word = ""
            continue
        # Identifier
        if ch.isalpha() or ch in ("_", "$"):
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in ("_", "$")):
                j += 1
            prev_word = src[i:j]
            prev_char = prev_word[-1]
            i = j
            continue
        prev_char = ch
        prev_word = ""
        i += 1
    return spans


def _consume_template_chunk(
    src: str, start: int, spans: list[tuple[int, int, str, bool, str]], brace_stack: list[str]
) -> int:
    n = len(src)
    j = start
    while j < n:
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if c == "$" and j + 1 < n and src[j + 1] == "{":
            if j > start:
                spans.append((start, j, "`", False, ""))
            brace_stack.append("tpl")
            return j + 2
        if c == "`":
            if j > start:
                spans.append((start, j, "`", False, ""))
            return j + 1
        j += 1
    return n


def _escape_for_quote(text: str, quote: str) -> str:
    s = text.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
    if quote == "'":
        return s.replace("'", "\\'")
    if quote == '"':
        return s.replace('"', '\\"')
    return s.replace("`", "\\`").replace("${", "\\${")


def _should_skip_js_string(raw: str, is_key: bool, prop_name: str) -> bool:
    if is_key or prop_name in _ASSET_PROPS:
        return True
    stripped = raw.strip()
    if not stripped:
        return True
    # Single token with no spaces: skip paths, URLs, filenames, CSS classes, snake/camelCase ids.
    if not any(c.isspace() for c in stripped):
        if _SINGLE_TOKEN_SKIP_RE.search(stripped) and not _is_simple_word(stripped):
            return True
    return False


def _is_simple_word(token: str) -> bool:
    # Allow plain words/punctuation like "Igloo", "IGLOO", "Igloo!", while rejecting camelCase/slugs.
    core = token.strip(".,!?:;()[]{}\"'")
    return bool(core) and core.isalpha() and not re.search(r"[a-z][A-Z]", core)


def _replace_js_or_json(text: str, find_re: re.Pattern[str], replace_text: str) -> tuple[str, int]:
    spans = _scan_js_strings(text)
    if not spans:
        return text, 0
    out: list[str] = []
    pos = 0
    total = 0
    for s, e, quote, is_key, prop in spans:
        if s < pos:
            continue
        raw = text[s:e]
        if _should_skip_js_string(raw, is_key, prop):
            continue
        safe_repl = _escape_for_quote(replace_text, quote)
        new_raw, cnt = _replace_prose(raw, find_re, safe_repl, skip_escapes=True)
        if cnt:
            out.append(text[pos:s])
            out.append(new_raw)
            pos = e
            total += cnt
    if not total:
        return text, 0
    out.append(text[pos:])
    return "".join(out), total


def safe_replace_in_content(
    text: str,
    find_text: str,
    replace_text: str,
    *,
    mime: str = "",
    url: str = "",
    case_sensitive: bool = False,
) -> tuple[str, int]:
    """Replace `find_text` with `replace_text` in `text` while protecting filenames, URLs,
    CSS selectors, HTML tags/attributes, and JS identifiers."""
    if not find_text:
        return text, 0
    m = (mime or "").lower()
    path = url.split("?", 1)[0].lower()
    find_re = _compile_find(find_text, case_sensitive)

    # CSS only holds selectors, properties and url(...) paths — skip in safe text mode.
    if "css" in m or path.endswith(".css"):
        return text, 0

    if "html" in m or path.endswith((".html", ".htm", ".xhtml")):
        return _replace_html_text(text, find_re, replace_text)

    if "svg" in m or "xml" in m or path.endswith((".svg", ".xml")):
        return _replace_html_text(text, find_re, replace_text)

    if any(k in m for k in ("javascript", "ecmascript", "json")) or path.endswith((".js", ".mjs", ".cjs", ".json")):
        return _replace_js_or_json(text, find_re, replace_text)

    # Extensionless API JSON or plain text: use JSON/JS scanner if it looks like JSON, else prose filter.
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return _replace_js_or_json(text, find_re, replace_text)
    return _replace_prose(text, find_re, replace_text)
