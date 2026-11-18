"""SQLite asset cache: one row per captured HTTP exchange, bodies stored as raw BLOBs.

Bodies are never decoded - they go in and come out as the exact byte stream the
browser received (after transfer decompression), so .glb/.wasm/.bin/audio/video
stay bit-identical.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import struct
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .util import (header_value, origin_of, path_only, path_query, request_key,
                   strip_fragment)

SCHEMA = """
CREATE TABLE IF NOT EXISTS responses(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT UNIQUE NOT NULL,
    method        TEXT NOT NULL,
    url           TEXT NOT NULL,
    origin        TEXT NOT NULL,
    path          TEXT NOT NULL,
    pathq         TEXT NOT NULL,
    status        INTEGER NOT NULL,
    status_text   TEXT,
    req_headers   TEXT,
    resp_headers  TEXT NOT NULL,
    mime          TEXT,
    resource_type TEXT,
    post_data     BLOB,
    body          BLOB,
    body_size     INTEGER NOT NULL DEFAULT 0,
    truncated     INTEGER NOT NULL DEFAULT 0,
    captured_at   REAL
);
CREATE INDEX IF NOT EXISTS ix_url      ON responses(method, url);
CREATE INDEX IF NOT EXISTS ix_path     ON responses(method, origin, path);
CREATE INDEX IF NOT EXISTS ix_pathq    ON responses(method, pathq);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


APP_DIR = Path(__file__).resolve().parent.parent


def default_root() -> Path:
    """Archives live in a visible 'archives' folder next to the app (override: REPLICATOR_HOME)."""
    env = os.environ.get("REPLICATOR_HOME")
    root = Path(env) / "archives" if env else APP_DIR / "archives"
    root.mkdir(parents=True, exist_ok=True)
    return root


def list_archives(root: Path | None = None) -> list["Archive"]:
    root = root or default_root()
    items = [Archive(p) for p in root.iterdir() if (p / "archive.db").exists()]
    return sorted(items, key=lambda a: a.db_path.stat().st_mtime, reverse=True)


class Archive:
    def __init__(self, directory: Path | str):
        self.dir = Path(directory)
        self.db_path = self.dir / "archive.db"
        self._lock = threading.RLock()
        self._index_sig: tuple[int, int] | None = None
        self._index_map: dict[str, Path] = {}

    @property
    def name(self) -> str:
        return self.dir.name

    @classmethod
    def create(cls, root: Path, name: str) -> "Archive":
        d = Path(root) / name
        d.mkdir(parents=True, exist_ok=True)
        a = cls(d)
        a.connect().close()
        return a

    @classmethod
    def open(cls, name_or_path: str, root: Path | None = None) -> "Archive":
        p = Path(name_or_path)
        if not (p / "archive.db").exists():
            p = (root or default_root()) / name_or_path
        if not (p / "archive.db").exists():
            raise FileNotFoundError(f"No archive at {p}")
        return cls(p)

    # ------------------------------------------------------------------ db
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        return conn

    def put(self, conn, *, method: str, url: str, status: int, status_text: str,
            req_headers, resp_headers, body: bytes | None, resource_type: str = "",
            post_data: bytes | None = None, truncated: bool = False) -> None:
        url = strip_fragment(url)
        method = method.upper()
        mime = header_value(resp_headers, "content-type") or ""
        with self._lock:
            conn.execute(
                """INSERT OR REPLACE INTO responses(key, method, url, origin, path, pathq, status,
                   status_text, req_headers, resp_headers, mime, resource_type, post_data, body,
                   body_size, truncated, captured_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (request_key(method, url, post_data), method, url, origin_of(url), path_only(url),
                 path_query(url), int(status), status_text or "", json.dumps(list(req_headers or [])),
                 json.dumps([list(p) for p in resp_headers]), mime, resource_type or "",
                 sqlite3.Binary(post_data) if post_data else None,
                 sqlite3.Binary(body) if body is not None else None,
                 len(body) if body is not None else 0, int(truncated), time.time()))
            conn.commit()

    def set_meta(self, conn, **kv) -> None:
        with self._lock:
            for k, v in kv.items():
                conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, json.dumps(v)))
            conn.commit()

    def meta(self, conn=None) -> dict:
        own = conn is None
        conn = conn or self.connect()
        try:
            return {r["k"]: json.loads(r["v"]) for r in conn.execute("SELECT k, v FROM meta")}
        finally:
            if own:
                conn.close()

    def stats(self, conn=None) -> dict:
        own = conn is None
        conn = conn or self.connect()
        try:
            r = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(body_size),0) b FROM responses").fetchone()
            return {"files": r["n"], "bytes": r["b"]}
        finally:
            if own:
                conn.close()

    def write_manifest(self, conn) -> None:
        data = self.meta(conn)
        data.update(self.stats(conn))
        (self.dir / "manifest.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    # -------------------------------------------------------------- lookup
    def lookup(self, conn, method: str, url: str, body: bytes | None = None,
               prefer_origins: tuple[str, ...] = ()):
        """Find the best cached response. Returns (row | None, how)."""
        m = "GET" if method.upper() == "HEAD" else method.upper()
        url = strip_fragment(url)
        q = lambda sql, args: conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()

        row = conn.execute("SELECT * FROM responses WHERE key=?", (request_key(m, url, body),)).fetchone()
        if row:
            return row, "exact"
        row = q("SELECT * FROM responses WHERE method=? AND url=?", (m, url))
        if row:
            return row, "same-url"
        # Cache-busting query strings (?t=171234...) differ every run: ignore the query.
        row = q("SELECT * FROM responses WHERE method=? AND origin=? AND path=?",
                (m, origin_of(url), path_only(url)))
        if row:
            return row, "query-ignored"
        # Root-relative URL resolved against the wrong origin (e.g. CDN css -> /fonts/x).
        pq = path_query(url)
        for o in prefer_origins:
            row = q("SELECT * FROM responses WHERE method=? AND origin=? AND pathq=?", (m, o, pq))
            if row:
                return row, "referer-origin"
        row = q("SELECT * FROM responses WHERE method=? AND pathq=?", (m, pq))
        if row:
            return row, "other-origin"
        return None, "miss"

    def start_document(self, conn):
        start = self.meta(conn).get("start_url")
        if not start:
            return None
        row, _ = self.lookup(conn, "GET", start)
        return row

    # ----------------------------------------------------------------- HAR
    def export_har(self, out_path: Path) -> Path:
        """Stream an HTTP Archive 1.2 file (bodies base64, so binaries survive)."""
        conn = self.connect()
        out_path = Path(out_path)
        try:
            with out_path.open("w", encoding="utf-8") as f:
                f.write('{"log":{"version":"1.2","creator":{"name":"Website Replicator","version":"1.0"},'
                        '"pages":[],"entries":[')
                first = True
                for r in conn.execute("SELECT * FROM responses ORDER BY id"):
                    req_h = [{"name": k, "value": v} for k, v in json.loads(r["req_headers"] or "[]")]
                    resp_h = [{"name": k, "value": v} for k, v in json.loads(r["resp_headers"])]
                    body = r["body"] or b""
                    entry = {
                        "startedDateTime": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(r["captured_at"] or 0)),
                        "time": 0,
                        "request": {"method": r["method"], "url": r["url"], "httpVersion": "HTTP/1.1",
                                    "cookies": [], "headers": req_h, "queryString": [],
                                    "headersSize": -1, "bodySize": len(r["post_data"] or b"")},
                        "response": {"status": r["status"], "statusText": r["status_text"] or "",
                                     "httpVersion": "HTTP/1.1", "cookies": [], "headers": resp_h,
                                     "content": {"size": len(body), "mimeType": r["mime"] or "",
                                                 "text": base64.b64encode(body).decode("ascii"),
                                                 "encoding": "base64"},
                                     "redirectURL": "", "headersSize": -1, "bodySize": len(body)},
                        "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0},
                    }
                    if r["post_data"]:
                        entry["request"]["postData"] = {
                            "mimeType": header_value(json.loads(r["req_headers"] or "[]"), "content-type") or "",
                            "text": base64.b64encode(r["post_data"]).decode("ascii"),
                            "encoding": "base64"}
                    f.write(("" if first else ",") + json.dumps(entry))
                    first = False
                f.write("]}}")
        finally:
            conn.close()
        return out_path

    # ------------------------------------------------------------ delete
    def delete(self) -> str:
        """Move the archive folder to the Windows Recycle Bin (restorable).
        Falls back to permanent removal on other systems. Returns 'recycled' or 'deleted'."""
        path = str(self.dir.resolve())
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                            ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                            ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                            ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

            FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
            op = SHFILEOPSTRUCTW(None, FO_DELETE, path + "\0", None,
                                 FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
                                 False, None, None)
            if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not self.dir.exists():
                return "recycled"
        import shutil
        shutil.rmtree(path)
        return "deleted"

    # ------------------------------------------------------ visible files
    def export_files(self, conn=None, log=print) -> Path:
        """Write every captured file (HTML, JS, CSS, GLB, WASM, images...) as a real file:
        <archive>/files/<host>/<path>. Bytes are copied 1:1 from the database."""
        own = conn is None
        conn = conn or self.connect()
        out = (self.dir / "files").resolve()
        # Keep every full path under Windows' 260-char limit so Explorer/editors can open it.
        budget = max(40, 245 - len(str(out)))
        index_lines, failed, n = [], [], 0
        try:
            for r in conn.execute("SELECT url, body FROM responses WHERE method='GET' AND status=200 "
                                  "AND body IS NOT NULL ORDER BY id"):
                url = r["url"]
                for rel in _candidate_paths(url, budget):
                    try:
                        _write(out / rel, r["body"])
                        index_lines.append(f"{rel.as_posix()}\t{url}")
                        n += 1
                        break
                    except OSError:  # e.g. 'a' is a file but 'a/b' needs it as a folder
                        continue
                else:
                    failed.append(url)
                    log(f"  ! could not write file for {url[:100]}")
            _write(out / "_index.tsv", ("local file\toriginal url\n" + "\n".join(index_lines) + "\n").encode("utf-8"))
        finally:
            if own:
                conn.close()
        self.write_launcher()
        log(f"Wrote {n} files to {out}" + (f" ({len(failed)} failed)" if failed else " - all files exported"))
        return out

    def _load_files_index(self) -> dict[str, Path]:
        """Parse <archive>/files/_index.tsv (cached by mtime/size) -> {url: local_path}."""
        files_root = (self.dir / "files").resolve()
        idx = files_root / "_index.tsv"
        try:
            st = _win_path(idx).stat()
        except OSError:
            self._index_sig = None
            self._index_map = {}
            return self._index_map
        sig = (st.st_mtime_ns, st.st_size)
        with self._lock:
            if self._index_sig == sig:
                return self._index_map
            mapping: dict[str, Path] = {}
            text = _win_path(idx).read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines()[1:]:
                if "\t" not in line:
                    continue
                rel_str, url = line.split("\t", 1)
                candidate = (files_root / rel_str).resolve()
                if candidate.is_relative_to(files_root):
                    mapping[url] = candidate
            self._index_sig = sig
            self._index_map = mapping
            return self._index_map

    def exported_path_for(self, url: str) -> Path | None:
        """Return the exported file path in <archive>/files/ for `url`, if indexed."""
        return self._load_files_index().get(strip_fragment(url))

    def body_for(self, row) -> bytes:
        """Return edited file bytes from <archive>/files/ when available, else DB body."""
        if row["method"] == "GET" and row["status"] == 200:
            p = self._load_files_index().get(row["url"])
            if p is not None:
                wp = _win_path(p)
                if wp.is_file():
                    return wp.read_bytes()
        return row["body"] or b""

    def sync_files(self, conn=None, log=print) -> int:
        """Copy modified files from <archive>/files/ back into archive.db."""
        mapping = self._load_files_index()
        if not mapping:
            log("No exported files found (export files first).")
            return 0
        own = conn is None
        conn = conn or self.connect()
        updated = 0
        try:
            with self._lock:
                for url, path in mapping.items():
                    wp = _win_path(path)
                    if not wp.is_file():
                        continue
                    disk_bytes = wp.read_bytes()
                    rows = conn.execute(
                        "SELECT id, body FROM responses WHERE method='GET' AND status=200 AND url=?",
                        (url,),
                    ).fetchall()
                    for r in rows:
                        if (r["body"] or b"") != disk_bytes:
                            conn.execute(
                                "UPDATE responses SET body=?, body_size=? WHERE id=?",
                                (sqlite3.Binary(disk_bytes), len(disk_bytes), r["id"]),
                            )
                            updated += 1
                if updated:
                    conn.commit()
        finally:
            if own:
                conn.close()
        log(f"Synced {updated} modified file(s) from files/ into archive.db.")
        return updated

    def list_entries(self, conn=None) -> list[dict]:
        """List GET 200 entries with metadata (without loading bodies) for inspection/editing."""
        own = conn is None
        conn = conn or self.connect()
        index = self._load_files_index()
        files_root = (self.dir / "files").resolve()
        try:
            rows = conn.execute(
                "SELECT id, url, mime, resource_type, body_size FROM responses "
                "WHERE method='GET' AND status=200 AND body IS NOT NULL "
                "ORDER BY (CASE WHEN lower(mime) LIKE '%html%' THEN 0 "
                "               WHEN lower(mime) LIKE '%javascript%' THEN 1 "
                "               WHEN lower(mime) LIKE '%css%' THEN 2 ELSE 3 END), id"
            ).fetchall()
            out = []
            for r in rows:
                p = index.get(r["url"])
                rel = p.relative_to(files_root).as_posix() if p is not None else ""
                size = _win_path(p).stat().st_size if p is not None and _win_path(p).is_file() else r["body_size"]
                out.append({
                    "id": r["id"],
                    "url": r["url"],
                    "mime": r["mime"] or "",
                    "resource_type": r["resource_type"] or "",
                    "body_size": size,
                    "local_rel": rel,
                    "local_path": p,
                })
            return out
        finally:
            if own:
                conn.close()

    def read_entry_bytes(self, url: str, conn=None) -> bytes:
        """Read current effective bytes (files/ override if present, else archive.db)."""
        own = conn is None
        conn = conn or self.connect()
        try:
            row = conn.execute(
                "SELECT method, status, url, body FROM responses "
                "WHERE method='GET' AND status=200 AND url=? ORDER BY id DESC LIMIT 1",
                (url,),
            ).fetchone()
            return self.body_for(row) if row is not None else b""
        finally:
            if own:
                conn.close()

    def save_entry_bytes(self, url: str, data: bytes, conn=None) -> None:
        """Persist edited bytes to both archive.db and <archive>/files/ (if exported)."""
        own = conn is None
        conn = conn or self.connect()
        try:
            with self._lock:
                conn.execute(
                    "UPDATE responses SET body=?, body_size=? "
                    "WHERE method='GET' AND status=200 AND url=?",
                    (sqlite3.Binary(data), len(data), url),
                )
                conn.commit()
            p = self.exported_path_for(url)
            if p is not None:
                _write(p, data)
        finally:
            if own:
                conn.close()

    def get_exchange(self, entry_id: int, conn=None) -> dict | None:
        """Fetch a single captured HTTP exchange by row id (with files/ override if applicable)."""
        own = conn is None
        conn = conn or self.connect()
        try:
            r = conn.execute("SELECT * FROM responses WHERE id=?", (int(entry_id),)).fetchone()
            if r is None:
                return None
            return {
                "id": r["id"],
                "method": r["method"],
                "status": r["status"],
                "status_text": r["status_text"] or "",
                "url": r["url"],
                "mime": r["mime"] or "",
                "resource_type": r["resource_type"] or "",
                "req_headers": json.loads(r["req_headers"] or "[]"),
                "resp_headers": json.loads(r["resp_headers"] or "[]"),
                "post_data": r["post_data"] or b"",
                "body": self.body_for(r),
            }
        finally:
            if own:
                conn.close()

    def search_contents(
        self,
        query: str,
        *,
        case_sensitive: bool = False,
        limit: int = 300,
        max_bytes: int = 10 * 1024 * 1024,
        conn=None,
    ) -> list[dict]:
        """Search across ALL captured exchanges (URLs, headers, POST bodies, and response bodies,
        including edited files in <archive>/files/)."""
        q = query.strip()
        if not q:
            return []
        own = conn is None
        conn = conn or self.connect()
        index = self._load_files_index()
        files_root = (self.dir / "files").resolve()
        needle_str = q if case_sensitive else q.lower()
        ascii_only = q.isascii()
        needle_bytes = needle_str.encode("ascii") if ascii_only else b""
        results: list[dict] = []
        try:
            for r in conn.execute("SELECT * FROM responses ORDER BY id"):
                where: list[str] = []
                total_hits = 0
                snippet = ""

                def _scan_text(label: str, text: str) -> None:
                    nonlocal total_hits, snippet
                    hay = text if case_sensitive else text.lower()
                    idx = hay.find(needle_str)
                    if idx < 0:
                        return
                    where.append(label)
                    total_hits += hay.count(needle_str)
                    if not snippet:
                        start = max(0, idx - 40)
                        end = min(len(text), idx + len(q) + 60)
                        snippet = " ".join(text[start:end].split())

                _scan_text("url", r["url"])
                if r["req_headers"]:
                    _scan_text("req-headers", r["req_headers"])
                if r["resp_headers"]:
                    _scan_text("resp-headers", r["resp_headers"])
                post = r["post_data"] or b""
                if post and len(post) <= max_bytes and b"\x00" not in post[:2048]:
                    if not ascii_only or needle_bytes in (post if case_sensitive else post.lower()):
                        _scan_text("post-body", post.decode("utf-8", errors="replace"))

                body = self.body_for(r)
                mime = (r["mime"] or "").lower()
                skip_mime = mime.startswith(("image/", "video/", "audio/", "font/", "model/")) and "svg" not in mime
                if body and not skip_mime and len(body) <= max_bytes and b"\x00" not in body[:2048]:
                    if not ascii_only or needle_bytes in (body if case_sensitive else body.lower()):
                        _scan_text("body", body.decode("utf-8", errors="replace"))

                if where:
                    p = index.get(r["url"]) if (r["method"] == "GET" and r["status"] == 200) else None
                    rel = p.relative_to(files_root).as_posix() if p is not None else ""
                    results.append({
                        "id": r["id"],
                        "method": r["method"],
                        "status": r["status"],
                        "url": r["url"],
                        "mime": r["mime"] or "",
                        "resource_type": r["resource_type"] or "",
                        "body_size": len(body),
                        "local_rel": rel,
                        "local_path": p,
                        "where": ", ".join(where),
                        "matches": total_hits,
                        "snippet": snippet,
                        "editable": (r["method"] == "GET" and r["status"] == 200),
                    })
                    if len(results) >= limit:
                        break
            return results
        finally:
            if own:
                conn.close()

    def replace_all(
        self,
        find_text: str,
        replace_text: str,
        *,
        case_sensitive: bool = False,
        file_type: str = "all",
        safe_text_only: bool = True,
        max_bytes: int = 50 * 1024 * 1024,
        conn=None,
    ) -> dict:
        """Replace all occurrences of `find_text` with `replace_text` across ALL captured
        non-binary responses (HTML, JS, CSS, JSON, API/POST responses, etc.) in both
        archive.db and <archive>/files/. When `safe_text_only=True`, filenames, URLs,
        CSS selectors, HTML tags/attributes and JS code identifiers are preserved."""
        if not find_text:
            return {"files_changed": 0, "replacements": 0, "urls": []}
        from .text_replace import safe_replace_in_content
        own = conn is None
        conn = conn or self.connect()
        ft = (file_type or "all").lower()
        pattern = None if case_sensitive else re.compile(re.escape(find_text), re.IGNORECASE)
        files_changed = 0
        total_replacements = 0
        changed_urls: set[str] = set()
        binary_exts = (
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".bmp",
            ".woff", ".woff2", ".ttf", ".otf", ".eot",
            ".mp4", ".webm", ".mp3", ".ogg", ".wav", ".m4a",
            ".wasm", ".glb", ".ktx2", ".bin", ".zip", ".gz", ".br", ".pdf",
        )
        try:
            rows = conn.execute(
                "SELECT id, method, status, url, mime, body FROM responses "
                "WHERE body IS NOT NULL ORDER BY id"
            ).fetchall()
            with self._lock:
                for r in rows:
                    mime = (r["mime"] or "").lower()
                    url = r["url"]
                    path_low = url.split("?", 1)[0].lower()
                    if ft == "html":
                        if "html" not in mime and not path_low.endswith((".html", ".htm")):
                            continue
                    elif ft == "js":
                        if "javascript" not in mime and not path_low.endswith((".js", ".mjs")):
                            continue
                    elif ft == "css":
                        if "css" not in mime and not path_low.endswith(".css"):
                            continue
                    else:
                        if mime.startswith(("image/", "video/", "audio/", "font/", "model/")) and "svg" not in mime:
                            continue
                        if mime in ("application/wasm", "application/zip", "application/pdf", "application/octet-stream") and not is_editable_text("", url):
                            continue
                        if path_low.endswith(binary_exts) and not is_editable_text(mime, ""):
                            continue

                    body = self.body_for(r)
                    if not body or len(body) > max_bytes or b"\x00" in body[:4096]:
                        continue
                    try:
                        text = body.decode("utf-8")
                        enc = "utf-8"
                    except UnicodeDecodeError:
                        text = body.decode("latin-1")
                        enc = "latin-1"

                    if safe_text_only:
                        new_text, count = safe_replace_in_content(
                            text, find_text, replace_text, mime=mime, url=url, case_sensitive=case_sensitive
                        )
                    elif case_sensitive:
                        count = text.count(find_text)
                        new_text = text.replace(find_text, replace_text) if count else text
                    else:
                        new_text, count = pattern.subn(lambda _m: replace_text, text)

                    if count > 0:
                        new_bytes = new_text.encode(enc, errors="replace")
                        conn.execute(
                            "UPDATE responses SET body=?, body_size=? WHERE id=?",
                            (sqlite3.Binary(new_bytes), len(new_bytes), r["id"]),
                        )
                        if r["method"] == "GET" and r["status"] == 200:
                            p = self.exported_path_for(url)
                            if p is not None:
                                _write(p, new_bytes)
                        files_changed += 1
                        total_replacements += count
                        changed_urls.add(url)
                if files_changed:
                    conn.commit()
            return {
                "files_changed": files_changed,
                "replacements": total_replacements,
                "urls": sorted(changed_urls),
            }
        finally:
            if own:
                conn.close()

    # ------------------------------------------------------------ overrides
    def get_overrides(self) -> dict:
        """Return persistent user overrides: custom CSS, JS function stubs, and blocked URL patterns."""
        css_file = self.dir / "overrides.css"
        js_file = self.dir / "overrides.js"
        blocked_file = self.dir / "blocked_urls.json"
        css = _win_path(css_file).read_text("utf-8", errors="replace") if _win_path(css_file).exists() else ""
        js = _win_path(js_file).read_text("utf-8", errors="replace") if _win_path(js_file).exists() else ""
        blocked: list[str] = []
        if _win_path(blocked_file).exists():
            try:
                blocked = json.loads(_win_path(blocked_file).read_text("utf-8", errors="replace"))
            except Exception:
                blocked = []
        return {"css": css, "js": js, "blocked": blocked}

    def save_overrides(
        self,
        *,
        css: str | None = None,
        js: str | None = None,
        blocked: list[str] | None = None,
    ) -> None:
        """Save persistent user overrides into the archive directory."""
        with self._lock:
            if css is not None:
                _write(self.dir / "overrides.css", css.encode("utf-8"))
            if js is not None:
                _write(self.dir / "overrides.js", js.encode("utf-8"))
            if blocked is not None:
                cleaned = [b.strip() for b in blocked if b and b.strip()]
                _write(self.dir / "blocked_urls.json", json.dumps(cleaned, indent=2).encode("utf-8"))

    def add_css_override(self, rule: str) -> None:
        """Append a CSS rule to overrides.css (e.g. from the in-browser element zapper)."""
        cur = self.get_overrides()["css"]
        new_css = (cur.rstrip() + "\n" + rule.strip() + "\n").lstrip()
        self.save_overrides(css=new_css)

    def undo_last_css_override(self) -> str | None:
        """Remove the most recently appended CSS rule block from overrides.css."""
        cur = self.get_overrides()["css"].strip()
        if not cur:
            return None
        blocks = [b.strip() for b in cur.split("}\n") if b.strip()]
        if not blocks:
            self.save_overrides(css="")
            return cur
        removed = blocks.pop()
        if not removed.endswith("}"):
            removed += "}"
        rest = ("}\n\n".join(blocks) + "}\n") if blocks else ""
        self.save_overrides(css=rest)
        return removed

    def add_blocked_url(self, pattern: str) -> None:
        """Add a URL substring or glob to blocked_urls.json."""
        pat = pattern.strip()
        if not pat:
            return
        cur = self.get_overrides()["blocked"]
        if pat not in cur:
            cur.append(pat)
            self.save_overrides(blocked=cur)

    def is_url_blocked(self, url: str) -> bool:
        """Check if `url` matches any entry in blocked_urls.json."""
        blocked = self.get_overrides().get("blocked", [])
        if not blocked:
            return False
        u_low = url.lower()
        for b in blocked:
            b_low = b.strip().lower()
            if not b_low:
                continue
            if b_low in u_low:
                return True
        return False

    # --------------------------------------------------------- visual edits
    def get_text_rules(self) -> list[dict]:
        """Saved text edits: [{"from": old, "to": new}, ...]."""
        p = _win_path(self.dir / "text_rules.json")
        if not p.exists():
            return []
        try:
            return [r for r in json.loads(p.read_text("utf-8")) if r.get("from")]
        except (OSError, ValueError, AttributeError):
            return []

    def set_text_rule(self, find_text: str, replace_text: str) -> dict:
        """Add or change one text edit. Setting a text back to itself removes the edit."""
        if not find_text:
            raise ValueError("Empty text")
        with self._lock:
            rules = self.get_text_rules()
            # One visible text can have several sources ("Shop" and "SHOP"): edit them together.
            originals = [r["from"] for r in rules if r["to"] == find_text] or [find_text]
            original = originals[0]
            rules = [r for r in rules if r["from"] not in originals]
            if replace_text not in originals:
                rules.extend({"from": o, "to": replace_text} for o in originals)
            _write(self.dir / "text_rules.json", json.dumps(rules, ensure_ascii=False, indent=2).encode("utf-8"))
        self._text_cache = {}
        return {"from": original, "rules": rules, "source_files": self._text_rule_files(original),
                "missing": self._missing_glyphs(replace_text), "was_edit": original != find_text}

    def apply_text_rules(self, row, body: bytes) -> bytes:
        """Apply saved text edits to a served text file (HTML, JS, JSON, SVG...)."""
        rules = self.get_text_rules()
        if not rules or row["status"] != 200 or not is_editable_text(row["mime"], row["url"]):
            return body
        rules = sorted(rules, key=lambda r: -len(r["from"]))
        needles = [r for r in rules if r["from"].encode("utf-8") in body]
        if not needles:
            return body
        import zlib
        key = (row["url"], zlib.crc32(body), json.dumps(needles, sort_keys=True))
        cache = self.__dict__.setdefault("_text_cache", {})
        if key not in cache:
            cache[key] = self._replace_rules(row, body, needles)[0]
        return cache[key]

    @staticmethod
    def _replace_rules(row, body: bytes, rules: list[dict]) -> tuple[bytes, int]:
        from .text_replace import safe_replace_in_content
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return body, 0
        total = 0
        is_html = "html" in (row["mime"] or "").lower()

        def inline_script(m):
            nonlocal total
            code, n = safe_replace_in_content(m.group(2), r["from"], r["to"].replace("</", "<\\/"),
                                              mime="text/javascript", case_sensitive=True, whole_word=True)
            total += n
            return m.group(1) + code + m.group(3)

        for r in rules:
            text, n = safe_replace_in_content(text, r["from"], r["to"], mime=row["mime"] or "",
                                              url=row["url"], case_sensitive=True, whole_word=True)
            total += n
            if is_html:  # the HTML replacer leaves <script> contents alone
                text = _INLINE_SCRIPT.sub(inline_script, text)
        return (text.encode("utf-8"), total) if total else (body, 0)

    def _text_rows(self, conn):
        for row in conn.execute("SELECT * FROM responses WHERE method='GET' AND status=200 AND body IS NOT NULL"):
            if is_editable_text(row["mime"], row["url"]):
                yield row

    def _text_rule_files(self, find_text: str) -> int:
        conn = self.connect()
        rule, needle, found = {"from": find_text, "to": find_text + "\0"}, find_text.encode("utf-8"), 0
        try:
            for row in self._text_rows(conn):
                body = self.body_for(row)
                if needle in body and self._replace_rules(row, body, [rule])[1]:
                    found += 1
        finally:
            conn.close()
        return found

    def _missing_glyphs(self, text: str) -> list[str]:
        """Characters absent from every MSDF/bitmap font atlas in the archive (empty if no atlas)."""
        conn, known = self.connect(), set()

        def glyph_char(g):
            code = g.get("unicode", g.get("id"))
            return g.get("char") or g.get("letter") or (chr(int(code)) if code is not None else None)

        def collect(node):
            if isinstance(node, list):
                for item in node:
                    collect(item)
            elif isinstance(node, dict):
                for key, value in node.items():
                    if key in ("chars", "glyphs"):
                        glyphs = value.values() if isinstance(value, dict) else value
                        if isinstance(glyphs, (list, type({}.values()))):
                            known.update(c for g in glyphs if isinstance(g, dict)
                                         for c in [glyph_char(g)] if c)
                    else:
                        collect(value)

        try:
            for row in self._text_rows(conn):
                body = self.body_for(row)
                is_font = (b'"chars"' in body and b'"xadvance"' in body) or (b'"glyphs"' in body and b'"unicode"' in body)
                if not is_font or "json" not in (row["mime"] or "").lower():
                    continue
                try:
                    collect(json.loads(body))
                except (ValueError, TypeError, OverflowError):
                    continue
        finally:
            conn.close()
        return sorted({c for c in text if not c.isspace() and c not in known}) if known else []

    def _original_path(self, url: str) -> Path:
        return self.dir / "originals" / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".bin")

    def list_images(self) -> dict:
        """Replaceable images/textures, one entry per distinct file."""
        conn, items, seen, compressed = self.connect(), [], set(), 0
        try:
            for r in conn.execute("SELECT url, mime, body FROM responses WHERE method='GET' AND status=200 "
                                  "AND body IS NOT NULL ORDER BY id"):
                path = urlsplit(r["url"]).path.lower()
                if path.endswith((".ktx2", ".basis", ".dds")):
                    digest = hashlib.sha1(r["body"]).digest()
                    if digest in seen:
                        continue
                    seen.add(digest)
                    backup = _win_path(self._original_path(r["url"]))
                    shape = _sdf_shape(backup.read_bytes()) if backup.exists() else None
                    current = _sdf_shape(r["body"])
                    if current:
                        items.append({"url": r["url"], "kind": "sdf", "mime": "image/ktx2", "size": len(r["body"]),
                                      "width": current["width"], "height": current["height"],
                                      "bbox": (shape or current)["bbox"], "preview": current["preview"],
                                      "edited": backup.exists()})
                    else:
                        compressed += 1
                    continue
                mime = (r["mime"] or "").split(";")[0].strip().lower()
                if mime not in ("image/png", "image/jpeg", "image/webp"):
                    continue
                digest = hashlib.sha1(r["body"]).digest()
                if digest not in seen:
                    seen.add(digest)
                    items.append({"url": r["url"], "mime": mime, "size": len(r["body"]),
                                  "edited": _win_path(self._original_path(r["url"])).exists()})
        finally:
            conn.close()
        return {"images": items, "compressed": compressed}

    def _same_file_urls(self, conn, url: str) -> list[str]:
        row = conn.execute("SELECT body FROM responses WHERE method='GET' AND status=200 AND url=?",
                           (url,)).fetchone()
        if row is None or row["body"] is None:
            raise KeyError(url)
        return [r["url"] for r in conn.execute(
            "SELECT url FROM responses WHERE method='GET' AND status=200 AND body=?", (row["body"],))]

    def replace_image(self, url: str, data: bytes) -> int:
        """Replace an image everywhere it occurs, keeping the original for restore_image()."""
        conn = self.connect()
        try:
            urls = self._same_file_urls(conn, url)
            for u in urls:
                backup = self._original_path(u)
                if not _win_path(backup).exists():
                    row = conn.execute("SELECT body FROM responses WHERE method='GET' AND status=200 AND url=?",
                                       (u,)).fetchone()
                    _write(backup, row["body"])
                self.save_entry_bytes(u, data, conn)
            return len(urls)
        finally:
            conn.close()

    def replace_sdf_texture(self, url: str, rgba: bytes) -> int:
        """Replace an RGBA8 KTX2 distance-field texture (logos, icons) with new pixels."""
        conn = self.connect()
        try:
            row = conn.execute("SELECT body FROM responses WHERE method='GET' AND status=200 AND url=?",
                               (url,)).fetchone()
        finally:
            conn.close()
        if row is None or row["body"] is None:
            raise KeyError(url)
        body = bytes(row["body"])
        info = _ktx2_info(body)
        if not info or len(rgba) != info["width"] * info["height"] * 4:
            raise ValueError("This texture cannot be replaced with these pixels.")
        out = bytearray(body[:info["offset"]])
        struct.pack_into("<I", out, 44, 0)                                     # no supercompression
        struct.pack_into("<3Q", out, 80, info["offset"], len(rgba), len(rgba))  # level 0
        out[info["dfd"] + 20] = 4                                              # bytes per texel
        return self.replace_image(url, bytes(out) + rgba)

    def restore_image(self, url: str) -> int:
        conn, restored = self.connect(), 0
        try:
            for u in self._same_file_urls(conn, url):
                backup = _win_path(self._original_path(u))
                if backup.exists():
                    self.save_entry_bytes(u, backup.read_bytes(), conn)
                    backup.unlink()
                    restored += 1
            return restored
        finally:
            conn.close()

    def write_launcher(self) -> None:
        """Make the archive folder a self-contained package that works without the app:
        a copy of the (stdlib-only) replay engine, 'Open website.bat' and an index.html."""
        import shutil
        from .util import path_query, preferred_port

        engine = self.dir / "_engine" / "replicator"
        engine.mkdir(parents=True, exist_ok=True)
        for src in Path(__file__).resolve().parent.glob("*.py"):
            shutil.copy2(src, engine / src.name)

        bat = """@echo off
chcp 65001 >nul
title Offline website - close this window to stop it
cd /d "%~dp0"
set "PYTHONPATH=%~dp0_engine"
set "PY="
if exist "%LOCALAPPDATA%\\WebsiteReplicator\\venv\\Scripts\\python.exe" set "PY=%LOCALAPPDATA%\\WebsiteReplicator\\venv\\Scripts\\python.exe"
if not defined PY ( where py >nul 2>nul && set "PY=py" )
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
    echo Python is needed to show this offline website.
    echo Install it from https://www.python.org/downloads/ and run this file again.
    pause
    exit /b 1
)
echo Starting the offline website... (keep this window open while you browse)
"%PY%" -m replicator serve "%~dp0."
pause
"""
        (self.dir / "Open website.bat").write_bytes(bat.replace("\n", "\r\n").encode("utf-8"))
        old = self.dir / "PLAY.bat"
        if old.exists():
            old.unlink()

        meta = self.meta()
        start = meta.get("start_url", "")
        port = preferred_port(self.name)
        title = (meta.get("title") or self.name).replace("<", "&lt;")
        html = INDEX_HTML.replace("__TITLE__", title).replace("__NAME__", json.dumps(self.name)) \
            .replace("__PORT__", str(port)).replace("__TO__", json.dumps(path_query(start) if start else "/")) \
            .replace("__URL__", start.replace("<", "&lt;"))
        (self.dir / "index.html").write_text(html, encoding="utf-8")
        (self.dir / "README.txt").write_text(
            "Open website.bat - double-click to open this website offline (keep its window open)\n"
            "index.html       - opens the website if 'Open website.bat' is running, otherwise explains what to do\n"
            "files/           - every captured file (HTML, JS, CSS, 3D models, WASM, images...)\n"
            "files/_index.tsv - which original URL each file came from\n"
            "archive.db       - the full archive (bodies + HTTP headers) that the offline server uses\n"
            "_engine/         - the small offline server (Python standard library only)\n\n"
            "This whole folder is the package: zip it, move it or copy it to another PC.\n"
            "Only Python is needed there (python.org).\n\n"
            "Why not just open files/.../index.html? Browsers block JavaScript modules, WebAssembly\n"
            "and fetch() on file:// pages, so modern sites must be served over http://.\n",
            encoding="utf-8")

    def export_single_html(self, out_path: Path, log=print) -> Path:
        """Write the whole site into one .html file that opens by double-click."""
        from .singlefile import export_single_html
        return export_single_html(self, out_path, log=log)

    def export_package(self, zip_path: Path, log=print) -> Path:
        """Zip the whole self-contained package (everything in the archive folder)."""
        import zipfile
        self.write_launcher()
        c = self.connect()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # make archive.db complete on its own
        c.close()
        zip_path = Path(zip_path)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in sorted(self.dir.rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(("-wal", "-shm")):
                    z.write("\\\\?\\" + str(p.resolve()) if os.name == "nt" else p,
                            Path(self.name) / p.relative_to(self.dir))
        log(f"Package saved: {zip_path} ({zip_path.stat().st_size / 1048576:.1f} MB)")
        return zip_path


INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ (offline copy)</title>
<style>
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f1115;color:#e6e6e6;
       font:16px/1.5 system-ui,Segoe UI,sans-serif}
  main{max-width:560px;padding:32px}
  h1{font-size:24px;margin:0 0 4px} .url{color:#8a93a6;font-size:13px;word-break:break-all}
  .card{margin-top:24px;padding:20px;border:1px solid #2a2f3a;border-radius:12px;background:#161a21}
  a.btn{display:inline-block;margin-top:12px;padding:10px 18px;border-radius:8px;background:#3d7bfd;color:#fff;
        text-decoration:none;font-weight:600} #st{color:#8a93a6}
  code{background:#232834;padding:2px 6px;border-radius:4px}
</style></head><body><main>
<h1>__TITLE__</h1><div class="url">Offline copy of __URL__</div>
<div class="card">
  <div id="st">Looking for the offline server...</div>
  <p>1. Double-click <code>Open website.bat</code> in this folder (keep its window open).<br>
     2. The website opens by itself. This page also jumps there as soon as the server is running.</p>
  <a class="btn" id="go" href="#">Open the website</a>
</div></main>
<script>
const BASE = 'http://127.0.0.1:__PORT__', NAME = __NAME__, TO = __TO__;
const target = BASE + '/__boot?to=' + encodeURIComponent(TO);
document.getElementById('go').href = target;
window.__replicator_pong = n => { if (n === NAME) location.replace(target); };
function ping() {   // a <script> tag works from file:// where fetch() is blocked
  const s = document.createElement('script');
  s.src = BASE + '/__replicator/ping.js?t=' + Date.now();
  s.onerror = () => { document.getElementById('st').textContent = 'The offline server is not running yet.'; s.remove(); };
  s.onload = () => s.remove();
  document.head.appendChild(s);
}
ping(); setInterval(ping, 1500);
</script></body></html>
"""

def _ktx2_info(body: bytes) -> dict | None:
    """Header of a single-image RGBA8 KTX2 file, or None for anything else."""
    if len(body) < 104 or body[:12] != b"\xabKTX 20\xbb\r\n\x1a\n":
        return None
    vk, _, width, height, depth, layers, faces, levels, scheme = struct.unpack_from("<9I", body, 12)
    if vk not in (37, 43) or depth or layers or faces != 1 or levels > 1 or scheme not in (0, 2):
        return None
    offset, length, _ = struct.unpack_from("<3Q", body, 80)
    dfd = struct.unpack_from("<I", body, 48)[0]
    if not width or not height or offset + length > len(body):
        return None
    return {"width": width, "height": height, "scheme": scheme, "offset": offset, "length": length, "dfd": dfd}


def _ktx2_pixels(body: bytes, info: dict) -> bytes | None:
    data = body[info["offset"]:info["offset"] + info["length"]]
    if info["scheme"] == 2:
        try:
            from compression import zstd  # Python 3.14+
        except ImportError:
            return None
        try:
            data = zstd.decompress(data)
        except zstd.ZstdError:
            return None
    return data if len(data) == info["width"] * info["height"] * 4 else None


def _gray_png(width: int, height: int, rows: bytes) -> str:
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\0" + rows[y * width:(y + 1) * width] for y in range(height))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _sdf_shape(body: bytes) -> dict | None:
    """Recognise a small distance-field logo/icon: preview plus the box its shape fills."""
    info = _ktx2_info(body)
    if not info or max(info["width"], info["height"]) > 256:
        return None
    pixels = _ktx2_pixels(body, info)
    if pixels is None:
        return None
    w, h = info["width"], info["height"]
    median = bytearray(w * h)
    for i in range(w * h):
        r, g, b = pixels[i * 4], pixels[i * 4 + 1], pixels[i * 4 + 2]
        median[i] = max(min(r, g), min(max(r, g), b))
    if max(median) < 200 or sum(1 for m in median if m <= 8) < w * h * 0.3:
        return None  # noise, colour data or a texture with no empty background
    upright, xs, ys = bytearray(w * h), [], []
    for row in range(h):  # GPU data textures are stored bottom row first
        y = h - 1 - row
        for x in range(w):
            if median[row * w + x] > 127:
                upright[y * w + x] = 255
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return {"width": w, "height": h, "bbox": [min(xs), min(ys), max(xs) + 1, max(ys) + 1],
            "preview": _gray_png(w, h, bytes(upright))}


_INLINE_SCRIPT = re.compile(r"(<script\b(?![^>]*\bsrc\s*=)[^>]*>)(.*?)(</script\s*>)", re.I | re.S)
_BAD_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
TEXT_MIMES = ("text/", "javascript", "ecmascript", "json", "xml", "svg", "xhtml", "css", "html")


def is_editable_text(mime: str, url: str) -> bool:
    m = (mime or "").lower()
    if any(t in m for t in TEXT_MIMES):
        return True
    path = url.split("?", 1)[0].lower()
    return path.endswith((".html", ".htm", ".js", ".mjs", ".css", ".json", ".svg", ".xml", ".txt"))


def _win_path(path: Path) -> Path:
    return Path("\\\\?\\" + str(path)) if os.name == "nt" and not str(path).startswith("\\\\?\\") else path


def _write(path: Path, data: bytes) -> None:
    p = _win_path(path)  # tolerate long paths
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _shorten(seg: str, keep: int) -> str:
    if len(seg) <= keep:
        return seg
    stem, ext = os.path.splitext(seg)
    h = hashlib.sha1(seg.encode()).hexdigest()[:6]
    return f"{stem[:max(1, keep - len(ext) - 7)]}~{h}{ext}"


def _candidate_paths(url: str, budget: int):
    """Readable path first, then progressively shorter ones that always fit `budget` chars."""
    full = _local_path(url)
    if len(str(full)) <= budget:
        yield full
    parts = list(full.parts)
    short = Path(*[_shorten(p, 24) for p in parts[:-1]], _shorten(parts[-1], 40))
    if len(str(short)) <= budget:
        yield short
    name = parts[-1]
    stem, ext = os.path.splitext(name)
    h = hashlib.sha1(url.encode()).hexdigest()[:10]
    room = max(0, budget - len("_long/") - len(h) - len(ext) - 1)
    yield Path("_long", f"{h}-{stem[:room]}{ext}" if room else f"{h}{ext}")


def _local_path(url: str) -> Path:
    s = urlsplit(url)
    host = _BAD_CHARS.sub("_", s.netloc) or "_"
    path = unquote(s.path or "/")
    if path.endswith("/"):
        path += "index.html"
    parts = [_BAD_CHARS.sub("_", p)[:120].rstrip(". ") or "_" for p in path.split("/")
             if p not in ("", ".", "..")]
    parts = [f"_{p}" if p.split(".")[0].lower() in _RESERVED else p for p in parts]
    if not parts:
        parts = ["index.html"]
    if s.query:
        stem, ext = os.path.splitext(parts[-1])
        parts[-1] = f"{stem}~{hashlib.sha1(s.query.encode()).hexdigest()[:8]}{ext}"
    return Path(host, *parts)
