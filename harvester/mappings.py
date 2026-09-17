"""Finding a .usmap for a game, so you are not hunting for one by hand.

Three sources, in order:
  1. a mappings/ folder beside the app - anything you have collected already
  2. the game's own folder - some titles ship one, and UE4SS writes one there
  3. the public Unreal-Mappings-Archive, fetched on request

Nothing is bundled with this app: the archive has no licence attached, so its
files are downloaded on demand into your own mappings folder, the same way the
extraction backends are.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from . import paths

ARCHIVE_REPO = "TheNaeem/Unreal-Mappings-Archive"
ARCHIVE_TREE = "https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"
ARCHIVE_RAW = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"
ARCHIVE_BRANCHES = ("main", "master")
ARCHIVE_PAGE = f"https://github.com/{ARCHIVE_REPO}"
DUMPER_ADVICE = (
    "No archive entry matched. Dump one yourself: with UE4SS installed, open its GUI "
    "console, Dumpers tab, \"Generate .usmap file\", or call DumpUSMAP() from a Lua "
    "script. It writes Mappings.usmap next to the game exe - drop that into the "
    "mappings folder and it will be picked up automatically."
)

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


@dataclass
class MappingHit:
    path: str            # path inside the archive, or a local absolute path
    game: str
    score: float
    local: Path | None = None

    @property
    def label(self) -> str:
        where = "local" if self.local else "archive"
        return f"{self.game} ({where}, match {self.score:.0%})"


def mappings_dir() -> Path:
    d = paths.app_dir() / "mappings"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _normalise(name: str) -> str:
    """Strip everything that differs between a folder name and an archive entry."""
    name = re.sub(r"\.(usmap)$", "", name, flags=re.I)
    name = re.sub(r"[^a-z0-9]+", " ", name.lower())
    for noise in ("the ", "game", "shipping", "win64", "windows", "remastered",
                  "definitive", "edition", "deluxe", "mappings"):
        name = name.replace(noise, " ")
    return " ".join(name.split())


def _similar(a: str, b: str) -> float:
    a, b = _normalise(a), _normalise(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.92
    return SequenceMatcher(None, a, b).ratio()


def find_local(game: str, extra_roots: list[Path] | None = None) -> MappingHit | None:
    """Best .usmap already on disk for this game."""
    best: MappingHit | None = None
    roots = [mappings_dir()] + list(extra_roots or [])
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        try:
            found = list(Path(root).rglob("*.usmap"))
        except OSError:
            continue
        for f in found:
            # score against the file name and its parent folder, take the better
            # ...and the root itself: UE4SS writes <Game>/.../Win64/Mappings.usmap, where
            # neither the file nor its folder names the game but the root we were given does
            score = max(_similar(game, f.stem), _similar(game, f.parent.name),
                        _similar(game, Path(root).name))
            if root == mappings_dir() and len(found) == 1 and score < 0.5:
                score = 0.5          # a lone file in your own folder is worth offering
            if best is None or score > best.score:
                best = MappingHit(str(f), f.parent.name or f.stem, score, local=f)
    return best if best and best.score >= 0.45 else None


def _http_json(url: str, timeout: int = 45) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def archive_index(log: Callable[[str], None] = print) -> tuple[list[str], str]:
    """Every .usmap path in the archive, and the branch it came from."""
    last = ""
    for branch in ARCHIVE_BRANCHES:
        try:
            data = _http_json(ARCHIVE_TREE.format(repo=ARCHIVE_REPO, branch=branch))
            files = [t["path"] for t in data.get("tree", [])
                     if t.get("type") == "blob" and t["path"].lower().endswith(".usmap")]
            if files:
                return files, branch
            last = f"{branch}: no .usmap entries"
        except Exception as exc:  # noqa: BLE001
            last = f"{branch}: {exc}"
    raise RuntimeError(f"could not read the mappings archive ({last}). "
                       f"Browse it yourself at {ARCHIVE_PAGE}")


def search_archive(game: str, log: Callable[[str], None] = print,
                   limit: int = 5) -> list[MappingHit]:
    files, _branch = archive_index(log)
    scored: list[MappingHit] = []
    for path in files:
        top = path.split("/")[0]
        score = max(_similar(game, top), _similar(game, Path(path).stem))
        if score >= 0.55:
            scored.append(MappingHit(path, top, score))
    scored.sort(key=lambda h: -h.score)
    return scored[:limit]


def fetch(hit: MappingHit, log: Callable[[str], None] = print) -> Path:
    """Download an archive entry into the local mappings folder."""
    if hit.local:
        return hit.local
    _files, branch = archive_index(log)
    url = ARCHIVE_RAW.format(repo=ARCHIVE_REPO, branch=branch,
                             path=urllib.parse.quote(hit.path))
    log(f"  {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        blob = resp.read()
    dest = mappings_dir() / _safe(hit.game) / Path(hit.path).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
    log(f"  saved {len(blob) / 1024:.0f} KB to {dest}")
    return dest


def resolve(game: str, extra_roots: list[Path] | None = None,
            allow_download: bool = False,
            log: Callable[[str], None] = print) -> tuple[Path | None, str]:
    """The .usmap to use for this game, and a line explaining where it came from."""
    local = find_local(game, extra_roots)
    if local and local.local:
        return local.local, f"using {local.local.name} from {local.local.parent}"
    if not allow_download:
        return None, ""
    try:
        hits = search_archive(game, log)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not search the mappings archive: {exc}"
    if not hits:
        return None, DUMPER_ADVICE
    best = hits[0]
    log(f"  archive match: {best.label}")
    try:
        return fetch(best, log), f"downloaded {best.game} mappings from the archive"
    except Exception as exc:  # noqa: BLE001
        return None, f"found {best.game} in the archive but could not download it: {exc}"


def _safe(name: str) -> str:
    bad = '<>:"/\\|?*'
    return "".join("_" if c in bad else c for c in name).strip(" .") or "mappings"


# --------------------------------------------------------------------------
# QuickBMS scripts
# --------------------------------------------------------------------------

def bms_candidates(game: str, extra_roots: list[Path] | None = None) -> list[Path]:
    """Every .bms script we hold, best name match first.

    QuickBMS cannot open anything without a script, and the script is per-game.
    Keeping a folder of them beside the app turns "find a script yourself" into
    "we will try the ones we have", which is the whole point of brute force.
    """
    roots = [paths.scripts_dir()] + list(extra_roots or [])
    found: list[tuple[float, Path]] = []
    seen: set[str] = set()
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        for script in sorted(Path(root).rglob("*.bms")):
            if script.name.lower() in seen:
                continue
            seen.add(script.name.lower())
            score = max(_similar(game, script.stem),
                        _similar(game, script.parent.name))
            found.append((score, script))
    found.sort(key=lambda sp: (-sp[0], str(sp[1])))
    return [s for _, s in found]
