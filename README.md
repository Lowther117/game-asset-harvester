# Game Asset Harvester

One front end over the game asset extraction tools, so you stop juggling five of them.

Point it at a game folder. It works out what engine the game uses, picks the right
extraction tool, runs it, and drops meshes and textures into one predictable output
tree with a manifest. Unreal, Unity, Godot and the long tail of one-off archive
formats all come out the same shape.

## Which file do I use?

| File | What it does |
|---|---|
| `build-exe.bat` | Double-click to build `dist\GameAssetHarvester\GameAssetHarvester.exe`. Installs Python if needed, downloads every backend, bakes them in, self-tests the result. |
| `run.bat` | Run from source without building. Sets up its own `.venv` on first run. |
| `ensure_python.ps1` | Helper both of the above call. Not run directly. |
| `harvester_app.py` | Entry point — the window, and the command-line modes below. |

## Windows only — and why

This one breaks the house "Windows and Mac" rule, deliberately. Four of the five
backends are Windows binaries with no macOS build: umodel stopped at a Win32/Linux
build in 2023, AssetStudioModCLI and CUE4Parse.CLI are .NET Windows releases, and
QuickBMS ships as a Windows exe. A Mac build would be a window with almost every
button greyed out. The code has no Windows-only imports, so if the backends ever
grow macOS releases, adding `build-app.command` is a small job.

## The backends

Nothing is redistributed with this repo. Each tool is downloaded from its own
official source — by `build-exe.bat` at build time, or from the Backends tab on
first run. The exe you build carries the copies you downloaded yourself.

| Engine | Backend | Notes |
|---|---|---|
| Unreal Engine 4 / 5 | **CUE4Parse.CLI** | Meshes to glTF/PSK/UEFormat, textures to PNG, AES keys, `.usmap` mappings |
| Unreal Engine 1–4 | **UE Viewer (umodel)** | Better for older UE1–UE3 games. No UE5 support |
| Unity | **AssetStudioModCLI** | Textures to PNG/TGA, meshes to OBJ (FBX only via its animator mode), MonoBehaviour dumps |
| Godot | **Godot RE Tools** | `.pck` files and exes with an embedded pck, full project recovery and GDScript decompilation |
| Anything else | **QuickBMS** | Script-driven. Needs a `.bms` script per game, from the same site |

### Where FModel went

FModel has no command line — the maintainer's own answer to that request was to
point at CUE4Parse, the library FModel is built on. So the Unreal path here drives
CUE4Parse directly. Same parser, same output, scriptable. Keep FModel installed for
eyeballing an asset tree; use this for anything you want done in bulk.

CUE4Parse.CLI and AssetStudioModCLI are .NET. The build prefers self-contained
releases where one exists; if it lands a framework-dependent build instead, the
Backends tab says so and you'll need the .NET runtime installed.

## Using it

**Pick the game's folder. Press Extract everything.** That is the whole thing. It scans
the folder to the bottom, downloads whatever backend is missing, finds the `.usmap` if the
game needs one, and works down a ladder of settings until assets actually come out — then
opens the output folder. No options to set, no Scan button to press first.

Everything below is for when you want to drive it by hand.

1. **Extract tab** → pick the game's install folder. Picking a single `.pak`/`.utoc`/`.ucas`,
   or the `Paks` folder itself, works too — the scan walks up to the game folder and says
   so, because the backends want the game directory and the version evidence lives several
   levels above `Paks`.
2. Check what it found. One row per engine — the detected version, the backend, the
   folder it will be pointed at, the engine tag — with every container file it matched
   listed underneath, so you can see exactly what it is working from.
3. Set the output folder and formats. **Show command** prints the exact command line
   it is about to run, which is the fastest way to see why something is off.
4. **Run once with these settings**. Live output in the Log tab, **Cancel** stops it.

### The whole folder, not the first thing it finds

The scan walks the entire tree — 32 levels deep, half a million files, with a two-minute
ceiling so a mistakenly-picked drive root cannot hang the app. A modern install scatters
containers across base game, DLC, plugin and chunk folders, and the scan reports every one
of them rather than the first.

What happens next depends on the backend. Unreal's take a directory and walk it, so one
job is pointed at the folder that contains all of them and covers the lot in a single
pass; if that turns out not to reach everything, the brute-force ladder retries one folder
at a time. AssetStudio, Godot RE Tools and QuickBMS each take **one** archive or data
folder, so every location found gets its own job — a game with a launcher and two bundled
mini-games produces three Unity jobs, not one.

The window opens in dark mode. The **Light** / **Dark** button in the status bar
(or **Ctrl+D**) switches, and the choice is remembered in `harvester-settings.json`.

Output goes to your Downloads folder by default (`Downloads\HarvestedAssets`, pre-filled
in the output field); **Default save folder…** in the status bar changes that for good,
**Reset to Downloads** puts it back, and every folder picker opens there.
Output lands in `<output>/<GameName>/<engine>/`, mirroring the game's own asset
paths — modders need those paths, so they are preserved by default. Tick **Sort
output** to shuffle everything into `Meshes` / `Textures` / `Audio` / `Data` / `Raw`
instead. Either way a `manifest.json` records every file, its category, size, SHA-1
and the exact command that produced it.

### If a backend will not download

Each tool is fetched from its own site, and those sites move things: gildor.org rotates
its download ids and refuses requests that do not look like a browser. The fetcher sends
a browser user-agent and referer and works through every candidate link it can find
rather than giving up on the first 404. If they all fail it tells you the page to get it
from and the exact folder to unzip it into — `backends\<name>\`, which is searched
before anything baked into the exe.

### If a download grabs the wrong file

Each backend is matched to a release asset by a ranked name pattern, and release
naming changes. If a fetch lands the wrong build, `fetch-backends --list` prints the
real asset names and `fetch-backends cue4parse --asset "win.*x64"` takes the one you
name. Dropping the binary into `backends\<name>\` by hand works too — that folder is
searched before anything baked into the exe.

### Engine version detection

Leave **UE version** on `auto`. Telling CUE4Parse the wrong engine version is the single
biggest cause of a run that appears to work and exports almost nothing — tens of thousands
of *"Could not load standard asset, check game version, mappings or keys"*. The scan works
the version out from four sources, strongest first:

| Evidence | Confidence |
|---|---|
| `Engine/Build/Build.version` | exact |
| The shipping exe's Windows version resource | exact |
| The `.pak` trailer's format version | narrow |
| `-WindowsNoEditor.pak` vs `-Windows.pak`, and whether `.utoc` containers exist | coarse |

The scan also reads the pak trailer's encrypted-index flag, so a game needing an AES key
says **"needs an AES key"** in the results table rather than producing a wall of failures.

Picking a specific tag overrides the detection. If you do that and the run then fails, the
app says so explicitly and tells you to go back to `auto`.

### Just extract everything

**Extract everything** (or `--brute`) is the main way to use this. It scans, downloads any
missing backend, finds a `.usmap` if the game needs one, then works down a ladder of
settings and keeps the first one that actually converts something.

The ladder is per backend, and it goes in three stages: settings first, then the other
backend for that engine, then one container folder at a time.

| Backend | What it varies |
| --- | --- |
| CUE4Parse | the detected engine version, then its eight nearest neighbours, then `UE5_LATEST` / `UE4_LATEST`; with and without mappings; `-f auto` and `-f png` (which also skips the sound decoder that crashes some games outright); glTF meshes, then ActorX, then textures only |
| umodel | glTF+PNG, then ActorX+TGA (its native pair, which survives assets the glTF writer aborts on), then textures only, then meshes only |
| AssetStudio | the usual types, then every type it supports, then textures only; grouped by container, by type, and flat |
| Godot RE Tools | `--recover` (rebuilds a project) and `--extract` (raw files) |
| QuickBMS | every `.bms` script in your library, closest name match first |

Optional extra flags are only ever added after checking the installed binary's own
`--help` for them. These tools rename their options between releases, and an unknown flag
turns a run that would have half-worked into an instant parse error — so if the help
cannot be read, nothing extra is added.

Each attempt is abandoned the moment it is clearly producing only raw payloads, so the
whole thing costs minutes of probing rather than a full pass per setting. An attempt that
has not written anything yet is left alone, because some backends only flush at the end.

### The AES key is found for you

An Unreal game with an encrypted pak index is useless to every backend until it has the
key — and the key is not a secret in any real sense, because the game has to carry it to
run. It sits in the shipping executable, either as 32 raw bytes or as a `0x…` hex string,
which is what the AESDumpster family of tools scans for.

This app does the same scan in plain Python, then does the thing those tools cannot: it
**proves each candidate against the game's own pak**. The first bytes of a pak index are
the mount-point string (`../../../Game/Content/Paks/`), so a key that decrypts them into
that is the key — no guessing, no decoys. The scan is tiered (hex-string literals, then
short zero-padded constants, then 16-byte-aligned data, then everything else) so a big exe
full of compressed resources cannot drown the real key, and it stops the moment one fits.

It runs on its own whenever a job needs a key and none is given, in both **Extract
everything** and a normal run, and it looks in three places in order:

1. `keys.json` beside the exe — every key that has ever worked here, matched by game
   name like mappings are, so a game you have done before is instant
2. any `.txt` / `.json` / `.ini` you drop in the `keys\` folder that contains a
   64-hex-digit string — paste a key from anywhere and it is picked up
3. the shipping exe, scanned and verified as above

**Find AES key** on the Extract tab (or `GameAssetHarvester.exe aeskey "<game folder>"`)
does it on demand and fills the AES field. If nothing in the exe decrypts the index, any
64-hex-digit string literals it did find are offered as guesses and brute force tries each
in turn; if there are none, the log says so plainly — the key is being fetched at runtime
or assembled from pieces, and you will need to find it another way.

### QuickBMS scripts

There is a `scripts\` folder beside the exe, working the same way as `mappings\`. Drop
any `.bms` scripts you collect (aluigi.altervista.org is the source) in there and brute
force will try them against anything it cannot otherwise open, closest name match first.
Nothing is bundled — the scripts are third-party and are not redistributed here.

### Mappings are found for you

There is a `mappings\` folder beside the exe. Anything you drop in there is matched to the
game by name automatically — collect a `.usmap` once and you never pick it again. The
game's own folder is searched too, which is where UE4SS writes its dump.

**Find mappings** (or `GameAssetHarvester.exe mappings "<game>" --download`) also searches
the public [Unreal-Mappings-Archive](https://github.com/TheNaeem/Unreal-Mappings-Archive)
and downloads a match into that folder. It covers roughly seventy games, so plenty of
titles are not in it — nothing is bundled with this app, it is fetched on request like the
backends.

For anything the archive does not have, dump your own: with UE4SS installed, open its GUI
console → Dumpers → *Generate .usmap file*, or call `DumpUSMAP()` from a Lua script. It
writes `Mappings.usmap` beside the game exe; move that into `mappings\<Game>\`.

### UE5 games need a .usmap

This is the one that looks like success. UE5 (and IoStore UE4.26+) stores object
properties **unversioned**, so without a mappings file CUE4Parse cannot deserialise a
single `UTexture2D` or `UStaticMesh`. It copies out the raw `.ubulk` payloads it can take
verbatim, converts nothing, and **exits 0**. You get a folder of `.ubulk` and no PNGs.

The scan flags this before you run — the results table says **"needs a .usmap"** — and
after a run that converted nothing the app says so outright rather than reporting success.

To get one: dump it with UE4SS in-game (its `DumpMappings` command), or find a community
`.usmap` for the title, then point the **Mappings .usmap** field at it.

### When a run goes wrong

The backends emit tens of thousands of near-identical lines — umodel alone prints a
`Loading package:` line and several `IntProperty: unknown ...` lines per asset. The log
collapses by the *shape* of the line rather than by a list of known messages, so paths,
numbers and per-asset class names are stripped out and the repeats land on one counter.
Eight of each shape are shown in full, then one reminder every 2000, and the run ends with
the top offenders and how many lines were hidden. A 10,000-line run reads as about 20.

If a backend aborts part-way — umodel stops the whole export on the first asset it cannot
parse, printing `*** ERROR: ...` — that is caught, quoted and explained, rather than
letting the run look like it simply finished. Whatever was written before the abort is
still usable.

At the end the app reads its own counters and prints a **WHAT WENT WRONG** block naming
the likely cause — wrong engine version, missing AES key, or missing `.usmap` mappings —
instead of just an exit code.

### What does *not* get a job

Two cases used to produce a run that could never work:

- A UE3 game with `.umap` map files also looked faintly like UE4, so a second CUE4Parse
  job appeared beside the umodel one and found nothing. `.umap` and `.u` are no longer
  treated as modern-Unreal evidence on their own — something genuinely UE4+ (`.pak`,
  `.uasset`, `.uexp`, `.ubulk`, `.utoc`, `.ucas`) has to be present too.
- A handful of loose `.dat`/`.bin` files is not an archive format. Those findings are now
  flagged **weak**: still reported in the scan so you can see what was there, but not
  queued, because QuickBMS cannot open them without a game-specific `.bms` script.
  **Extract everything** still tries them.

### Backend flags drift

Every backend changes its options between releases. The Backends tab has a **Check
real flags** button that runs the installed binary's own `--help` and shows it, so
you are reading the truth rather than what this README believed at the time. Anything
you add to **Extra arguments** is appended verbatim.

## Command line

```
GameAssetHarvester.exe                                    open the window
GameAssetHarvester.exe fetch-backends [--force] [name]    download the tools
GameAssetHarvester.exe fetch-backends --list              show what each release offers
GameAssetHarvester.exe fetch-backends <name> --asset RE   take a specific release asset
GameAssetHarvester.exe backends                           show what is installed
GameAssetHarvester.exe probe <name>                       print a backend's own --help
GameAssetHarvester.exe mappings <game> [--download]       find a .usmap for a game
GameAssetHarvester.exe aeskey <folder>                    find and verify the AES key
GameAssetHarvester.exe extract <folder> --brute           try everything, no options
GameAssetHarvester.exe selftest                           check the build
GameAssetHarvester.exe extract <source> [options]         run headless
```

`extract` options: `--brute`, `--out`, `--mesh glb|psk|ueformat|fbx|none` (CUE4Parse has
no FBX writer: `fbx` gives glTF 2 there),
`--texture png|tga|jpg|webp|none`, `--include`, `--game`, `--aes 0x...`,
`--mappings file.usmap`, `--bms script.bms`, `--organise`, `--dry-run`.

`--brute` also queues the findings that are too weak to run on their own, so it really
does try everything.

`--game` defaults to `auto` (use the detected version). `--dry-run` prints the detection
evidence and the exact commands without running anything — the quickest way to see what
version the scan thinks a game is.

`--dry-run` prints the commands and runs nothing. Use it before a long job.

## Where things are written

Everything lives beside the exe: `mappings\` (your .usmap library),
`scripts\` (your QuickBMS .bms library), `keys.json` and `keys\` (AES keys that worked, and
any you drop in),
`harvester-settings.json`, `harvester-log.txt`,
`harvester-cache.json`, `harvester-crash.log`, `harvester-selftest.txt`, and a
writable `backends\` folder.
That writable folder is searched before the baked-in copies, so you can update a
single backend without rebuilding.

## Legal

Extraction tools are for assets you already have a licence to. Ripping a game's
assets for redistribution is a different thing from looking at them to build a mod.
This app does not ship anyone else's binary; it fetches each one from its own
official release page, under that project's own licence (Apache-2.0, MIT and GPL-2.0
between them).

## Licence

MIT No Attribution (MIT-0): do whatever you like with it - no credit needed, no warranty. See `LICENSE`.
