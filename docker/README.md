# opdub

Adds a correctly-timed English dub audio track to fan-edited video episodes.

Fan edits often reassemble footage from multiple source episodes into a tighter
cut. This tool figures out exactly which moment of which source episode each
moment of the edit came from, then builds a new audio track from the English
dub files and muxes it into the video — without re-encoding anything.

---

## How it works

1. **Align** — fingerprints the Japanese audio in both the edit and all source
   episodes, then uses a Hough-transform accumulator to map every moment of the
   edit back to its source episode and timestamp. Outputs an EDL (edit decision
   list) JSON file.

2. **Render** — reads the EDL, extracts the corresponding English dub segments
   from the source files, and concatenates them into a single WAV file that
   matches the edit's runtime.

3. **Mux** — adds the rendered WAV as a new audio track to the video file
   without re-encoding the video or any existing audio tracks.

---

## Requirements

- Docker or Podman (recommended), **or** Python 3.11+ with ffmpeg installed
- Source files: the original episodes in both Japanese and English
- Edit files: the fan-edited episodes (Japanese audio only)

---

## File naming

The tool identifies whether a file is a Japanese source or an English dub by
looking at the filename suffix:

| Filename | Meaning |
|---|---|
| `Episode 313 Title.jpn.mp4` | Japanese audio — used for alignment |
| `Episode 313 Title.eng.mp4` | English dub — used for rendering |
| `Episode 313 Title.skip.mp4` | Excluded from everything |

Files without a language suffix fall back to reading the stream language tag
embedded in the file. If the tag is also missing or wrong, the tool uses
episode number matching (e.g. `Episode 313` in both filenames) to pair up
Japanese and English files automatically.

### Renaming your files

Run the included rename script once on your source directory before processing:

```bash
# Preview what will be renamed (no changes made)
python3 rename_sources.py /path/to/sources --dry-run

# Apply the renames
python3 rename_sources.py /path/to/sources
```

The script reads stream language tags from each file, matches Japanese/English
pairs by episode number, and renames them to the `.jpn.ext` / `.eng.ext`
convention. Files where both copies have the same (wrong) tag get a note in
the output — check those manually and rename if needed.

---

## Running with Docker / Podman

Build the image once:

```bash
cd /path/to/docker
sudo podman build -t opdub .
# or: sudo docker build -t opdub .
```

Then run commands by passing them to the container. Replace
`/path/to/your/media` with the directory that contains your edits and sources
folders.

```bash
MEDIA=/path/to/your/media

# Align
sudo podman run --rm -v "$MEDIA:/data" opdub align \
    "/data/edits/Series 20 Episode 01.mp4" \
    --sources /data/sources \
    --out /data/out/edls \
    -v

# Render
sudo podman run --rm -v "$MEDIA:/data" opdub render \
    "/data/out/edls/Series 20 Episode 01.json" \
    --sources /data/sources \
    --out /data/out/wav \
    -v

# Mux (plain ffmpeg — no Python needed)
sudo podman run --rm -v "$MEDIA:/data" --entrypoint ffmpeg opdub \
    -i "/data/edits/Series 20 Episode 01.mp4" \
    -i "/data/out/wav/Series 20 Episode 01.dub.wav" \
    -map 0 -map 1:a \
    -c copy -c:a:1 aac -b:a:1 192k \
    -metadata:s:a:1 language=eng \
    -metadata:s:a:1 "title=English Dub" \
    "/data/out/muxed/Series 20 Episode 01.mkv"
```

Outputs land in `/path/to/your/media/out/`.

---

## Running directly (no Docker)

Install dependencies:

```bash
pip install numpy scipy
# also requires ffmpeg in PATH: https://ffmpeg.org/download.html
```

Then run the same commands without the container prefix:

```bash
# Align
python3 -m opdub.cli align \
    "/path/to/edits/Series 20 Episode 01.mp4" \
    --sources /path/to/sources \
    --out out/edls \
    -v

# Render
python3 -m opdub.cli render \
    "out/edls/Series 20 Episode 01.json" \
    --sources /path/to/sources \
    --out out/wav \
    -v

# Mux
ffmpeg \
    -i "/path/to/edits/Series 20 Episode 01.mp4" \
    -i "out/wav/Series 20 Episode 01.dub.wav" \
    -map 0 -map 1:a \
    -c copy -c:a:1 aac -b:a:1 192k \
    -metadata:s:a:1 language=eng \
    -metadata:s:a:1 "title=English Dub" \
    "out/muxed/Series 20 Episode 01.mkv"
```

---

## Command reference

### `align`

```
python3 -m opdub.cli align <edit> --sources <dir> --out <dir> [options]
```

| Argument | Description |
|---|---|
| `edit` | Path to the fan-edited episode (must have Japanese audio) |
| `--sources` | Directory containing the original source episodes |
| `--out` | Directory to write the EDL JSON file |
| `--dub-lang` | Language tag of the dub track (default: `eng`) |
| `--source-files` | Only fingerprint these specific filenames within `--sources` (space-separated). Speeds up processing when you know which episodes are used. |
| `-v` | Verbose output showing each stage's progress |

Writes `<out>/<edit-stem>.json`.

### `render`

```
python3 -m opdub.cli render <edl> --sources <dir> --out <dir> [options]
```

| Argument | Description |
|---|---|
| `edl` | Path to the EDL JSON produced by `align` |
| `--sources` | Same source directory used during `align` |
| `--out` | Directory to write the `.dub.wav` file |
| `--dub-lang` | Language tag of the dub track (default: `eng`) |
| `-v` | Verbose output |

Writes `<out>/<edit-stem>.dub.wav`.

---

## Tips

**Speed up alignment with `--source-files`**

Each fan edit typically draws from only 2–4 source episodes. If you know which
ones, pass only those filenames to `--source-files`. This skips fingerprinting
the irrelevant episodes and cuts align time significantly.

```bash
python3 -m opdub.cli align \
    "/path/to/edits/Series 20 Episode 01.mp4" \
    --sources /path/to/sources \
    --source-files \
        "Episode 313 Title.jpn.mp4" \
        "Episode 313 Title.eng.mp4" \
        "Episode 314 Title.jpn.mp4" \
        "Episode 314 Title.eng.mp4" \
    --out out/edls -v
```

**Processing multiple episodes**

Run align + render + mux for each episode in sequence. The align step is the
slow one (2–5 minutes per episode depending on how many sources are indexed);
render and mux are fast.

**Checking the EDL**

The EDL JSON is human-readable. Each entry in `segments` looks like:

```json
{
  "t0": 142.3,
  "t1": 198.7,
  "src": 1,
  "src_t0": 834.1,
  "delta": 691.8,
  "conf": 18240.0,
  "status": "ok"
}
```

- `t0` / `t1` — start/end time in the edit (seconds)
- `src` — index into the `sources` array (which original episode)
- `src_t0` — start time in the source episode (seconds)
- `conf` — alignment confidence (higher is better; anything above ~1000 is solid)

If a segment has low confidence or looks wrong you can edit the JSON by hand
and re-run `render` without re-running `align`.

---

## Directory layout

```
docker/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── rename_sources.py      # run once on your source directory
├── README.md
└── opdub/
    ├── cli.py             # align and render commands
    ├── align.py           # fingerprint alignment pipeline
    ├── fingerprint.py     # audio fingerprinting (constellation hashing)
    ├── render.py          # dub audio extraction and rendering
    └── media.py           # ffmpeg/ffprobe wrappers
```
