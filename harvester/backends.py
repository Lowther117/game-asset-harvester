"""Backend registry: what each extraction tool is, how to get it, how to drive it.

The point of this file is that every supported tool is described declaratively,
so adding a sixth one is a dict entry rather than a new code path.

Nothing here redistributes anyone's binary. Each backend is downloaded from its
own official source, either at build time (build-exe.bat calls `fetch-backends`)
or on first run. The built exe then carries the copies you downloaded yourself.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import paths

# Some of these sites (gildor.org, aluigi.altervista.org) refuse a non-browser
# agent or a request with no referer, which reads as a 404 rather than a block
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"
CACHE_TTL = 24 * 3600


# --------------------------------------------------------------------------
# Backend definitions
# --------------------------------------------------------------------------

@dataclass
class Backend:
    key: str
    name: str
    engines: tuple[str, ...]           # engine keys this backend can service
    summary: str
    homepage: str
    licence: str
    # acquisition: either a GitHub repo + ranked asset patterns, or a scrape spec
    github_repo: str | None = None
    asset_patterns: tuple[str, ...] = ()      # ranked regexes, best first
    direct_url: str | None = None
    scrape_page: str | None = None
    scrape_link: str | None = None            # regex matched against hrefs
    fallback_urls: tuple[str, ...] = ()       # tried in order if the primary method fails
    exe_patterns: tuple[str, ...] = ()        # ranked regexes for the executable
    needs_dotnet: bool = False
    notes: str = ""

    @property
    def install_dir(self) -> Path:
        return paths.backends_dir() / self.key

    def find_exe(self) -> Path | None:
        """Locate this backend's executable in the writable dir or a baked-in copy."""
        roots = [self.install_dir] + [d / self.key for d in paths.bundled_backends_dirs()]
        seen: set[Path] = set()
        for root in roots:
            if not root.is_dir() or root in seen:
                continue
            seen.add(root)
            hit = _match_exe(root, self.exe_patterns)
            if hit:
                return hit
        return None

    def installed(self) -> bool:
        return self.find_exe() is not None


def _match_exe(root: Path, patterns: Sequence[str]) -> Path | None:
    try:
        candidates = [p for p in root.rglob("*") if p.is_file()]
    except OSError:
        return None
    exe_like = [
        p for p in candidates
        if p.suffix.lower() in (".exe", "") and not p.name.lower().endswith(
            (".dll", ".json", ".txt", ".md", ".pdb", ".config", ".xml"))
    ]
    for pattern in patterns:
        rx = re.compile(pattern, re.I)
        for p in exe_like:
            if rx.search(p.name):
                return p
    return None


REGISTRY: dict[str, Backend] = {}


def _register(b: Backend) -> Backend:
    REGISTRY[b.key] = b
    return b


_register(Backend(
    key="cue4parse",
    name="CUE4Parse.CLI",
    engines=("ue4", "ue5"),
    summary="Unreal Engine 4/5 bulk export. Same parser FModel is built on, "
            "but scriptable: meshes to glTF/PSK, textures to PNG, AES keys and .usmap mappings.",
    homepage="https://github.com/joric/CUE4Parse.CLI",
    licence="Apache-2.0 (CUE4Parse)",
    github_repo="joric/CUE4Parse.CLI",
    asset_patterns=(
        r"win.*x64.*(self|sc|standalone)",
        r"win.*(x64|64).*\.zip$",
        r"win.*\.zip$",
        r"\.zip$",
    ),
    exe_patterns=(r"^CUE4Parse\.CLI\.exe$", r"cue4parse.*\.exe$", r"\.exe$"),
    needs_dotnet=True,
    notes="FModel itself has no command line, so this is what replaces it for batch work.",
))

_register(Backend(
    key="umodel",
    name="UE Viewer (umodel)",
    engines=("ue1", "ue2", "ue3", "ue4"),
    summary="Gildor's viewer/exporter. Still the best option for UE1-UE4 era games, "
            "especially older ones CUE4Parse does not cover. No UE5 support.",
    homepage="https://www.gildor.org/en/projects/umodel",
    licence="Free for personal use, see gildor.org",
    scrape_page="https://www.gildor.org/en/projects/umodel",
    scrape_link=r"umodel[_-]win(32|64).*\.zip|umodel.*\.zip",
    exe_patterns=(r"^umodel_64\.exe$", r"^umodel\.exe$", r"umodel.*\.exe$", r"\.exe$"),
    notes="Last Windows build 2023. UE5 packages will be rejected - use CUE4Parse for those.",
))

_register(Backend(
    key="assetstudio",
    name="AssetStudioModCLI",
    engines=("unity",),
    summary="Unity assets, bundles and level files. Textures to PNG/TGA, meshes to OBJ "
            "(FBX only via its separate animator mode), MonoBehaviour dumps, "
            "name/container/regex filters.",
    homepage="https://github.com/aelurum/AssetStudioMod",
    licence="MIT",
    github_repo="aelurum/AssetStudioMod",
    asset_patterns=(
        r"CLI.*net8.*win.*(x64|64)",
        r"CLI.*win.*(x64|64)",
        r"CLI.*net8",
        r"CLI",
    ),
    exe_patterns=(r"^AssetStudioModCLI\.exe$", r"AssetStudio.*CLI.*\.exe$", r"\.exe$"),
    needs_dotnet=True,
))

_register(Backend(
    key="gdre",
    name="Godot RE Tools",
    engines=("godot",),
    summary="Godot .pck/.exe/.apk extraction and full project recovery, including "
            "decompiling .gdc scripts back to GDScript.",
    homepage="https://github.com/GDRETools/gdsdecomp",
    licence="MIT",
    github_repo="GDRETools/gdsdecomp",
    asset_patterns=(
        r"windows.*(x86_64|x64|64)",
        r"win.*(x86_64|x64|64)",
        r"windows",
        r"\.zip$",
    ),
    # the .console.exe wrapper is the one that prints to a pipe; prefer it
    exe_patterns=(r"^gdre_tools\.console\.exe$", r"^gdre[_-]?tools.*\.exe$",
                  r"gdre.*\.exe$", r"\.exe$"),
))

_register(Backend(
    key="quickbms",
    name="QuickBMS",
    engines=("generic",),
    summary="The long tail. Script-driven extraction for a few thousand one-off game "
            "formats - Source VPK, id pk3/pk4, bespoke archives. Needs a .bms script per game.",
    homepage="https://aluigi.altervista.org/quickbms.htm",
    licence="GPL-2.0",
    direct_url="https://aluigi.altervista.org/papers/quickbms.zip",
    exe_patterns=(r"^quickbms_4gb_files\.exe$", r"^quickbms\.exe$", r"quickbms.*\.exe$", r"\.exe$"),
    notes="Scripts are a separate download from the same site; point the app at a .bms file.",
))


ENGINE_LABELS = {
    "ue5": "Unreal Engine 5",
    "ue4": "Unreal Engine 4",
    "ue3": "Unreal Engine 3",
    "ue2": "Unreal Engine 2",
    "ue1": "Unreal Engine 1",
    "unity": "Unity",
    "godot": "Godot",
    "generic": "Generic / other archive",
}

# Which backend wins when more than one can service an engine
ENGINE_PREFERENCE = {
    "ue5": ("cue4parse",),
    "ue4": ("cue4parse", "umodel"),
    "ue3": ("umodel",),
    "ue2": ("umodel",),
    "ue1": ("umodel",),
    "unity": ("assetstudio",),
    "godot": ("gdre",),
    "generic": ("quickbms",),
}


def backend_for_engine(engine: str, prefer: str | None = None) -> Backend | None:
    order = ENGINE_PREFERENCE.get(engine, ())
    if prefer and prefer in order:
        return REGISTRY.get(prefer)
    for key in order:
        b = REGISTRY.get(key)
        if b and b.installed():
            return b
    return REGISTRY.get(order[0]) if order else None


# --------------------------------------------------------------------------
# Acquisition
# --------------------------------------------------------------------------

def _load_cache() -> dict:
    try:
        return json.loads(paths.cache_path().read_text("utf-8"))
    except Exception:
        return {}


def _save_cache(data: dict) -> None:
    try:
        paths.cache_path().write_text(json.dumps(data, indent=2), "utf-8")
    except Exception:
        pass


def _http_get(url: str, timeout: int = 60, referer: str = "") -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def candidate_urls(b: Backend, log: Callable[[str], None] = print) -> list[str]:
    """Every URL worth trying for this backend, best first.

    A scraped link can 404 (rotating download ids, hotlink protection), so the
    fetcher works through the list rather than giving up on the first answer.
    """
    urls: list[str] = []
    if b.direct_url:
        urls.append(b.direct_url)
    if b.scrape_page and b.scrape_link:
        try:
            html = _http_get(b.scrape_page).decode("utf-8", "replace")
            rx = re.compile(b.scrape_link, re.I)
            for href in re.findall(r'href=["\']([^"\']+)["\']', html, re.I):
                if rx.search(href):
                    full = href if href.startswith("http") else \
                        urllib.parse.urljoin(b.scrape_page, href)
                    if full not in urls:
                        urls.append(full)
        except Exception as exc:  # noqa: BLE001
            log(f"  could not read {b.scrape_page}: {exc}")
    urls += [u for u in b.fallback_urls if u not in urls]
    return urls


def resolve_download(b: Backend, log: Callable[[str], None] = print) -> tuple[str, str]:
    """Return (url, version) for the best download of this backend."""
    if b.direct_url:
        return b.direct_url, "latest"

    cache = _load_cache()
    entry = cache.get(b.key)
    if entry and time.time() - entry.get("at", 0) < CACHE_TTL:
        return entry["url"], entry.get("version", "?")

    if b.github_repo:
        log(f"  querying GitHub releases for {b.github_repo}")
        data = json.loads(_http_get(GITHUB_API.format(repo=b.github_repo)).decode("utf-8"))
        version = data.get("tag_name") or data.get("name") or "?"
        assets = [a for a in data.get("assets", []) if a.get("browser_download_url")]
        if not assets:
            raise RuntimeError(f"{b.name}: release {version} has no downloadable assets")
        url = _rank_assets(assets, b.asset_patterns)
        cache[b.key] = {"url": url, "version": version, "at": time.time()}
        _save_cache(cache)
        return url, version

    if b.scrape_page and b.scrape_link:
        log(f"  looking for a download link on {b.scrape_page}")
        try:
            html = _http_get(b.scrape_page).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            if b.fallback_urls:
                log(f"  page unreachable ({exc}), trying known direct URLs")
                return _first_reachable(b.fallback_urls, b), "fallback"
            raise
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.I)
        rx = re.compile(b.scrape_link, re.I)
        for href in hrefs:
            if rx.search(href):
                url = urllib.parse.urljoin(b.scrape_page, href) if not href.startswith("http") else href
                cache[b.key] = {"url": url, "version": "latest", "at": time.time()}
                _save_cache(cache)
                return url, "latest"
        if b.fallback_urls:
            log("  no matching link on the page, trying known direct URLs")
            return _first_reachable(b.fallback_urls, b), "fallback"
        raise RuntimeError(f"{b.name}: no link matching /{b.scrape_link}/ on {b.scrape_page}")

    if b.fallback_urls:
        return _first_reachable(b.fallback_urls, b), "fallback"
    raise RuntimeError(f"{b.name}: no download method defined")


def _manual_install_message(b: Backend, detail: str) -> str:
    return (f"{b.name}: could not be downloaded automatically ({detail}). "
            f"Download it yourself from {b.homepage} and unzip it into "
            f"{b.install_dir} - the app looks there before anything baked into the exe.")


def _first_reachable(urls: Sequence[str], b: Backend) -> str:
    last = ""
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
            with urllib.request.urlopen(req, timeout=30):
                return url
        except Exception as exc:  # noqa: BLE001
            last = f"{url}: {exc}"
    raise RuntimeError(
        f"{b.name}: no download worked. Download it yourself from {b.homepage} and unzip "
        f"into the backends folder as '{b.key}'. Last error - {last}")


def _rank_assets(assets: list[dict], patterns: Sequence[str]) -> str:
    for pattern in patterns:
        rx = re.compile(pattern, re.I)
        for a in assets:
            if rx.search(a["name"]):
                return a["browser_download_url"]
    return assets[0]["browser_download_url"]


def list_assets(b: Backend) -> list[str]:
    """What the backend's latest release actually offers.

    The asset-name patterns in this file are ranked guesses. When one picks the
    wrong file, this is how you see the real names - then pass the right one with
    `fetch-backends <key> --asset <regex>`.
    """
    if not b.github_repo:
        return [b.direct_url or f"(scraped from {b.scrape_page})"]
    data = json.loads(_http_get(GITHUB_API.format(repo=b.github_repo)).decode("utf-8"))
    tag = data.get("tag_name", "?")
    return [f"{tag}: {a['name']}" for a in data.get("assets", [])]


def install_backend(b: Backend, log: Callable[[str], None] = print, force: bool = False,
                    asset: str | None = None) -> Path:
    """Download and unpack a backend. Returns the path to its executable."""
    if not force:
        existing = b.find_exe()
        if existing:
            log(f"{b.name}: already present ({existing.name})")
            return existing

    log(f"{b.name}: fetching")
    if asset and b.github_repo:
        data = json.loads(_http_get(GITHUB_API.format(repo=b.github_repo)).decode("utf-8"))
        version = data.get("tag_name", "?")
        urls, version = [_rank_assets([a for a in data.get("assets", [])], (asset,))], version
    elif b.github_repo:
        url, version = resolve_download(b, log)
        urls = [url]
    else:
        urls, version = candidate_urls(b, log), "latest"

    if not urls:
        raise RuntimeError(_manual_install_message(b, "no download link could be found"))

    blob = b""
    used = ""
    problems: list[str] = []
    for url in urls:
        try:
            log(f"  trying {url}")
            blob = _http_get(url, timeout=300, referer=b.scrape_page or b.homepage)
            used = url
            break
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{url} -> {exc}")
            log(f"    {exc}")
    if not blob:
        raise RuntimeError(_manual_install_message(b, "; ".join(problems)))
    url = used
    log(f"  {version} <- {url}")
    log(f"  {len(blob) / 1048576:.1f} MB downloaded")

    dest = b.install_dir
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    name = url.rsplit("/", 1)[-1].lower()
    if name.endswith(".zip") or blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            zf.extractall(dest)
    elif name.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")):
        with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
            try:
                tf.extractall(dest, filter="data")   # 3.12+ (and backports)
            except TypeError:
                tf.extractall(dest)
    elif name.endswith(".exe"):
        target = dest / url.rsplit("/", 1)[-1]
        target.write_bytes(blob)
    else:
        (dest / url.rsplit("/", 1)[-1]).write_bytes(blob)

    # a single top-level folder is the common shape - leave it, find_exe recurses
    for p in dest.rglob("*"):
        if p.is_file() and p.suffix.lower() in ("", ".exe", ".sh", ".command"):
            try:
                p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:
                pass

    exe = b.find_exe()
    if not exe:
        raise RuntimeError(
            f"{b.name}: downloaded and unpacked to {dest} but no executable matched "
            f"{b.exe_patterns}. Look in that folder and check the release layout."
        )
    log(f"  installed: {exe}")
    (dest / "VERSION.txt").write_text(f"{version}\n{url}\n", "utf-8")
    return exe


def installed_version(b: Backend) -> str:
    for root in [b.install_dir] + [d / b.key for d in paths.bundled_backends_dirs()]:
        f = root / "VERSION.txt"
        if f.is_file():
            try:
                return f.read_text("utf-8").splitlines()[0].strip()
            except Exception:
                pass
    return "?" if b.installed() else "-"


def fetch_all(log: Callable[[str], None] = print, only: Iterable[str] | None = None,
              force: bool = False, asset: str | None = None) -> dict[str, str]:
    """Install every backend. Returns {key: 'ok' | error message}."""
    results: dict[str, str] = {}
    keys = list(only) if only else list(REGISTRY)
    for key in keys:
        b = REGISTRY.get(key)
        if not b:
            results[key] = "unknown backend"
            continue
        try:
            install_backend(b, log, force=force, asset=asset)
            results[key] = "ok"
        except Exception as exc:  # noqa: BLE001 - report, never abort the batch
            log(f"{b.name}: FAILED - {exc}")
            results[key] = str(exc)
    return results


def dotnet_present() -> bool:
    exe = shutil.which("dotnet")
    if not exe:
        return False
    try:
        out = subprocess.run([exe, "--list-runtimes"], capture_output=True, text=True, timeout=20)
        return "Microsoft.NETCore.App" in (out.stdout or "")
    except Exception:
        return False


def probe_help(b: Backend, timeout: int = 30) -> str:
    """Ask the installed backend what it actually accepts. Flags drift between versions."""
    exe = b.find_exe()
    if not exe:
        return f"{b.name} is not installed."
    for flag in (["--help"], ["-h"], []):
        try:
            out = subprocess.run([str(exe)] + flag, capture_output=True, text=True,
                                 timeout=timeout, cwd=str(exe.parent))
            text = (out.stdout or "") + (out.stderr or "")
            if text.strip():
                return text.strip()
        except Exception as exc:  # noqa: BLE001
            return f"Could not run {exe.name}: {exc}"
    return f"{exe.name} produced no help output."
