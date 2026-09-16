"""Where things live: app dir, backends dir, settings, bundled resources.

Rules from the house build standard:
  * data (settings, logs, manifests) lives BESIDE the exe, never inside a bundle
  * bundled resources live under sys._MEIPASS, and we search both places
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Folder the app writes to. Beside the exe when frozen, repo root otherwise."""
    if is_frozen():
        exe = Path(sys.executable).resolve()
        # macOS .app bundle: write to the folder CONTAINING the bundle
        parts = exe.parts
        for i, part in enumerate(parts):
            if part.endswith(".app"):
                return Path(*parts[:i]).resolve()
        return exe.parent
    return Path(__file__).resolve().parent.parent


def resource_dirs() -> list[Path]:
    """Places a bundled backend might have been baked into, best first."""
    dirs: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass))
    dirs.append(app_dir())
    dirs.append(Path(__file__).resolve().parent.parent)
    out: list[Path] = []
    for d in dirs:
        if d not in out:
            out.append(d)
    return out


def backends_dir() -> Path:
    """Writable backend folder beside the app. Created on demand."""
    d = app_dir() / "backends"
    d.mkdir(parents=True, exist_ok=True)
    return d


def bundled_backends_dirs() -> list[Path]:
    return [d / "backends" for d in resource_dirs()]


def scripts_dir() -> Path:
    """Library of QuickBMS .bms scripts beside the app, mirroring mappings\\."""
    d = app_dir() / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_path() -> Path:
    return app_dir() / "harvester-settings.json"


def cache_path() -> Path:
    return app_dir() / "harvester-cache.json"


def log_path() -> Path:
    return app_dir() / "harvester-log.txt"


def crash_path() -> Path:
    return app_dir() / "harvester-crash.log"


def selftest_path() -> Path:
    return app_dir() / "harvester-selftest.txt"


def downloads_dir() -> Path:
    """The user's real Downloads folder (Windows asks the shell, so a relocated
    Downloads is honoured); ~/Downloads elsewhere; home if there is none."""
    home = Path(os.path.expanduser("~"))
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            # FOLDERID_Downloads {374DE290-123F-4565-9164-39C4925E467B}
            fid = GUID(0x374DE290, 0x123F, 0x4565,
                       (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(fid), 0, None, ctypes.byref(out)) == 0:
                found = Path(out.value or "")
                ctypes.windll.ole32.CoTaskMemFree(out)
                if found.is_dir():
                    return found
        except Exception:  # noqa: BLE001
            pass
    d = home / "Downloads"
    return d if d.is_dir() else home


def default_save_dir() -> Path:
    """Where anything the app saves goes by default: the "save_dir" the user chose
    in harvester-settings.json if it still exists, otherwise Downloads."""
    try:
        data = json.loads(settings_path().read_text("utf-8"))
        chosen = str(data.get("save_dir") or "").strip()
        if chosen and Path(chosen).is_dir():
            return Path(chosen)
    except Exception:  # noqa: BLE001
        pass
    return downloads_dir()


def default_output_dir() -> Path:
    """The extraction tree: <default save folder>/HarvestedAssets/<Game>/<engine>."""
    return default_save_dir() / "HarvestedAssets"
