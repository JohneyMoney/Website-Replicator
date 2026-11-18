"""Built-in inspector, full-archive search & editor for captured files and HTTP exchanges."""
from __future__ import annotations

import os
import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Callable

from .store import Archive, _win_path, is_editable_text
from .text_replace import safe_replace_in_content
from .util import human_size

MAX_EDIT_BYTES = 2 * 1024 * 1024  # keep Tkinter responsive on huge minified bundles


class ArchiveEditor(tk.Toplevel):
    def __init__(self, master: tk.Misc, archive: Archive, log: Callable[[str], None] = print):
        super().__init__(master)
        self.archive = archive
        self.log = log
        self.title(f"Search & edit files — {archive.name}")
        self.geometry("1160x760")
        self.minsize(880, 560)

        self.entries: list[dict] = []
        self.search_results: list[dict] = []
        self.search_mode = False
        self._search_seq = 0
        self._done_q: queue.Queue = queue.Queue()
        self.current: dict | None = None
        self.current_iid: str | None = None
        self.current_encoding = "utf-8"
        self.dirty = False

        self.v_query = tk.StringVar()
        self.v_kind = tk.StringVar(value="Editable text")
        self.v_search_all = tk.StringVar()
        self.v_list_info = tk.StringVar(value="")
        self.v_find = tk.StringVar()
        self.v_replace = tk.StringVar()
        self.v_case = tk.BooleanVar(value=False)
        self.v_text_only = tk.BooleanVar(value=True)
        self.v_url = tk.StringVar(value="Select a file on the left, or search across all captured data")
        self.v_status = tk.StringVar(value="")

        self._build()
        self._load_entries()
        self.after(50, self._drain_jobs)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _drain_jobs(self) -> None:
        while True:
            try:
                fn = self._done_q.get_nowait()
            except queue.Empty:
                break
            fn()
        self.after(50, self._drain_jobs)

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=8)

        # ---- left: filter + full-archive search + list
        left = ttk.Frame(paned)
        paned.add(left, weight=1)

        fbar = ttk.Frame(left)
        fbar.pack(fill="x", pady=(0, 4))
        ttk.Label(fbar, text="Filter:").pack(side="left")
        q_entry = ttk.Entry(fbar, textvariable=self.v_query)
        q_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.v_query.trace_add("write", lambda *_: self._apply_filter())

        kind_cb = ttk.Combobox(
            fbar, textvariable=self.v_kind, state="readonly", width=13,
            values=("Editable text", "HTML", "JS", "CSS", "All files"),
        )
        kind_cb.pack(side="left")
        kind_cb.bind("<<ComboboxSelected>>", lambda _e: self._apply_filter())

        sbar = ttk.Frame(left)
        sbar.pack(fill="x", pady=(0, 4))
        ttk.Label(sbar, text="Search all:").pack(side="left")
        s_entry = ttk.Entry(sbar, textvariable=self.v_search_all)
        s_entry.pack(side="left", fill="x", expand=True, padx=4)
        s_entry.bind("<Return>", lambda _e: self.run_search())
        ttk.Button(sbar, text="Search", command=self.run_search).pack(side="left", padx=(0, 2))
        ttk.Button(sbar, text="Clear", command=self.clear_search).pack(side="left", padx=(0, 2))
        ttk.Button(sbar, text="Replace all...", command=self.open_replace_all_dialog).pack(side="left", padx=(0, 2))
        ttk.Button(sbar, text="Remove word...", command=lambda: self.open_replace_all_dialog(delete_mode=True)).pack(side="left")

        ttk.Label(left, textvariable=self.v_list_info).pack(fill="x", pady=(0, 4))

        tree_wrap = ttk.Frame(left)
        tree_wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_wrap, columns=("type", "size"), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="URL / local path")
        self.tree.heading("type", text="Type / Match")
        self.tree.heading("size", text="Size")
        self.tree.column("#0", width=310)
        self.tree.column("type", width=110)
        self.tree.column("size", width=70, anchor="e")
        sb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # ---- right: toolbar + editor
        right = ttk.Frame(paned)
        paned.add(right, weight=3)

        top = ttk.Frame(right)
        top.pack(fill="x", pady=(0, 4))
        ttk.Entry(top, textvariable=self.v_url, state="readonly").pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.btn_save = ttk.Button(top, text="Save (Ctrl+S)", command=self.save_current, state="disabled")
        self.btn_save.pack(side="left", padx=2)
        ttk.Button(top, text="Open externally", command=self.open_external).pack(side="left", padx=2)
        ttk.Button(top, text="Overrides...", command=self.open_overrides_dialog).pack(side="left", padx=2)
        ttk.Button(top, text="Reload", command=self._reload_current).pack(side="left", padx=2)

        find_bar = ttk.Frame(right)
        find_bar.pack(fill="x", pady=(0, 4))
        ttk.Label(find_bar, text="Find:").pack(side="left")
        f_entry = ttk.Entry(find_bar, textvariable=self.v_find, width=16)
        f_entry.pack(side="left", padx=4)
        f_entry.bind("<Return>", lambda _e: self.find_next())
        ttk.Button(find_bar, text="Next", command=self.find_next).pack(side="left")
        ttk.Label(find_bar, text="Replace:").pack(side="left", padx=(8, 0))
        r_entry = ttk.Entry(find_bar, textvariable=self.v_replace, width=16)
        r_entry.pack(side="left", padx=4)
        r_entry.bind("<Return>", lambda _e: self.replace_one())
        ttk.Button(find_bar, text="Replace", command=self.replace_one).pack(side="left", padx=(0, 2))
        ttk.Button(find_bar, text="In file", command=self.replace_in_file).pack(side="left", padx=(0, 2))
        ttk.Button(find_bar, text="In ALL files", command=self.replace_across_all).pack(side="left", padx=(0, 2))
        ttk.Checkbutton(find_bar, text="Case", variable=self.v_case).pack(side="left", padx=2)
        ttk.Checkbutton(find_bar, text="Text only", variable=self.v_text_only).pack(side="left", padx=2)

        self.text = ScrolledText(
            right, wrap="none", undo=True, font=("Consolas", 10),
            bg="#15171c", fg="#e6e6e6", insertbackground="#ffffff",
        )
        xsb = ttk.Scrollbar(right, orient="horizontal", command=self.text.xview)
        self.text.configure(xscrollcommand=xsb.set)
        self.text.pack(fill="both", expand=True)
        xsb.pack(fill="x")
        ttk.Label(right, textvariable=self.v_status).pack(fill="x", pady=(3, 0))
        self.text.tag_configure("match", background="#5c4d1a", foreground="#ffffff")
        self.text.bind("<<Modified>>", self._on_modified)

        self.bind("<Control-s>", lambda _e: self.save_current())
        self.bind("<Control-f>", lambda _e: (f_entry.focus_set(), f_entry.select_range(0, "end")))
        self.bind("<Control-h>", lambda _e: self.open_replace_all_dialog())
        self.bind("<Control-Shift-F>", lambda _e: (s_entry.focus_set(), s_entry.select_range(0, "end")))

    # -------------------------------------------------------------- loading
    def _load_entries(self) -> None:
        self.entries = self.archive.list_entries()
        if not self.search_mode:
            self._apply_filter()

    def _apply_filter(self) -> None:
        if self.search_mode:
            return
        q = self.v_query.get().strip().lower()
        kind = self.v_kind.get()
        self.tree.delete(*self.tree.get_children())
        shown = 0
        for idx, e in enumerate(self.entries):
            mime = e["mime"].lower()
            url = e["url"].lower()
            if kind == "Editable text" and not is_editable_text(mime, url):
                continue
            if kind == "HTML" and "html" not in mime and not url.split("?")[0].endswith((".html", ".htm")):
                continue
            if kind == "JS" and "javascript" not in mime and not url.split("?")[0].endswith((".js", ".mjs")):
                continue
            if kind == "CSS" and "css" not in mime and not url.split("?")[0].endswith(".css"):
                continue
            hay = f"{e['url']} {e['local_rel']} {e['mime']}".lower()
            if q and q not in hay:
                continue
            label = e["local_rel"] or e["url"]
            short_mime = e["mime"].split(";", 1)[0].strip() or e["resource_type"]
            self.tree.insert("", "end", iid=f"f:{idx}", text=label, values=(short_mime, human_size(e["body_size"])))
            shown += 1
        self.v_list_info.set(f"Showing {shown} of {len(self.entries)} GET 200 file(s)")

    # ------------------------------------------------------- archive search
    def run_search(self) -> None:
        q = self.v_search_all.get().strip()
        if not q:
            self.clear_search()
            return
        self._search_seq += 1
        seq = self._search_seq
        self.search_mode = True
        self.v_list_info.set(f"Searching all captured files & exchanges for '{q}'...")

        def worker() -> None:
            try:
                res = self.archive.search_contents(q)
                err = None
            except Exception as exc:
                res, err = [], str(exc)
            self._done_q.put(lambda: self._on_search_done(seq, q, res, err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_search_done(self, seq: int, q: str, results: list[dict], err: str | None) -> None:
        if seq != self._search_seq or not self.search_mode:
            return
        if err:
            self.v_list_info.set(f"Search error: {err}")
            return
        self.search_results = results
        self.tree.delete(*self.tree.get_children())
        for idx, r in enumerate(results):
            prefix = "" if (r["method"] == "GET" and r["status"] == 200) else f"[{r['method']} {r['status']}] "
            label = prefix + (r["local_rel"] or r["url"])
            info = f"{r['matches']}x ({r['where']})"
            self.tree.insert("", "end", iid=f"s:{idx}", text=label, values=(info, human_size(r["body_size"])))
        self.v_list_info.set(
            f"Search '{q}': {len(results)} match(es) across all files & exchanges"
            + (" (capped at 300)" if len(results) >= 300 else "")
        )

    def clear_search(self) -> None:
        self._search_seq += 1
        self.search_mode = False
        self.search_results = []
        self.v_search_all.set("")
        self._apply_filter()

    # ------------------------------------------------------------ selection
    def _on_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        kind, _, idx_str = iid.partition(":")
        entry = self.search_results[int(idx_str)] if kind == "s" else self.entries[int(idx_str)]
        if self.current and entry.get("id") == self.current.get("id") and entry["url"] == self.current["url"]:
            return
        if not self._confirm_discard():
            if self.current_iid and self.tree.exists(self.current_iid):
                self.tree.selection_set(self.current_iid)
            return
        self.current_iid = iid
        if kind == "s" and not entry.get("editable", True):
            self._open_exchange(entry)
        else:
            self._open_entry(entry)
        if kind == "s" and self.v_search_all.get().strip():
            self.v_find.set(self.v_search_all.get().strip())
            self.text.mark_set("insert", "1.0")
            self.find_next()

    def _open_entry(self, entry: dict) -> None:
        self.current = entry
        self.v_url.set(entry["url"])
        raw = self.archive.read_entry_bytes(entry["url"])
        if len(raw) > MAX_EDIT_BYTES:
            self._set_readonly_notice(
                f"File is {human_size(len(raw))} (over 2 MB limit for the built-in editor).\n"
                "Click 'Open externally' to edit it in VS Code / Notepad."
            )
            return
        if not is_editable_text(entry["mime"], entry["url"]) and b"\x00" in raw[:4096]:
            self._set_readonly_notice(
                f"Binary file ({entry['mime'] or 'unknown type'}, {human_size(len(raw))}).\n"
                "Use 'Open externally' if you want to inspect or replace it on disk."
            )
            return
        try:
            text = raw.decode("utf-8")
            self.current_encoding = "utf-8"
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
            self.current_encoding = "latin-1"

        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.edit_reset()
        self.text.edit_modified(False)
        self.dirty = False
        self.btn_save.configure(state="normal")
        where = f"files/{entry['local_rel']}" if entry.get("local_rel") else "archive.db"
        self.v_status.set(f"{human_size(len(raw))} · {self.current_encoding} · {where}")

    def _open_exchange(self, entry: dict) -> None:
        """Read-only inspector for non-GET-200 exchanges (POST, 3xx/4xx/5xx, etc.)."""
        self.current = entry
        ex = self.archive.get_exchange(entry["id"])
        if not ex:
            self._set_readonly_notice("Exchange not found in archive.db.")
            return
        self.v_url.set(f"[{ex['method']} {ex['status']}] {ex['url']}")
        lines = [
            f"=== {ex['method']} {ex['url']} ===",
            f"Status: {ex['status']} {ex['status_text']}".strip(),
            f"MIME:   {ex['mime']}",
            "",
            "--- Request Headers ---",
            *[f"{k}: {v}" for k, v in ex["req_headers"]],
        ]
        if ex["post_data"]:
            lines += ["", "--- Request Body ---",
                      ex["post_data"][:MAX_EDIT_BYTES].decode("utf-8", errors="replace")]
        lines += [
            "",
            "--- Response Headers ---",
            *[f"{k}: {v}" for k, v in ex["resp_headers"]],
            "",
            "--- Response Body ---",
            ex["body"][:MAX_EDIT_BYTES].decode("utf-8", errors="replace") if ex["body"] else "(empty)",
        ]
        self._set_readonly_notice("\n".join(lines), status=f"Exchange #{ex['id']} (read-only)")

    def _set_readonly_notice(self, msg: str, status: str = "Read-only preview") -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", msg)
        self.text.edit_modified(False)
        self.text.configure(state="disabled")
        self.dirty = False
        self.btn_save.configure(state="disabled")
        self.v_status.set(status)

    def _on_modified(self, _event=None) -> None:
        if self.text.edit_modified():
            self.dirty = True
            self.v_status.set("Unsaved changes (Ctrl+S to save)")
            self.text.edit_modified(False)

    # -------------------------------------------------------------- actions
    def save_current(self) -> None:
        if not self.current or str(self.btn_save["state"]) == "disabled":
            return
        content = self.text.get("1.0", "end-1c")
        data = content.encode(self.current_encoding, errors="replace")
        self.archive.save_entry_bytes(self.current["url"], data)
        self.current["body_size"] = len(data)
        if self.current_iid and self.tree.exists(self.current_iid):
            vals = list(self.tree.item(self.current_iid, "values"))
            if len(vals) >= 2:
                vals[1] = human_size(len(data))
                self.tree.item(self.current_iid, values=vals)
        self.dirty = False
        self.text.edit_modified(False)
        self.v_status.set(f"Saved ({human_size(len(data))}) — reload browser (F5) to see changes")
        self.log(f"Saved edit: {self.current['url']}")

    def _reload_current(self) -> None:
        if not self.current or not self._confirm_discard():
            return
        if self.current_iid and self.current_iid.startswith("s:") and not self.current.get("editable", True):
            self._open_exchange(self.current)
        else:
            self._open_entry(self.current)

    def find_next(self) -> None:
        needle = self.v_find.get()
        if not needle:
            return
        self.text.tag_remove("match", "1.0", "end")
        nocase = not self.v_case.get()
        start = self.text.index("insert + 1c")
        pos = self.text.search(needle, start, stopindex="end", nocase=nocase)
        if not pos:
            pos = self.text.search(needle, "1.0", stopindex="end", nocase=nocase)
        if not pos:
            self.v_status.set(f"Not found: {needle}")
            return
        end = f"{pos}+{len(needle)}c"
        self.text.tag_add("match", pos, end)
        self.text.mark_set("insert", pos)
        self.text.see(pos)

    def replace_one(self) -> None:
        needle = self.v_find.get()
        if not needle or str(self.btn_save["state"]) == "disabled":
            return
        repl = self.v_replace.get()
        ranges = self.text.tag_ranges("match")
        if ranges:
            s, e = ranges[0], ranges[1]
            cur = self.text.get(s, e)
            ok = (cur == needle) if self.v_case.get() else (cur.lower() == needle.lower())
            if ok:
                self.text.delete(s, e)
                self.text.insert(s, repl)
                self.text.mark_set("insert", f"{s}+{len(repl)}c")
        self.find_next()

    def replace_in_file(self) -> None:
        needle = self.v_find.get()
        if not needle or str(self.btn_save["state"]) == "disabled":
            return
        repl = self.v_replace.get()
        content = self.text.get("1.0", "end-1c")
        if self.v_text_only.get() and self.current:
            new_content, count = safe_replace_in_content(
                content, needle, repl,
                mime=self.current.get("mime", ""), url=self.current.get("url", ""),
                case_sensitive=self.v_case.get(),
            )
        elif self.v_case.get():
            count = content.count(needle)
            new_content = content.replace(needle, repl) if count else content
        else:
            new_content, count = re.subn(re.escape(needle), lambda _m: repl, content, flags=re.IGNORECASE)
        if not count:
            self.v_status.set(f"No replaceable text matches for: {needle}")
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", new_content)
        self.dirty = True
        self.v_status.set(f"Replaced {count} in file (Ctrl+S to save)")

    def _run_replace_all(
        self, find_s: str, repl_s: str, *, case_sensitive: bool, file_type: str,
        safe_text_only: bool = True, scope_label: str = "All captured files",
        parent: tk.Misc | None = None,
    ) -> bool:
        if not find_s or not self._confirm_discard():
            return False
        mode_note = "Text only (protects filenames, URLs, tags & code)" if safe_text_only else "RAW (replaces everywhere, including filenames/URLs)"
        if not messagebox.askyesno(
            "Confirm Replace All",
            f"Replace ALL occurrences of:\n  {find_s!r}\nwith:\n  {repl_s!r}\n\n"
            f"Scope: {scope_label}\nMode: {mode_note}\n\nThis updates archive.db and files/.",
            parent=parent or self,
        ):
            return False
        self.v_find.set(find_s)
        self.v_replace.set(repl_s)
        self.v_case.set(case_sensitive)
        self.v_text_only.set(safe_text_only)
        self.v_status.set(f"Replacing {find_s!r} across {scope_label}...")
        self.v_list_info.set(f"Replacing {find_s!r} across {scope_label}...")

        def worker() -> None:
            try:
                res = self.archive.replace_all(
                    find_s, repl_s, case_sensitive=case_sensitive,
                    file_type=file_type, safe_text_only=safe_text_only,
                )
                err = None
            except Exception as exc:
                res, err = None, str(exc)
            self._done_q.put(lambda: self._on_replace_all_done(find_s, repl_s, res, err))

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _on_replace_all_done(self, find_s: str, repl_s: str, res: dict | None, err: str | None) -> None:
        if err or res is None:
            msg = f"Replace failed: {err}"
            self.v_status.set(msg)
            self.v_list_info.set(msg)
            messagebox.showerror("Replace all", msg, parent=self)
            return
        self._load_entries()
        if self.search_mode and self.v_search_all.get().strip():
            self.run_search()
        if self.current and self.current["url"] in res["urls"]:
            if self.current_iid and self.current_iid.startswith("s:") and not self.current.get("editable", True):
                self._open_exchange(self.current)
            else:
                self._open_entry(self.current)
        msg = (f"Replaced {res['replacements']} occurrence(s) across {res['files_changed']} file(s) "
               "— reload browser (F5)")
        self.v_status.set(msg)
        self.v_list_info.set(msg)
        self.log(f"Replace all ({find_s!r} -> {repl_s!r}): {res['replacements']} in {res['files_changed']} file(s)")

    def replace_across_all(self) -> None:
        find_s = self.v_find.get()
        if not find_s:
            self.open_replace_all_dialog()
            return
        self._run_replace_all(
            find_s, self.v_replace.get(),
            case_sensitive=self.v_case.get(), file_type="all",
            safe_text_only=self.v_text_only.get(),
        )

    def open_replace_all_dialog(self, delete_mode: bool = False) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("Remove word from ALL captured files" if delete_mode else "Replace in ALL captured files")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)

        v_find = tk.StringVar(value=self.v_find.get() or self.v_search_all.get())
        v_repl = tk.StringVar(value="" if delete_mode else self.v_replace.get())
        v_scope = tk.StringVar(value="All captured files")
        v_case = tk.BooleanVar(value=self.v_case.get())
        v_text_only = tk.BooleanVar(value=self.v_text_only.get())

        pad = dict(padx=8, pady=5)
        ttk.Label(dlg, text="Word to remove:" if delete_mode else "Find:").grid(row=0, column=0, sticky="w", **pad)
        e_find = ttk.Entry(dlg, textvariable=v_find, width=42)
        e_find.grid(row=0, column=1, columnspan=2, sticky="ew", **pad)
        if not delete_mode:
            ttk.Label(dlg, text="Replace with:").grid(row=1, column=0, sticky="w", **pad)
            ttk.Entry(dlg, textvariable=v_repl, width=42).grid(row=1, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Label(dlg, text="Files:").grid(row=2, column=0, sticky="w", **pad)
        ttk.Combobox(
            dlg, textvariable=v_scope, state="readonly", width=22,
            values=("All captured files", "HTML", "JS", "CSS"),
        ).grid(row=2, column=1, sticky="w", **pad)
        ttk.Checkbutton(dlg, text="Case sensitive", variable=v_case).grid(row=2, column=2, sticky="e", **pad)
        ttk.Checkbutton(
            dlg, text="Text only (protect filenames, URLs, HTML tags & code)", variable=v_text_only,
        ).grid(row=3, column=0, columnspan=3, sticky="w", **pad)

        scope_map = {"All captured files": "all", "HTML": "html", "JS": "js", "CSS": "css"}

        def go(remove: bool = delete_mode) -> None:
            find_s = v_find.get()
            if not find_s:
                messagebox.showwarning("Remove / Replace", "Enter a word or text to find.", parent=dlg)
                return
            if self._run_replace_all(
                find_s, "" if remove else v_repl.get(), case_sensitive=v_case.get(),
                file_type=scope_map[v_scope.get()], safe_text_only=v_text_only.get(),
                scope_label=v_scope.get(), parent=dlg,
            ):
                dlg.destroy()

        e_find.bind("<Return>", lambda _e: go(delete_mode))
        btns = ttk.Frame(dlg)
        btns.grid(row=4, column=0, columnspan=3, sticky="e", padx=8, pady=(4, 10))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right", padx=(4, 0))
        if delete_mode:
            ttk.Button(btns, text="Remove from ALL files", command=lambda: go(True)).pack(side="right")
        else:
            ttk.Button(btns, text="Replace in ALL files", command=lambda: go(False)).pack(side="right", padx=(4, 0))
            ttk.Button(btns, text="Remove from ALL files", command=lambda: go(True)).pack(side="right")
        e_find.focus_set()
        e_find.select_range(0, "end")

    def open_external(self) -> None:
        if not self.current:
            return
        p = self.archive.exported_path_for(self.current["url"])
        if p is None or not _win_path(p).is_file():
            self.archive.export_files(log=self.log)
            self._load_entries()
            p = self.archive.exported_path_for(self.current["url"])
        if p is not None and _win_path(p).is_file():
            os.startfile(str(p))
            self.v_status.set("Opened externally — save there and press F5 in browser")
        else:
            messagebox.showinfo("Edit files", "Could not export this URL to files/.", parent=self)

    def open_overrides_dialog(self) -> None:
        OverridesDialog(self, self.archive, log=self.log)

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        res = messagebox.askyesnocancel("Unsaved changes", "Save changes before switching?", parent=self)
        if res is None:
            return False
        if res:
            self.save_current()
        return True

    def _on_close(self) -> None:
        if self._confirm_discard():
            self.destroy()


class OverridesDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, archive: Archive, log: Callable[[str], None] = print):
        super().__init__(master)
        self.archive = archive
        self.log = log
        self.title(f"Overrides & Function Mocks — {archive.name}")
        self.geometry("740x540")
        self.minsize(580, 400)
        self.transient(master)

        cur = archive.get_overrides()

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 6))

        # Tab 1: CSS
        f_css = ttk.Frame(nb)
        nb.add(f_css, text="CSS Overrides (Hidden Elements)")
        ttk.Label(
            f_css,
            text="CSS rules injected into every page (e.g. .popup, #banner { display: none !important; }):",
        ).pack(anchor="w", padx=8, pady=(8, 4))
        self.t_css = ScrolledText(
            f_css, font=("Consolas", 10), bg="#15171c", fg="#e6e6e6", insertbackground="#fff", wrap="none"
        )
        self.t_css.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.t_css.insert("1.0", cur.get("css", ""))

        # Tab 2: JS
        f_js = ttk.Frame(nb)
        nb.add(f_js, text="JS Function Mocks")
        ttk.Label(
            f_js,
            text="JavaScript executed BEFORE any page scripts load (use to stub out functions):",
        ).pack(anchor="w", padx=8, pady=(8, 4))
        self.t_js = ScrolledText(
            f_js, font=("Consolas", 10), bg="#15171c", fg="#e6e6e6", insertbackground="#fff", wrap="none"
        )
        self.t_js.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.t_js.insert(
            "1.0",
            cur.get("js", "")
            or "// Stub out functions before page scripts load:\n// window.someFunction = () => {};\n// window.telemetry = () => {};\n",
        )

        # Tab 3: Blocked
        f_blk = ttk.Frame(nb)
        nb.add(f_blk, text="Blocked Scripts & URLs")
        ttk.Label(
            f_blk,
            text="URLs matching these patterns will be answered with an empty 200 OK (one per line):",
        ).pack(anchor="w", padx=8, pady=(8, 4))
        self.t_blk = ScrolledText(
            f_blk, font=("Consolas", 10), bg="#15171c", fg="#e6e6e6", insertbackground="#fff", wrap="none"
        )
        self.t_blk.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.t_blk.insert("1.0", "\n".join(cur.get("blocked", [])))

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=(4, 0))
        ttk.Button(btns, text="Save Overrides", command=self.save).pack(side="right")

    def save(self):
        css = self.t_css.get("1.0", "end-1c").strip()
        js = self.t_js.get("1.0", "end-1c").strip()
        blk = [line.strip() for line in self.t_blk.get("1.0", "end-1c").splitlines() if line.strip()]
        self.archive.save_overrides(
            css=css + ("\n" if css else ""),
            js=js + ("\n" if js else ""),
            blocked=blk,
        )
        self.log(f"Saved overrides for '{self.archive.name}'")
        self.destroy()

