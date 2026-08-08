# opdub

Adds a correctly-timed English dub audio track to fan-edited video episodes.

Fan edits reassemble footage from multiple source episodes into a tighter cut.
`opdub` figures out which moment of which source episode each moment of the
edit came from, stitches the corresponding dub segments together, and muxes
the result into the video — without re-encoding anything.

## How it works

1. **align** — fingerprints the Japanese audio in the edit and all source
   episodes, then uses a Hough-transform accumulator to map every moment of
   the edit back to its source and timestamp. Writes an EDL (edit decision
   list) JSON file.

2. **render** — reads the EDL, extracts the English dub segments from the
   source files, and concatenates them into a `.dub.wav` that matches the
   edit's runtime.

3. **mux** — plain `ffmpeg` call to add the WAV as a new audio track without
   touching the video or existing audio.

## Requirements

- Python 3.11+
- `numpy`, `scipy` — `pip install -r requirements.txt`
- `ffmpeg` in your PATH

## Quickstart

```bash
pip install -r requirements.txt

# 1. Align — writes out/edls/<edit-stem>.json
python3 -m opdub.cli align \
    "/path/to/edits/Episode 01.mp4" \
    --sources /path/to/sources \
    --out out/edls \
    -v

# 2. Render — writes out/wav/<edit-stem>.dub.wav
python3 -m opdub.cli render \
    "out/edls/Episode 01.json" \
    --sources /path/to/sources \
    --out out/wav \
    -v

# 3. Mux — copy-mux, no re-encode
ffmpeg \
    -i "/path/to/edits/Episode 01.mp4" \
    -i "out/wav/Episode 01.dub.wav" \
    -map 0 -map 1:a \
    -c copy \
    -metadata:s:a:1 language=eng \
    -metadata:s:a:1 title="English Dub" \
    "out/muxed/Episode 01.mkv"
```

## File layout

```
sources/
├── Episode 313 Title.jpn.mp4   ← Japanese audio (used for alignment)
├── Episode 313 Title.eng.mp4   ← English dub   (used for rendering)
├── Episode 314 Title.jpn.mp4
└── ...

edits/
└── Episode 01.mp4              ← fan edit (Japanese audio only)
```

The tool detects language from filename suffix (`.jpn.`, `.eng.`, `.skip.`)
with a fallback to the stream language tag embedded in the file. Files without
either get matched by episode number.

## `align` reference

```
python3 -m opdub.cli align <edit> --sources <dir> --out <dir> [options]
```

| Flag | Default | Description |
|---|---|---|
| `--sources` | required | Directory of source episodes |
| `--out` | required | Directory to write the EDL JSON |
| `--dub-lang` | `eng` | Language tag for the dub stream |
| `--source-files` | all | Fingerprint only these filenames (speeds up alignment when you know which episodes the edit draws from) |
| `-v` | off | Verbose stage-by-stage output |

Writes `<out>/<edit-stem>.json`.

## `render` reference

```
python3 -m opdub.cli render <edl> --sources <dir> --out <dir> [options]
```

| Flag | Default | Description |
|---|---|---|
| `--sources` | required | Same directory used during `align` |
| `--out` | required | Directory to write the `.dub.wav` |
| `--dub-lang` | `eng` | Language tag for the dub stream |
| `-v` | off | Verbose output |

Writes `<out>/<edit-stem>.dub.wav`.

## Tips

**Speed up alignment with `--source-files`**

Each fan edit typically draws from only 2–4 source episodes. Passing just
those filenames skips fingerprinting the rest and cuts align time by 3–5×.

```bash
python3 -m opdub.cli align \
    "/path/to/edits/Episode 01.mp4" \
    --sources /path/to/sources \
    --source-files \
        "Episode 313 Title.jpn.mp4" \
        "Episode 313 Title.eng.mp4" \
        "Episode 314 Title.jpn.mp4" \
        "Episode 314 Title.eng.mp4" \
    --out out/edls -v
```

**Batch processing multiple episodes**

```bash
for ep in /path/to/edits/*.mp4; do
    stem=$(basename "$ep" .mp4)
    python3 -m opdub.cli align "$ep" --sources /path/to/sources --out out/edls -v
    python3 -m opdub.cli render "out/edls/${stem}.json" --sources /path/to/sources --out out/wav -v
    ffmpeg -y -i "$ep" -i "out/wav/${stem}.dub.wav" \
        -map 0 -map 1:a -c copy \
        -metadata:s:a:1 language=eng -metadata:s:a:1 title="English Dub" \
        "out/muxed/${stem}.mkv"
done
```

**Reading the EDL**

The JSON is human-readable and can be hand-edited before rendering:

```json
{
  "t0": 142.3,
  "t1": 198.7,
  "src": 1,
  "src_t0": 834.1,
  "conf": 18240.0,
  "status": "ok"
}
```

- `t0` / `t1` — segment start/end in the edit (seconds)
- `src` — index into the `sources` array in the EDL header
- `src_t0` — start time in the source episode (seconds)
- `conf` — alignment confidence; values above ~1000 are solid

Low-confidence segments or obvious errors can be corrected by editing the JSON
and re-running `render` — no need to re-run `align`.
