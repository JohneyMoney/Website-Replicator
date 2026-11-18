"""Website Replicator - desktop GUI (Tkinter, no extra dependencies)."""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from tkinter import filedialog, messagebox, ttk

from replicator import theme
from replicator.store import Archive, default_root, list_archives
from replicator.util import human_size

APP_TITLE = "Website Replicator"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x780")
        self.minsize(1000, 680)
        self.log_q: queue.Queue[str] = queue.Queue()
        self.capture_stop: threading.Event | None = None
        self.capture_thread: threading.Thread | None = None
        self.servers: dict[str, object] = {}
        self._busy = False
        self._scan_x = 0
        self._build()
        self.refresh_archives()
        self.after(100, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ UI
    def _build(self):
        f = self.fonts = theme.apply_theme(self)
        self.icon = theme.app_icon(self)
        self.iconphoto(True, self.icon)
        self.bind_class("Toplevel", "<Map>", lambda e: theme.dark_titlebar(e.widget), add="+")
        self.bind("<Map>", lambda e: theme.dark_titlebar(self) if e.widget is self else None, add="+")

        self.v_url = tk.StringVar()
        self.v_name = tk.StringVar()
        self.v_depth = tk.IntVar(value=1)  # 1 = also save the pages the start page links to
        self.v_pages = tk.IntVar(value=25)
        self.v_wait = tk.DoubleVar(value=8.0)
        self.v_timeout = tk.DoubleVar(value=60.0)
        self.v_maxmb = tk.DoubleVar(value=512.0)
        self.v_show = tk.BooleanVar(value=False)   # headless is faster
        self.v_workers = tk.IntVar(value=8)
        self.v_scroll = tk.BooleanVar(value=True)
        self.v_click = tk.BooleanVar(value=True)

        # ---- header
        head = ttk.Frame(self)
        head.pack(fill="x", padx=18, pady=(14, 10))
        mark = tk.Canvas(head, width=34, height=34, bg=theme.INK, highlightthickness=0)
        mark.create_rectangle(2, 2, 23, 23, outline=theme.MUTED, width=2)
        mark.create_rectangle(11, 11, 32, 32, fill=theme.ACCENT, outline=theme.ACCENT)
        mark.pack(side="left", padx=(0, 11))
        tk.Label(head, text="WEBSITE REPLICATOR", bg=theme.INK, fg=theme.TEXT, font=f["title"]).pack(side="left")
        self.status = tk.Label(head, text="●  READY", bg=theme.RAISED, fg=theme.ACCENT, font=f["small"], padx=11, pady=5)
        self.status.pack(side="right")
        ttk.Button(head, text="↻  Refresh", style="Header.TButton", command=self.refresh_archives).pack(
            side="right", padx=(0, 8))
        self.summary = tk.Label(head, bg=theme.INK, fg=theme.MUTED, font=f["ui"])
        self.summary.pack(side="right", padx=8)

        # ---- capture
        cap = ttk.Frame(self, style="Panel.TFrame", padding=(14, 13, 14, 10))
        cap.pack(fill="x", padx=18)
        row = ttk.Frame(cap, style="Panel.TFrame")
        row.pack(fill="x")
        url_entry = ttk.Entry(row, textvariable=self.v_url, style="Hero.TEntry", font=f["hero"])
        url_entry.pack(side="left", fill="x", expand=True)
        url_entry.bind("<Return>", lambda _e: self.start_capture())
        self.url_entry = url_entry
        self.url_hint = tk.Label(row, text="Paste a website address to replicate", bg=theme.RAISED, fg=theme.MUTED,
                                 font=f["hero"], cursor="xterm")
        self.url_hint.place(in_=url_entry, x=13, rely=0.5, anchor="w")
        self.url_hint.bind("<Button-1>", lambda _e: url_entry.focus_set())
        self.v_url.trace_add("write", lambda *_: self._sync_hint())
        self.btn_capture = ttk.Button(row, text="Replicate", style="Accent.TButton", command=self.start_capture)
        self.btn_capture.pack(side="left", padx=(8, 0), ipady=2)
        self.btn_stop = ttk.Button(row, text="Stop", command=self.stop_capture, state="disabled")
        self.btn_stop.pack(side="left", padx=(6, 0), ipady=3)
        self.scan = tk.Canvas(cap, height=3, bg=theme.LINE, highlightthickness=0)
        self.scan.pack(fill="x", pady=(9, 6))
        self.scan_bar = self.scan.create_rectangle(0, 0, 0, 3, fill=theme.ACCENT, outline="")

        bar = ttk.Frame(cap, style="Panel.TFrame")
        bar.pack(fill="x")
        ttk.Label(bar, text="Name", style="Panel.TLabel").pack(side="left", padx=(0, 7))
        ttk.Entry(bar, textvariable=self.v_name, width=30).pack(side="left")
        self.btn_options = ttk.Button(bar, text="Options  ▾", style="Ghost.TButton", command=self._toggle_options)
        self.btn_options.pack(side="right")

        self.options = opt = ttk.Frame(cap, style="Panel.TFrame")
        spins = ((self.v_depth, 0, 5, 1, "Crawl depth"), (self.v_pages, 1, 500, 1, "Max pages"),
                 (self.v_wait, 0, 300, 1, "WebGL wait (s)"), (self.v_timeout, 10, 600, 5, "Timeout (s)"),
                 (self.v_maxmb, 1, 4096, 16, "Max asset MB"), (self.v_workers, 1, 16, 1, "Parallel tabs"))
        for i, (var, low, high, step, label) in enumerate(spins):
            ttk.Label(opt, text=label, style="Panel.TLabel").grid(row=0, column=i, sticky="w", padx=(0, 12), pady=(8, 3))
            ttk.Spinbox(opt, textvariable=var, from_=low, to=high, increment=step, width=9).grid(
                row=1, column=i, sticky="w", padx=(0, 12))
        checks = ttk.Frame(opt, style="Panel.TFrame")
        checks.grid(row=2, column=0, columnspan=6, sticky="w", pady=(8, 2))
        for var, label in ((self.v_show, "Show browser"), (self.v_scroll, "Auto-scroll and interact"),
                           (self.v_click, "Click every button, menu and tab")):
            ttk.Checkbutton(checks, text=label, variable=var).pack(side="left", padx=(0, 16))

        # ---- log (packed first so it keeps its height)
        logf = ttk.Frame(self, style="Panel.TFrame", padding=(12, 8))
        logf.pack(side="bottom", fill="x", padx=18, pady=(10, 16))
        ttk.Label(logf, text="ACTIVITY", style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        self.log_box = tk.Text(logf, height=5, font=f["mono"], state="disabled", bg=theme.PANEL, fg="#b9c1cf",
                               relief="flat", borderwidth=0, highlightthickness=0, wrap="word")
        log_bar = ttk.Scrollbar(logf, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=log_bar.set)
        log_bar.pack(side="right", fill="y")
        self.log_box.pack(side="left", fill="both", expand=True)
        self.log_box.tag_configure("bad", foreground=theme.DANGER)
        self.log_box.tag_configure("good", foreground=theme.ACCENT)
        self.log_box.tag_configure("time", foreground="#596273")

        # ---- archives and actions
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=18, pady=(10, 0))
        holder = ttk.Frame(body, style="Panel.TFrame", width=290)
        holder.pack(side="right", fill="y", padx=(10, 0))
        holder.pack_propagate(False)
        rail_view = tk.Canvas(holder, bg=theme.PANEL, highlightthickness=0, width=272)
        rail_bar = ttk.Scrollbar(holder, orient="vertical", command=rail_view.yview)
        rail_view.configure(yscrollcommand=rail_bar.set)
        rail_view.pack(side="left", fill="both", expand=True)
        rail = ttk.Frame(rail_view, style="Panel.TFrame", padding=11)
        rail_item = rail_view.create_window(0, 0, anchor="nw", window=rail)
        rail.columnconfigure((0, 1), weight=1, uniform="rail")

        def fit_rail(_event=None):
            rail_view.itemconfigure(rail_item, width=rail_view.winfo_width())
            needed, shown = rail.winfo_reqheight(), rail_view.winfo_height()
            rail_view.configure(scrollregion=(0, 0, rail_view.winfo_width(), needed))
            if needed > shown > 1:
                rail_bar.pack(side="right", fill="y", before=rail_view)
            else:
                rail_bar.pack_forget()
                rail_view.yview_moveto(0)

        def wheel(event):
            if rail.winfo_reqheight() > rail_view.winfo_height():
                rail_view.yview_scroll(-1 if event.delta > 0 else 1, "units")

        rail.bind("<Configure>", fit_rail)
        rail_view.bind("<Configure>", fit_rail)
        holder.bind("<Enter>", lambda _e: self.bind_all("<MouseWheel>", wheel))
        holder.bind("<Leave>", lambda _e: self.unbind_all("<MouseWheel>"))

        lst = ttk.Frame(body, style="Panel.TFrame", padding=(1, 1))
        lst.pack(side="left", fill="both", expand=True)
        cols = ("url", "files", "size", "date", "status")
        self.tree = ttk.Treeview(lst, columns=cols, show="tree headings", height=8, selectmode="browse")
        self.tree.heading("#0", text="  CAPTURED SITES", anchor="w")
        self.tree.column("#0", width=210, minwidth=130)
        for c, w, t, stretch in (("url", 220, "START URL", True), ("files", 54, "FILES", False),
                                 ("size", 76, "SIZE", False), ("date", 130, "CAPTURED", False),
                                 ("status", 120, "SERVER", False)):
            self.tree.heading(c, text=t, anchor="w")
            self.tree.column(c, width=w, minwidth=44, anchor="w", stretch=stretch)
        sb = ttk.Scrollbar(lst, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.tag_configure("live", foreground=theme.ACCENT)
        self.tree.bind("<Double-1>", lambda _e: self.play_offline())
        self.tree.bind("<Delete>", lambda _e: self.delete_archive())
        self.tree.bind("<Button-3>", self._context_menu)
        self.empty = tk.Label(lst, text="Nothing here yet.\nPaste an address above and press Replicate.",
                              bg=theme.PANEL, fg=theme.MUTED, font=f["hero"], justify="center")

        line = [0]

        def section(title):
            ttk.Label(rail, text=title, style="Section.TLabel").grid(
                row=line[0], column=0, columnspan=2, sticky="w", pady=(11, 4))
            line[0] += 1

        def buttons(*items):
            for i, (text, command, *style) in enumerate(items):
                ttk.Button(rail, text=text, command=command, style=style[0] if style else "TButton").grid(
                    row=line[0] + i // 2, column=i % 2, sticky="ew", padx=(0, 3) if i % 2 == 0 else (3, 0), pady=3)
            line[0] += (len(items) + 1) // 2

        ttk.Button(rail, text="▶   Play Offline", style="Accent.TButton", command=self.play_offline).grid(
            row=0, column=0, columnspan=2, sticky="ew", ipady=4, pady=(0, 3))
        line[0] = 1
        buttons(("Chromium", self.play_exact), ("Stop server", self.stop_server))
        section("EDIT")
        buttons(("Edit files", self.edit_files), ("Overrides", self.open_overrides), ("Sync to DB", self.sync_files))
        section("EXPORT")
        buttons(("Single HTML", self.export_single_html), ("Zip package", self.export_package),
                ("Files to folder", self.export_files), ("HAR", self.export_har))
        section("MANAGE")
        buttons(("Open folder", self.open_folder), ("Delete", self.delete_archive, "Danger.TButton"))

        self.menu = tk.Menu(self, tearoff=0)
        for label, command in (("Play Offline", self.play_offline), ("Play in Chromium (exact)", self.play_exact),
                               ("Edit files...", self.edit_files), ("Overrides & Mocks...", self.open_overrides),
                               ("Open folder", self.open_folder), ("Export single HTML...", self.export_single_html),
                               ("Export package (.zip)...", self.export_package)):
            self.menu.add_command(label=label, command=command)
        self.menu.add_separator()
        self.menu.add_command(label="Delete", command=self.delete_archive)

    def _sync_hint(self):
        if self.v_url.get():
            self.url_hint.place_forget()
        else:
            self.url_hint.place(in_=self.url_entry, x=13, rely=0.5, anchor="w")

    def _toggle_options(self):
        if self.options.winfo_ismapped():
            self.options.pack_forget()
            self.btn_options.configure(text="Options  ▾")
        else:
            self.options.pack(fill="x")
            self.btn_options.configure(text="Options  ▴")

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.btn_capture.configure(state="disabled" if busy else "normal", text="Replicating..." if busy else "Replicate")
        self.btn_stop.configure(state="normal" if busy else "disabled")
        self.status.configure(text="●  REPLICATING" if busy else "●  READY", fg=theme.ACCENT)
        if busy:
            self._animate()
        else:
            self.scan.coords(self.scan_bar, 0, 0, 0, 3)

    def _animate(self):
        if not self._busy:
            return
        width = max(self.scan.winfo_width(), 1)
        size = width // 5
        self._scan_x = (self._scan_x + max(5, width // 120)) % (width + size)
        self.scan.coords(self.scan_bar, self._scan_x - size, 0, self._scan_x, 3)
        self.status.configure(fg=theme.ACCENT if self._scan_x % 240 < 150 else theme.MUTED)
        self.after(16, self._animate)

    # ------------------------------------------------------------- logging
    def log(self, msg: str) -> None:  # thread-safe
        self.log_q.put(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def _drain_log(self):
        lines = []
        while True:
            try:
                lines.append(self.log_q.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.log_box.configure(state="normal")
            for line in lines:
                stamp, _, text = line.partition(" ")
                low = text.lower()
                tag = "bad" if any(w in low for w in ("failed", "error", "could not", "miss:")) else \
                    "good" if any(w in low for w in ("saved", "exported", "wrote", "done", "complete")) else ""
                self.log_box.insert("end", stamp + "  ", "time")
                self.log_box.insert("end", text + "\n", tag)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(100, self._drain_log)

    # ------------------------------------------------------------ archives
    def refresh_archives(self, select: str | None = None):
        cur = select or self.selected_name()
        self.tree.delete(*self.tree.get_children())
        count = total = 0
        for a in list_archives():
            try:
                m, s = a.meta(), a.stats()
            except Exception as e:  # corrupt / locked db
                m, s = {"start_url": f"<error: {e}>"}, {"files": 0, "bytes": 0}
            srv = self.servers.get(a.name)
            count += 1
            total += s["bytes"]
            self.tree.insert("", "end", iid=a.name, text="  " + a.name, tags=("live",) if srv else (), values=(
                m.get("start_url", "(incomplete)"), s["files"], human_size(s["bytes"]),
                m.get("created_at", ""), "●  " + srv.base_url.split("//")[-1] if srv else ""))
        self.summary.configure(text=f"{count} {'site' if count == 1 else 'sites'}  ·  {human_size(total)}")
        if count:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.55, anchor="center")
        if cur and self.tree.exists(cur):
            self.tree.selection_set(cur)
            self.tree.see(cur)

    def selected_name(self) -> str | None:
        sel = self.tree.selection() if hasattr(self, "tree") else ()
        return sel[0] if sel else None

    def selected_archive(self) -> Archive | None:
        name = self.selected_name()
        if not name:
            messagebox.showinfo(APP_TITLE, "Select an archive in the list first.")
            return None
        return Archive(default_root() / name)

    # ------------------------------------------------------------- capture
    def start_capture(self):
        from replicator.capture import CaptureOptions, capture_blocking
        url = self.v_url.get().strip()
        if not url:
            messagebox.showwarning(APP_TITLE, "Enter a URL to capture.")
            return
        if self.capture_thread and self.capture_thread.is_alive():
            return
        try:
            opts = CaptureOptions(
                url=url, name=self.v_name.get(), depth=int(self.v_depth.get()),
                max_pages=int(self.v_pages.get()), wait_seconds=float(self.v_wait.get()),
                timeout_seconds=float(self.v_timeout.get()), headless=not self.v_show.get(),
                scroll=self.v_scroll.get(), max_asset_mb=float(self.v_maxmb.get()),
                workers=int(self.v_workers.get()), click_all=self.v_click.get())
        except (tk.TclError, ValueError) as e:
            messagebox.showerror(APP_TITLE, f"Invalid setting: {e}")
            return
        self.capture_stop = threading.Event()
        self._set_busy(True)
        self.log(f"Capturing {url} ...")

        def run():
            name = None
            try:
                name = capture_blocking(opts, self.log, self.capture_stop).name
            except Exception as e:
                self.log(f"CAPTURE FAILED: {e}")
                self.log(traceback.format_exc().rstrip())
            finally:
                self.after(0, lambda: self._capture_done(name))

        self.capture_thread = threading.Thread(target=run, daemon=True)
        self.capture_thread.start()

    def _capture_done(self, name):
        self._set_busy(False)
        self.refresh_archives(select=name)

    def stop_capture(self):
        if self.capture_stop:
            self.capture_stop.set()
            self.log("Stopping after the current page...")

    # -------------------------------------------------------------- replay
    def play_offline(self):
        from replicator.server import ReplayServer
        a = self.selected_archive()
        if not a:
            return
        srv = self.servers.get(a.name)
        if srv is None:
            try:
                srv = ReplayServer(a, log=self.log)
                srv.start()
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"Could not start server:\n{e}")
                return
            self.servers[a.name] = srv
            self.refresh_archives()
        self.log(f"Opening {srv.launch_url}  (Chrome/Edge recommended; misses are logged here)")
        webbrowser.open(srv.launch_url)

    def play_exact(self):
        from replicator.replay import replay_exact_blocking
        a = self.selected_archive()
        if not a:
            return

        def run():
            try:
                replay_exact_blocking(a, self.log)
            except Exception as e:
                self.log(f"EXACT REPLAY FAILED: {e}")

        threading.Thread(target=run, daemon=True).start()

    def stop_server(self):
        name = self.selected_name()
        srv = self.servers.pop(name, None) if name else None
        if srv:
            srv.stop()
            self.log(f"Stopped server for '{name}'.")
            self.refresh_archives()

    # --------------------------------------------------------------- misc
    def export_har(self):
        a = self.selected_archive()
        if not a:
            return
        out = filedialog.asksaveasfilename(defaultextension=".har", initialfile=f"{a.name}.har",
                                           filetypes=[("HTTP Archive", "*.har")])
        if not out:
            return

        def run():
            try:
                self.log(f"Exported HAR: {a.export_har(out)}")
            except Exception as e:
                self.log(f"HAR export failed: {e}")

        threading.Thread(target=run, daemon=True).start()

    def edit_files(self):
        from replicator.editor import ArchiveEditor
        a = self.selected_archive()
        if not a:
            return
        ArchiveEditor(self, a, log=self.log)

    def open_overrides(self):
        from replicator.editor import OverridesDialog
        a = self.selected_archive()
        if not a:
            return
        OverridesDialog(self, a, log=self.log)

    def sync_files(self):
        a = self.selected_archive()
        if not a:
            return

        def run():
            try:
                a.sync_files(log=self.log)
                self.after(0, lambda: self.refresh_archives(select=a.name))
            except Exception as e:
                self.log(f"Sync failed: {e}")

        threading.Thread(target=run, daemon=True).start()

    def export_files(self):
        a = self.selected_archive()
        if not a:
            return
        if (a.dir / "files" / "_index.tsv").exists():
            if not messagebox.askyesno(
                APP_TITLE,
                "The 'files/' folder already exists.\n\n"
                "Re-exporting from archive.db will overwrite any un-synced edits in 'files/'.\n"
                "Continue?",
            ):
                return

        def run():
            try:
                out = a.export_files(log=self.log)
                os.startfile(a.dir)
            except Exception as e:
                self.log(f"Export failed: {e}")

        threading.Thread(target=run, daemon=True).start()

    def export_single_html(self):
        a = self.selected_archive()
        if not a:
            return
        out = filedialog.asksaveasfilename(defaultextension=".html", initialfile=f"{a.name}.html",
                                           filetypes=[("HTML file", "*.html")])
        if not out:
            return

        def run():
            try:
                self.log("Building single HTML file...")
                a.export_single_html(out, log=self.log)
                os.startfile(os.path.dirname(out))
            except Exception as e:
                self.log(f"Single HTML export failed: {e}")

        threading.Thread(target=run, daemon=True).start()

    def export_package(self):
        a = self.selected_archive()
        if not a:
            return
        out = filedialog.asksaveasfilename(defaultextension=".zip", initialfile=f"{a.name}.zip",
                                           filetypes=[("Zip package", "*.zip")])
        if not out:
            return

        def run():
            try:
                self.log("Building package (files + offline server + index.html)...")
                a.export_package(out, log=self.log)
                os.startfile(os.path.dirname(out))
            except Exception as e:
                self.log(f"Package export failed: {e}")

        threading.Thread(target=run, daemon=True).start()

    def _context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.menu.tk_popup(event.x_root, event.y_root)

    def delete_archive(self):
        name = self.selected_name()
        if not name:
            messagebox.showinfo(APP_TITLE, "Select an archive in the list first.")
            return
        if not messagebox.askyesno(APP_TITLE, f"Delete '{name}'?\n\nIt is moved to the Recycle Bin, "
                                              "so you can restore it from there."):
            return
        srv = self.servers.pop(name, None)
        if srv:
            srv.stop()
        try:
            how = Archive(default_root() / name).delete()
            self.log(f"Deleted '{name}' ({'moved to Recycle Bin' if how == 'recycled' else 'removed'}).")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not delete '{name}':\n{e}\n\n"
                                            "Close any window or PLAY.bat that uses it and try again.")
        self.refresh_archives()

    def open_folder(self):
        name = self.selected_name()
        os.startfile(default_root() / name if name else default_root())

    def on_close(self):
        if self.capture_stop:
            self.capture_stop.set()
        for srv in self.servers.values():
            try:
                srv.stop()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
