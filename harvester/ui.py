"""Tkinter front end: one window, three tabs - Extract, Backends, Log."""
from __future__ import annotations

import queue
import threading
import webbrowser
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__, config, paths, theme
from .backends import (REGISTRY, Backend, ENGINE_LABELS, ENGINE_PREFERENCE,
                       dotnet_present, fetch_all, installed_version, probe_help)
from . import aeskey
from . import mappings as mapping_lib
from .detect import Detection, detect
from .jobs import (MESH_FORMATS, TEXTURE_FORMATS, UE_GAME_TAGS, ExportOptions, Job,
                   Runner, build_command, job_output_dir, plan_jobs, resolve_ue_tag)

PAD = 8


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"Game Asset Harvester {__version__}")
        self.geometry("1040x720")
        self.minsize(880, 600)

        self.opts = config.load()
        self.dark = bool(self.opts.dark)
        self.detection: Detection | None = None
        self.jobs: list[Job] = []
        self.prefer: dict[str, str] = {}
        # worker threads never touch widgets: they put log lines (str) or
        # callables on this queue and _drain runs them on the Tk thread
        self._q: queue.Queue = queue.Queue()
        self.runner = Runner(self.log, self._on_job_update)

        self._build()
        self._apply_theme()
        for cls in ("all", "TEntry", "TCombobox", "Text"):   # Tk gives Ctrl-D to entries as "delete char"
            self.bind_class(cls, "<Control-d>", self._on_ctrl_d)
            self.bind_class(cls, "<Control-D>", self._on_ctrl_d)
        self.after(120, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.log(f"Game Asset Harvester {__version__} - data folder: {paths.app_dir()}")
        self.after(400, self._startup_check)

    # ---------------------------------------------------------------- layout
    def _build(self) -> None:
        # status bar first, side=bottom, so a tall Extract tab can never squeeze it
        # off the bottom of the window
        bar = ttk.Frame(self)
        bar.pack(side="bottom", fill="x", padx=PAD, pady=(0, PAD))
        self.status = ttk.Label(bar, text="Ready")
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=180)
        self.progress.pack(side="right")
        self.btn_theme = ttk.Button(bar, text="Light", width=6, style="Theme.TButton",
                                    command=self._toggle_theme)
        self.btn_theme.pack(side="right", padx=(0, PAD))
        ttk.Button(bar, text="Reset to Downloads", style="Theme.TButton",
                   command=lambda: self._set_save_dir("")).pack(side="right", padx=(0, PAD))
        ttk.Button(bar, text="Default save folder...", style="Theme.TButton",
                   command=self._pick_save_dir).pack(side="right", padx=(0, 4))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        self.nb = nb
        self.tab_extract = ttk.Frame(nb)
        self.tab_backends = ttk.Frame(nb)
        self.tab_log = ttk.Frame(nb)
        nb.add(self.tab_extract, text="Extract")
        nb.add(self.tab_backends, text="Backends")
        nb.add(self.tab_log, text="Log")
        self._build_extract(self.tab_extract)
        self._build_backends(self.tab_backends)
        self._build_log(self.tab_log)

    # ----------------------------------------------------------------- theme
    def _apply_theme(self) -> None:
        c = theme.apply(self, self.dark)
        self.c = c
        style = ttk.Style(self)
        # widgets this app uses that the shared module has no style for
        style.configure("TLabelframe", background=c["bg"], bordercolor=c["border"])
        style.configure("TLabelframe.Label", background=c["bg"], foreground=c["dim"])
        style.configure("Horizontal.TProgressbar", background=c["accent"],
                        troughcolor=c["panel"], bordercolor=c["border"],
                        lightcolor=c["accent"], darkcolor=c["accent"])
        style.configure("Theme.TButton", padding=(8, 1))   # fits the status bar
        mono = theme.mono_family(self)
        ui = theme.ui_family(self)
        # classic tk widgets are not covered by ttk styles: recolour by hand
        for widget, font in ((self.text, (mono, 10)), (self.detail, (ui, 10))):
            widget.configure(
                background=c["panel"], foreground=c["text"], font=font,
                insertbackground=c["accent"], selectbackground=c["sel"],
                selectforeground=c["text"], highlightthickness=1,
                highlightbackground=c["field_border"], highlightcolor=c["accent"],
                relief="flat", borderwidth=0, padx=6, pady=4)
        self.btn_theme.configure(text="Light" if self.dark else "Dark")

    def _on_ctrl_d(self, _event=None) -> str:
        self._toggle_theme()
        return "break"

    def _toggle_theme(self) -> None:
        self.dark = not self.dark
        self._apply_theme()
        self.opts.dark = self.dark
        config.save(self.opts)

    def _build_extract(self, parent: ttk.Frame) -> None:
        src = ttk.LabelFrame(parent, text="Source")
        src.pack(fill="x", padx=PAD, pady=(PAD, 4))
        self.var_source = tk.StringVar()
        ttk.Entry(src, textvariable=self.var_source).grid(
            row=0, column=0, sticky="ew", padx=(PAD, 4), pady=PAD)
        ttk.Button(src, text="Game folder...", command=self._pick_folder).grid(row=0, column=1, padx=2)
        ttk.Button(src, text="Single file...", command=self._pick_file).grid(row=0, column=2, padx=2)
        ttk.Button(src, text="Scan", command=self._scan).grid(row=0, column=3, padx=(2, PAD))
        src.columnconfigure(0, weight=1)

        found = ttk.LabelFrame(parent, text="What was found")
        found.pack(fill="both", expand=False, padx=PAD, pady=4)
        self.tree = ttk.Treeview(found, columns=("backend", "target", "status"),
                                 show="tree headings", height=10)
        self.tree.heading("#0", text="Engine / containers found")
        self.tree.column("#0", width=300, anchor="w")
        for col, label, width in (("backend", "Backend", 150),
                                  ("target", "Points at", 380), ("status", "Status", 200)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=PAD, pady=PAD, side="left")
        sb = ttk.Scrollbar(found, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y", pady=PAD)
        self.tree.configure(yscrollcommand=sb.set)

        opt = ttk.LabelFrame(parent, text="Export options")
        opt.pack(fill="x", padx=PAD, pady=4)
        self.var_out = tk.StringVar(value=self.opts.output_root)
        self.var_mesh = tk.StringVar(value=self.opts.mesh_format)
        self.var_tex = tk.StringVar(value=self.opts.texture_format)
        self.var_include = tk.StringVar(value=self.opts.include)
        self.var_game = tk.StringVar(value=self.opts.ue_game_tag)
        self.var_aes = tk.StringVar(value=self.opts.aes_key)
        self.var_map = tk.StringVar(value=self.opts.mappings)
        self.var_unity = tk.StringVar(value=self.opts.unity_types)
        self.var_bms = tk.StringVar(value=self.opts.bms_script)
        self.var_extra = tk.StringVar(value=self.opts.extra_args)
        self.var_org = tk.BooleanVar(value=self.opts.organise)
        self.var_manifest = tk.BooleanVar(value=self.opts.write_manifest)

        r = 0
        ttk.Label(opt, text="Output folder").grid(row=r, column=0, sticky="w", padx=PAD, pady=3)
        ttk.Entry(opt, textvariable=self.var_out).grid(row=r, column=1, columnspan=3, sticky="ew", pady=3)
        ttk.Button(opt, text="...", width=3, command=self._pick_out).grid(row=r, column=4, padx=(4, PAD))
        r += 1
        ttk.Label(opt, text="Meshes").grid(row=r, column=0, sticky="w", padx=PAD, pady=3)
        ttk.Combobox(opt, textvariable=self.var_mesh, values=list(MESH_FORMATS),
                     state="readonly", width=12).grid(row=r, column=1, sticky="w", pady=3)
        ttk.Label(opt, text="Textures").grid(row=r, column=2, sticky="e", padx=6)
        ttk.Combobox(opt, textvariable=self.var_tex, values=list(TEXTURE_FORMATS),
                     state="readonly", width=12).grid(row=r, column=3, sticky="w", pady=3)
        r += 1
        ttk.Label(opt, text="Include pattern").grid(row=r, column=0, sticky="w", padx=PAD, pady=3)
        ttk.Entry(opt, textvariable=self.var_include, width=28).grid(row=r, column=1, sticky="w", pady=3)
        ttk.Label(opt, text="UE version").grid(row=r, column=2, sticky="e", padx=6)
        ttk.Combobox(opt, textvariable=self.var_game, values=UE_GAME_TAGS,
                     width=20).grid(row=r, column=3, sticky="w", pady=3)
        r += 1
        ttk.Label(opt, text="AES key (UE)").grid(row=r, column=0, sticky="w", padx=PAD, pady=3)
        ttk.Entry(opt, textvariable=self.var_aes).grid(row=r, column=1, columnspan=3, sticky="ew", pady=3)
        r += 1
        ttk.Label(opt, text="Mappings .usmap").grid(row=r, column=0, sticky="w", padx=PAD, pady=3)
        ttk.Entry(opt, textvariable=self.var_map).grid(row=r, column=1, columnspan=3, sticky="ew", pady=3)
        ttk.Button(opt, text="...", width=3,
                   command=lambda: self._pick_into(self.var_map, [("Mappings", "*.usmap")])
                   ).grid(row=r, column=4, padx=(4, PAD))
        r += 1
        ttk.Label(opt, text="Unity types").grid(row=r, column=0, sticky="w", padx=PAD, pady=3)
        ttk.Entry(opt, textvariable=self.var_unity).grid(row=r, column=1, columnspan=3, sticky="ew", pady=3)
        r += 1
        ttk.Label(opt, text="QuickBMS script").grid(row=r, column=0, sticky="w", padx=PAD, pady=3)
        ttk.Entry(opt, textvariable=self.var_bms).grid(row=r, column=1, columnspan=3, sticky="ew", pady=3)
        ttk.Button(opt, text="...", width=3,
                   command=lambda: self._pick_into(self.var_bms, [("BMS script", "*.bms")])
                   ).grid(row=r, column=4, padx=(4, PAD))
        r += 1
        ttk.Label(opt, text="Extra arguments").grid(row=r, column=0, sticky="w", padx=PAD, pady=3)
        ttk.Entry(opt, textvariable=self.var_extra).grid(row=r, column=1, columnspan=3, sticky="ew", pady=3)
        r += 1
        ttk.Checkbutton(opt, text="Sort output into Meshes / Textures / Audio folders",
                        variable=self.var_org).grid(row=r, column=0, columnspan=3, sticky="w",
                                                    padx=PAD, pady=(3, PAD))
        ttk.Checkbutton(opt, text="Write manifest.json", variable=self.var_manifest).grid(
            row=r, column=3, sticky="w", pady=(3, PAD))
        opt.columnconfigure(1, weight=1)
        opt.columnconfigure(3, weight=1)

        act = ttk.Frame(parent)
        act.pack(fill="x", padx=PAD, pady=(0, PAD))
        self.btn_brute = ttk.Button(act, text="Extract everything",
                                    style="Accent.TButton", command=self._run_brute)
        self.btn_brute.pack(side="left")
        self.btn_cancel = ttk.Button(act, text="Cancel", command=self.runner.cancel,
                                     state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        ttk.Label(act, text=(
            "Pick the game's folder, press Extract everything. It scans the whole folder, "
            "downloads what it needs, finds the mappings and tries every setting until "
            "assets come out.")).pack(side="left", padx=(12, 0))

        adv = ttk.Frame(parent)
        adv.pack(fill="x", padx=PAD, pady=(0, PAD))
        self.btn_run = ttk.Button(adv, text="Run once with these settings", command=self._run)
        self.btn_run.pack(side="left")
        ttk.Button(adv, text="Find mappings", command=self._find_mappings).pack(side="left", padx=6)
        ttk.Button(adv, text="Find AES key", command=self._find_key).pack(side="left", padx=6)
        ttk.Button(adv, text="Show command", command=self._show_command).pack(side="left", padx=6)
        ttk.Button(adv, text="Open output folder", command=self._open_out).pack(side="left", padx=6)

    def _build_backends(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=PAD, pady=PAD)
        ttk.Label(top, text=(
            "Each tool is downloaded from its own official source. Nothing is redistributed "
            "with this app; the build script fetches them so the exe you build carries your copies."
        ), wraplength=960, justify="left").pack(anchor="w")

        self.btree = ttk.Treeview(parent, columns=("engine", "version", "state", "licence"),
                                  show="tree headings", height=8)
        self.btree.heading("#0", text="Backend")
        self.btree.column("#0", width=200)
        for col, label, width in (("engine", "Handles", 220), ("version", "Version", 130),
                                  ("state", "State", 160), ("licence", "Licence", 180)):
            self.btree.heading(col, text=label)
            self.btree.column(col, width=width, anchor="w")
        self.btree.pack(fill="both", expand=True, padx=PAD, pady=4)
        self.btree.bind("<<TreeviewSelect>>", lambda e: self._show_backend_detail())

        row = ttk.Frame(parent)
        row.pack(fill="x", padx=PAD, pady=4)
        ttk.Button(row, text="Get missing", command=lambda: self._fetch(False)).pack(side="left")
        ttk.Button(row, text="Re-download selected", command=self._refetch_selected).pack(side="left", padx=6)
        ttk.Button(row, text="Check real flags", command=self._probe).pack(side="left", padx=6)
        ttk.Button(row, text="Open homepage", command=self._open_home).pack(side="left", padx=6)
        ttk.Button(row, text="Open backends folder", command=self._open_backends).pack(side="left", padx=6)

        self.detail = tk.Text(parent, height=10, wrap="word")
        self.detail.pack(fill="both", expand=True, padx=PAD, pady=(4, PAD))
        self.detail.configure(state="disabled")
        self._refresh_backends()

    def _build_log(self, parent: ttk.Frame) -> None:
        self.text = tk.Text(parent, wrap="none")
        self.text.pack(fill="both", expand=True, padx=PAD, pady=PAD, side="left")
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.text.yview)
        sb.pack(side="right", fill="y", pady=PAD)
        self.text.configure(yscrollcommand=sb.set, state="disabled")

    # ------------------------------------------------------------- plumbing
    def log(self, line: str) -> None:
        self._q.put(line)

    def _post(self, fn: Callable[[], None]) -> None:
        """Run fn on the Tk thread. Safe to call from any thread."""
        self._q.put(fn)

    def _drain(self) -> None:
        wrote = False
        try:
            while True:
                line = self._q.get_nowait()
                if callable(line):
                    try:
                        line()
                    except Exception as exc:  # noqa: BLE001
                        self.log(f"!! {exc}")
                    continue
                self.text.configure(state="normal")
                self.text.insert("end", line + "\n")
                self.text.configure(state="disabled")
                wrote = True
                try:
                    with paths.log_path().open("a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except OSError:
                    pass
        except queue.Empty:
            pass
        if wrote:
            self.text.see("end")
        self.after(120, self._drain)

    def _collect(self) -> ExportOptions:
        self.opts = ExportOptions(
            output_root=self.var_out.get().strip(),
            mesh_format=self.var_mesh.get(),
            texture_format=self.var_tex.get(),
            include=self.var_include.get().strip() or "*",
            ue_game_tag=self.var_game.get().strip() or "auto",
            aes_key=self.var_aes.get().strip(),
            mappings=self.var_map.get().strip(),
            unity_types=self.var_unity.get().strip(),
            bms_script=self.var_bms.get().strip(),
            organise=bool(self.var_org.get()),
            write_manifest=bool(self.var_manifest.get()),
            extra_args=self.var_extra.get().strip(),
            dark=self.dark,
            save_dir=self.opts.save_dir,
        )
        config.save(self.opts)
        return self.opts

    # --------------------------------------------------------------- actions
    def _pick_folder(self) -> None:
        d = filedialog.askdirectory(title="Pick the game's install folder",
                                    initialdir=paths.default_save_dir())
        if d:
            self.var_source.set(d)
            self._scan()

    def _pick_file(self) -> None:
        f = filedialog.askopenfilename(title="Pick an archive")
        if f:
            self.var_source.set(f)
            self._scan()

    def _pick_out(self) -> None:
        d = filedialog.askdirectory(title="Where should extracted assets go?",
                                    initialdir=paths.default_save_dir())
        if d:
            self.var_out.set(d)

    def _pick_save_dir(self) -> None:
        d = filedialog.askdirectory(title="Default folder for everything this app saves",
                                    initialdir=paths.default_save_dir())
        if d:
            self._set_save_dir(d)

    def _set_save_dir(self, folder: str) -> None:
        """Persist "save_dir" (empty = Downloads) and re-point the output field if it
        was still on the old default."""
        old_default = str(paths.default_output_dir())
        self.opts.save_dir = folder
        config.save(self.opts)
        new_default = str(paths.default_output_dir())
        if self.var_out.get().strip() in ("", old_default):
            self.var_out.set(new_default)
        self.status.configure(text=f"Default save folder: {paths.default_save_dir()}")

    def _pick_into(self, var: tk.StringVar, types: list[tuple[str, str]]) -> None:
        f = filedialog.askopenfilename(filetypes=types + [("All files", "*.*")])
        if f:
            var.set(f)

    def _scan(self, then: Callable[[], None] | None = None) -> None:
        src = self.var_source.get().strip()
        if not src or not Path(src).exists():
            messagebox.showinfo("Game Asset Harvester", "Pick a game folder or archive first.")
            return
        self.status.configure(text="Scanning the whole folder...")
        self.progress.start(12)

        def work():
            det = detect(Path(src))
            self._post(lambda: self._scan_done(det, then))

        threading.Thread(target=work, daemon=True).start()

    def _scan_done(self, det: Detection, then: Callable[[], None] | None = None) -> None:
        self.progress.stop()
        self.detection = det
        self.log(det.summary())
        opts = self._collect()
        self.jobs = plan_jobs(det, opts, self.prefer)
        self._refresh_jobs()
        weak = [f for f in det.findings if f.weak]
        if weak and not self.jobs:
            self.log("The only thing found was a handful of loose data files, which no "
                     "backend can open without a QuickBMS script for this specific game. "
                     '"Extract everything" will still try.')
        if not self.jobs:
            self.status.configure(text="Nothing recognised - try a deeper folder, or use Generic/QuickBMS")
            if then:
                then()
            return
        ue = next((f.ue for f in det.findings if f.ue and f.ue.major), None)
        if not ue:
            self.status.configure(text=f"{len(self.jobs)} job(s) ready")
            if then:
                then()
            return
        if (self.var_game.get() or "auto").lower() == "auto":
            self.status.configure(
                text=f"{len(self.jobs)} job(s) ready - detected {ue.label} "
                     f"({ue.tag}, {ue.confidence})")
        else:
            self.status.configure(
                text=f"{len(self.jobs)} job(s) ready - detected {ue.tag}, "
                     f"but you have forced {self.var_game.get()}")
        if ue.encrypted_index and not self.var_aes.get().strip():
            self.log("This game's pak index is encrypted - it needs an AES key or "
                     "almost nothing will extract.")
        if then:
            then()

    def _refresh_jobs(self) -> None:
        self.tree.delete(*self.tree.get_children())
        opts = self.opts
        for i, job in enumerate(self.jobs):
            backend = REGISTRY.get(job.backend_key)
            if job.status != "queued":
                state = job.status
            elif not (backend and backend.installed()):
                state = "backend missing"
            elif job.needs_key and not opts.aes_key.strip():
                state = "ready - needs an AES key"
            elif job.needs_mappings and not opts.mappings.strip():
                state = "ready - needs a .usmap"
            elif job.backend_key == "cue4parse":
                state = f"ready - {resolve_ue_tag(job, opts)}"
            else:
                state = "ready"
            if job.message:
                state = f"{job.status} - {job.message}"
            label = job.engine_label or ENGINE_LABELS.get(job.engine, job.engine)
            files = self._files_for(job)
            if files:
                label = f"{label}  ({len(files)} container(s))"
            parent = self.tree.insert("", "end", iid=str(i), text=label, open=True, values=(
                backend.name if backend else "?", job.target, state))
            # list what was actually found, not just a one-line summary
            for n, f in enumerate(files[:200]):
                try:
                    rel = str(Path(f).relative_to(job.source))
                except (ValueError, TypeError):
                    rel = Path(f).name
                size = ""
                try:
                    size = f"{Path(f).stat().st_size / 1048576:.1f} MB"
                except OSError:
                    pass
                self.tree.insert(parent, "end", iid=f"{i}:{n}", text="   " + rel,
                                 values=("", size, ""))
            if len(files) > 200:
                self.tree.insert(parent, "end", iid=f"{i}:more",
                                 text=f"   ... and {len(files) - 200} more", values=("", "", ""))

    def _files_for(self, job: Job):
        """The container files the scan actually matched for this job."""
        if not self.detection:
            return []
        for f in self.detection.findings:
            if f.engine == job.engine:
                return f.files
        return []

    def _on_job_update(self, job: Job) -> None:
        self._post(self._refresh_jobs)

    def _show_command(self) -> None:
        if not self.jobs:
            messagebox.showinfo("Game Asset Harvester", "Scan a folder first.")
            return
        opts = self._collect()
        lines = []
        for job in self.jobs:
            backend = REGISTRY.get(job.backend_key)
            try:
                argv = build_command(backend, job, opts, job_output_dir(job, opts))
                lines.append(" ".join(f'"{a}"' if " " in a else a for a in argv))
            except Exception as exc:  # noqa: BLE001
                lines.append(f"# {job.label}: {exc}")
        self.log("\n".join(lines))
        self.nb.select(self.tab_log)

    def _run(self) -> None:
        if not self.jobs:
            messagebox.showinfo("Game Asset Harvester", "Scan a folder first.")
            return
        opts = self._collect()
        missing = [REGISTRY[j.backend_key].name for j in self.jobs
                   if not REGISTRY[j.backend_key].installed()]
        if missing:
            if not messagebox.askyesno(
                    "Backends missing",
                    "These are not installed yet:\n  " + "\n  ".join(sorted(set(missing)))
                    + "\n\nDownload them now?"):
                return
            self._fetch(False, then=self._run)
            return
        for job in self.jobs:
            job.status = "queued"
            job.message = ""
        self.btn_run.configure(state="disabled")
        self.btn_brute.configure(state="disabled")   # one run at a time: the Runner is shared
        self.btn_cancel.configure(state="normal")
        self.progress.start(12)
        self.status.configure(text="Extracting...")
        self.nb.select(self.tab_log)
        self.runner.start(self.jobs, opts, on_finish=lambda jobs: self._post(self._run_done))

    def _find_mappings(self) -> None:
        """Get a .usmap for this game without going looking for one."""
        if not self.jobs:
            messagebox.showinfo("Game Asset Harvester", "Scan a game folder first.")
            return
        game = self.jobs[0].game or Path(self.var_source.get()).name
        self.nb.select(self.tab_log)
        self.status.configure(text=f"Looking for mappings for {game}...")
        self.progress.start(12)

        source = self.var_source.get() or "."    # read Tk variables on the Tk thread only

        def work():
            self.log(f"Looking for a .usmap for {game}")
            found, note = mapping_lib.resolve(
                game, extra_roots=[Path(source)],
                allow_download=True, log=self.log)
            self._post(lambda: self._mappings_done(found, note))

        threading.Thread(target=work, daemon=True).start()

    def _mappings_done(self, found, note) -> None:
        self.progress.stop()
        if found:
            self.var_map.set(str(found))
            self._collect()
            self._refresh_jobs()
            self.log(note)
            self.status.configure(text=f"Mappings set: {Path(found).name}")
        else:
            self.log(note or "No mappings found.")
            self.status.configure(text="No mappings found - see the log")

    def _find_key(self) -> None:
        """Pull the AES key out of the shipping exe and prove it against the pak."""
        src = self.var_source.get().strip()
        if not src:
            messagebox.showinfo("Game Asset Harvester", "Pick a game folder first.")
            return
        root = Path(src) if Path(src).is_dir() else Path(src).parent
        if getattr(self, "detection", None):
            root = self.detection.root
        game = self.jobs[0].game if self.jobs else root.name
        self.nb.select(self.tab_log)
        self.status.configure(text=f"Looking for the AES key for {game}...")
        self.progress.start(12)

        def work():
            self.log(f"Looking for the AES key for {game}")
            try:
                result = aeskey.find(root, game, log=self.log)
            except Exception as exc:  # noqa: BLE001
                result = aeskey.KeySearch(notes=[f"key search failed: {exc}"])
            self._post(lambda: self._key_done(result))

        threading.Thread(target=work, daemon=True).start()

    def _key_done(self, result) -> None:
        self.progress.stop()
        for note in result.notes:
            self.log(note)
        if result.verified:
            self.var_aes.set(result.verified[0])
            self._collect()
            self.status.configure(text="AES key found and verified against the pak")
        elif result.unverified:
            self.var_aes.set(result.unverified[0])
            self._collect()
            self.status.configure(
                text=f"AES key guessed ({len(result.unverified)} candidate(s)) - not verified")
        else:
            self.status.configure(text="No AES key needed, or none found - see the log")

    def _run_brute(self, scanned: bool = False) -> None:
        """Point and go: scan, fetch what is missing, then try settings until
        something actually converts. This is the whole app in one button."""
        if not self.jobs and not scanned and self.var_source.get().strip():
            # never make him press Scan first. scanned=True on the way back, or a
            # folder with nothing queueable would rescan itself for ever
            self._scan(then=lambda: self._run_brute(True))
            return
        opts = self._collect()
        # brute force means brute force: include the findings too weak to queue normally
        if getattr(self, "detection", None):
            self.jobs = plan_jobs(self.detection, opts, self.prefer, include_weak=True)
            self._refresh_jobs()
        if not self.jobs:
            messagebox.showinfo("Game Asset Harvester", "Scan a game folder first.")
            return
        missing = [REGISTRY[j.backend_key].name for j in self.jobs
                   if not REGISTRY[j.backend_key].installed()]
        if missing:
            if not messagebox.askyesno(
                    "Backends missing",
                    "These are not installed yet:\n  " + "\n  ".join(sorted(set(missing)))
                    + "\n\nDownload them now?"):
                return
            self._fetch(False, then=self._run_brute)
            return
        for job in self.jobs:
            job.status, job.message = "queued", ""
        self.btn_run.configure(state="disabled")
        self.btn_brute.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.progress.start(12)
        self.status.configure(text="Extracting everything - trying every setting...")
        self.nb.select(self.tab_log)
        self.log("")
        self.log("== extracting the whole folder. Every container found is covered, and "
                 "each job tries settings in turn until one converts something.")

        def work():
            try:
                for job in self.jobs:
                    if self.runner._cancel.is_set():
                        break
                    mapping = opts.mappings
                    if job.needs_mappings and not mapping:
                        found, note = mapping_lib.resolve(
                            job.game, extra_roots=[Path(job.source)],
                            allow_download=True, log=self.log)
                        if found:
                            mapping = str(found)
                            self.log(note)
                        else:
                            self.log(note or "No mappings found for this game.")
                    self.runner.run_brute(job, opts, mapping)
            except Exception as exc:  # noqa: BLE001 - never leave the buttons dead
                self.log(f"!! the run stopped on an unexpected error: {exc}")
            finally:
                self._post(self._run_done)

        self.runner.running = True
        self.runner._cancel.clear()     # Runner.start does this; an earlier Cancel must not stick
        threading.Thread(target=work, daemon=True).start()

    def _run_done(self) -> None:
        self.progress.stop()
        self.runner.running = False
        self.btn_run.configure(state="normal")
        self.btn_brute.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        ok = sum(1 for j in self.jobs if j.status == "done")
        total = sum(j.produced for j in self.jobs)
        self.status.configure(text=f"Finished: {ok}/{len(self.jobs)} job(s), {total} file(s)")
        self._refresh_jobs()
        if total:
            self._open_out()

    def _open_out(self) -> None:
        target = Path(self.var_out.get().strip() or paths.default_output_dir())
        target.mkdir(parents=True, exist_ok=True)
        _open_path(target)

    # -------------------------------------------------------------- backends
    def _refresh_backends(self) -> None:
        self.btree.delete(*self.btree.get_children())
        for key, b in REGISTRY.items():
            engines = ", ".join(ENGINE_LABELS.get(e, e) for e in b.engines)
            state = "installed" if b.installed() else "not installed"
            if b.needs_dotnet and b.installed() and not dotnet_present():
                state = "installed (.NET may be needed)"
            self.btree.insert("", "end", iid=key, text=b.name,
                              values=(engines, installed_version(b), state, b.licence))

    def _selected_backend(self) -> Backend | None:
        sel = self.btree.selection()
        return REGISTRY.get(sel[0]) if sel else None

    def _show_backend_detail(self) -> None:
        b = self._selected_backend()
        if not b:
            return
        exe = b.find_exe()
        body = [b.name, "", b.summary, "", f"Home: {b.homepage}", f"Licence: {b.licence}"]
        if b.notes:
            body += ["", f"Note: {b.notes}"]
        body += ["", f"Executable: {exe or 'not installed'}"]
        if b.needs_dotnet:
            body.append(f".NET runtime detected: {'yes' if dotnet_present() else 'no'}")
        self._set_detail("\n".join(body))

    def _set_detail(self, text: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def _fetch(self, force: bool, only=None, then=None) -> None:
        self.progress.start(12)
        self.status.configure(text="Downloading backends...")
        self.nb.select(self.tab_log)

        def work():
            results = fetch_all(self.log, only=only, force=force)
            self._post(lambda: self._fetch_done(results, then))

        threading.Thread(target=work, daemon=True).start()

    def _fetch_done(self, results: dict, then) -> None:
        self.progress.stop()
        self._refresh_backends()
        bad = {k: v for k, v in results.items() if v != "ok"}
        self.status.configure(
            text=f"{len(results) - len(bad)}/{len(results)} backend(s) ready"
            + (f", {len(bad)} failed" if bad else ""))
        if bad:
            self.log("PROBLEMS FOUND:")
            for k, v in bad.items():
                self.log(f"  {k}: {v}")
        if then and not bad:
            then()

    def _refetch_selected(self) -> None:
        b = self._selected_backend()
        if not b:
            messagebox.showinfo("Game Asset Harvester", "Pick a backend in the list first.")
            return
        self._fetch(True, only=[b.key])

    def _probe(self) -> None:
        b = self._selected_backend()
        if not b:
            messagebox.showinfo("Game Asset Harvester", "Pick a backend in the list first.")
            return
        self.status.configure(text=f"Asking {b.name} for its options...")

        def work():
            text = probe_help(b)
            self._post(lambda: (self._set_detail(text), self.status.configure(text="Ready")))

        threading.Thread(target=work, daemon=True).start()

    def _open_home(self) -> None:
        b = self._selected_backend()
        if b:
            webbrowser.open(b.homepage)

    def _open_backends(self) -> None:
        _open_path(paths.backends_dir())

    # -------------------------------------------------------------- lifecycle
    def _startup_check(self) -> None:
        missing = [b.name for b in REGISTRY.values() if not b.installed()]
        if missing:
            self.log("Backends not yet present: " + ", ".join(missing))
            self.log("Use the Backends tab -> Get missing, or run: GameAssetHarvester.exe fetch-backends")

    def _close(self) -> None:
        try:
            self._collect()
        except Exception:  # noqa: BLE001
            pass
        self.runner.cancel()
        self.destroy()


def _open_path(path: Path) -> None:
    import os
    import subprocess
    import sys
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:  # noqa: BLE001
        pass


def run() -> int:
    app = App()
    app.mainloop()
    return 0
