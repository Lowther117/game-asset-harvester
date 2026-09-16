"""Turning a detection into a command, running it, and tidying what comes out."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict, replace
from pathlib import Path
from functools import lru_cache
from typing import Callable, Iterable, NamedTuple

from . import aeskey, paths
from .backends import (Backend, ENGINE_PREFERENCE, REGISTRY,
                       backend_for_engine, probe_help)
from .detect import Detection, Finding, all_targets, input_target

MESH_FORMATS = {
    "glb": "glTF 2.0 (.glb)",
    "psk": "ActorX (.psk / .pskx)",
    "ueformat": "UEFormat (.uemodel)",
    "fbx": "FBX (.fbx) - CUE4Parse has no FBX writer and produces glTF 2 instead",
    "none": "do not convert meshes",
}
TEXTURE_FORMATS = {
    "png": "PNG",
    "tga": "TGA",
    "jpg": "JPEG",
    "webp": "WebP",
    "none": "do not convert textures",
}
MAX_JOBS_PER_ENGINE = 24     # a game with dozens of loose archives still finishes

UE_GAME_TAGS = [
    "auto",
    "GAME_UE5_LATEST", "GAME_UE5_6", "GAME_UE5_5", "GAME_UE5_4", "GAME_UE5_3",
    "GAME_UE5_2", "GAME_UE5_1", "GAME_UE5_0", "GAME_UE4_27", "GAME_UE4_26",
    "GAME_UE4_25", "GAME_UE4_24", "GAME_UE4_23", "GAME_UE4_22", "GAME_UE4_21",
    "GAME_UE4_20", "GAME_UE4_LATEST",
]

CATEGORIES = {
    "Meshes": {".gltf", ".glb", ".psk", ".pskx", ".psa", ".fbx", ".obj", ".uemodel",
               ".ueanim", ".uepose", ".usd", ".usda", ".usdz", ".dae", ".md5mesh"},
    "Textures": {".png", ".tga", ".dds", ".jpg", ".jpeg", ".webp", ".bmp", ".hdr",
                 ".exr", ".tif", ".tiff", ".ktx", ".ktx2"},
    "Audio": {".wav", ".ogg", ".mp3", ".flac", ".bnk", ".wem", ".at9", ".opus"},
    "Data": {".json", ".txt", ".ini", ".xml", ".csv", ".yaml", ".yml", ".gd", ".cs",
             ".lua", ".cfg", ".locres", ".tres", ".tscn"},
    "Raw": {".uasset", ".uexp", ".ubulk", ".uptnl", ".umap", ".upk", ".u", ".udk",
            ".assets", ".resS", ".bytes", ".bin", ".dat"},
}


@dataclass
class ExportOptions:
    output_root: str = ""
    mesh_format: str = "glb"
    texture_format: str = "png"
    include: str = "*"
    ue_game_tag: str = "auto"   # "auto" = use whatever the scan detected
    aes_key: str = ""
    mappings: str = ""
    unity_types: str = "tex2d,sprite,mesh,audio,textAsset"
    unity_group: str = "container"   # AssetStudio -g: container | type | source | none
    bms_script: str = ""
    organise: bool = False
    write_manifest: bool = True
    overwrite: bool = True
    extra_args: str = ""
    out_format: str = "auto"     # CUE4Parse -f: auto converts everything it can,
                                 # png restricts to textures and skips sound decoding
    godot_mode: str = "auto"     # auto | recover | extract
    backend_override: str = ""   # brute force trying the other backend for this engine
    brute: bool = False          # set by the brute-force ladder, not by the user; stops
                                 # the log claiming "you overrode the detected value"
    dark: bool = True            # UI theme; lives here so it shares harvester-settings.json
    save_dir: str = ""           # default save folder; empty = the user's Downloads


@dataclass
class Job:
    source: str
    engine: str
    backend_key: str
    target: str
    label: str = ""
    engine_label: str = ""
    game: str = ""               # the game's folder name, used to look up mappings
    ue_tag: str = ""             # detected -g tag, used unless the user overrides
    targets: list[str] = field(default_factory=list)  # every container folder found
    needs_key: bool = False      # the pak index is encrypted
    key_candidates: list[str] = field(default_factory=list)  # unverified guesses to try
    needs_mappings: bool = False # unversioned properties: a .usmap is required
    notes: list[str] = field(default_factory=list)
    status: str = "queued"       # queued | running | done | failed | cancelled
    message: str = ""
    produced: int = 0
    out_dir: str = ""
    started: float = 0.0
    finished: float = 0.0

    @property
    def duration(self) -> float:
        if not self.started:
            return 0.0
        return (self.finished or time.time()) - self.started


# --------------------------------------------------------------------------
# Command construction
# --------------------------------------------------------------------------

class UnsupportedJob(RuntimeError):
    pass


def _split_extra(extra: str) -> list[str]:
    import shlex
    if not extra.strip():
        return []
    if os.name != "nt":
        return shlex.split(extra)
    # non-posix mode keeps Windows backslashes intact but leaves the quotes on
    # each token; strip them or the backend receives a literal "quoted path"
    out = []
    for tok in shlex.split(extra, posix=False):
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        out.append(tok)
    return out


def resolve_ue_tag(job: "Job", opts: ExportOptions) -> str:
    """The engine tag to hand the backend.

    The scan's answer wins unless the user picked a specific tag, because the
    commonest cause of a run that writes almost nothing is a default tag that
    does not match the game.
    """
    chosen = (opts.ue_game_tag or "auto").strip()
    if chosen and chosen.lower() != "auto":
        return chosen
    return job.ue_tag or "GAME_UE5_LATEST"


def build_command(backend: Backend, job: Job, opts: ExportOptions, out_dir: Path) -> list[str]:
    exe = backend.find_exe()
    if not exe:
        raise UnsupportedJob(f"{backend.name} is not installed - fetch it on the Backends tab.")
    target = Path(job.target)
    argv: list[str]

    if backend.key == "cue4parse":
        tag = resolve_ue_tag(job, opts)
        argv = [str(exe), "-i", str(target), "-o", str(out_dir),
                "-p", opts.include or "*", "-g", tag]
        if opts.texture_format != "none":
            argv += ["--texture-format", {"png": "Png", "tga": "Tga",
                                          "jpg": "Jpeg", "webp": "Webp"}[opts.texture_format]]
        if opts.mesh_format != "none":
            mesh = {"glb": "Gltf2", "psk": "ActorX", "ueformat": "UEFormat",
                    "fbx": "Gltf2"}[opts.mesh_format]
            argv += ["--mesh-format", mesh]
        if opts.aes_key.strip():
            argv += ["-k", opts.aes_key.strip()]
        if opts.mappings.strip():
            argv += ["-m", opts.mappings.strip()]
        if opts.out_format and opts.out_format != "auto":
            argv += ["-f", opts.out_format]
        if opts.overwrite:
            argv += ["-y"]

    elif backend.key == "umodel":
        argv = [str(exe), f"-path={target}", f"-out={out_dir}", "-export"]
        if opts.mesh_format == "glb":
            argv.append("-gltf")
        if opts.texture_format == "png":
            argv.append("-png")
        elif opts.texture_format == "tga":
            argv.append("-tga")
        if opts.aes_key.strip():
            argv.append(f"-aes={opts.aes_key.strip()}")
        argv.append(opts.include or "*")

    elif backend.key == "assetstudio":
        argv = [str(exe), str(target), "-o", str(out_dir), "-m", "export"]
        if opts.unity_types.strip():
            argv += ["-t", opts.unity_types.strip()]
        argv += ["--image-format", "none" if opts.texture_format == "none" else opts.texture_format]
        argv += ["-g", opts.unity_group or "container"]
        if opts.include and opts.include != "*":
            argv += ["--filter-by-name", opts.include]

    elif backend.key == "gdre":
        if opts.godot_mode == "recover":
            mode = "--recover"
        elif opts.godot_mode == "extract":
            mode = "--extract"
        else:
            mode = ("--recover" if target.suffix.lower() in (".pck", ".exe", ".apk")
                    else "--extract")
        argv = [str(exe), "--headless", f"{mode}={target}", f"--output={out_dir}"]

    elif backend.key == "quickbms":
        if not opts.bms_script.strip():
            raise UnsupportedJob(
                "QuickBMS needs a .bms script for this game. Download one from "
                "aluigi.altervista.org and pick it in the Generic options.")
        argv = [str(exe), "-o", opts.bms_script.strip(), str(target), str(out_dir)]

    else:
        raise UnsupportedJob(f"No command builder for {backend.name}")

    argv += _split_extra(opts.extra_args)
    return argv


def plan_jobs(detection: Detection, opts: ExportOptions,
              prefer: dict[str, str] | None = None,
              include_weak: bool = False) -> list[Job]:
    """One job per engine found under a source folder.

    Findings flagged weak (a few .dat files and nothing else) are skipped unless
    include_weak is set - queueing them only produces a backend run that cannot work.
    """
    prefer = prefer or {}
    jobs: list[Job] = []
    for finding in detection.findings:
        if finding.weak and not include_weak:
            continue
        backend = backend_for_engine(finding.engine, prefer.get(finding.engine))
        if not backend:
            continue
        spots = all_targets(detection.root, finding)
        # Unreal takes a directory and walks it, so one job covers every container
        # under it. The others take ONE archive or ONE data folder at a time, so a
        # game with several gets one job each - otherwise everything but the first
        # is silently skipped.
        if finding.engine in ("ue1", "ue2", "ue3", "ue4", "ue5"):
            primary = [input_target(detection.root, finding)]
            fallbacks = [str(t) for t in spots]
        else:
            primary = spots[:MAX_JOBS_PER_ENGINE]
            fallbacks = []      # each location already has its own job
        for n, target in enumerate(primary, 1):
            suffix = ""
            if len(primary) > 1:
                try:
                    suffix = f"  [{Path(target).relative_to(detection.root)}]"
                except ValueError:
                    suffix = f"  [{Path(target).name}]"
            job = Job(
                source=str(detection.root),
                engine=finding.engine,
                backend_key=backend.key,
                target=str(target),
                targets=fallbacks or [str(target)],
                label=f"{finding.label} via {backend.name}{suffix}",
                engine_label=finding.label,
                game=Path(detection.root).name,
            )
            if finding.ue:
                job.ue_tag = finding.ue.tag
                job.needs_key = finding.ue.encrypted_index
                job.needs_mappings = finding.ue.needs_mappings
                job.notes = list(finding.ue.evidence)
            if len(spots) > 1 and finding.engine.startswith("ue"):
                job.notes = list(job.notes) + [
                    f"{len(spots)} container folder(s) found; pointing the backend at "
                    f"{target} so one pass covers them all"]
            jobs.append(job)
    return jobs


def job_output_dir(job: Job, opts: ExportOptions) -> Path:
    root = Path(opts.output_root or paths.default_output_dir())
    game = Path(job.source).name or "extracted"
    return root / _safe(game) / job.engine


def _safe(name: str) -> str:
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad else c for c in name).strip(" .")
    return out or "extracted"


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

FAILURE_PATTERNS = {
    "load_failed": re.compile(
        r"could not load .*asset|check game version|failed to (load|read) package", re.I),
    "key_failed": re.compile(r"aes|decrypt|encrypted", re.I),
    "mappings": re.compile(r"mappings|usmap|unversioned propert", re.I),
    "sound_crash": re.compile(r"SoundDecoder|USoundWave", re.I),
    # umodel prints this and stops dead - the export ends there, however many
    # packages were left. Without this the run just looks like it "finished".
    "fatal": re.compile(r"\*\*\*\s*ERROR:|Fatal [Ee]rror|Unhandled [Ee]xception", re.I),
}
PROGRESS_PATTERN = re.compile(r"package\s+(\d+)\s+of\s+(\d+)", re.I)

SHOW_PER_SHAPE = 8      # lines of any one shape shown in full before collapsing
REPEAT_EVERY = 2000     # ...then one reminder every this many

_SHAPE_NUM = re.compile(r"\b(?:0x)?[0-9a-fA-F]{4,}\b|\d+")
_SHAPE_PATH = re.compile(r"[\w./\\-]*[/\\][\w./\\-]*")
_SHAPE_FILE = re.compile(r"^[\w.\-]+\.[A-Za-z0-9]{1,6}$")


def _line_shape(line: str) -> str:
    """A key that is the same for every line the backend repeats.

    Paths, numbers and per-asset identifiers are stripped out, then only the first
    few words are kept, so "Loading package: A.upk" and "Loading package: B.upk"
    collapse together - and so do lines whose tail is a different class name each
    time, which is what makes umodel's output unreadable.
    """
    text = _SHAPE_PATH.sub("<file>", line.strip())
    text = _SHAPE_NUM.sub("#", text)
    words = []
    for word in text.split()[:3]:
        # a long identifier or a qualified name is per-asset detail, not shape
        if len(word) > 18 or "::" in word:
            word = "<id>"
        elif _SHAPE_FILE.match(word):
            word = "<file>"
        words.append(word)
    return " ".join(words)[:80] or text[:80]


MAPPINGS_ADVICE = (
    "Dump one with UE4SS in-game (its DumpMappings command), or find a community "
    ".usmap for this title, then put it in the Mappings field."
)


def diagnose(job: "Job", opts: ExportOptions, counts: dict[str, int],
             total_packages: int, produced: int,
             categories: dict[str, int] | None = None,
             fatal: str = "") -> list[str]:
    """Turn a wall of backend errors - or a quiet success - into what is wrong.

    The output shape matters as much as the exit code: a run that writes only
    raw .ubulk payloads and exits 0 has not worked, it has just failed quietly.
    """
    hints: list[str] = []
    categories = categories or {}

    if fatal:
        hints.append(f"The backend aborted: {fatal}")
        if job.backend_key == "umodel":
            hints.append("umodel stops the entire export on the first asset it cannot "
                         "parse, so everything after that point was never reached. "
                         "Anything already written is still usable.")
            hints.append("Narrow the export (one package folder at a time) to get past the "
                         "bad asset, or override the engine version - a mismatched version "
                         "is the usual cause of a size-mismatch abort.")
        else:
            hints.append("Whatever was written before the abort is still usable; the rest "
                         "of the archive was never reached.")
        if produced == 0:
            return hints
    converted = sum(categories.get(k, 0) for k in ("Meshes", "Textures", "Audio"))
    raw = sum(categories.get(k, 0) for k in ("Raw", "Other", "Data"))

    if produced and converted == 0 and raw:
        hints.append(f"{raw} file(s) were written but not one was converted - these are raw "
                     f"payloads, not meshes or textures.")
        if job.needs_mappings and not opts.mappings.strip():
            hints.append("This game stores object properties UNVERSIONED, so a .usmap "
                         "mappings file is required before any texture or mesh can be "
                         "decoded. Without one this is exactly what you get: .ubulk "
                         "payloads and nothing else, with a successful exit code.")
            hints.append(MAPPINGS_ADVICE)
        elif counts.get("sound_crash"):
            hints.append("The run died inside CUE4Parse's sound decoder, which kills the "
                         "whole export before the conversion stage. Set the output format "
                         "to png (textures only, sounds skipped) to get past it.")
        elif opts.mesh_format == "none" and opts.texture_format == "none":
            hints.append("Both mesh and texture formats are set to 'none', so there was "
                         "nothing to convert.")
        else:
            hints.append("Mappings and engine version look right, so the export stage "
                         "itself did not complete - check the log for a crash, and note "
                         "that CUE4Parse only writes converted assets after walking the "
                         "whole archive, so a cancelled run leaves only raw payloads.")
        return hints

    failed = counts.get("load_failed", 0)
    if not failed:
        return hints

    coverage = (produced / total_packages) if total_packages else 0.0
    hints.append(f"{failed} asset(s) failed to load"
                 + (f" out of {total_packages} packages" if total_packages else ""))

    if job.needs_key and not opts.aes_key.strip():
        hints.append("This game's pak index is encrypted and no AES key could be found in "
                     "the shipping exe. Find the key for this title and paste it into the "
                     "AES field (0x followed by 64 hex characters), or drop a text file "
                     "containing it in the keys folder beside the app.")
        return hints
    if job.needs_key and counts.get("key_failed"):
        hints.append(f"The backend rejected the AES key {opts.aes_key[:12]}... - it may be "
                     "the wrong one, or this game uses a different key per pak.")

    tag = resolve_ue_tag(job, opts)
    if coverage < 0.5:
        forced = (opts.ue_game_tag or "auto").lower() != "auto" and not opts.brute
        if forced and job.ue_tag and job.ue_tag != tag:
            hints.append(f"The run used {tag} because you picked it, but the scan "
                         f"detected {job.ue_tag}. Set UE version back to 'auto' and retry.")
        else:
            hints.append(f"Most assets failed with {tag}, which usually means the engine "
                         f"version is wrong. Try the neighbouring versions"
                         + (f" around {job.ue_tag}" if job.ue_tag else "") + ".")
        if job.needs_mappings and not opts.mappings.strip():
            hints.append("This game also needs a .usmap mappings file - its properties are "
                         "unversioned, so nothing can be decoded without one. " + MAPPINGS_ADVICE)
        elif counts.get("mappings"):
            hints.append("Some errors mention mappings - a .usmap file for this game "
                         "would also help, especially for UE5 titles.")
    return hints


CONVERTED_EXTS = CATEGORIES["Meshes"] | CATEGORIES["Textures"] | CATEGORIES["Audio"]
PROBE_PACKAGES = 400      # judge an attempt after this many packages
PROBE_SECONDS = 120       # ...or this long, whichever comes first
PROBE_FAIL_RATIO = 0.25   # abandon an attempt failing to load this share of packages

# Why the probe watches ERRORS and not output files: CUE4Parse collects every
# exportable object into one session and converts it after the whole archive has
# been walked. Raw .ubulk sidecars appear as it goes, the PNGs and glTFs only at
# the very end. Judging an attempt by "has it written a PNG yet" therefore kills
# every attempt, including the correct one, long before it could produce anything.


def count_output(out_dir: Path) -> tuple[int, int]:
    """(converted, total) files under here. Raw payloads are not converted."""
    if not out_dir.is_dir():
        return 0, 0
    converted = total = 0
    for p in out_dir.rglob("*"):
        if not p.is_file() or p.name == "manifest.json":
            continue
        total += 1
        if p.suffix.lower() in CONVERTED_EXTS:
            converted += 1
        if converted > 4:
            return converted, total
    return converted, total


def count_converted(out_dir: Path) -> int:
    return count_output(out_dir)[0]


class Attempt(NamedTuple):
    """One thing to try: a label, the settings, and what to point at."""
    label: str
    opts: ExportOptions
    target: str = ""      # empty = the job's own target


BRUTE_MAX = 24            # a ceiling, not a target - most games take one or two


@lru_cache(maxsize=16)
def supported_flags(backend_key: str) -> frozenset[str]:
    """Flags the INSTALLED backend actually advertises in its own --help.

    Every one of these tools renames its options between releases. Reading the
    help once and filtering candidate flags against it means extra options can be
    thrown at a stubborn game without the risk that an unknown flag turns a run
    that would have half-worked into an instant parse error.
    """
    backend = REGISTRY.get(backend_key)
    if not backend or not backend.installed():
        return frozenset()
    try:
        text = probe_help(backend, timeout=20)
    except Exception:  # noqa: BLE001
        return frozenset()
    return frozenset(re.findall(r"(?<![\w-])(--?[A-Za-z][\w-]{1,30})", text or ""))


def _with_flags(opts: ExportOptions, backend_key: str, *flags: str) -> ExportOptions:
    """Append only the flags this build of the backend admits to understanding."""
    ok = supported_flags(backend_key)
    # if the help could not be read, add nothing: an unknown flag can turn a run
    # that would have half-worked into an instant parse error
    keep = [f for f in flags if f.split("=")[0] in ok]
    if not keep:
        return opts
    extra = (opts.extra_args + " " + " ".join(keep)).strip()
    return replace(opts, extra_args=extra)


def ue_tag_order(detected: str) -> list[str]:
    """Engine tags to try, nearest the detected one first."""
    detected = detected or "GAME_UE5_LATEST"
    major = "5" if "UE5" in detected else "4"
    same = [t for t in UE_GAME_TAGS[1:] if f"UE{major}" in t]
    order = [detected]
    if detected in same:
        i = same.index(detected)
        for step in (1, -1, 2, -2, 3, -3, 4, -4):
            j = i + step
            if 0 <= j < len(same) and same[j] not in order:
                order.append(same[j])
    for extra in ("GAME_UE5_LATEST", "GAME_UE4_LATEST"):
        if extra not in order:
            order.append(extra)
    return order


def _cue4parse_ladder(job: "Job", opts: ExportOptions, mapping: str) -> list[Attempt]:
    order = ue_tag_order(job.ue_tag)

    maps: list[str] = []
    for m in (mapping or opts.mappings, ""):
        if m not in maps:
            maps.append(m)
    # a game with unversioned properties cannot work without mappings at all, so
    # do not spend an attempt proving it
    if job.needs_mappings and maps[0]:
        maps = [maps[0]]

    def make(tag: str, m: str, fmt: str = "auto", mesh: str = "",
             tex: str = "", note: str = "") -> Attempt:
        bits = [tag, "+ mappings" if m else "no mappings"]
        if fmt != "auto":
            bits.append(f"-f {fmt}")
        if note:
            bits.append(note)
        o = replace(opts, ue_game_tag=tag, mappings=m, out_format=fmt, brute=True)
        if mesh:
            o = replace(o, mesh_format=mesh)
        if tex:
            o = replace(o, texture_format=tex)
        return Attempt(" ".join(bits), o)

    out: list[Attempt] = []
    # unverified key guesses come right after the first attempt: with the wrong
    # key every other setting is irrelevant, so settle the key before varying them
    guesses = [Attempt(f"{order[0]} with key {guess[:10]}...",
                       replace(opts, ue_game_tag=order[0], mappings=maps[0],
                               aes_key=guess, brute=True))
               for guess in (job.key_candidates or [])[1:4]]
    for m in maps:
        first = order[0]
        # The version comes straight off the shipping exe, so doubt everything else
        # before doubting it.
        out.append(make(first, m))
        # "png" also skips the sound decoder, which is what crashes the whole export
        # on some games before it can write anything
        out.append(make(first, m, "png"))
        # the glTF writer chokes on some skeletal meshes; ActorX is the older, blunter
        # path and gets the meshes out when glTF cannot
        out.append(make(first, m, "auto", mesh="psk", note="ActorX meshes"))
        # textures only: the fastest way to prove the archive itself is readable
        out.append(make(first, m, "auto", mesh="none", note="textures only"))
        for tag in order[1:]:
            out.append(make(tag, m))
    return out[:1] + guesses + out[1:]


def _umodel_ladder(job: "Job", opts: ExportOptions) -> list[Attempt]:
    base = replace(opts, brute=True)
    out = [
        Attempt("glTF meshes + PNG textures",
                replace(base, mesh_format="glb", texture_format="png")),
        # ActorX/TGA is umodel's native pair and survives assets the glTF writer aborts on
        Attempt("ActorX meshes + TGA textures",
                replace(base, mesh_format="psk", texture_format="tga")),
        Attempt("textures only (skips a mesh that aborts the run)",
                replace(base, mesh_format="none", texture_format="png")),
        Attempt("meshes only",
                replace(base, mesh_format="psk", texture_format="none")),
    ]
    # optional extras, kept only if this build of umodel lists them
    out.append(Attempt("everything including third-party formats",
                       _with_flags(replace(base, mesh_format="psk", texture_format="tga"),
                                   "umodel", "-3rdparty", "-sounds")))
    out.append(Attempt("ignoring package errors",
                       _with_flags(base, "umodel", "-nolightmap", "-noanim")))
    return out


def _assetstudio_ladder(job: "Job", opts: ExportOptions) -> list[Attempt]:
    base = replace(opts, brute=True)
    everything = "tex2d,sprite,mesh,audio,textAsset,video,font,shader,movieTexture,animationClip"
    out = [
        Attempt("the usual asset types", base),
        Attempt("every asset type", replace(base, unity_types=everything)),
        Attempt("textures only", replace(base, unity_types="tex2d,sprite")),
        Attempt("every type, grouped by asset type",
                replace(base, unity_types=everything, unity_group="type")),
        Attempt("every type, flat output",
                replace(base, unity_types=everything, unity_group="none")),
    ]
    return out


def _gdre_ladder(job: "Job", opts: ExportOptions) -> list[Attempt]:
    base = replace(opts, brute=True)
    return [
        Attempt("recover project files", replace(base, godot_mode="recover")),
        Attempt("extract raw files", replace(base, godot_mode="extract")),
    ]


def _quickbms_ladder(job: "Job", opts: ExportOptions) -> list[Attempt]:
    from . import mappings as mapping_lib
    out: list[Attempt] = []
    chosen = opts.bms_script.strip()
    if chosen:
        out.append(Attempt(f"your script: {Path(chosen).name}", replace(opts, brute=True)))
    scripts = mapping_lib.bms_candidates(job.game, extra_roots=[Path(job.source)])
    for script in scripts:
        if str(script) == chosen:
            continue
        out.append(Attempt(f"script {script.name}",
                           replace(opts, bms_script=str(script), brute=True)))
    if not out:
        out.append(Attempt("as detected", replace(opts, brute=True)))
    return out


LADDERS = {
    "cue4parse": lambda job, opts, mapping: _cue4parse_ladder(job, opts, mapping),
    "umodel": lambda job, opts, mapping: _umodel_ladder(job, opts),
    "assetstudio": lambda job, opts, mapping: _assetstudio_ladder(job, opts),
    "gdre": lambda job, opts, mapping: _gdre_ladder(job, opts),
    "quickbms": lambda job, opts, mapping: _quickbms_ladder(job, opts),
}


def brute_attempts(job: "Job", opts: ExportOptions,
                   mapping: str = "") -> list[Attempt]:
    """Everything worth trying for this job, best guess first.

    Three stages, and it stops the moment one of them converts something:
      1. settings on the target the scan chose
      2. the other backend that handles this engine, if there is one
      3. the same settings against each container folder in turn, for the case
         where the backend did not walk the tree the way we assumed
    """
    build = LADDERS.get(job.backend_key)
    out: list[Attempt] = list(build(job, opts, mapping)) if build else [
        Attempt("as detected", replace(opts, brute=True))]

    # stage 2: the other backend for this engine. UE4 is the case that matters -
    # umodel reads some older packages CUE4Parse refuses, and the reverse is also true.
    others = [k for k in ENGINE_PREFERENCE.get(job.engine, ()) if k != job.backend_key]
    for key in others:
        backend = REGISTRY.get(key)
        if not backend or not backend.installed():
            continue
        alt = LADDERS.get(key)
        if not alt:
            continue
        clone = replace_job(job, backend_key=key)
        for att in list(alt(clone, opts, mapping))[:2]:
            out.append(Attempt(f"{backend.name}: {att.label}",
                               replace(att.opts, backend_override=key)))

    # stage 3: one container folder at a time
    spots = [t for t in (job.targets or []) if t != job.target]
    if spots:
        first = out[0]
        for spot in spots[:8]:
            try:
                where = Path(spot).relative_to(job.source)
            except ValueError:
                where = Path(spot).name
            out.append(Attempt(f"{first.label}, only {where}", first.opts, target=spot))

    # dedupe on what actually reaches the command line
    seen: set[tuple] = set()
    unique: list[Attempt] = []
    for att in out:
        key = (att.opts.ue_game_tag, att.opts.mappings, att.opts.out_format,
               att.opts.mesh_format, att.opts.texture_format, att.opts.unity_types,
               att.opts.bms_script, att.opts.godot_mode, att.opts.extra_args,
               att.opts.unity_group, att.opts.backend_override, att.opts.aes_key,
               att.target)
        if key in seen:
            continue
        seen.add(key)
        unique.append(att)
    return unique[:BRUTE_MAX]


def replace_job(job: "Job", **changes) -> "Job":
    from dataclasses import replace as _replace
    return _replace(job, **changes)


class Runner:
    """Runs jobs one at a time on a worker thread, streaming output to a callback."""

    def __init__(self, log: Callable[[str], None],
                 on_update: Callable[[Job], None] | None = None):
        self.log = log
        self._counts: dict[str, int] = {}
        self._total_packages = 0
        self._fatal = ""
        self._key_cache: dict[str, aeskey.KeySearch] = {}
        self._probe_dir: Path | None = None
        self._probe_failed = False
        self.on_update = on_update or (lambda job: None)
        self._proc: subprocess.Popen | None = None
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False

    def cancel(self) -> None:
        self._cancel.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    def start(self, jobs: list[Job], opts: ExportOptions,
              on_finish: Callable[[list[Job]], None] | None = None) -> None:
        if self.running:
            self.log("A run is already in progress.")
            return
        self._cancel.clear()
        self.running = True

        def work():
            try:
                self.run_all(jobs, opts)
            finally:
                self.running = False
                if on_finish:
                    on_finish(jobs)

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def ensure_key(self, job: Job, opts: ExportOptions) -> ExportOptions:
        """Fill in the AES key for an encrypted Unreal game, if one can be found.

        Looked up once per game folder and cached, so the brute-force ladder does
        not rescan the exe on every attempt.
        """
        if opts.aes_key.strip() or not job.engine.startswith("ue"):
            return opts
        search = self._key_cache.get(job.source)
        if search is None:
            self.log("   no AES key given - checking whether this game needs one")
            try:
                search = aeskey.find(Path(job.source), job.game,
                                     paks=[Path(t) for t in job.targets if Path(t).is_file()],
                                     log=self.log)
            except Exception as exc:  # noqa: BLE001
                search = aeskey.KeySearch(notes=[f"key search failed: {exc}"])
            for note in search.notes:
                self.log(f"   {note}")
            self._key_cache[job.source] = search
        if search.verified:
            job.needs_key = True
            return replace(opts, aes_key=search.verified[0])
        if search.unverified:
            job.needs_key = True
            job.key_candidates = list(search.unverified)
            return replace(opts, aes_key=search.unverified[0])
        return opts

    def run_brute(self, job: Job, opts: ExportOptions, mapping: str = "") -> None:
        """Try settings until one actually converts something, then let it finish.

        Each attempt is abandoned as soon as it is clear it is producing nothing,
        so this costs minutes of probing rather than a full pass per setting.
        """
        opts = self.ensure_key(job, opts)
        attempts = brute_attempts(job, opts, mapping)
        out_dir = job_output_dir(job, opts)
        original_target = job.target
        self.log("")
        self.log(f"== brute force: {len(attempts)} setting(s) to try for {job.label}")
        if len(job.targets) > 1:
            self.log(f"   {len(job.targets)} container folder(s) found under this game")
        for n, att in enumerate(attempts, 1):
            label, attempt_opts = att.label, att.opts
            if self._cancel.is_set():
                job.status = "cancelled"
                job.target = original_target
                self.on_update(job)
                return
            job.target = att.target or original_target
            self.log("")
            self.log(f"-- attempt {n}/{len(attempts)}: {label}")
            self._probe_dir = out_dir
            self._probe_failed = False
            self.run_one(job, attempt_opts, announce=False)
            if self._probe_failed:
                job.status = "queued"
                job.message = ""
                continue
            if count_converted(out_dir):
                job.target = original_target
                if attempt_opts.aes_key.strip() and job.game:
                    aeskey.remember(job.game, attempt_opts.aes_key)
                job.message = f"{label} worked - {job.message}"
                self.log(f"== {label} is the setting that works for this game")
                self.on_update(job)
                return
        job.target = original_target
        self._probe_dir = None
        job.status = "failed"
        job.message = ("nothing converted with any of the settings tried. "
                       + (MAPPINGS_ADVICE if job.needs_mappings else
                          "This game may need an AES key, or a backend that supports it."))
        self.log(f"== {job.message}")
        self.on_update(job)

    def run_all(self, jobs: list[Job], opts: ExportOptions) -> None:
        for job in jobs:
            if self._cancel.is_set():
                job.status = "cancelled"
                self.on_update(job)
                continue
            self.run_one(job, opts)

    def run_one(self, job: Job, opts: ExportOptions, announce: bool = True) -> None:
        backend = REGISTRY.get(opts.backend_override or job.backend_key)
        job.started = time.time()
        job.status = "running"
        self.on_update(job)
        out_dir = job_output_dir(job, opts)
        job.out_dir = str(out_dir)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            opts = self.ensure_key(job, opts)
            argv = build_command(backend, job, opts, out_dir)
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.message = str(exc)
            job.finished = time.time()
            self.log(f"!! {job.label}: {exc}")
            self.on_update(job)
            return

        before = _snapshot(out_dir)
        self.log("")
        self.log(f">> {job.label}"
                 + (f"  (using {backend.name})" if opts.backend_override else ""))
        self.log(f"   reading: {job.target}")
        for note in job.notes:
            self.log(f"   detected: {note}")
        if (opts.backend_override or job.backend_key) == "cue4parse":
            forced = (opts.ue_game_tag or "auto").lower() != "auto"
            self.log(f"   engine tag: {resolve_ue_tag(job, opts)}"
                     + ("  (you overrode the detected value)"
                        if forced and not opts.brute else ""))
        if job.needs_key and not opts.aes_key.strip():
            self.log("   WARNING: this pak index is encrypted and no AES key could be found - "
                     "expect almost everything to fail. Paste one into the AES field or drop "
                     "a text file containing it in the keys folder.")
        elif job.needs_key and job.key_candidates:
            self.log(f"   AES key {opts.aes_key} is a guess that did not decrypt the index; "
                     f"brute force will try the other {len(job.key_candidates) - 1} candidate(s)")
        if job.needs_mappings and not opts.mappings.strip():
            self.log("   WARNING: this game uses unversioned properties and no .usmap was "
                     "given. Raw .ubulk payloads will come out; nothing will be converted "
                     "to PNG or glTF. See the README for where to get one.")
        self.log(f"   {' '.join(_quote(a) for a in argv)}")
        self._counts = {}
        self._total_packages = 0
        self._fatal = ""
        rc = self._stream(argv, cwd=str(Path(argv[0]).parent))

        after = _snapshot(out_dir)
        new_files = sorted(after - before)
        job.produced = len(new_files)
        job.finished = time.time()

        if self._cancel.is_set():
            job.status = "cancelled"
            job.message = f"cancelled after {job.produced} file(s)"
        elif rc != 0 and job.produced == 0:
            job.status = "failed"
            job.message = f"exit code {rc}, nothing written"
        elif rc != 0:
            job.status = "done"
            job.message = f"exit code {rc}, but {job.produced} file(s) written - check the log"
        else:
            job.status = "done"
            job.message = f"{job.produced} file(s) in {job.duration:.0f}s"

        produced_categories: dict[str, int] = {}
        for rel in new_files:
            cat = categorise(out_dir / rel)
            produced_categories[cat] = produced_categories.get(cat, 0) + 1
        hints = diagnose(job, opts, self._counts, self._total_packages, job.produced,
                         produced_categories, self._fatal)
        if self._fatal and job.status == "done":
            job.message = f"aborted after {job.produced} file(s) - {self._fatal}"
        if hints:
            job.message = hints[1] if len(hints) > 1 else hints[0]
            self.log("")
            self.log("   WHAT WENT WRONG")
            for hint in hints:
                self.log(f"     {hint}")
            self.log("")

        if new_files and opts.organise:
            moved = organise(out_dir, [out_dir / f for f in new_files], self.log)
            self.log(f"   sorted {moved} file(s) into category folders")
        if new_files and opts.write_manifest:
            manifest = write_manifest(job, out_dir, [out_dir / f for f in new_files], argv)
            self.log(f"   manifest: {manifest}")

        self.log(f"   {job.status}: {job.message}")
        self.on_update(job)

    def _stream(self, argv: list[str], cwd: str | None) -> int:
        creation = 0
        if os.name == "nt":
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                argv, cwd=cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
                creationflags=creation,
            )
        except FileNotFoundError:
            self.log(f"   executable not found: {argv[0]}")
            return 127
        except OSError as exc:
            self.log(f"   could not start: {exc}")
            return 126

        assert self._proc.stdout is not None
        if self._cancel.is_set():   # Cancel pressed while Popen was starting
            self.cancel()
        shown: dict[str, int] = {}
        samples: dict[str, str] = {}
        started = time.time()
        for line in self._proc.stdout:
            if self._cancel.is_set():
                break
            line = line.rstrip()
            if not line:
                continue
            kind = ""
            for name, rx in FAILURE_PATTERNS.items():
                if rx.search(line):
                    self._counts[name] = self._counts.get(name, 0) + 1
                    kind = kind or name
                    if name == "fatal" and not self._fatal:
                        self._fatal = line.strip().lstrip("* ").strip()[:200]
            m = PROGRESS_PATTERN.search(line)
            if m:
                kind = "progress"
                try:
                    self._total_packages = max(self._total_packages, int(m.group(2)))
                    done = int(m.group(1))
                except ValueError:
                    done = 0
                if self._probe_dir is not None and (
                        done >= PROBE_PACKAGES or time.time() - started > PROBE_SECONDS):
                    failed = self._counts.get("load_failed", 0)
                    ratio = failed / max(done, 1)
                    if count_converted(self._probe_dir):
                        self.log("   probe: already converting - letting it run")
                        self._probe_dir = None
                    elif ratio > PROBE_FAIL_RATIO:
                        self.log(f"   probe: {failed} of {done} packages failed to load "
                                 f"({ratio:.0%}) - wrong settings, moving on")
                        self._probe_failed = True
                        try:
                            self._proc.terminate()
                        except Exception:  # noqa: BLE001
                            pass
                        break
                    else:
                        self.log(f"   probe: {done} packages in, {failed} load failure(s) - "
                                 f"these settings look right, letting it finish "
                                 f"(conversion happens at the end)")
                        self._probe_dir = None
            # Throttle by the SHAPE of the line, not by which pattern matched it.
            # umodel alone emits one "Loading package:" and several
            # "IntProperty: unknown ..." lines per asset - tens of thousands of lines
            # that no pattern list will ever enumerate. Collapsing by shape catches
            # every backend, including ones added later.
            shape = "fatal" if kind == "fatal" else _line_shape(line)
            seen = shown.get(shape, 0) + 1
            shown[shape] = seen
            samples.setdefault(shape, line[:90])
            if seen > SHOW_PER_SHAPE:
                if seen % REPEAT_EVERY == 0:
                    self.log(f"   ... {seen} lines like \"{samples[shape]}\"")
                continue
            self.log("   " + line)
        self._proc.wait()
        hidden = sum(c - SHOW_PER_SHAPE for c in shown.values() if c > SHOW_PER_SHAPE)
        if hidden:
            self.log(f"   ({hidden} repetitive line(s) hidden, "
                     f"{len(shown)} distinct kind(s) of message)")
            for shape, count in sorted(shown.items(), key=lambda kv: -kv[1])[:5]:
                if count > SHOW_PER_SHAPE:
                    self.log(f"     x{count}  {samples[shape]}")
        rc = self._proc.returncode
        self._proc = None
        return rc


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


def _snapshot(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    out = set()
    for p in root.rglob("*"):
        if p.is_file():
            try:
                out.add(str(p.relative_to(root)))
            except ValueError:
                continue
    return out


# --------------------------------------------------------------------------
# Post-processing
# --------------------------------------------------------------------------

def categorise(path: Path) -> str:
    ext = path.suffix.lower()
    for category, exts in CATEGORIES.items():
        if ext in exts:
            return category
    return "Other"


def organise(out_dir: Path, files: Iterable[Path], log: Callable[[str], None]) -> int:
    """Move exported files into Meshes/Textures/... keeping their relative path below."""
    moved = 0
    for src in files:
        if not src.is_file():
            continue
        category = categorise(src)
        try:
            rel = src.relative_to(out_dir)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in CATEGORIES or (rel.parts and rel.parts[0] == "Other"):
            continue
        dest = out_dir / category / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dest))
            moved += 1
        except OSError as exc:
            log(f"   could not move {rel}: {exc}")
    _prune_empty(out_dir)
    return moved


def _prune_empty(root: Path) -> None:
    for p in sorted(root.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        if p.is_dir():
            try:
                next(p.iterdir())
            except StopIteration:
                try:
                    p.rmdir()
                except OSError:
                    pass
            except OSError:
                pass


def write_manifest(job: Job, out_dir: Path, files: list[Path], argv: list[str]) -> Path:
    entries = []
    for p in files:
        loc = p if p.is_file() else None
        if loc is None:
            # organise() may have moved it
            matches = list(out_dir.rglob(p.name))
            loc = matches[0] if matches else None
        if loc is None or not loc.is_file():
            continue
        try:
            stat = loc.stat()
            entries.append({
                "path": str(loc.relative_to(out_dir)),
                "category": categorise(loc),
                "bytes": stat.st_size,
                "sha1": _sha1(loc) if stat.st_size <= 64 * 1024 * 1024 else None,
            })
        except OSError:
            continue
    data = {
        "app": "Game Asset Harvester",
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": job.source,
        "target": job.target,
        "engine": job.engine,
        "backend": job.backend_key,
        "command": argv,
        "file_count": len(entries),
        "files": entries,
    }
    dest = out_dir / "manifest.json"
    dest.write_text(json.dumps(data, indent=2), "utf-8")
    return dest


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
