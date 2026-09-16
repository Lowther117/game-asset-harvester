"""Work out which Unreal Engine version a game was built with.

Getting this wrong is the single biggest cause of a run that "succeeds" and
produces almost nothing: CUE4Parse is told GAME_UE5_LATEST, the packages are
4.27, and every asset fails with "Could not load standard asset".

Four sources of evidence, strongest first:
  1. Engine/Build/Build.version  - the engine's own JSON, exact when shipped
  2. the shipping exe's PE version resource - exact, and nearly always present
  3. the .pak footer's format version - a range, always available
  4. pak filename convention and the presence of .utoc - coarse hints
"""
from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

# CUE4Parse -g tags we can actually emit
UE_TAGS = {
    (4, 20): "GAME_UE4_20", (4, 21): "GAME_UE4_21", (4, 22): "GAME_UE4_22",
    (4, 23): "GAME_UE4_23", (4, 24): "GAME_UE4_24", (4, 25): "GAME_UE4_25",
    (4, 26): "GAME_UE4_26", (4, 27): "GAME_UE4_27",
    (5, 0): "GAME_UE5_0", (5, 1): "GAME_UE5_1", (5, 2): "GAME_UE5_2",
    (5, 3): "GAME_UE5_3", (5, 4): "GAME_UE5_4", (5, 5): "GAME_UE5_5",
    (5, 6): "GAME_UE5_6",
}

# pak footer format version -> plausible (major, minor) range
PAK_VERSION_RANGES = {
    1: ((4, 0), (4, 2)), 2: ((4, 0), (4, 2)), 3: ((4, 3), (4, 15)),
    4: ((4, 16), (4, 19)), 5: ((4, 20), (4, 21)), 6: ((4, 21), (4, 21)),
    7: ((4, 22), (4, 22)), 8: ((4, 22), (4, 24)), 9: ((4, 23), (4, 24)),
    10: ((4, 25), (4, 25)), 11: ((4, 26), (5, 6)),
}

PAK_MAGIC = 0x5A6F12E1


@dataclass
class UEVersion:
    major: int | None = None
    minor: int | None = None
    tag: str = ""
    confidence: str = "none"          # exact | narrow | coarse | none
    evidence: list[str] = field(default_factory=list)
    encrypted_index: bool = False
    pak_version: int | None = None
    has_iostore: bool = False

    @property
    def needs_mappings(self) -> bool:
        """Whether a .usmap is required before anything will convert.

        UE5 serialises object properties UNVERSIONED: without a mappings file
        CUE4Parse cannot deserialise a single UTexture2D or UStaticMesh, so it
        writes the raw .ubulk payloads it can copy verbatim and nothing else.
        The run exits 0 and looks like it worked.
        """
        if self.major is None:
            return False
        if self.major >= 5:
            return True
        # UE4.25 added unversioned properties; IoStore games are the ones that use it
        return self.major == 4 and (self.minor or 0) >= 25 and self.has_iostore

    @property
    def label(self) -> str:
        if self.major is None:
            return "Unreal Engine (version unknown)"
        if self.minor is None:
            return f"Unreal Engine {self.major}"
        return f"Unreal Engine {self.major}.{self.minor}"

    def summary(self) -> str:
        bits = [f"{self.label} -> {self.tag or 'no tag'} ({self.confidence})"]
        bits += [f"    {e}" for e in self.evidence]
        if self.encrypted_index:
            bits.append("    pak index is ENCRYPTED - an AES key is required "
                        "(the app will look for it in the shipping exe)")
        if self.needs_mappings:
            bits.append("    properties are UNVERSIONED - a .usmap mappings file is required "
                        "or nothing will convert")
        return "\n".join(bits)


def tag_for(major: int | None, minor: int | None) -> str:
    if major is None:
        return ""
    if minor is not None and (major, minor) in UE_TAGS:
        return UE_TAGS[(major, minor)]
    if major >= 5:
        return "GAME_UE5_LATEST"
    if minor is not None and minor < 20:
        return "GAME_UE4_20"
    return "GAME_UE4_LATEST"


# ---------------------------------------------------------------- evidence 1

def _from_build_version(game_root: Path) -> tuple[int, int, str] | None:
    for candidate in list(game_root.glob("*/Build/Build.version")) + \
                     list(game_root.glob("Engine/Build/Build.version")) + \
                     list(game_root.glob("*/*/Build/Build.version")):
        try:
            data = json.loads(candidate.read_text("utf-8", errors="replace"))
            major, minor = int(data["MajorVersion"]), int(data["MinorVersion"])
            return major, minor, f"Build.version says {major}.{minor} ({candidate.name})"
        except Exception:  # noqa: BLE001
            continue
    return None


# ---------------------------------------------------------------- evidence 2

def _rva_to_offset(rva: int, sections: list[tuple[int, int, int, int]]) -> int | None:
    for va, vsize, raw_ptr, raw_size in sections:
        if va <= rva < va + max(vsize, raw_size):
            return raw_ptr + (rva - va)
    return None


def pe_file_version(path: Path) -> tuple[int, int, int, int] | None:
    """Read a Windows exe/dll's FileVersion without pywin32.

    Locates the resource section from the PE headers, then scans it for the
    VS_FIXEDFILEINFO signature rather than walking the resource tree - same
    answer, far less code to get wrong.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(0x400)
            if head[:2] != b"MZ":
                return None
            e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
            if e_lfanew <= 0 or e_lfanew > 0x10000:
                return None
            fh.seek(e_lfanew)
            pe = fh.read(0x200)
            if pe[:4] != b"PE\0\0":
                return None
            num_sections = struct.unpack_from("<H", pe, 6)[0]
            size_opt = struct.unpack_from("<H", pe, 20)[0]
            opt_magic = struct.unpack_from("<H", pe, 24)[0]
            if opt_magic == 0x10B:
                dd = 24 + 96
            elif opt_magic == 0x20B:
                dd = 24 + 112
            else:
                return None
            if dd + 24 > len(pe):
                return None
            rsrc_rva, rsrc_size = struct.unpack_from("<II", pe, dd + 2 * 8)
            if not rsrc_rva or not rsrc_size:
                return None

            fh.seek(e_lfanew + 24 + size_opt)
            sect_blob = fh.read(40 * num_sections)
            sections = []
            for i in range(num_sections):
                off = 40 * i
                if off + 40 > len(sect_blob):
                    break
                vsize, va, raw_size, raw_ptr = struct.unpack_from("<IIII", sect_blob, off + 8)
                sections.append((va, vsize, raw_ptr, raw_size))

            file_off = _rva_to_offset(rsrc_rva, sections)
            if file_off is None:
                return None
            fh.seek(file_off)
            blob = fh.read(min(rsrc_size, 8 * 1024 * 1024))

        idx = blob.find(b"\xBD\x04\xEF\xFE")
        if idx < 0 or idx + 20 > len(blob):
            return None
        ms, ls = struct.unpack_from("<II", blob, idx + 8)
        return (ms >> 16) & 0xFFFF, ms & 0xFFFF, (ls >> 16) & 0xFFFF, ls & 0xFFFF
    except Exception:  # noqa: BLE001
        return None


def _from_shipping_exe(game_root: Path) -> tuple[int, int, str] | None:
    patterns = ["*/Binaries/Win64/*-Shipping.exe", "*/Binaries/Win64/*.exe",
                "*/Binaries/WinGDK/*.exe", "Engine/Binaries/Win64/*.exe", "*.exe"]
    seen: set[Path] = set()
    for pattern in patterns:
        for exe in sorted(game_root.glob(pattern))[:12]:
            if exe in seen or not exe.is_file():
                continue
            seen.add(exe)
            ver = pe_file_version(exe)
            if not ver:
                continue
            major, minor = ver[0], ver[1]
            # UE shipping binaries carry the engine version; anything else is noise
            if major in (4, 5) and 0 <= minor <= 40:
                return major, minor, f"{exe.name} reports engine {major}.{minor}"
    return None


# ---------------------------------------------------------------- evidence 3

def read_pak_footer(pak: Path) -> tuple[int | None, bool]:
    """Return (pak format version, encrypted-index flag) from a .pak trailer."""
    try:
        size = pak.stat().st_size
        if size < 64:
            return None, False
        with pak.open("rb") as fh:
            fh.seek(max(0, size - 512))
            tail = fh.read(512)
    except OSError:
        return None, False

    magic = struct.pack("<I", PAK_MAGIC)
    idx = tail.rfind(magic)
    if idx < 0 or idx + 8 > len(tail):
        return None, False
    version = struct.unpack_from("<I", tail, idx + 4)[0]
    encrypted = False
    if version >= 4 and idx >= 1:
        encrypted = tail[idx - 1] == 1
    if not (1 <= version <= 64):
        return None, encrypted
    return version, encrypted


# ---------------------------------------------------------------- top level

def detect_ue_version(game_root: Path, paks: list[Path] | None = None) -> UEVersion:
    game_root = Path(game_root)
    result = UEVersion()
    paks = paks or []

    pak_files = [p for p in paks if p.suffix.lower() == ".pak"]
    result.has_iostore = any(p.suffix.lower() in (".utoc", ".ucas") for p in paks)

    for pak in pak_files[:4]:
        version, encrypted = read_pak_footer(pak)
        if encrypted:
            result.encrypted_index = True
        if version and result.pak_version is None:
            result.pak_version = version
            result.evidence.append(f"pak format version {version} ({pak.name})")

    exact = _from_build_version(game_root) or _from_shipping_exe(game_root)
    if exact:
        result.major, result.minor, note = exact
        result.evidence.insert(0, note)
        result.confidence = "exact"
        result.tag = tag_for(result.major, result.minor)
        return result

    # filename convention: UE4 ships -WindowsNoEditor, UE5 ships -Windows
    names = " ".join(p.name.lower() for p in pak_files)
    if "windowsnoeditor" in names:
        result.major, result.confidence = 4, "narrow"
        result.evidence.append("pak named *-WindowsNoEditor.pak, which is UE4 naming")
    elif re.search(r"-windows(client|server)?\.pak", names) and result.has_iostore:
        result.major, result.confidence = 5, "coarse"
        result.evidence.append("pak named *-Windows.pak with IoStore containers, which is UE5 naming")

    if result.pak_version and result.pak_version in PAK_VERSION_RANGES:
        (lo_major, lo_minor), (hi_major, hi_minor) = PAK_VERSION_RANGES[result.pak_version]
        if result.major is None:
            result.major = lo_major
            result.confidence = "coarse"
        if result.major == lo_major == hi_major and lo_minor == hi_minor:
            result.minor = lo_minor
            result.confidence = "narrow"
        elif result.major == 4 and lo_major == 4:
            # a UE4 game with pak v11 is almost always 4.26/4.27
            result.minor = 27 if result.pak_version == 11 else hi_minor
            result.confidence = "narrow" if result.pak_version != 11 else "coarse"

    if result.major is None and result.has_iostore:
        result.major, result.confidence = 5, "coarse"
        result.evidence.append("IoStore .utoc/.ucas present, so 4.26 or newer")

    result.tag = tag_for(result.major, result.minor)
    if not result.evidence:
        result.evidence.append("no version evidence found - using the fallback tag")
    return result
