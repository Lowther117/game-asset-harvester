"""Work out what engine a folder belongs to, and what to point a backend at.

Detection is deliberately evidence-based rather than clever: it counts the files
that only one engine produces, and reports what it found so you can overrule it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .backends import ENGINE_LABELS
from .ueversion import UEVersion, detect_ue_version

# The scan must cover the WHOLE game folder: a modern install buries DLC and
# plugin containers a dozen levels down, and stopping early is how a run quietly
# misses half the game. These caps exist only so a mistakenly-picked drive root
# cannot hang the app - a real game tree finishes long before any of them.
MAX_SCAN_FILES = 500000
MAX_DEPTH = 32
MAX_SCAN_SECONDS = 120.0

# extension -> (engine, weight). Weight is how strongly it implies that engine.
SIGNATURES: dict[str, tuple[str, int]] = {
    ".utoc": ("ue5", 10),
    ".ucas": ("ue5", 10),
    ".pak": ("ue4", 6),
    ".uasset": ("ue4", 4),
    ".uexp": ("ue4", 4),
    ".ubulk": ("ue4", 3),
    ".umap": ("ue4", 2),   # UE3 uses .umap too - see AMBIGUOUS_UE_EXTS below
    ".udk": ("ue3", 6),
    ".upk": ("ue3", 6),
    ".u": ("ue2", 3),
    ".utx": ("ue1", 6),
    ".uax": ("ue1", 5),
    ".usx": ("ue1", 5),
    ".assets": ("unity", 8),
    ".bundle": ("unity", 5),
    ".unity3d": ("unity", 8),
    ".resS": ("unity", 6),
    ".resource": ("unity", 3),
    ".pck": ("godot", 9),
    ".vpk": ("generic", 6),
    ".pk3": ("generic", 6),
    ".pk4": ("generic", 6),
    ".bsa": ("generic", 6),
    ".ba2": ("generic", 6),
    ".rpa": ("generic", 6),
    ".xnb": ("generic", 5),
    ".wad": ("generic", 4),
    ".arc": ("generic", 3),
    ".dat": ("generic", 1),
    ".bin": ("generic", 1),
}

# folder or file names that clinch it. Folder patterns are anchored so they score
# once for the folder itself, not once per file inside it.
# Content/Paks exists in every UE4 *and* UE5 game, so it only says "Unreal 4+";
# .utoc/.ucas above are what promote that to UE5.
MARKERS: list[tuple[str, str, int]] = [
    (r"(^|[\\/])Content[\\/]Paks$", "ue4", 12),
    (r"^(.+_Data|Data)[\\/]Managed$", "unity", 14),
    (r"UnityPlayer\.dll$", "unity", 14),
    (r"globalgamemanagers$", "unity", 14),
    (r"level\d+$", "unity", 4),
    (r"^Engine[\\/]Binaries$", "ue4", 8),
    (r"\.godot$", "godot", 10),
    (r"project\.godot$", "godot", 12),
]

# Extensions that do NOT distinguish modern Unreal from UE1-3. A folder whose only
# "UE4" evidence is one of these is a UE3 game with map files, not a UE4 game, and
# handing it to CUE4Parse just produces a second job that cannot possibly work.
AMBIGUOUS_UE_EXTS = {".umap", ".u"}
# Evidence that genuinely only exists from UE4 onwards.
MODERN_UE_EVIDENCE = {".pak", ".utoc", ".ucas", ".uasset", ".uexp", ".ubulk",
                      "marker"}

UE_CONTAINER_EXTS = {".pak", ".utoc", ".ucas", ".uasset", ".uexp", ".ubulk", ".umap"}
UE_CLIMB_DIRS = {"paks", "content"}


def _safe_iterdir(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except (OSError, PermissionError):
        return []


def resolve_source(path: Path) -> tuple[Path, str]:
    """Walk up from a picked file or Paks folder to the game's install root.

    Picking a single .ucas is the natural thing to do and it cannot work:
    CUE4Parse's -i wants the game DIRECTORY, IoStore needs the whole container
    set together, and the version evidence - the shipping exe - sits several
    levels above the Paks folder.

    Returns (resolved path, note) where note is empty if nothing changed.
    """
    original = Path(path)
    if original.is_file() and original.suffix.lower() not in UE_CONTAINER_EXTS:
        return original, ""          # a .pck or a one-off archive: leave it alone

    current = original if original.is_dir() else original.parent
    for _ in range(6):               # .../Content/Paks -> .../Content -> ...
        if current.name.lower() in UE_CLIMB_DIRS and current.parent != current:
            current = current.parent
            continue
        break

    # current is now <Game>; step up once to the install root, but ONLY when
    # current is itself a UE game folder. Testing the parent's other children
    # instead would drag an unrelated folder up to whatever contains it.
    if (current / "Content" / "Paks").is_dir() and current.parent != current:
        current = current.parent

    if current == original:
        return original, ""
    what = "file" if original.is_file() else "folder"
    return current, (f"you picked the {what} {original.name}, so the scan moved up to "
                     f"{current} - the backend needs the game directory, not one container")


GODOT_MAGIC = b"GDPC"  # start of a .pck, and the last 4 bytes of an exe with one embedded


def _godot_embedded(path: Path) -> bool:
    """A Godot exe with an embedded pck ends with <pck size:8><'GDPC'>."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            if fh.tell() < 16:
                return False
            fh.seek(-4, 2)
            return fh.read(4) == GODOT_MAGIC
    except OSError:
        return False


@dataclass
class Finding:
    engine: str
    score: int
    files: list[Path] = field(default_factory=list)
    sample: list[str] = field(default_factory=list)
    ue: UEVersion | None = None
    weak: bool = False           # evidence too thin to be worth a job on its own
    locations: list[Path] = field(default_factory=list)  # every folder holding containers

    @property
    def label(self) -> str:
        if self.ue and self.ue.major is not None:
            return self.ue.label
        return ENGINE_LABELS.get(self.engine, self.engine)


@dataclass
class Detection:
    root: Path
    findings: list[Finding]
    scanned: int
    truncated: bool
    note: str = ""

    @property
    def best(self) -> Finding | None:
        return self.findings[0] if self.findings else None

    def summary(self) -> str:
        if not self.findings:
            return (f"{self.note}\n" if self.note else "") + \
                   f"Nothing recognisable under {self.root}."
        lines = ([f"Note: {self.note}"] if self.note else []) + \
                [f"Scanned {self.scanned} files under {self.root}"
                 + (" (stopped early - very large tree)" if self.truncated else "")]
        for f in self.findings:
            names = ", ".join(f.sample[:4])
            lines.append(f"  {f.label}: {len(f.files)} candidate file(s), score {f.score}"
                         + ("  (weak - not worth a job on its own)" if f.weak else "")
                         + (f"  e.g. {names}" if names else ""))
            if len(f.locations) > 1:
                lines.append(f"    in {len(f.locations)} separate folder(s):")
                for spot in f.locations[:6]:
                    try:
                        shown = spot.relative_to(self.root)
                    except ValueError:
                        shown = spot
                    lines.append(f"      {shown}")
                if len(f.locations) > 6:
                    lines.append(f"      ...and {len(f.locations) - 6} more")
            if f.ue:
                lines.append("  " + f.ue.summary().replace("\n", "\n  "))
        return "\n".join(lines)


def _iter_files(root: Path, limit: int = MAX_SCAN_FILES):
    """Every file under root, to the bottom.

    Symlinked directories are walked but never followed twice, and a directory
    already visited by another path is skipped, so a folder that links to its own
    parent cannot spin forever.
    """
    import time as _time
    count = 0
    deadline = _time.monotonic() + MAX_SCAN_SECONDS
    seen: set[tuple] = set()
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_DEPTH:
            continue
        try:
            key = current.stat()
            key = (key.st_dev, key.st_ino)
            if key in seen:
                continue
            seen.add(key)
        except OSError:
            pass
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError):
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    stack.append((entry, depth + 1))
                    yield entry, True
                else:
                    count += 1
                    yield entry, False
                    if count >= limit:
                        return
            except OSError:
                continue
        if count and _time.monotonic() > deadline:
            return


def detect(root: Path, limit: int = MAX_SCAN_FILES) -> Detection:
    root, note = resolve_source(Path(root))
    _resolve_note = note
    scores: dict[str, int] = {}
    hits: dict[str, list[Path]] = {}
    strongest: dict[str, int] = {}   # engine -> heaviest single piece of evidence
    evidence: dict[str, set[str]] = {}   # engine -> which signatures actually fired
    places: dict[str, dict[Path, int]] = {}  # engine -> folder -> containers in it
    scanned = 0
    truncated = False

    if root.is_file():
        engine = _engine_for_file(root)
        if engine == "generic" and root.suffix.lower() == ".exe" and _godot_embedded(root):
            engine = "godot"
        f = Finding(engine, 10, [root], [root.name])
        return Detection(root, [f], 1, False)

    marker_rx = [(re.compile(p, re.I), engine, w) for p, engine, w in MARKERS]

    def add(engine: str, weight: int, entry: Path | None = None,
            token: str = "marker") -> None:
        scores[engine] = scores.get(engine, 0) + weight
        strongest[engine] = max(strongest.get(engine, 0), weight)
        evidence.setdefault(engine, set()).add(token)
        if entry is not None:
            bucket = hits.setdefault(engine, [])
            if len(bucket) < 500:
                bucket.append(entry)
            # the file list is capped, the LOCATION list is not: a game with DLC or
            # plugin paks keeps them in separate folders and all of them must be seen
            if weight >= 3:
                where = entry.parent if entry.is_file() else entry
                seen = places.setdefault(engine, {})
                if where in seen or len(seen) < 400:
                    seen[where] = seen.get(where, 0) + 1

    for entry, is_dir in _iter_files(root, limit):
        rel = str(entry.relative_to(root)) if entry != root else entry.name
        for rx, engine, weight in marker_rx:
            if rx.search(rel) or rx.search(entry.name):
                add(engine, weight)
        if is_dir:
            continue
        scanned += 1
        ext = entry.suffix
        sig = SIGNATURES.get(ext) or SIGNATURES.get(ext.lower())
        if sig:
            add(sig[0], sig[1], entry, ext.lower())
        elif ext.lower() == ".exe" and len(entry.relative_to(root).parts) <= 2 \
                and _godot_embedded(entry):
            add("godot", 12, entry)
    truncated = scanned >= limit

    # UE5 containers imply UE, but .pak alone next to .utoc should not split the vote
    if "ue5" in scores and "ue4" in scores:
        scores["ue5"] += scores.pop("ue4", 0) // 2
        hits.setdefault("ue5", []).extend(hits.pop("ue4", [])[:200])
        _merge_places(places, "ue4", "ue5")
    # .umap exists in UE3 as well as UE4. If that (or a bare .u) is the ONLY thing
    # suggesting modern Unreal, and a UE1-3 signature is also present, this is an old
    # game: fold the evidence in rather than raising a CUE4Parse job that will find
    # nothing. Enslaved is the case that showed this up.
    for modern in ("ue4", "ue5"):
        if modern not in scores:
            continue
        if evidence.get(modern, set()) & MODERN_UE_EVIDENCE:
            continue
        older = [e for e in ("ue3", "ue2", "ue1") if e in scores]
        if not older:
            continue
        keep = max(older, key=lambda e: scores[e])
        scores[keep] += scores.pop(modern)
        hits.setdefault(keep, []).extend(hits.pop(modern, [])[:200])
        evidence.setdefault(keep, set()).update(evidence.pop(modern, set()))
        _merge_places(places, modern, keep)

    # UE1-3 all share .u files and all go to umodel: one job, not three
    old = [e for e in ("ue3", "ue2", "ue1") if e in scores]
    if len(old) > 1:
        keep = max(old, key=lambda e: scores[e])
        for e in old:
            if e != keep:
                scores[keep] += scores.pop(e) // 2
                hits.setdefault(keep, []).extend(hits.pop(e, [])[:200])
                _merge_places(places, e, keep)

    findings = []
    for engine, score in sorted(scores.items(), key=lambda kv: -kv[1]):
        files = hits.get(engine, [])
        where = places.get(engine, {})
        spots = [d for d, _ in sorted(where.items(), key=lambda kv: (-kv[1], str(kv[0])))]
        findings.append(Finding(engine, score, files, [p.name for p in files[:6]],
                                locations=spots))
    # drop generic noise (.dat/.bin/.arc) when a real engine was identified; a real
    # archive format (.vpk, .bsa, ...) next to it still gets its own row
    if len(findings) > 1 and findings[0].engine != "generic":
        findings = [f for f in findings
                    if not (f.engine == "generic" and strongest.get("generic", 0) < 5)]
    # A handful of .dat/.bin files is not an archive format, it is just files. On its own
    # that used to raise a QuickBMS job with no script, which can only ever fail. Keep the
    # finding so the scan still reports what it saw, but flag it so nothing is queued.
    for f in findings:
        if f.engine == "generic" and strongest.get("generic", 0) < 5 and f.score < 8:
            f.weak = True

    # the engine VERSION, not just the family: sending the wrong -g tag is what
    # makes a run finish "successfully" having exported almost nothing
    for finding in findings:
        if finding.engine in ("ue4", "ue5"):
            finding.ue = detect_ue_version(root, finding.files)
            if finding.ue.major == 5 and finding.engine == "ue4":
                finding.engine = "ue5"
            elif finding.ue.major == 4 and finding.engine == "ue5":
                finding.engine = "ue4"
            break

    det = Detection(root, findings, scanned, truncated)
    det.note = _resolve_note
    return det


def _merge_places(places: dict[str, dict[Path, int]], src: str, dst: str) -> None:
    for where, n in places.pop(src, {}).items():
        places.setdefault(dst, {})[where] = places.setdefault(dst, {}).get(where, 0) + n


def all_targets(root: Path, finding: Finding) -> list[Path]:
    """Every place a backend could be pointed at for this finding.

    One job per location for the backends that take a single container, and the
    fallback list for Unreal when one broad pass finds nothing.
    """
    root = Path(root)
    if finding.engine in ("unity",):
        out = []
        for spot in finding.locations:
            p = spot
            if p.name.lower().endswith("_data") or p.name.lower() == "data":
                pass
            elif p.parent.name.lower().endswith("_data"):
                p = p.parent
            if p not in out:
                out.append(p)
        return out or [_input_target(root, finding)]
    if finding.engine == "godot":
        out = [f for f in finding.files if f.suffix.lower() == ".pck"]
        out += [f for f in finding.files if f.suffix.lower() == ".exe"]
        return out or [_input_target(root, finding)]
    if finding.engine == "generic":
        return finding.files[:64] or [root]
    return list(finding.locations) or [_input_target(root, finding)]


def common_root(paths_: list[Path], limit: Path) -> Path:
    """The shallowest folder holding all of these, never above limit."""
    import os
    if not paths_:
        return limit
    try:
        common = Path(os.path.commonpath([str(p) for p in paths_]))
    except ValueError:
        return limit
    try:
        common.relative_to(limit)
    except ValueError:
        return limit
    return common


def _engine_for_file(path: Path) -> str:
    sig = SIGNATURES.get(path.suffix) or SIGNATURES.get(path.suffix.lower())
    return sig[0] if sig else "generic"


def input_target(root: Path, finding: Finding) -> Path:
    target = _input_target(root, finding)
    # CUE4Parse and umodel both take a game DIRECTORY; handing either a single
    # container gets "The game directory could not be found" and nothing else
    if finding.engine in ("ue1", "ue2", "ue3", "ue4", "ue5") and target.is_file():
        return target.parent
    return target


def _input_target(root: Path, finding: Finding) -> Path:
    """The path a backend should actually be pointed at.

    Unreal wants the Paks folder, Unity wants the *_Data folder, Godot wants the
    .pck itself, QuickBMS wants the individual archive.
    """
    root = Path(root)
    if root.is_file():
        return root

    if finding.engine in ("ue1", "ue2", "ue3", "ue4", "ue5"):
        # One pak folder: point straight at it, which is what has always worked.
        # Several - base game plus DLC, plugins, chunk folders - point at the folder
        # that contains them all, so a single pass covers the lot instead of the
        # first one found. If the backend turns out not to recurse, the brute-force
        # ladder falls back to one location at a time.
        spots = [d for d in finding.locations if d.is_dir()]
        if len(spots) > 1:
            return common_root(spots, root)
        if spots:
            return spots[0]
        for f in finding.files:
            if f.parent.name.lower() == "paks":
                return f.parent
        if finding.files:
            return finding.files[0].parent
        return root

    if finding.engine == "unity":
        for f in finding.files:
            p = f.parent
            if p.name.lower().endswith("_data") or p.name.lower() == "data":
                return p
            if p.parent.name.lower().endswith("_data"):
                return p.parent
        if finding.files:
            return finding.files[0].parent
        return root

    if finding.engine == "godot":
        for f in finding.files:
            if f.suffix.lower() == ".pck":
                return f
        for f in finding.files:
            if f.suffix.lower() == ".exe":   # pck embedded in the exe
                return f
        return root

    return finding.files[0] if finding.files else root
