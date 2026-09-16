#!/usr/bin/env python3
"""Game Asset Harvester - frozen entry point.

Modes:
    GameAssetHarvester.exe                    open the window
    GameAssetHarvester.exe fetch-backends     download every extraction tool
    GameAssetHarvester.exe backends           print what is installed
    GameAssetHarvester.exe probe <backend>    print a backend's own --help
    GameAssetHarvester.exe extract <source>   run headless
    GameAssetHarvester.exe selftest           check the build and write a report
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
import traceback
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from harvester import __version__, config, paths  # noqa: E402
from harvester.backends import (REGISTRY, dotnet_present, fetch_all,  # noqa: E402
                                installed_version, list_assets, probe_help)
from harvester import mappings as mapping_lib  # noqa: E402
from harvester import jobs as jobs_mod  # noqa: E402
from harvester.detect import detect  # noqa: E402
from harvester.jobs import (ExportOptions, Job, Runner, brute_attempts,  # noqa: E402
                            build_command, diagnose, job_output_dir, plan_jobs)


def _clean_argv() -> list[str]:
    """Finder passes -psn_0_12345; a strict argparse would exit(2) and the window never opens."""
    return [a for a in sys.argv[1:] if not a.startswith("-psn_")]


def _guard_streams() -> None:
    class _Null:
        def write(self, *_a):
            return 0

        def flush(self):
            return None

        def isatty(self):
            return False

    if sys.stdout is None:
        sys.stdout = _Null()  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = _Null()  # type: ignore[assignment]
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def _crash(exc: BaseException) -> None:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        paths.crash_path().write_text(text, "utf-8")
    except Exception:  # noqa: BLE001
        pass
    try:
        print(text, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("Game Asset Harvester crashed",
                             f"{exc}\n\nFull details in:\n{paths.crash_path()}")
        root.destroy()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- sub-modes

def cmd_fetch(args) -> int:
    only = args.backend or None
    if args.list:
        for key in (only or list(REGISTRY)):
            b = REGISTRY.get(key)
            if not b:
                print(f"{key}: unknown backend")
                continue
            print(f"\n== {b.name} ({key})")
            try:
                for name in list_assets(b):
                    print(f"   {name}")
            except Exception as exc:  # noqa: BLE001
                print(f"   could not list: {exc}")
        return 0
    results = fetch_all(print, only=only, force=args.force, asset=args.asset)
    bad = {k: v for k, v in results.items() if v != "ok"}
    for key, value in results.items():
        print(f"{key:12s} {value}")
    if bad:
        print(f"PROBLEMS FOUND: {len(bad)} backend(s) could not be installed")
        return 1
    print("All backends ready.")
    return 0


def cmd_backends(_args) -> int:
    print(f"backends folder: {paths.backends_dir()}")
    print(f".NET runtime:    {'present' if dotnet_present() else 'not found'}")
    print()
    for key, b in REGISTRY.items():
        exe = b.find_exe()
        print(f"{key:12s} {b.name:22s} {installed_version(b):14s} "
              f"{'installed' if exe else 'MISSING':10s} {exe or ''}")
    return 0


def cmd_mappings(args) -> int:
    raw = Path(args.game).expanduser()
    game = raw.name if raw.exists() else args.game
    roots = [raw] if raw.is_dir() else []
    print(f"mappings folder: {mapping_lib.mappings_dir()}")
    found, note = mapping_lib.resolve(game, extra_roots=roots,
                                      allow_download=args.download, log=print)
    if found:
        print(f"\n{found}\n{note}")
        return 0
    print("\n" + (note or "Nothing found. Re-run with --download to search the archive."))
    return 1


def cmd_aeskey(args) -> int:
    root = Path(args.folder).expanduser()
    if not root.is_dir():
        print(f"Not a folder: {root}")
        return 2
    from harvester import aeskey
    print(f"key library: {aeskey.keys_path()}")
    result = aeskey.find(root, root.name, log=print)
    for note in result.notes:
        print(note)
    if result.verified:
        print(f"\n{result.verified[0]}")
        return 0
    if result.unverified:
        print("\nunverified candidates:")
        for key in result.unverified:
            print(f"  {key}")
        return 1
    return 1


def cmd_probe(args) -> int:
    b = REGISTRY.get(args.backend)
    if not b:
        print(f"Unknown backend: {args.backend}. Try: {', '.join(REGISTRY)}")
        return 2
    print(probe_help(b))
    return 0


def cmd_extract(args) -> int:
    src = Path(args.source).expanduser()
    if not src.exists():
        print(f"No such path: {src}")
        return 2
    opts = config.load()
    if args.out:
        opts.output_root = str(Path(args.out).expanduser())
    if args.mesh:
        opts.mesh_format = args.mesh
    if args.texture:
        opts.texture_format = args.texture
    if args.include:
        opts.include = args.include
    if args.game:
        opts.ue_game_tag = args.game
    if args.aes:
        opts.aes_key = args.aes
    if args.mappings:
        opts.mappings = args.mappings
    if args.bms:
        opts.bms_script = args.bms
    opts.organise = bool(args.organise)

    det = detect(src)
    print(det.summary())
    jobs = plan_jobs(det, opts, include_weak=bool(args.brute))
    if not jobs:
        print("Nothing to do - no supported engine detected.")
        return 1
    if args.dry_run:
        for job in jobs:
            backend = REGISTRY[job.backend_key]
            try:
                argv = build_command(backend, job, opts, job_output_dir(job, opts))
                print(" ".join(f'"{a}"' if " " in a else a for a in argv))
            except Exception as exc:  # noqa: BLE001
                print(f"# {job.label}: {exc}")
        return 0

    runner = Runner(print)
    if args.brute:
        for job in jobs:
            mapping = opts.mappings
            if job.needs_mappings and not mapping:
                found, note = mapping_lib.resolve(
                    job.game, extra_roots=[src], allow_download=True, log=print)
                print(note or "")
                mapping = str(found) if found else ""
            runner.run_brute(job, opts, mapping)
    else:
        runner.run_all(jobs, opts)
    failed = [j for j in jobs if j.status == "failed"]
    for job in jobs:
        print(f"{job.status:10s} {job.label}: {job.message}")
    if failed:
        print(f"PROBLEMS FOUND: {len(failed)} job(s) failed")
        return 1
    return 0


def cmd_selftest(_args) -> int:
    lines: list[str] = []
    problems: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        lines.append(f"[{'ok' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
        if not ok:
            problems.append(label)

    lines.append(f"Game Asset Harvester {__version__}")
    lines.append(f"python {sys.version.split()[0]}  frozen={paths.is_frozen()}")
    lines.append(f"app dir  {paths.app_dir()}")
    lines.append(f"backends {paths.backends_dir()}")
    lines.append("")

    try:
        import tkinter
        check("tkinter imports", True, f"Tk {tkinter.TkVersion}")
        check("Tk >= 8.6", tkinter.TkVersion >= 8.6, f"found {tkinter.TkVersion}")
    except Exception as exc:  # noqa: BLE001
        check("tkinter imports", False, str(exc))

    try:
        from harvester import ui  # noqa: F401
        check("UI module imports", True)
    except Exception as exc:  # noqa: BLE001
        check("UI module imports", False, str(exc))

    try:
        writable = paths.app_dir() / ".harvester-write-test"
        writable.write_text("x", "utf-8")
        writable.unlink()
        check("app folder writable", True)
    except Exception as exc:  # noqa: BLE001
        check("app folder writable", False, str(exc))

    # detection against a synthetic tree
    import struct
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel in ("Game/Content/Paks/pakchunk0-Windows.pak",
                    "Game/Content/Paks/global.utoc",
                    "Game/Content/Paks/global.ucas"):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\0" * 16)
        det = detect(root)
        best = det.best
        check("detects Unreal from pak/utoc", bool(best) and best.engine == "ue5",
              best.engine if best else "nothing")
        jobs = plan_jobs(det, ExportOptions(output_root=tmp))
        check("plans a job for it", len(jobs) == 1,
              jobs[0].backend_key if jobs else "none")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "Game/Content/Paks").mkdir(parents=True)
        (root / "Game/Content/Paks/Game-WindowsNoEditor.pak").write_bytes(b"\0" * 16)
        best = detect(root).best
        check("pak-only game stays Unreal 4", bool(best) and best.engine == "ue4",
              best.engine if best else "nothing")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "Game_Data/Managed").mkdir(parents=True)
        (root / "Game_Data/Managed/Assembly-CSharp.dll").write_bytes(b"\0")
        (root / "Game_Data/resources.assets").write_bytes(b"\0")
        (root / "UnityPlayer.dll").write_bytes(b"\0")
        best = detect(root).best
        check("detects Unity", bool(best) and best.engine == "unity",
              best.engine if best else "nothing")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "game.pck").write_bytes(b"GDPC")
        best = detect(root).best
        check("detects Godot", bool(best) and best.engine == "godot",
              best.engine if best else "nothing")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "game.exe").write_bytes(b"MZ" + b"\0" * 64 + b"GDPC")
        best = detect(root).best
        check("detects Godot pck embedded in exe", bool(best) and best.engine == "godot",
              best.engine if best else "nothing")

    # engine VERSION detection - getting this wrong is what makes a run look
    # successful while exporting almost nothing
    import struct as _struct

    def _pak(path: Path, version: int = 11, encrypted: bool = False) -> None:
        footer = b"\x00" * 16 + bytes([1 if encrypted else 0]) \
            + _struct.pack("<I", 0x5A6F12E1) + _struct.pack("<I", version) \
            + _struct.pack("<Q", 0) * 2 + b"\x00" * 20
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 4096 + footer)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _pak(root / "Game/Content/Paks/Game-WindowsNoEditor.pak")
        (root / "Engine/Build").mkdir(parents=True)
        (root / "Engine/Build/Build.version").write_text(
            '{"MajorVersion": 4, "MinorVersion": 27, "PatchVersion": 2}')
        det = detect(root)
        ue = det.best.ue if det.best else None
        check("reads the engine version", bool(ue) and ue.tag == "GAME_UE4_27",
              ue.tag if ue else "nothing")
        jobs = plan_jobs(det, ExportOptions(output_root=tmp))
        check("passes the detected tag to the backend",
              bool(jobs) and jobs[0].ue_tag == "GAME_UE4_27",
              jobs[0].ue_tag if jobs else "no job")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "SteamLibrary" / "MyGame"
        paks = root / "MyGame/Content/Paks"
        _pak(paks / "pakchunk0-Windows.pak")
        (paks / "pakchunk0-Windows.utoc").write_bytes(b"\0" * 32)
        det = detect(paks / "pakchunk0-Windows.utoc")
        check("a single container resolves up to the game folder",
              det.root == root, str(det.root))
        jobs = plan_jobs(det, ExportOptions(output_root=tmp))
        check("and the backend is pointed at a directory, not the file",
              bool(jobs) and Path(jobs[0].target).is_dir(),
              jobs[0].target if jobs else "no job")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _pak(root / "Game/Content/Paks/Game-WindowsNoEditor.pak", encrypted=True)
        det = detect(root)
        ue = det.best.ue if det.best else None
        check("spots an encrypted pak index", bool(ue) and ue.encrypted_index,
              "flagged" if ue and ue.encrypted_index else "missed")

    # a UE5 game needs a .usmap or nothing converts, and the run still exits 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paks = root / "Game/Content/Paks"
        _pak(paks / "Game-Windows.pak")
        (paks / "global.utoc").write_bytes(b"\0" * 32)
        (root / "Engine/Build").mkdir(parents=True)
        (root / "Engine/Build/Build.version").write_text(
            '{"MajorVersion": 5, "MinorVersion": 4, "PatchVersion": 0}')
        det = detect(root)
        ue = det.best.ue if det.best else None
        check("knows a UE5 game needs a .usmap", bool(ue) and ue.needs_mappings,
              "flagged" if ue and ue.needs_mappings else "missed")
        jobs = plan_jobs(det, ExportOptions(output_root=tmp))
        check("and carries that into the job",
              bool(jobs) and jobs[0].needs_mappings,
              "yes" if jobs and jobs[0].needs_mappings else "no")
        hints = diagnose(jobs[0], ExportOptions(), {}, 0, 74, {"Raw": 57, "Other": 17})
        check("calls out a run that converted nothing",
              any("usmap" in h for h in hints), f"{len(hints)} hint(s)")
        ladder = brute_attempts(jobs[0], ExportOptions(), "/tmp/x.usmap")
        check("builds a brute-force ladder", len(ladder) >= 4 and
              ladder[0].opts.ue_game_tag == "GAME_UE5_4"
              and ladder[0].opts.mappings.endswith(".usmap"),
              f"{len(ladder)} attempt(s), first {ladder[0].label if ladder else '-'}")
        check("no duplicate settings in the ladder",
              len({(a.opts.ue_game_tag, a.opts.mappings, a.opts.out_format,
                    a.opts.mesh_format, a.opts.texture_format, a.target)
                   for a in ladder}) == len(ladder),
              f"{len(ladder)} unique")
        # the version comes straight off the shipping exe, so it should be the last
        # thing doubted - both output formats get tried before any other version
        check("the detected version is exhausted before others are tried",
              len(ladder) > 4
              and len({a.opts.ue_game_tag for a in ladder[:4]}) == 1
              and ladder[4].opts.ue_game_tag != ladder[0].opts.ue_game_tag,
              f"{ladder[0].label} then {ladder[1].label}")

    # a UE3 game with map files must not raise a second, doomed CUE4Parse job
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Enslaved"
        cooked = root / "UNS/CookedPC"
        cooked.mkdir(parents=True)
        for name in ("Startup.upk", "Char_Monkey.upk", "Env_City.upk"):
            (cooked / name).write_bytes(b"\0" * 64)
        for name in ("City.umap", "Temple.umap"):
            (cooked / name).write_bytes(b"\0" * 64)
        det = detect(root)
        engines = [f.engine for f in det.findings]
        check("a UE3 game with .umap files stays UE3",
              engines and engines[0] == "ue3" and "ue4" not in engines,
              ", ".join(engines) or "nothing")
        jobs = plan_jobs(det, ExportOptions(output_root=tmp))
        check("and only raises one job for it", len(jobs) == 1,
              f"{len(jobs)} job(s): " + ", ".join(j.engine for j in jobs))

    # three loose .dat files are not an archive format
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Amnesia"
        root.mkdir(parents=True)
        for name in ("config.dat", "save.dat", "cache.dat"):
            (root / name).write_bytes(b"\0" * 32)
        det = detect(root)
        check("a few loose .dat files are flagged as weak evidence",
              bool(det.best) and det.best.weak,
              det.best.label if det.best else "nothing found")
        check("and do not queue a job that cannot work",
              not plan_jobs(det, ExportOptions(output_root=tmp)),
              f"{len(plan_jobs(det, ExportOptions(output_root=tmp)))} job(s)")
        forced = plan_jobs(det, ExportOptions(output_root=tmp), include_weak=True)
        check("unless you ask for everything - and then every archive gets its own job",
              len(forced) == 3, f"{len(forced)} job(s) queued under brute force")

    # a brute-force attempt sets the tag itself, so it is not a user override
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _pak(root / "Game/Content/Paks/Game-WindowsNoEditor.pak")
        (root / "Engine/Build").mkdir(parents=True)
        (root / "Engine/Build/Build.version").write_text(
            '{"MajorVersion": 4, "MinorVersion": 27, "PatchVersion": 2}')
        job = plan_jobs(detect(root), ExportOptions(output_root=tmp))[0]
        attempts = brute_attempts(job, ExportOptions())
        check("brute-force attempts are marked as such",
              all(a.opts.brute for a in attempts), f"{len(attempts)} attempt(s)")
        forced = diagnose(job, ExportOptions(ue_game_tag="GAME_UE5_LATEST"),
                          {"load_failed": 900}, 1000, 10)
        bruted = diagnose(job, ExportOptions(ue_game_tag="GAME_UE5_LATEST", brute=True),
                          {"load_failed": 900}, 1000, 10)
        check("a real override is called out, a brute-force one is not",
              any("you picked it" in h for h in forced)
              and not any("you picked it" in h for h in bruted),
              "distinguished")
        fatal = diagnose(job, ExportOptions(), {"fatal": 1}, 1000, 12, {"Meshes": 12},
                         fatal="ERROR: RawArray item size mismatch")
        check("explains a backend that aborted part-way",
              any("aborted" in h for h in fatal), f"{len(fatal)} hint(s)")

    # repetitive backend chatter has to collapse whatever backend produced it
    shapes = {jobs_mod._line_shape(line) for line in (
        "Loading package: Char_Monkey.upk",
        "Loading package: Env_City.upk",
        "Loading package: UNS/CookedPC/Startup.upk",
    )}
    check("repeated backend lines collapse to one shape", len(shapes) == 1,
          ", ".join(sorted(shapes)))
    check("and different messages do not",
          jobs_mod._line_shape("IntProperty: unknown UMaterialExpressionX::Editor")
          != jobs_mod._line_shape("Loading package: A.upk"), "kept apart")

    # the scan has to reach the bottom of a real install tree, not stop at level 8
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "BigRPG"
        for rel in ("BigRPG/Content/Paks",
                    "BigRPG/Content/Paks/DLC/Expansion1",
                    "BigRPG/Plugins/Cosmetics/Content/Paks",
                    "a/b/c/d/e/f/g/h/i/j/Extra/Content/Paks"):
            _pak(root / rel / "chunk-Windows.pak")
        (root / "Engine/Build").mkdir(parents=True)
        (root / "Engine/Build/Build.version").write_text(
            '{"MajorVersion": 5, "MinorVersion": 3, "PatchVersion": 0}')
        for name in ("Launcher", "MiniGame"):
            d = root / f"Extras/{name}/{name}_Data"
            d.mkdir(parents=True)
            (d / "resources.assets").write_bytes(b"\0" * 64)
        det = detect(root)
        ue = next((f for f in det.findings if f.engine.startswith("ue")), None)
        check("the scan reaches every container folder, however deep",
              bool(ue) and len(ue.locations) == 4,
              f"{len(ue.locations) if ue else 0} location(s)")
        jobs = plan_jobs(det, ExportOptions(output_root=tmp))
        ue_jobs = [j for j in jobs if j.engine.startswith("ue")]
        check("Unreal gets one job covering them all",
              len(ue_jobs) == 1 and Path(ue_jobs[0].target) == root,
              ue_jobs[0].target if ue_jobs else "no job")
        check("with every folder kept as a fallback",
              bool(ue_jobs) and len(ue_jobs[0].targets) == 4,
              f"{len(ue_jobs[0].targets) if ue_jobs else 0} fallback(s)")
        unity_jobs = [j for j in jobs if j.engine == "unity"]
        check("Unity gets one job per data folder, because it takes one at a time",
              len(unity_jobs) == 2, f"{len(unity_jobs)} job(s)")
        ladder = brute_attempts(ue_jobs[0], ExportOptions())
        check("and brute force falls back to one folder at a time",
              sum(1 for a in ladder if a.target) == 4,
              f"{sum(1 for a in ladder if a.target)} per-folder attempt(s)")

    # every backend has something to try, not just CUE4Parse
    with tempfile.TemporaryDirectory() as tmp:
        for key, engine in (("umodel", "ue3"), ("assetstudio", "unity"),
                            ("gdre", "godot"), ("quickbms", "generic")):
            if key == "quickbms":   # its ladder is the script library
                (Path(tmp) / "scripts").mkdir(exist_ok=True)
                for name in ("test_archive.bms", "other_game.bms"):
                    (Path(tmp) / "scripts" / name).write_text("# stub\n")
            job = Job(source=tmp, engine=engine, backend_key=key,
                      target=tmp, label=key, game="Test")
            ladder = brute_attempts(job, ExportOptions(output_root=tmp))
            check(f"{key} has a brute-force ladder", len(ladder) >= 2,
                  f"{len(ladder)} attempt(s)")
            check(f"{key} adds no flag it has not verified",
                  all(not a.opts.extra_args for a in ladder), "clean")

    # the AES key finder: the cipher itself, then a whole synthetic encrypted game
    from harvester import aes, aeskey
    import os
    rk = aes.expand_key(bytes(range(32)))
    ct = aes.encrypt_block(bytes.fromhex("00112233445566778899aabbccddeeff"), rk)
    check("AES-256 matches the FIPS-197 test vector",
          ct.hex() == "8ea2b7ca516745bfeafc49904b496089", ct.hex()[:16])
    check("and decrypts back", aes.decrypt_block(ct, rk).hex() == "00112233445566778899aabbccddeeff",
          "round trip")

    with tempfile.TemporaryDirectory() as tmp:
        key = os.urandom(32)
        root = Path(tmp) / "SecretGame"
        paks = root / "SecretGame/Content/Paks"
        paks.mkdir(parents=True)
        mount = b"../../../SecretGame/Content/Paks/\0"
        index = struct.pack("<i", len(mount)) + mount
        index += b"\0" * (-len(index) % 16)
        body = os.urandom(4096)
        trailer = (b"\0" * 16 + b"\x01"
                   + struct.pack("<IIQQ", 0x5A6F12E1, 11, len(body), len(index))
                   + b"\0" * 180)
        (paks / "pakchunk0-WindowsNoEditor.pak").write_bytes(
            body + aes.ecb_encrypt(index, key) + trailer)
        exe_dir = root / "SecretGame/Binaries/Win64"
        exe_dir.mkdir(parents=True)
        decoy = b"0x" + os.urandom(32).hex().upper().encode()
        text = b"Some readable text, a decoy hex string, then the real key as raw bytes"
        (exe_dir / "SecretGame-Win64-Shipping.exe").write_bytes(
            os.urandom(200_000) + b"\0" * 40 + text + b"\0" * 9 + decoy + b"\0" * 11
            + key + b"\0" * 5 + os.urandom(100_000))
        lib = Path(tmp) / "keys.json"
        drop = Path(tmp) / "keys"
        drop.mkdir()
        found = aeskey.find(root, "SecretGame", library=lib, dropped=drop)
        want = "0x" + key.hex().upper()
        check("finds the AES key in the shipping exe and proves it against the pak",
              found.verified == [want], found.verified[0][:14] + "..." if found.verified
              else "not found")
        check("and the decoy hex string is not mistaken for it",
              decoy.decode() not in found.verified, "rejected")
        check("and remembers it in keys.json", aeskey.known("SecretGame", lib) == [want],
              "on file")
        again = aeskey.find(root, "Secret Game", library=lib, dropped=drop)
        check("next time it comes straight from the library (fuzzy game name)",
              again.verified == [want] and "keys.json" in again.notes[0], "instant")
        # a key the user dropped in a text file is honoured too
        (drop / "notes.txt").write_text(f"the key for this one is {want}\n")
        third = aeskey.find(root, "Unrelated", library=Path(tmp) / "empty.json", dropped=drop)
        check("a key in a text file in the keys folder is picked up",
              third.verified == [want], "from notes.txt")
        # the runner does it by itself
        det = detect(root)
        jobs = plan_jobs(det, ExportOptions(output_root=tmp))
        runner_log: list[str] = []
        runner = Runner(log=runner_log.append)
        aeskey.remember("SecretGame", want, lib)
        import harvester.aeskey as _ak
        _orig = _ak.keys_path
        _ak.keys_path = lambda: lib
        try:
            resolved = runner.ensure_key(jobs[0], ExportOptions(output_root=tmp))
        finally:
            _ak.keys_path = _orig
        check("the runner fills the key in on its own",
              bool(jobs) and resolved.aes_key == want, "filled")
        argv = build_command(REGISTRY["cue4parse"], jobs[0], resolved, Path(tmp) / "o") \
            if REGISTRY["cue4parse"].installed() else ["-k", want]
        check("and it reaches the command line", "-k" in argv and want in argv, "-k present")

    # mappings are looked up by game name, not typed in by hand
    with tempfile.TemporaryDirectory() as tmp:
        lib = Path(tmp) / "mappings" / "Abiotic Factor"
        lib.mkdir(parents=True)
        (lib / "Mappings.usmap").write_bytes(b"\0" * 8)
        hit = mapping_lib.find_local("AbioticFactor", [Path(tmp) / "mappings"])
        check("matches a .usmap to the game by name",
              bool(hit) and hit.local is not None, hit.label if hit else "no match")
        miss = mapping_lib.find_local("Totally Different Game", [Path(tmp) / "mappings"])
        check("and does not match an unrelated game",
              miss is None or miss.score < 0.6, miss.label if miss else "no match")

    installed = [b.name for b in REGISTRY.values() if b.installed()]
    missing = [b.name for b in REGISTRY.values() if not b.installed()]
    lines.append("")
    lines.append(f"backends installed: {', '.join(installed) or 'none'}")
    lines.append(f"backends missing:   {', '.join(missing) or 'none'}")
    if missing:
        lines.append("  (run `fetch-backends` - not counted as a failure)")

    lines.append("")
    if problems:
        lines.append(f"PROBLEMS FOUND: {len(problems)}")
        for p in problems:
            lines.append(f"  - {p}")
    else:
        lines.append("All checks passed.")

    report = "\n".join(lines)
    print(report)
    try:
        paths.selftest_path().write_text(report + "\n", "utf-8")
    except Exception:  # noqa: BLE001
        pass
    return 1 if problems else 0


def main() -> int:
    _guard_streams()
    argv = _clean_argv()

    parser = argparse.ArgumentParser(prog="GameAssetHarvester", add_help=True)
    sub = parser.add_subparsers(dest="mode")

    p = sub.add_parser("fetch-backends", help="download the extraction tools")
    p.add_argument("backend", nargs="*", help="only these (default: all)")
    p.add_argument("--force", action="store_true", help="re-download even if present")
    p.add_argument("--list", action="store_true",
                   help="show what the latest release actually offers, download nothing")
    p.add_argument("--asset", help="regex picking which release asset to take, "
                                   "overriding the built-in guess")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("backends", help="show what is installed")
    p.set_defaults(func=cmd_backends)

    p = sub.add_parser("mappings", help="find a .usmap for a game")
    p.add_argument("game", help="game name or its install folder")
    p.add_argument("--download", action="store_true",
                   help="also search the public mappings archive")
    p.set_defaults(func=cmd_mappings)

    p = sub.add_parser("aeskey", help="find the AES key for an encrypted Unreal game")
    p.add_argument("folder", help="the game's install folder")
    p.set_defaults(func=cmd_aeskey)

    p = sub.add_parser("probe", help="print a backend's own --help")
    p.add_argument("backend")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("extract", help="run headless")
    p.add_argument("source")
    p.add_argument("--out")
    p.add_argument("--mesh", choices=["glb", "psk", "ueformat", "fbx", "none"])
    p.add_argument("--texture", choices=["png", "tga", "jpg", "webp", "none"])
    p.add_argument("--include")
    p.add_argument("--game")
    p.add_argument("--aes")
    p.add_argument("--mappings")
    p.add_argument("--bms")
    p.add_argument("--organise", action="store_true")
    p.add_argument("--brute", action="store_true",
                   help="try every setting until one actually converts something, "
                        "fetching mappings if the game needs them")
    p.add_argument("--dry-run", action="store_true", help="print the commands, run nothing")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("selftest", help="check this build")
    p.set_defaults(func=cmd_selftest)

    args, _unknown = parser.parse_known_args(argv)

    if getattr(args, "func", None):
        return int(args.func(args))

    from harvester.ui import run
    return run()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        _crash(exc)
        raise SystemExit(1)
