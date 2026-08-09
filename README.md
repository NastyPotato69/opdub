# opdub

Adds a correctly-timed dub audio track to fan-edited video episodes.

A fan edit reassembles footage from several source episodes into a tighter cut,
and often ships with the original-language audio only. `opdub` works out which
moment of which source episode every moment of the edit came from, stitches the
matching dub segments together, and muxes the result into the video — without
re-encoding anything.

**What you need to supply:** the edit, and the source episodes it was cut from,
with a dub track available for those sources. Everything else is derived.

![The review tab, showing an edit's timeline broken into segments and coloured
by which source episode each one came from](docs/img/05-review.png)

---

## Contents

**Tutorial** — start here, in order

1. [Get the code](#step-1--get-the-code)
2. [Put your media in `input/`](#step-2--put-your-media-in-input)
3. [Start it with Docker](#step-3--start-it-with-docker)
4. [Set up your source library](#step-4--set-up-your-source-library)
5. [Describe each edit](#step-5--describe-each-edit)
6. [Run it](#step-6--run-it)
7. [Review and fix](#step-7--review-and-fix)

**Reference**

- [Other ways to install](#other-ways-to-install) — no Docker, or you want it on the host
- [How it works](#how-it-works)
- [Command line](#command-line)
- [The EDL format](#the-edl-format)
- [Configuration](#configuration)
- [File layout](#file-layout)
- [Troubleshooting](#troubleshooting)
- [Uninstalling](#uninstalling)
- [Development](#development)

---

# Tutorial

**Docker is the recommended way to run this**, and the tutorial assumes it.
It is the configuration that gets tested, it needs nothing on your machine but
Docker itself, and it mounts your media read-only so the tool physically cannot
modify your originals. If you would rather run it on the host, see
[Other ways to install](#other-ways-to-install) and then rejoin at
[step 2](#step-2--put-your-media-in-input).

## Step 1 · Get the code

You need [Docker](https://docs.docker.com/get-docker/) with the Compose plugin.
Check both:

```bash
docker --version
docker compose version
```

Then clone the repo and go into it. **Every command from here on is run from
inside this folder.**

```bash
git clone https://github.com/NastyPotato69/opdub.git
cd opdub
```

The clone already contains the two folders the container mounts, so there is
nothing to create:

```
opdub/
├── input/          your media — mounted read-only
│   ├── edits/      ← already here
│   └── sources/    ← already here
└── out/            everything opdub produces — already here
```

Confirm it, if you like:

```bash
ls -d input/edits input/sources out
```

If any are missing — an old clone, or a zip download that dropped empty
directories — just make them, they carry no content:

```bash
mkdir -p input/edits input/sources out
```

---

## Step 2 · Put your media in `input/`

Copy your files in. Sources are the original episodes; edits are the fan re-cuts
you want dubbed.

```bash
cp /path/to/your/edits/*.mkv     input/edits/
cp /path/to/your/originals/*.mkv input/sources/
```

**Your sources need a dub somewhere.** Either as separate files per language, or
as one file with several audio tracks. Both work, and you pick which in
[step 4](#step-4--set-up-your-source-library).

**The layout is a suggestion, not a requirement.** Nest it however you like —
`input/season-2/`, `input/arc-11/sources/` — because the app lists any folder
that contains media rather than expecting fixed names.

**Copying hundreds of gigabytes is optional** — but *not* with a symlink.
A link inside `input/` pointing at your library is rejected: every requested
path is resolved before it is checked, so the link resolves to its real
location, which is outside the configured roots. That is the same check that
keeps the tool away from the rest of your disk, and it applies whether or not
you use Docker.

Point the mount itself at your library instead, with a `.env` file next to
`docker-compose.yml`:

```ini
OPDUB_INPUT=/mnt/media/my-show
OPDUB_OUTPUT=/mnt/big-disk/opdub-out
```

Your library then appears as the media root, so arrange edits and sources in
subfolders under it.

---

## Step 3 · Start it with Docker

```bash
docker compose up --build
```

The first run builds the image — a few minutes, mostly installing ffmpeg. You
are ready when the log shows:

```
Uvicorn running on http://0.0.0.0:8000
```

**Open <http://localhost:8000>.**

To run it in the background instead, add `-d`, and use `docker compose logs -f`
to watch it and `docker compose down` to stop it.

What the compose file sets up:

| Host | Container | Mode |
|---|---|---|
| `./input` | `/input` | **read-only** |
| `./out` | `/out` | writable |

That `:ro` flag is the enforcement mechanism for "never modify the originals" —
not a convention the code is trusted to follow.

> **Upgrading later.** The frontend is baked into the image
> (`COPY opdub ./opdub`), so `git pull` alone changes nothing you can see.
> Always rebuild: `git pull && docker compose up -d --build`. See
> [Troubleshooting](#the-ui-did-not-change-after-an-upgrade).

You should see four numbered tabs. They are meant to be worked through in
order, and the badge on each shows how many items are ready.

**The guiding idea:** the app never quietly decides anything. Which file holds
which language, which audio stream is the dub, which two files are the same
episode, which sources an edit was cut from, where the opening theme sits —
each is something you state, with a **▶** button next to it so you can confirm
by ear first. A wrong guess here surfaces as dialogue over the wrong scene
twenty minutes into a render, which is why nothing is guessed.

---

## Step 4 · Set up your source library

**Tab 1 · Source episodes.** The goal is a list of episodes where each one
knows its original-language track (used to find where things are) and its dub
track (used to build the new audio).

**First, tell it how your library is laid out** using the switch near the top:

| Your layout | What you do |
|---|---|
| **Separate files per language** | Every file is listed on the left. Drag one into the dub slot and one into the original-language slot of an episode row — or click a file, then click a slot. |
| **One file, multiple tracks** | Each file's audio tracks are listed. Mark one as the original language and one as the dub. |

![The source episodes tab: the file pool on the left, paired episode rows on
the right, and a live count of what is ready](docs/img/01-sources.png)

**Then press ✨ Assign automatically.** It reads stream language tags and
titles first, then filenames, and understands the usual episode conventions —
`Episode 313`, `S21E1071`, `Show - 1071 [1080p]`, `Ep.313`, `#313`, `E313`, and
a bare trailing number. Years and resolutions are never mistaken for episode
numbers, so `Some Show 1999 Episode 313` matches on 313.

It declines rather than guesses. Two files for the same episode with no tags
and no language words in their names are left alone, because which is which is
genuinely unknowable. Every skipped file is listed with the reason.

**Check its work.** The status line counts episodes ready, rows still
incomplete, and files not yet assigned — it updates as you drag, so it is a
live tally rather than a report. Press **▶** on any file or track to hear it.
The bar at the bottom shows what is playing; for multi-track files it offers a
switcher that jumps to another track *at the same moment*, which is the fastest
way to confirm two tracks are the same scene in different languages. The scrub
bar spans the whole track, so if playback lands in silence you can drag
somewhere with dialogue.

**Finally, measure the offset.** Separate dub files are often not aligned with
their original-language counterpart. Press **Measure** on each row and it
reports the offset together with a quality score — a matched pair scores well
above 2. You can also type a value. If you skip this, 0 ms is used.

---

## Step 5 · Describe each edit

**Tab 2 · Edits.** Every edit in the folder starts ticked; untick any you do
not want to process. For each one:

1. **Pick its audio stream.** Listen and confirm — many edits have a single
   untagged stream, and it still deserves a check.
2. **Tick the source episodes it was cut from.** Nothing is detected for you.
   Ticking only the 2–4 originals an edit actually uses cuts alignment time by
   3–5×, and a missing source shows up as an obvious silent gap rather than a
   plausible wrong match.
3. **Mark the opening theme.** Press **Load waveform**, then drag across the
   theme and press **Mark as passthrough**. Shift+scroll zooms the waveform
   about the pointer and alt+drag pans it, so you can set the boundary
   precisely; **Fit** returns to the whole file.

![The edits tab: stream chooser, source episode tickboxes, and the waveform
with the intro marked as a passthrough range](docs/img/02-edits.png)

**Why the theme needs marking:** it is identical across every episode of an
arc, so alignment there is a coin flip. A passthrough range keeps the edit's
own audio for that stretch — nothing is matched and nothing is replaced.

Each edit shows a status pill: `ready`, `N warning` (non-blocking — hover to
read them), or `N to fix` (blocking; the run will refuse).

---

## Step 6 · Run it

**Tab 3 · Run.**

1. Press **Check plans**. This validates every file and stream without doing
   any work, and lists anything blocking.
2. Set the options if you need to:

   | Option | Default | Meaning |
   |---|---|---|
   | Mux into MKV when done | on | Also produce a finished `.mkv`, not just the WAV |
   | Crossfade at seams | `0.02` | Equal-power fade at segment boundaries, seconds. `0` is a hard cut |
   | Dub track language tag | `eng` | Language tag written into the new track |

3. Press **Run selected edits**. Jobs queue and run one at a time — alignment
   is CPU-bound — with live progress and a log you can watch.

Each finished job offers its outputs for download, and writes them under
`out/`. Expect minutes per edit, mostly fingerprinting.

![The run tab: a finished job with its progress bar, download links for the
EDL, WAV and MKV, and the stage-by-stage log](docs/img/04-run-done.png)

---

## Step 7 · Review and fix

**Tab 4 · Review & fix.** The EDL is the real product; rendering is just a
consequence of it, and some cuts will always want a nudge.

Pick an EDL and you get a timeline coloured by source, with low-confidence cuts
flagged in red. Click a segment to inspect it, then:

- **Hear the seam** in both languages — the same scene should be playing.
- **Nudge the cut** by ±10/100/500 ms, or type an exact time. Moving a cut
  moves the next segment's source time with it, so its content stays put.
- **Save & re-render** — alignment does *not* run again, so this is quick.

![The review tab: the EDL timeline coloured by source with the passthrough in
grey, the segment table, and the inspector's nudge controls](docs/img/05-review.png)

---

# Reference

## Other ways to install

Docker is the tested path and the tutorial above assumes it. These run opdub
directly on the host instead. All of them need the repo cloned first, and
`input/` and `out/` in it exactly as
[step 1](#step-1--get-the-code) describes — the folders and the workflow do not
change, only how the server gets started.

Running on the host, you supply what the container would have: **Python 3.11+**,
`numpy`/`scipy`, and **`ffmpeg`/`ffprobe` on your `PATH`**. Nothing enforces the
read-only rule any more — that came from the `:ro` mount — so the media roots
are protected only by the server's own path checks.

### A. Install script

Installs whatever is missing, nothing that is not:

```bash
git clone https://github.com/NastyPotato69/opdub.git
cd opdub
./install.sh
```

It checks for ffmpeg and a usable Python, fetches whichever is absent, and
creates a virtualenv at `.venv` holding numpy, scipy, fastapi and uvicorn —
fastapi and uvicorn being the web UI's server. The frontend itself needs no
installing: it is plain HTML and JavaScript served from `opdub/static/`, with
no build step and no npm.

Nothing is written outside your home directory and root is never required.

Then start it — the script prints this line for you when it finishes:

```bash
OPDUB_MEDIA=./input OPDUB_OUT=./out \
    .venv/bin/uvicorn opdub.server:app --port 8000
```

and open <http://localhost:8000>. Those are the defaults, so plain
`.venv/bin/uvicorn opdub.server:app --port 8000` from the project folder does
the same thing. The server runs in the foreground; closing the terminal stops
it. Continue from [step 2](#step-2--put-your-media-in-input).

| Option | Meaning |
|---|---|
| `--prefix DIR` | Where binaries go (default `~/.local`) |
| `--venv DIR` | Where the virtualenv goes (default `./.venv`) |
| `--skip-ffmpeg` / `--skip-python` | Leave that component alone |
| `--no-dev` | Skip `pytest` |
| `--force` | Reinstall components that are already present |
| `--verify` | Run the tests and score the fixtures afterwards |
| `--dry-run` | Print the plan, change nothing |

Everything it installs is recorded in `.opdub-install.manifest`, which is what
makes [uninstalling](#uninstalling) exact. An ffmpeg or Python that was already
on the machine is not recorded and never touched.

> **macOS:** the script will not download binaries. Run `brew install ffmpeg`
> first, then re-run it.

### B. Manual

If you already have Python 3.11+ and ffmpeg:

```bash
git clone https://github.com/NastyPotato69/opdub.git
cd opdub
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn opdub.server:app --port 8000
```

### C. By hand, no root

Exactly what `install.sh` automates, written out so you can see it:

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

# 3. start it
.venv/bin/uvicorn opdub.server:app --port 8000
```

Use `linuxarm64` instead of `linux64` on ARM.

## How it works

The core method is aligning **same-language audio to same-language audio**. The
edit's original-language track and the source episode's original-language track
contain literally the same recording, so they correlate strongly. Matching a
dub against an original would not.

1. **align** — fingerprints the original-language audio of the edit and of each
   ticked source, then uses a Hough-transform accumulator to map every moment
   of the edit back to a source and a timestamp. The mapping is piecewise
   constant in its offset; recovering that segment list is the whole problem.
   Writes an EDL (edit decision list) JSON file.

2. **render** — reads the EDL, extracts the corresponding dub segments from the
   source files, and concatenates them into a `.dub.wav` matching the edit's
   runtime.

3. **mux** — adds that WAV as a new audio track. Copy-mux only, so video,
   existing audio, subtitles, chapters and attachments all survive untouched.

Because the EDL sits between alignment and rendering, you can correct a cut and
re-render in seconds without re-aligning.

## Command line

The UI drives the same code. Everything below is also available directly:

```bash
.venv/bin/python -m opdub.cli --help
```

### `run` — align from a plan

What the UI uses. A plan states every ambiguous choice outright, so nothing is
inferred from filenames or language tags:

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

The `jpn` key names the original-language track used for alignment; `dub` names
the track the new audio is built from. `dub_offset` is optional — omit it and
it is measured, with the measurement reported alongside its quality rather than
silently accepted. `passthrough` ranges are carved out of the segment list
after alignment and keep the edit's own audio at render time.

### `align` — align by auto-detection

The older path. Infers language streams and pairings from filename suffixes and
stream tags; prefer `run` when you want certainty.

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

### `render` — EDL to WAV

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

### `mux` — WAV into the video

```bash
python3 -m opdub.cli mux <edit> <dub.wav> <out.mkv> \
    [--lang eng] [--title "English Dub"] [--default]
```

Copy-mux only: `-map 0 -c copy`.

### Batching a whole arc

```bash
for ep in input/edits/*.mkv; do
    stem=$(basename "$ep" .mkv)
    python3 -m opdub.cli align "$ep" --sources input/sources --out out/edls -v
    python3 -m opdub.cli render "out/edls/${stem}.json" --out out/wav -v
    python3 -m opdub.cli mux "$ep" "out/wav/${stem}.dub.wav" "out/muxed/${stem}.mkv"
done
```

In the UI, tick several edits and press run — same thing, with progress.

## The EDL format

Human-readable JSON, editable by hand before rendering. One segment looks like:

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

| Field | Meaning |
|---|---|
| `t0` / `t1` | Segment start/end in the edit, seconds |
| `src` | Index into the `sources` array in the EDL header |
| `src_t0` | Start time in the source episode, seconds |
| `conf` | Vote density, **not** a probability. Real segments run from a couple of hundred to tens of thousands; below ~1000 is worth a look |
| `status` | `ok`, `gap`, `gap-filled`, or `passthrough` |

Fix a bad segment by editing the JSON and re-running `render` — alignment does
not need to run again.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `OPDUB_MEDIA` | `./input` | Colon-separated read-only media roots |
| `OPDUB_OUT` | `./out` | The only writable directory |

Both default to paths relative to where you start the server, which is why the
tutorial runs it from the project folder. To read media from elsewhere:

```bash
OPDUB_MEDIA=/mnt/media/edits:/mnt/media/originals \
OPDUB_OUT=/mnt/big-disk/opdub-out \
    .venv/bin/uvicorn opdub.server:app --port 8000
```

Media roots are treated as strictly read-only: every write path is checked
against `OPDUB_OUT` and nothing else.

## File layout

```
input/                          ← read-only
├── edits/
│   └── Episode 01.mp4          ← the fan edit
└── sources/
    ├── Episode 313.jpn.mp4     ← original language (alignment)
    ├── Episode 313.eng.mp4     ← dub               (rendering)
    └── ...

out/                            ← the only writable location
├── edls/      EDL JSON per edit
├── wav/       rendered dub tracks
├── muxed/     finished episodes
├── plans/     the plan each run was built from
├── projects/  saved UI setups
└── cache/     waveform peaks
```

Those `.jpn.` / `.eng.` suffixes are only read by the `align` command, which
falls back to the stream language tag and then to the episode number. It
recognises `.jpn`/`.jap` as the original language, `.eng`/`.dub` as the dub,
and `.skip` to exclude a file entirely. The web UI and `run` ignore filenames
completely — you name the file and the stream.

## Troubleshooting

### The UI did not change after an upgrade

Two causes, and the header tells you which.

The build marker in the header — `ui b87aad12` — is hashed from the frontend
files the server is serving.

- **The marker did not change after `git pull`.** The Docker image was not
  rebuilt; the frontend is baked into it. Run
  `git pull && docker compose up -d --build`.
- **The marker is red and says `stale`.** The page in front of you is a cached
  copy older than the server's. Reload with <kbd>ctrl+shift+R</kbd>.

### Files in `out/` are owned by root

The container runs as root, so everything it writes into the mounted `out/` is
root-owned on the host. Reading and playing them is fine; deleting or moving
them needs `sudo`. To take ownership of what is already there:

```bash
sudo chown -R "$USER:$USER" out/
```

To avoid it for future runs, add your own ids to the service in
`docker-compose.yml`:

```yaml
    user: "1000:1000"      # your `id -u`:`id -g`
```

The folder itself ships in the clone precisely so Docker does not create it
root-owned before you ever get the chance.

### An edit I already loaded is not selected

Edits arrive ticked, but only ones discovered fresh. Any edit already saved in
your browser's local storage keeps whatever state it had. Tick it, or clear the
site's local storage.

### "No matching audio track" on a run

opdub selects streams by language tag with a fallback to title keywords, and
fails loudly rather than grabbing stream 1 and hoping. The error prints what
the file actually contains. Pick the stream explicitly in tab 1 or 2.

### Segments are flagged low-confidence

`conf` below ~1000 usually means that stretch of the edit came from a source
you did not tick, or from a range where the audio genuinely repeats — a theme,
a recap, silence. Check the ticked sources first, then mark the range as
passthrough if it is a theme.

### The dub is offset from the picture

Measure the offset in tab 1 rather than leaving it blank. A separate dub file
is frequently a different master with a different start.

## Uninstalling

```bash
./uninstall.sh                  # remove what install.sh added, keep out/
./uninstall.sh --dry-run        # list it first
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

## Development

```bash
./install.sh --verify          # install, run tests, score the fixtures
.venv/bin/python -m pytest tests -q
.venv/bin/python score.py fixtures/ground_truth.json --edl-dir out/edls
```

`score.py` writes `metrics.json` and exits non-zero if any gate regresses.
Regenerate the fixtures — deterministic from a seed — with:

```bash
python make_fixtures.py fixtures --sources 6 --edits 3
```

Two optional UI test harnesses live in `tests/ui/` and are dev-only; the app
itself still has no build step and no runtime JavaScript dependencies.

- `e2e.mjs` drives the real frontend in jsdom against a running server with
  real media, through the whole workflow.
- `layout.mjs` drives a real Chromium to measure actual box geometry, which
  jsdom cannot do.

See `tests/ui/README.md`.

The `fixtures/` tree is checked in rather than ignored, so the tests and the
scorer work straight after a clone. It is synthetic — swept tones built from a
seed, not clips of anything — and regenerating it is deterministic.

## Licence

[MIT](LICENSE). The tool ships no video or audio: it operates on files you
already have, and what you do with the output is on you.
