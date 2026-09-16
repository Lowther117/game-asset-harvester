"""Settings persisted beside the exe."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from . import paths
from .jobs import ExportOptions


def load() -> ExportOptions:
    opts = ExportOptions(output_root=str(paths.default_output_dir()))
    try:
        data = json.loads(paths.settings_path().read_text("utf-8"))
    except Exception:
        return opts
    for key, value in data.items():
        if hasattr(opts, key) and isinstance(value, type(getattr(opts, key))):
            setattr(opts, key, value)
    if not opts.output_root:
        opts.output_root = str(paths.default_output_dir())
    return opts


def save(opts: ExportOptions) -> None:
    try:
        paths.settings_path().write_text(json.dumps(asdict(opts), indent=2), "utf-8")
    except Exception:
        pass
