# opdub

Adds a correctly-timed English dub audio track to fan-edited video episodes.

Fan edits reassemble footage from multiple source episodes into a tighter cut.
`opdub` figures out which moment of which source episode each moment of the
edit came from, stitches the corresponding dub segments together, and muxes
the result into the video — without re-encoding anything.

## Contents

- [Install](#install)
  - [A. Install script](#a-install-script) — recommended, no root needed
  - [B. Docker](#b-docker) — media mounted read-only
  - [C. Manual install](#c-manual-install) — you already have Python 3.11+ and ffmpeg
  - [D. No root and no system Python](#d-no-root-and-no-system-python) — what the script does, by hand
  - [Uninstalling](#uninstalling)
- [Web UI](#web-ui)
- [How it works](#how-it-works)
- [CLI](#cli)
  - [run](#run) — align from a plan
  - [align](#align) — align by auto-detection
  - [render](#render) — EDL to WAV
  - [mux](#mux) — WAV into the video
- [The EDL](#the-edl)
- [File layout](#file-layout)
- [Tips](#tips)

## Install

Requirements are Python 3.11+, `numpy`/`scipy`, and `ffmpeg` on your PATH.
Pick whichever path below matches your machine.

| Path | Use it when | Root needed |
|---|---|---|
| [A. Install script](#a-install-script) | Most cases. Installs only what is missing. | no |
| [B. Docker](#b-docker) | You want isolation and read-only media by construction. | docker only |
| [C. Manual install](#c-manual-install) | You already have Python 3.11+ and ffmpeg. | no |
| [D. No root and no system Python](#d-no-root-and-no-system-python) | Locked-down box; you want to see every step. | no |

### A. Install script

```bash
./install.sh            # installs only what is missing
./install.sh --verify   # ...then runs the tests and the fixture scorer
```

It checks for ffmpeg and a usable Python, installs whichever is absent, and
creates a virtualenv at `.venv`. Nothing goes outside your home directory and
root is never required.

| Option | Meaning |
|---|---|
| `--prefix DIR` | Where binaries go (default `~/.local`) |
| `--venv DIR` | Where the virtualenv goes (default `./.venv`) |
| `--skip-ffmpeg` / `--skip-python` | Leave that component alone |
| `--no-dev` | Skip `pytest` |
| `--force` | Reinstall components that are already present |
| `--verify` | Run the test suite and score the fixtures afterwards |
| `--dry-run` | Print the plan, change nothing |

Everything it installs is recorded in `.opdub-install.manifest`, which is what
makes the uninstall exact. If ffmpeg or Python were already on the machine,
they are not recorded and never touched.

On macOS the script will not download binaries — install ffmpeg with
`brew install ffmpeg` first, then re-run it.

### B. Docker

Two volumes, both inside the project folder — one in, one out:

```
input/     → /input  (read-only)    your media
out/       → /out    (writable)     everything opdub produces
```

Put your media anywhere under `input/`; nest it however you like, because the
UI discovers folders that contain media rather than assuming a layout. A
starting shape is `input/edits/` and `input/sources/`, and both folders ship
empty in the repo.

```bash
docker compose up --build
```

The UI is on <http://localhost:8000>.

`input/` is mounted `:ro`. That read-only flag — not a convention the code is
trusted to follow — is what guarantees source episodes are never modified.

If your media cannot live in the project folder, point the mounts elsewhere
with a `.env` file next to `docker-compose.yml`:

```ini
OPDUB_INPUT=/mnt/media/onepiece
OPDUB_OUTPUT=/mnt/big-disk/opdub-out
```

### C. Manual install

If you already have Python 3.11+ and ffmpeg:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn opdub.server:app --port 8000
```

### D. No root and no system Python

This is exactly what `install.sh` automates, written out so you can see it:

```bash
# 1. a Python that does not exist on the system yet
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv venv --python 3.11 .venv
uv pip install --python .venv -r requirements.txt

# 2. a static ffmpeg, no package manager involved
curl -sS -L -o ff.tar.xz \
  https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz
tar xf ff.tar.xz
cp ffmpeg-*/bin/ffmpeg ffmpeg-*/bin/ffprobe "$HOME/.local/bin/"
```

Use `linuxarm64` instead of `linux64` on ARM.

### Uninstalling

```bash
./uninstall.sh              # remove what install.sh added, keep out/
./uninstall.sh --dry-run    # list it first
./uninstall.sh --purge-output   # also delete out/ (asks first)
```

It reads `.opdub-install.manifest` and removes only what is listed there. An
ffmpeg or Python that predates the install is left alone.

The manifest is treated as untrusted input: a virtualenv is only deleted if it
actually contains a `pyvenv.cfg`, and a binary only if it is named `ffmpeg`,
`ffprobe`, `uv` or `uvx`. **Source media is never deleted under any flag** —
without that check, a media directory sitting inside the repo root would look
like a legitimate target.

`out/` — your EDLs, renders and saved setups — is kept unless you pass
`--purge-output`, and even then it asks.

## Web UI

```bash
OPDUB_MEDIA=/workspace/onepace:/workspace/onepiece \
OPDUB_OUT=/workspace/out \
    .venv/bin/uvicorn opdub.server:app --port 8000
```

Then open <http://localhost:8000>.

The UI exists to take the guesswork out of the run. Everything the pipeline
used to infer — which file holds Japanese audio, which audio stream is the
dub, which two files are the same episode, which sources an edit was cut
from, where the opening theme sits — is an explicit input you make, with a
**▶ listen** button next to each one so you can check by ear before running
anything.

| Step | What you decide |
|---|---|
| 1 · Source episodes | Pair each original episode's Japanese file+stream with its dub file+stream by hand. Measure the jpn→dub offset, or type one. |
| 2 · Edits | Confirm the edit's Japanese stream, tick the source episodes it uses, and drag across the waveform to mark the opening theme. |
| 3 · Run | Check the plans, then queue the edits. They run one at a time with live progress. |
| 4 · Review & fix | Timeline of the EDL coloured by source, flagged low-confidence cuts, nudge a cut, hear the seam in both languages, re-render without re-aligning. |

**Passthrough ranges.** The opening theme is identical across every episode of
an arc, so alignment there is a coin flip. Mark it in step 2 and that range
keeps the edit's own audio: no matching is attempted and nothing is replaced.

**Nothing is auto-detected.** Source episodes are never guessed. A missing
source shows up as an obvious silent gap rather than a plausible wrong match.

| Variable | Default | Meaning |
|---|---|---|
| `OPDUB_MEDIA` | `/workspace/onepace:/workspace/onepiece` | Colon-separated read-only media roots |
| `OPDUB_OUT` | `/workspace/out` | The only writable directory |

## How it works

1. **align** — fingerprints the Japanese audio in the edit and all source
   episodes, then uses a Hough-transform accumulator to map every moment of
   the edit back to its source and timestamp. Writes an EDL (edit decision
   list) JSON file.

2. **render** — reads the EDL, extracts the English dub segments from the
   source files, and concatenates them into a `.dub.wav` that matches the
   edit's runtime.

3. **mux** — adds the WAV as a new audio track without touching the video or
   the existing audio.

## CLI

```bash
.venv/bin/python -m opdub.cli --help
```

### run

Align from a plan. This is what the UI drives. A plan states every ambiguous
choice outright, so nothing is inferred from filenames or language tags:

```json
{
  "edit":   {"path": "/media/edits/Episode 01.mp4", "audio_stream": 1},
  "sources": [
    {"label": "Episode 313",
     "jpn": {"path": "/media/sources/Ep313 (1).mp4", "audio_stream": 1},
     "dub": {"path": "/media/sources/Ep313.mp4",     "audio_stream": 1},
     "dub_offset": 0.0732}
  ],
  "passthrough": [{"t0": 0.0, "t1": 89.5, "label": "intro"}]
}
```

```bash
python3 -m opdub.cli run plan.json --out out/edls -v
```

`dub_offset` is optional — omit it and it is measured, and the measurement is
reported with its quality rather than silently accepted. `passthrough` ranges
are carved out of the segment list after alignment and keep the edit's own
audio at render time.

### align

The older auto-detecting path. Infers Japanese/dub streams and pairings from
filename suffixes and stream tags; use `run` when you want certainty.

```bash
python3 -m opdub.cli align <edit> --sources <dir> --out <dir> [options]
```

| Flag | Default | Description |
|---|---|---|
| `--sources` | required | Directory of source episodes |
| `--out` | required | Directory to write the EDL JSON |
| `--dub-lang` | `eng` | Language tag for the dub stream |
| `--source-files` | all | Fingerprint only these filenames |
| `-v` | off | Verbose stage-by-stage output |

Writes `<out>/<edit-stem>.json`.

### render

```bash
python3 -m opdub.cli render <edl> --out <dir> [options]
```

| Flag | Default | Description |
|---|---|---|
| `--sources` | optional | Only needed for EDLs without absolute source paths |
| `--out` | required | Directory to write the `.dub.wav` |
| `--dub-lang` | `eng` | Language tag for the dub stream |
| `--crossfade` | `0` | Equal-power crossfade at segment boundaries, seconds |
| `-v` | off | Verbose output |

Writes `<out>/<edit-stem>.dub.wav`.

### mux

```bash
python3 -m opdub.cli mux <edit> <dub.wav> <out.mkv> \
    [--lang eng] [--title "English Dub"] [--default]
```

Copy-mux only: `-map 0 -c copy`, so video, existing audio, subtitles, chapters
and attachments all survive untouched.

## The EDL

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
- `conf` — vote density, not a probability. Real segments run from a couple of
  hundred to tens of thousands; below ~1000 is worth a look
- `status` — `ok`, `gap`, `gap-filled`, or `passthrough`

Low-confidence segments or obvious errors can be corrected by editing the JSON
and re-running `render` — no need to re-run the alignment.

## File layout

One input tree and one output tree, both in the project folder:

```
input/                          ← mounted read-only
├── edits/
│   └── Episode 01.mp4          ← fan edit (Japanese audio only)
└── sources/
    ├── Episode 313 Title.jpn.mp4   ← Japanese audio (alignment)
    ├── Episode 313 Title.eng.mp4   ← English dub   (rendering)
    └── ...

out/                            ← the only writable location
├── edls/      EDL JSON per edit
├── wav/       rendered dub tracks
├── muxed/     finished episodes
├── plans/     the plan each run was built from
├── projects/  saved UI setups
└── cache/     waveform peaks
```

The nesting is up to you — `input/arc-11/`, `input/whisky-peak/sources/` and so
on all work, because the UI lists folders that contain media instead of
expecting fixed names.

The `.jpn.` / `.eng.` / `.skip.` suffixes are only used by the `align` command,
which falls back to the stream language tag and then to episode number. The
web UI and `run` ignore filenames entirely — you name the file and the stream.

## Tips

**Pick only the sources an edit actually uses.** Each fan edit typically draws
from 2–4 originals. Fingerprinting only those cuts align time by 3–5×. In the
UI this is step 2; on the CLI it is `--source-files` (for `align`) or simply
the `sources` list in your plan.

**Batch a whole arc.** In the UI, tick several edits and press run — they queue
and execute one at a time. On the CLI:

```bash
for ep in /path/to/edits/*.mp4; do
    stem=$(basename "$ep" .mp4)
    python3 -m opdub.cli align "$ep" --sources /path/to/sources --out out/edls -v
    python3 -m opdub.cli render "out/edls/${stem}.json" --out out/wav -v
    python3 -m opdub.cli mux "$ep" "out/wav/${stem}.dub.wav" "out/muxed/${stem}.mkv"
done
```

**Soften the passthrough seam.** Where the opening theme hands over to the dub,
`--crossfade 0.02` (or the crossfade box in the UI) applies a 20 ms equal-power
fade. The default is 0, which is a hard cut.

## Development

```bash
./install.sh --verify              # install, run tests, score the fixtures
.venv/bin/python -m pytest tests -q
.venv/bin/python score.py fixtures/ground_truth.json --edl-dir out/edls
```

`score.py` writes `metrics.json` and fails if any gate regresses. Regenerate
the fixtures with `python make_fixtures.py fixtures --sources 6 --edits 3`.
