"""Find the AES key an Unreal game uses for its pak index, without being asked.

A pak with an encrypted index is useless to every backend until it has the key,
and the key is not a secret in any real sense: the game has to carry it to run.
It sits in the shipping executable, either as 32 raw bytes or as a
"0x<64 hex>" string, which is exactly what the AESDumpster family of tools scans
for. This does the same in plain Python, then does something those tools cannot:
it checks each candidate against the game's own pak. The first 16 bytes of a pak
index are the mount-point string ("../../../<Game>/Content/Paks/"), so a key
that decrypts them into that is the key - no guessing left.

Three sources, in order:
  1. keys.json beside the app - every key that ever worked here, by game name
  2. the shipping exe, scanned for candidates and verified against the pak
  3. hex strings in any .txt / .json / .ini the user dropped in the keys folder
"""
from __future__ import annotations

import json
import math
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import aes, paths

PAK_MAGIC = 0x5A6F12E1
HEX_KEY = re.compile(rb"0[xX]([0-9A-Fa-f]{64})(?![0-9A-Fa-f])")
HEX_KEY_WIDE = re.compile(rb"0\x00[xX]\x00((?:[0-9A-Fa-f]\x00){64})")
BARE_HEX = re.compile(rb"(?<![0-9A-Fa-f])([0-9A-Fa-f]{64})(?![0-9A-Fa-f])")
MAX_EXE_MB = 400
SCAN_SECONDS = 90.0      # per binary; the key is normally found in the first second


# ---------------------------------------------------------------- library

def keys_path() -> Path:
    return paths.app_dir() / "keys.json"


def keys_dir() -> Path:
    d = paths.app_dir() / "keys"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_library(path: Path | None = None) -> dict[str, str]:
    try:
        data = json.loads((path or keys_path()).read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remember(game: str, key: str, path: Path | None = None) -> None:
    """Store a key that worked, so the next run of this game is instant."""
    path = path or keys_path()
    lib = _load_library(path)
    lib[game] = normalise(key)
    try:
        path.write_text(json.dumps(lib, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def known(game: str, path: Path | None = None) -> list[str]:
    """Keys on file for this game, closest name first."""
    from .mappings import _similar
    lib = _load_library(path)
    scored = [(_similar(game, name), key) for name, key in lib.items()]
    scored.sort(key=lambda sk: -sk[0])
    return [key for score, key in scored if score >= 0.6]


def dropped_keys(folder: Path | None = None) -> list[str]:
    """Any 64-hex-digit strings in text files the user left in keys\\."""
    out: list[str] = []
    for f in sorted((folder or keys_dir()).rglob("*")):
        if f.suffix.lower() not in (".txt", ".json", ".ini", ".md", ".key") or not f.is_file():
            continue
        try:
            blob = f.read_bytes()[:1_000_000]
        except OSError:
            continue
        for m in HEX_KEY.finditer(blob):
            out.append("0x" + m.group(1).decode("ascii").upper())
        for m in BARE_HEX.finditer(blob):
            out.append("0x" + m.group(1).decode("ascii").upper())
    return _dedupe(out)


def normalise(key: str) -> str:
    key = key.strip()
    if key.lower().startswith("0x"):
        key = key[2:]
    return "0x" + key.upper()


def _dedupe(keys: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for k in keys:
        k = normalise(k)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


# ---------------------------------------------------------------- the pak side

@dataclass
class PakIndex:
    pak: Path
    offset: int
    size: int
    encrypted: bool
    version: int


def pak_index(pak: Path) -> PakIndex | None:
    """Where the index lives, from the FPakInfo trailer.

    Layout after the magic: int32 Version, int64 IndexOffset, int64 IndexSize.
    The encrypted flag is the byte immediately before the magic (v4+).
    """
    try:
        size = pak.stat().st_size
        if size < 64:
            return None
        with pak.open("rb") as fh:
            fh.seek(max(0, size - 512))
            tail = fh.read(512)
    except OSError:
        return None
    idx = tail.rfind(struct.pack("<I", PAK_MAGIC))
    if idx < 0 or idx + 24 > len(tail):
        return None
    version, offset, isize = struct.unpack_from("<IQQ", tail, idx + 4)
    encrypted = idx >= 1 and tail[idx - 1] == 1
    if not (1 <= version <= 64) or offset <= 0 or offset >= size or isize <= 0:
        return None
    return PakIndex(pak, offset, isize, encrypted, version)


def looks_like_mount_point(block: bytes) -> bool:
    """A decrypted index starts with an FString: int32 length, then the text.

    Mount points are "../../../Game/Content/Paks/" or a variant, so a plausible
    length followed by printable characters beginning with '.' or '/' is as good
    a proof as exists that the key is right - a wrong key gives noise here.
    """
    if len(block) < 8:
        return False
    length = struct.unpack_from("<i", block, 0)[0]
    if length < 0:                      # UTF-16 mount point: rare, check the pattern
        n = -length
        if not (2 <= n <= 512):
            return False
        text = block[4:4 + min(2 * n, len(block) - 4)]
        return text[:2] in (b"./", b"/\x00") or text[:4] == b".\x00.\x00"
    if not (2 <= length <= 512):
        return False
    text = block[4:4 + min(length, len(block) - 4)]
    if length <= len(block) - 4:        # the whole string fits: drop its NUL terminator
        if text[-1:] != b"\0":
            return False
        text = text[:-1]
    if not text:
        return False
    if not all(32 <= b < 127 for b in text):
        return False
    return text[:1] in (b".", b"/") or text[:3] == b"../"


def verify(key_hex: str, index: PakIndex) -> bool:
    try:
        key = bytes.fromhex(normalise(key_hex)[2:])
    except ValueError:
        return False
    if len(key) != 32:
        return False
    try:
        with index.pak.open("rb") as fh:
            fh.seek(index.offset)
            head = fh.read(32)
    except OSError:
        return False
    if len(head) < 32:
        return False
    return looks_like_mount_point(aes.ecb_decrypt(head, key))


# ---------------------------------------------------------------- the exe side

def _entropy(window: bytes) -> float:
    counts: dict[int, int] = {}
    for b in window:
        counts[b] = counts.get(b, 0) + 1
    n = len(window)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _pe_data_sections(blob: bytes) -> list[tuple[int, int]]:
    """(offset, size) of the initialised-data sections - where constants live."""
    try:
        if blob[:2] != b"MZ":
            return []
        e_lfanew = struct.unpack_from("<I", blob, 0x3C)[0]
        if blob[e_lfanew:e_lfanew + 4] != b"PE\0\0":
            return []
        coff = e_lfanew + 4
        num_sections = struct.unpack_from("<H", blob, coff + 2)[0]
        size_opt = struct.unpack_from("<H", blob, coff + 16)[0]
        sect = coff + 20 + size_opt
        out = []
        for i in range(num_sections):
            off = sect + i * 40
            name = blob[off:off + 8].rstrip(b"\0")
            raw_size, raw_ptr = struct.unpack_from("<II", blob, off + 16)
            chars = struct.unpack_from("<I", blob, off + 36)[0]
            initialised = bool(chars & 0x40)      # IMAGE_SCN_CNT_INITIALIZED_DATA
            if initialised and raw_size and raw_ptr and name not in (b".rsrc", b".reloc",
                                                                       b".pdata"):
                out.append((raw_ptr, min(raw_size, len(blob) - raw_ptr)))
        return out
    except (struct.error, IndexError):
        return []


def _windows(run: bytes, stride: int, base: int):
    for i in range(0, len(run) - 31, stride):
        w = run[i:i + 32]
        if len(set(w)) < 24:              # text and padding never pass this
            continue
        if all(32 <= b < 127 for b in w):  # printable: a string, not a key
            continue
        yield base + i, w


def candidates_from_bytes(blob: bytes):
    """Yield (key, how) for everything in this binary that could be a key,
    most likely first, and never stop early: the caller verifies each one
    against the pak as it arrives and returns the moment one fits.

    Order matters because a big game exe can hold megabytes of embedded
    high-entropy data (compressed resources, shader blobs) that would drown a
    fixed-size candidate list. A key is a short constant, usually padded with
    zeros and 16-byte aligned, so those are tried first; the deep sweep of
    long high-entropy stretches comes last and is bounded by the caller's
    time budget.
    """
    # 1. the easy form: a literal "0x..." hex string, narrow or wide
    for m in HEX_KEY.finditer(blob):
        yield "0x" + m.group(1).decode("ascii").upper(), "hex string"
    for m in HEX_KEY_WIDE.finditer(blob):
        yield "0x" + m.group(1).decode("utf-16-le").upper(), "wide hex string"

    sections = _pe_data_sections(blob) or [(0, len(blob))]
    # a stretch of non-zero bytes, allowing one zero inside it
    run_rx = re.compile(rb"[^\x00]+(?:\x00[^\x00]+)?")
    short: list[tuple[int, bytes]] = []
    aligned: list[tuple[int, bytes]] = []
    long_runs: list[tuple[int, bytes]] = []
    for start, size in sections:
        region = blob[start:start + size]
        for m in run_rx.finditer(region):
            run = m.group(0)
            if len(run) < 32:
                continue
            base = start + m.start()
            if len(run) <= 96:
                short.append((base, run))       # a padded constant: the classic case
            else:
                long_runs.append((base, run))

    # 2. short padded constants, every offset
    for base, run in short:
        for off, w in _windows(run, 1, base):
            yield "0x" + w.hex().upper(), f"raw bytes at 0x{off:X}"
    # 3. 16-byte-aligned windows inside longer stretches
    for base, run in long_runs:
        first = (-base) % 16
        for off, w in _windows(run[first:], 16, base + first):
            yield "0x" + w.hex().upper(), f"raw bytes at 0x{off:X} (aligned)"
    # 4. everything else, shortest stretches first - a key is not inside a
    #    megabyte of compressed data
    long_runs.sort(key=lambda br: len(br[1]))
    for base, run in long_runs:
        for off, w in _windows(run, 1, base):
            if (off - base) % 16 == 0:
                continue                         # already tried above
            yield "0x" + w.hex().upper(), f"raw bytes at 0x{off:X}"


def shipping_exes(game_root: Path) -> list[Path]:
    patterns = ["*/Binaries/Win64/*-Shipping.exe", "*/Binaries/Win64/*.exe",
                "*/Binaries/WinGDK/*.exe", "*/Binaries/Win64/*.dll", "*.exe"]
    out: list[Path] = []
    for pattern in patterns:
        for exe in sorted(game_root.glob(pattern)):
            if exe.is_file() and exe not in out and exe.stat().st_size <= MAX_EXE_MB * 2**20:
                out.append(exe)
    # the shipping binary first, everything else after
    out.sort(key=lambda p: (0 if "shipping" in p.name.lower() else 1, -p.stat().st_size))
    return out[:6]


def encrypted_paks(game_root: Path, paks: list[Path] | None = None) -> list[PakIndex]:
    files = list(paks or [])
    if not files:
        files = [p for p in game_root.rglob("*.pak")][:64]
    out = []
    for pak in files:
        info = pak_index(pak)
        if info and info.encrypted:
            out.append(info)
    return out


# ---------------------------------------------------------------- the whole thing

@dataclass
class KeySearch:
    verified: list[str] = field(default_factory=list)     # decrypts the pak index
    unverified: list[str] = field(default_factory=list)   # plausible, could not test
    notes: list[str] = field(default_factory=list)

    @property
    def best(self) -> str:
        return self.verified[0] if self.verified else (self.unverified[0] if self.unverified else "")


def find(game_root: Path, game: str = "", paks: list[Path] | None = None,
         log: Callable[[str], None] = lambda s: None,
         library: Path | None = None, dropped: Path | None = None) -> KeySearch:
    result = KeySearch()
    game_root = Path(game_root)
    game = game or game_root.name
    indexes = encrypted_paks(game_root, paks)
    if not indexes:
        result.notes.append("no pak with an encrypted index under this folder, so no key is needed")
        return result
    index = indexes[0]
    log(f"   encrypted index in {index.pak.name} - looking for the AES key")

    def check(keys: list[str], source: str) -> bool:
        for key in keys:
            if verify(key, index):
                result.verified.append(key)
                result.notes.append(f"{key} decrypts {index.pak.name}'s index ({source})")
                log(f"   AES key found: {key}  ({source})")
                return True
        return False

    # 1. keys that worked before
    if check(known(game, library), "from keys.json"):
        return result
    # 2. anything the user dropped in the keys folder
    if check(dropped_keys(dropped), "from a file in the keys folder"):
        return result
    # 3. scan the binaries, verifying each candidate as it turns up
    import time as _time
    deadline = _time.monotonic() + SCAN_SECONDS
    for exe in shipping_exes(game_root):
        try:
            blob = exe.read_bytes()
        except OSError:
            continue
        log(f"   scanning {exe.name} ({len(blob) // 2**20} MB) for the key...")
        tried = 0
        for key, how in candidates_from_bytes(blob):
            tried += 1
            if verify(key, index):
                result.verified.append(key)
                result.notes.append(f"{key} decrypts {index.pak.name}'s index "
                                    f"(found in {exe.name} as {how})")
                log(f"   AES key found after {tried} candidate(s): {key}  ({how}, {exe.name})")
                remember(game, key, library)
                return result
            if "hex string" in how and key not in result.unverified:
                # a literal that happens to be 64 hex digits is a key far more
                # often than not, even when it does not fit THIS pak
                result.unverified.append(key)
            if tried % 20000 == 0 and _time.monotonic() > deadline:
                log(f"   gave up on {exe.name} after {tried} candidates and "
                    f"{SCAN_SECONDS:.0f}s")
                break
        else:
            log(f"   {tried} candidate(s) in {exe.name}, none decrypt the index")
    if result.unverified:
        result.notes.append(f"{len(result.unverified)} hex-string candidate(s) did not "
                            f"decrypt the index - they are offered anyway")
    else:
        result.notes.append("no key in the shipping exe decrypts this pak's index - the key "
                            "is probably fetched at runtime or split; paste one into the "
                            "AES field, or drop a text file containing it in the keys folder")
    return result
