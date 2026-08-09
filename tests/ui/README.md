# UI end-to-end test

Optional and **dev-only**. The app itself still has no build step and no
runtime JavaScript dependencies — this directory is a test harness, not part
of what gets served.

It loads the real `index.html` and `app.js` into a DOM and drives the whole
workflow against a running server with real media: folder defaults, source
pairing, auto-assign, playback and track switching, edit selection,
passthrough marking, validation, a full align → render → mux run, then EDL
review, a cut nudge, save and re-render.

## Running it

```bash
# 1. a tree with some media in it
mkdir -p /tmp/ui/input/sources /tmp/ui/input/edits
cp fixtures/sources/*.mkv /tmp/ui/input/sources/
cp fixtures/edits/edit_00.mkv /tmp/ui/input/edits/

# 2. the server, pointed at it
OPDUB_MEDIA=/tmp/ui/input OPDUB_OUT=/tmp/ui/out \
    .venv/bin/uvicorn opdub.server:app --port 8091 &

# 3. the test
cd tests/ui && npm install && BASE=http://127.0.0.1:8091 npm test
```

Takes a couple of minutes, most of it real alignment. Exits non-zero if any
check fails.

jsdom has no canvas, so the harness installs a recording stub: the drawing
code really executes and would throw on a bad call, and the test asserts the
timeline issued at least one `fillRect` per segment.

# Layout test

`layout.mjs`, run separately, because it needs the one thing jsdom does not
have: a layout engine. It drives a real Chromium and measures real boxes.

It exists for a specific bug. `grid-template-columns: 1fr 1fr` takes each
track's minimum from its item's *min-content* width, and a `nowrap` filename's
min-content is the whole string — so one long name grew its column to 966px
inside a 708px card and pushed the slot 271px past the card's edge. Nothing in
jsdom can see that.

It needs a source file whose name is long enough to overflow, which is the
entire point, so stage one:

```bash
cp fixtures/sources/src_00.mkv \
  "/tmp/ui/input/sources/[Erai-raws] One Piece - 0384 ~ 0385 (1080p AV1 10bit \
Multiple Subtitle) [Enies Lobby Arc, Complete Batch Release] [F00DCAFE].mkv"

cd tests/ui && npm install
CHROME=/usr/bin/chromium BASE=http://127.0.0.1:8091 npm run layout
```

`CHROME` is any Chromium or Chrome binary — `puppeteer-core` drives one, it
does not download one. To confirm the test still bites, check out a revision
from before the fix and watch "the drop slot stays inside the card" fail.
