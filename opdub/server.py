"""FastAPI backend for the opdub web UI.

Design rule for this server: it never decides anything the operator could get
wrong.  It probes files and reports what it found; it measures offsets and
reports the measurement together with its quality; it never pairs files by
name, never picks an audio stream by language tag, and never guesses which
source episodes an edit was cut from.  Those are all inputs.

    OPDUB_MEDIA   colon-separated read-only media roots
                  (default: /workspace/onepace:/workspace/onepiece)
    OPDUB_OUT     writable output directory (default: /workspace/out)

    uvicorn opdub.server:app --port 8000

Media roots are treated as strictly read-only: every write path is checked
against OPDUB_OUT and nothing else.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import wave
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .detect import auto_assign
from .media import decode_audio, probe
from .plan import PlanError, measure_pair_offset, validate_plan

MEDIA_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov"}

STATIC_DIR = Path(__file__).parent / "static"

MEDIA_ROOTS = [
    Path(p).expanduser().resolve()
    for p in os.environ.get(
        "OPDUB_MEDIA", "/workspace/onepace:/workspace/onepiece"
    ).split(":")
    if p.strip()
]
OUT_DIR = Path(os.environ.get("OPDUB_OUT", "/workspace/out")).expanduser().resolve()

EDL_DIR = OUT_DIR / "edls"
WAV_DIR = OUT_DIR / "wav"
MUX_DIR = OUT_DIR / "muxed"
PLAN_DIR = OUT_DIR / "plans"
PROJECT_DIR = OUT_DIR / "projects"
CACHE_DIR = OUT_DIR / "cache"
JOBS_FILE = OUT_DIR / "jobs.json"

for d in (EDL_DIR, WAV_DIR, MUX_DIR, PLAN_DIR, PROJECT_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Preview playback rate.  44.1 kHz mono is plenty for judging "is this English"
# and for hearing a seam, and keeps the response small.
PREVIEW_SR = 44_100

# Waveform peaks are computed from a 2 kHz decode: far below anything audible,
# but the envelope is what gets drawn, and a 30-minute episode stays a few
# megabytes in memory.
WAVEFORM_SR = 2_000

app = FastAPI(title="opdub")


# --------------------------------------------------------------------------
# path safety
# --------------------------------------------------------------------------

def _resolve_readable(raw: str) -> Path:
    """Resolve a path the UI asked to read, refusing anything outside roots."""
    if not raw:
        raise HTTPException(400, "path is required")
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError as exc:
        raise HTTPException(400, f"bad path: {exc}") from exc

    allowed = MEDIA_ROOTS + [OUT_DIR]
    for root in allowed:
        if p == root or root in p.parents:
            return p
    raise HTTPException(
        403,
        f"{p} is outside the configured roots "
        f"({', '.join(str(r) for r in allowed)})",
    )


def _resolve_writable(raw: str) -> Path:
    """Resolve a path the UI asked to write. Only OPDUB_OUT is ever writable."""
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError as exc:
        raise HTTPException(400, f"bad path: {exc}") from exc
    if p != OUT_DIR and OUT_DIR not in p.parents:
        raise HTTPException(
            403,
            f"refusing to write outside {OUT_DIR}. The media directories are "
            f"read-only.",
        )
    return p


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------

_probe_cache: dict[tuple[str, int, int], dict] = {}


def _probe_file(path: Path) -> dict:
    """ffprobe a media file, reporting every stream without interpretation."""
    if not path.exists():
        return {"path": str(path), "name": path.name, "error": "file not found",
                "audio": [], "video": [], "duration": None, "size": 0}
    st = path.stat()
    key = (str(path), st.st_size, int(st.st_mtime))
    if key in _probe_cache:
        return _probe_cache[key]

    try:
        info = probe(path)
    except RuntimeError as exc:
        result = {"path": str(path), "name": path.name, "error": str(exc),
                  "audio": [], "video": [], "duration": None, "size": st.st_size}
        _probe_cache[key] = result
        return result

    duration = None
    fmt = info.get("format") or {}
    if fmt.get("duration"):
        try:
            duration = float(fmt["duration"])
        except (TypeError, ValueError):
            duration = None

    audio, video = [], []
    for s in info.get("streams", []):
        tags = s.get("tags") or {}
        if s.get("codec_type") == "audio":
            dur = s.get("duration")
            audio.append({
                "index": s.get("index"),
                "codec": s.get("codec_name"),
                "channels": s.get("channels"),
                "sample_rate": s.get("sample_rate"),
                # Reported verbatim. The UI shows these as evidence, never as
                # a decision — an untagged or mistagged stream is the exact
                # failure this tool exists to avoid.
                "language": tags.get("language"),
                "title": tags.get("title"),
                "duration": float(dur) if dur else None,
            })
        elif s.get("codec_type") == "video":
            video.append({
                "index": s.get("index"),
                "codec": s.get("codec_name"),
                "width": s.get("width"),
                "height": s.get("height"),
            })

    if duration is None and audio and audio[0].get("duration"):
        duration = audio[0]["duration"]

    result = {
        "path": str(path),
        "name": path.name,
        "size": st.st_size,
        "duration": duration,
        "audio": audio,
        "video": video,
        "error": None,
    }
    _probe_cache[key] = result
    return result


# --------------------------------------------------------------------------
# audio helpers
# --------------------------------------------------------------------------

def _pcm_to_wav(pcm: bytes, sr: int) -> bytes:
    """Wrap raw s16le mono PCM in a correct WAV header.

    ffmpeg piping WAV writes a placeholder RIFF size because the pipe is not
    seekable; some browsers refuse that. Building the header here avoids it.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def _extract_pcm(path: Path, stream: int, start: float, dur: float,
                 sr: int = PREVIEW_SR) -> bytes:
    """Decode a short window to s16le mono. Fast-seek is fine for listening."""
    cmd = [
        "ffmpeg", "-v", "error",
        "-ss", f"{max(0.0, start):.3f}",
        "-i", str(path),
        "-map", f"0:{stream}",
        "-t", f"{max(0.05, dur):.3f}",
        "-ac", "1", "-ar", str(sr), "-f", "s16le", "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise HTTPException(
            500, proc.stderr.decode("utf-8", "replace").strip()[:500] or "ffmpeg failed"
        )
    return proc.stdout


def _waveform_peaks(path: Path, stream: int, points: int) -> dict:
    """Return a max-abs envelope of the whole stream, cached on disk."""
    st = path.stat()
    key = hashlib.sha1(
        f"{path}|{stream}|{st.st_size}|{int(st.st_mtime)}|{points}".encode()
    ).hexdigest()
    cache_file = CACHE_DIR / f"wave_{key}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except json.JSONDecodeError:
            cache_file.unlink(missing_ok=True)

    audio = decode_audio(path, stream, sr=WAVEFORM_SR)
    if len(audio) == 0:
        raise HTTPException(500, f"decoded no audio from stream {stream}")

    duration = len(audio) / WAVEFORM_SR
    points = max(100, min(points, 4000))
    edges = np.linspace(0, len(audio), points + 1).astype(int)
    peaks = np.zeros(points, dtype=np.float32)
    for i in range(points):
        a, b = edges[i], edges[i + 1]
        if b > a:
            peaks[i] = np.abs(audio[a:b]).max()

    top = float(peaks.max()) or 1.0
    result = {
        "duration": duration,
        "points": [round(float(v / top), 4) for v in peaks],
    }
    cache_file.write_text(json.dumps(result))
    return result


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

class Job:
    def __init__(self, kind: str, title: str, spec: dict):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind           # "pipeline" | "rerender"
        self.title = title
        self.spec = spec
        self.state = "queued"      # queued running done failed cancelled
        self.stage = "queued"
        self.pct = 0
        self.lines: list[str] = []
        self.error: str | None = None
        self.outputs: dict[str, str] = {}
        self.created = time.time()
        self.started: float | None = None
        self.ended: float | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.cancelled = False

    def public(self, with_lines: bool = True) -> dict:
        d = {
            "id": self.id, "kind": self.kind, "title": self.title,
            "state": self.state, "stage": self.stage, "pct": self.pct,
            "error": self.error, "outputs": self.outputs,
            "created": self.created, "started": self.started, "ended": self.ended,
        }
        if with_lines:
            d["lines"] = self.lines[-400:]
        return d


JOBS: dict[str, Job] = {}
JOB_ORDER: list[str] = []
QUEUE: asyncio.Queue[str] = asyncio.Queue()
SUBSCRIBERS: set[asyncio.Queue] = set()
_worker_started = False


def _broadcast(event: dict) -> None:
    for q in list(SUBSCRIBERS):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _emit(job: Job, line: str | None = None) -> None:
    if line is not None:
        job.lines.append(line)
    _broadcast({"type": "job", "job": job.public(with_lines=False),
                "line": line, "id": job.id})


def _save_jobs() -> None:
    try:
        JOBS_FILE.write_text(json.dumps(
            [JOBS[i].public(with_lines=False) for i in JOB_ORDER][-200:], indent=2
        ))
    except OSError:
        pass


# Stage lines printed by plan.run_plan, mapped onto the first 70% of the bar.
# Align is by far the longest phase, so it gets most of the range.
_ALIGN_STAGES = 9


def _progress_from_line(line: str) -> tuple[str, int] | None:
    s = line.strip()
    if s.startswith("Stage "):
        head = s.split(":", 1)[0]
        try:
            n = int(head.split()[1])
        except (IndexError, ValueError):
            return None
        label = s.split(":", 1)[1].strip() if ":" in s else s
        return label, int(round(min(n, _ALIGN_STAGES) / _ALIGN_STAGES * 70))
    return None


async def _run_step(job: Job, args: list[str], phase: str,
                    base_pct: int, span_pct: int) -> int:
    """Run one subprocess, streaming its stdout into the job log."""
    job.stage = phase
    job.pct = base_pct
    _emit(job, f"$ {' '.join(args[2:])}")

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(Path(__file__).parent.parent),
    )
    job.proc = proc

    assert proc.stdout is not None
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").rstrip()
        if not line:
            continue
        prog = _progress_from_line(line)
        if phase == "align":
            # Only a stage line moves the bar; other output must not drag it
            # back down from the stage it already reached.
            if prog:
                job.stage, job.pct = prog[0], prog[1]
        else:
            job.pct = max(job.pct, base_pct)
        _emit(job, line)

    rc = await proc.wait()
    job.proc = None
    return rc


async def _run_pipeline(job: Job) -> None:
    spec = job.spec
    plan = spec["plan"]
    stem = Path(plan["edit"]["path"]).stem

    plan_file = PLAN_DIR / f"{stem}.plan.json"
    plan_file.write_text(json.dumps(plan, indent=2))
    job.outputs["plan"] = str(plan_file)

    py = sys.executable or "python3"

    rc = await _run_step(
        job, [py, "-m", "opdub.cli", "run", str(plan_file),
              "--out", str(EDL_DIR), "-v"],
        "align", 0, 70)
    if job.cancelled:
        raise asyncio.CancelledError()
    if rc != 0:
        raise RuntimeError(f"align failed (exit {rc})")

    edl_file = EDL_DIR / f"{stem}.json"
    if not edl_file.exists():
        raise RuntimeError(f"align reported success but {edl_file.name} is missing")
    job.outputs["edl"] = str(edl_file)

    await _run_render_and_mux(job, edl_file, spec.get("crossfade", 0.0),
                              spec.get("mux", True), spec.get("dub_lang", "eng"))


async def _run_render_and_mux(job: Job, edl_file: Path, crossfade: float,
                              do_mux: bool, dub_lang: str) -> None:
    py = sys.executable or "python3"
    edl = json.loads(edl_file.read_text())
    stem = Path(edl.get("edit", edl_file.stem)).stem

    args = [py, "-m", "opdub.cli", "render", str(edl_file),
            "--out", str(WAV_DIR), "--dub-lang", dub_lang, "-v"]
    if crossfade:
        args += ["--crossfade", str(crossfade)]

    rc = await _run_step(job, args, "render", 70, 20)
    if job.cancelled:
        raise asyncio.CancelledError()
    if rc != 0:
        raise RuntimeError(f"render failed (exit {rc})")

    wav = WAV_DIR / f"{stem}.dub.wav"
    if not wav.exists():
        raise RuntimeError(f"render reported success but {wav.name} is missing")
    job.outputs["wav"] = str(wav)

    if not do_mux:
        job.pct = 100
        return

    video = edl.get("edit_path")
    if not video or not Path(video).exists():
        raise RuntimeError("cannot mux: the EDL does not record a usable edit path")

    out_mkv = MUX_DIR / f"{stem}.mkv"
    rc = await _run_step(
        job, [py, "-m", "opdub.cli", "mux", str(video), str(wav), str(out_mkv),
              "--lang", dub_lang, "--title", "English Dub", "-v"],
        "mux", 90, 10)
    if rc != 0:
        raise RuntimeError(f"mux failed (exit {rc})")

    job.outputs["mkv"] = str(out_mkv)
    job.pct = 100


async def _worker() -> None:
    """One job at a time. Align is CPU-bound; parallel runs only thrash."""
    while True:
        job_id = await QUEUE.get()
        job = JOBS.get(job_id)
        if job is None or job.cancelled:
            QUEUE.task_done()
            continue

        job.state = "running"
        job.started = time.time()
        _emit(job)

        try:
            if job.kind == "rerender":
                edl_file = Path(job.spec["edl_path"])
                job.outputs["edl"] = str(edl_file)
                await _run_render_and_mux(
                    job, edl_file, job.spec.get("crossfade", 0.0),
                    job.spec.get("mux", True), job.spec.get("dub_lang", "eng"))
            else:
                await _run_pipeline(job)
            job.state = "done"
            job.stage = "finished"
            job.pct = 100
        except asyncio.CancelledError:
            job.state = "cancelled"
            job.stage = "cancelled"
        except Exception as exc:                      # noqa: BLE001
            job.state = "failed"
            job.stage = "failed"
            job.error = str(exc)
            job.lines.append(f"ERROR: {exc}")
        finally:
            job.ended = time.time()
            _emit(job)
            _save_jobs()
            QUEUE.task_done()


@app.on_event("startup")
async def _startup() -> None:
    global _worker_started
    if not _worker_started:
        asyncio.create_task(_worker())
        _worker_started = True


# --------------------------------------------------------------------------
# routes — discovery
# --------------------------------------------------------------------------

@app.get("/api/config")
def api_config() -> dict:
    return {
        "roots": [{"path": str(r), "exists": r.exists(), "writable": False}
                  for r in MEDIA_ROOTS],
        "out": str(OUT_DIR),
        "media_exts": sorted(MEDIA_EXTS),
    }


@app.get("/api/browse")
def api_browse(dir: str) -> dict:
    d = _resolve_readable(dir)
    if not d.is_dir():
        raise HTTPException(400, f"{d} is not a directory")
    dirs, files = [], []
    for p in sorted(d.iterdir(), key=lambda x: x.name.lower()):
        if p.is_dir():
            dirs.append({"name": p.name, "path": str(p)})
        elif p.suffix.lower() in MEDIA_EXTS:
            files.append({"name": p.name, "path": str(p), "size": p.stat().st_size})
    return {"dir": str(d), "dirs": dirs, "files": files}


# How deep to look for media when populating the folder pickers. The input
# tree is a bind mount the operator organises however they like, so this walks
# rather than assuming a layout — but stays bounded so a deep tree cannot
# turn the folder dropdown into a long scan.
MEDIA_SCAN_DEPTH = 4


def _walk_folders(root: Path, depth: int) -> list[dict]:
    """Every directory at or under root, with how much media each holds.

    Empty folders are included deliberately: a fresh checkout has an empty
    input/sources, and it still has to be selectable in the UI. Reporting the
    media count lets the picker show which ones actually have anything.
    """
    found: list[dict] = []

    def walk(d: Path, level: int) -> None:
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except (PermissionError, OSError):
            return
        n = 0
        subdirs = []
        for p in entries:
            if p.name.startswith("."):
                continue
            if p.is_dir():
                # Never descend into the output tree if it sits inside a root.
                if p.resolve() == OUT_DIR:
                    continue
                subdirs.append(p)
            elif p.suffix.lower() in MEDIA_EXTS:
                n += 1

        found.append({
            "path": str(d),
            "root": str(root),
            "rel": "." if d == root else str(d.relative_to(root)),
            "name": d.name,
            "files": n,
            "subdirs": len(subdirs),
            "depth": level,
        })
        if level < depth:
            for p in subdirs:
                walk(p, level + 1)

    if root.is_dir():
        walk(root, 0)
    return found


@app.get("/api/folders")
async def api_folders() -> dict:
    """Every folder under the media roots, so nothing has to be typed by hand."""
    def scan() -> list[dict]:
        out: list[dict] = []
        for root in MEDIA_ROOTS:
            out.extend(_walk_folders(root, MEDIA_SCAN_DEPTH))
        return out
    return {"folders": await asyncio.to_thread(scan)}


@app.post("/api/probe")
async def api_probe(req: Request) -> dict:
    body = await req.json()
    paths = [_resolve_readable(raw) for raw in (body.get("paths") or [])]
    # ffprobe is a subprocess per file; off the loop so the job stream and
    # the rest of the UI stay responsive while a folder is being scanned.
    out = await asyncio.to_thread(lambda: [_probe_file(p) for p in paths])
    return {"files": out}


@app.get("/api/preview")
def api_preview(path: str, stream: int, t: float = 0.0, dur: float = 6.0):
    """A few seconds of one audio stream, as WAV.

    This is how the operator confirms which stream is Japanese and which is
    the dub: by listening, not by trusting a tag.
    """
    p = _resolve_readable(path)
    pcm = _extract_pcm(p, stream, t, min(dur, 60.0))
    return Response(content=_pcm_to_wav(pcm, PREVIEW_SR), media_type="audio/wav")


@app.get("/api/waveform")
def api_waveform(path: str, stream: int, points: int = 1400) -> dict:
    p = _resolve_readable(path)
    return _waveform_peaks(p, stream, points)


@app.post("/api/auto-assign")
async def api_auto_assign(req: Request) -> dict:
    """Propose source pairings from stream tags and filenames.

    A proposal, not a decision: it lands in the UI as a filled-in form the
    operator reviews. Anything it cannot match confidently is returned in
    `unmatched` with the reason, and left blank.
    """
    body = await req.json()
    mode = body.get("mode") or "separate"
    if mode not in ("separate", "multitrack"):
        raise HTTPException(400, f"unknown mode {mode!r}")
    paths = [_resolve_readable(p) for p in (body.get("paths") or [])]

    def work() -> dict:
        return auto_assign([_probe_file(p) for p in paths], mode)

    return await asyncio.to_thread(work)


@app.post("/api/offset")
async def api_offset(req: Request) -> dict:
    """Measure one jpn->dub offset and report the measurement plus its quality.

    Reports rather than decides: a rejected measurement comes back with the
    reason so the operator can re-pair the files or type an offset by hand.
    """
    b = await req.json()
    jpn = _resolve_readable(b["jpn_path"])
    dub = _resolve_readable(b["dub_path"])
    try:
        # Decodes two full episodes and cross-correlates: seconds of CPU, so
        # it must not run on the event loop.
        return await asyncio.to_thread(
            measure_pair_offset,
            jpn, int(b["jpn_stream"]), dub, int(b["dub_stream"]))
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


# --------------------------------------------------------------------------
# routes — jobs
# --------------------------------------------------------------------------

@app.post("/api/validate")
async def api_validate(req: Request) -> dict:
    """Dry-run a plan: probe every file and stream it names, decode nothing."""
    body = await req.json()
    plans = body.get("plans") or [body.get("plan")]

    def check_all() -> list[dict]:
        results = []
        for plan in plans:
            try:
                validate_plan(plan)
                results.append({"ok": True, "error": None,
                                "edit": Path(plan["edit"]["path"]).name})
            except (PlanError, KeyError, TypeError) as exc:
                results.append({"ok": False, "error": str(exc),
                                "edit": (plan.get("edit") or {}).get("path", "?")})
        return results

    # One ffprobe per file and stream named in every plan.
    return {"results": await asyncio.to_thread(check_all)}


@app.post("/api/jobs")
async def api_create_jobs(req: Request) -> dict:
    body = await req.json()
    plans = body.get("plans") or []
    if not plans:
        raise HTTPException(400, "no plans submitted")

    created = []
    for plan in plans:
        try:
            await asyncio.to_thread(validate_plan, plan)
        except (PlanError, KeyError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc

        job = Job("pipeline", Path(plan["edit"]["path"]).name, {
            "plan": plan,
            "crossfade": float(body.get("crossfade", 0.0)),
            "mux": bool(body.get("mux", True)),
            "dub_lang": body.get("dub_lang", "eng"),
        })
        JOBS[job.id] = job
        JOB_ORDER.append(job.id)
        await QUEUE.put(job.id)
        created.append(job.public(with_lines=False))

    _save_jobs()
    _broadcast({"type": "queue"})
    return {"jobs": created}


@app.post("/api/rerender")
async def api_rerender(req: Request) -> dict:
    body = await req.json()
    edl_path = _resolve_writable(body["edl_path"])
    if not edl_path.exists():
        raise HTTPException(404, f"{edl_path} not found")

    job = Job("rerender", f"re-render {edl_path.stem}", {
        "edl_path": str(edl_path),
        "crossfade": float(body.get("crossfade", 0.0)),
        "mux": bool(body.get("mux", True)),
        "dub_lang": body.get("dub_lang", "eng"),
    })
    JOBS[job.id] = job
    JOB_ORDER.append(job.id)
    await QUEUE.put(job.id)
    _save_jobs()
    _broadcast({"type": "queue"})
    return job.public(with_lines=False)


@app.get("/api/jobs")
def api_jobs() -> dict:
    return {"jobs": [JOBS[i].public(with_lines=False) for i in reversed(JOB_ORDER)]}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return job.public()


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel(job_id: str) -> dict:
    # async, not sync: this touches the asyncio subprocess handle and the SSE
    # queues, neither of which is safe to poke from FastAPI's threadpool.
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    job.cancelled = True
    if job.state == "queued":
        job.state = "cancelled"
        job.ended = time.time()
    if job.proc is not None:
        try:
            job.proc.terminate()
        except ProcessLookupError:
            pass
    _emit(job, "cancelled by operator")
    return job.public(with_lines=False)


@app.get("/api/events")
async def api_events(request: Request):
    """One SSE stream carrying every job update."""
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    SUBSCRIBERS.add(q)

    async def gen():
        try:
            yield f"data: {json.dumps({'type': 'hello'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            SUBSCRIBERS.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# --------------------------------------------------------------------------
# routes — EDLs, projects, downloads
# --------------------------------------------------------------------------

@app.get("/api/edls")
def api_edls() -> dict:
    out = []
    for p in sorted(EDL_DIR.rglob("*.json")):
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        segs = d.get("segments") or []
        out.append({
            "path": str(p),
            "name": p.name,
            "edit": d.get("edit"),
            "duration": d.get("duration"),
            "segments": len(segs),
            "passthrough": len(d.get("passthrough") or []),
            "mtime": p.stat().st_mtime,
        })
    return {"edls": sorted(out, key=lambda e: -e["mtime"])}


@app.get("/api/edl")
def api_edl(path: str) -> dict:
    p = _resolve_writable(path)
    if not p.exists():
        raise HTTPException(404, f"{p} not found")
    return json.loads(p.read_text())


@app.post("/api/edl")
async def api_save_edl(req: Request) -> dict:
    body = await req.json()
    p = _resolve_writable(body["path"])
    edl = body["edl"]

    segs = edl.get("segments") or []
    for i, s in enumerate(segs):
        if s["t1"] <= s["t0"]:
            raise HTTPException(
                400, f"segment {i}: t1 ({s['t1']}) must be after t0 ({s['t0']})")
        if i and abs(segs[i - 1]["t1"] - s["t0"]) > 1e-6:
            raise HTTPException(
                400,
                f"segment {i} starts at {s['t0']:.4f}s but segment {i-1} ends "
                f"at {segs[i-1]['t1']:.4f}s — the EDL must stay contiguous",
            )

    if p.exists():
        shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
    p.write_text(json.dumps(edl, indent=2))
    return {"ok": True, "path": str(p), "backup": str(p) + ".bak"}


@app.get("/api/projects")
def api_projects() -> dict:
    out = []
    for p in sorted(PROJECT_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.append({"name": p.stem, "path": str(p),
                    "sources": len(d.get("sources") or []),
                    "edits": len(d.get("edits") or []),
                    "mtime": p.stat().st_mtime})
    return {"projects": sorted(out, key=lambda e: -e["mtime"])}


def _project_name(raw: str) -> str:
    """Strip anything that could escape PROJECT_DIR or name a hidden file."""
    name = "".join(c for c in (raw or "").strip()
                   if c.isalnum() or c in " -_")
    return name.strip(" .-_") or "project"


@app.post("/api/projects")
async def api_save_project(req: Request) -> dict:
    body = await req.json()
    name = _project_name(body.get("name"))
    p = _resolve_writable(str(PROJECT_DIR / f"{name}.json"))
    p.write_text(json.dumps(body.get("data") or {}, indent=2))
    return {"ok": True, "name": name, "path": str(p)}


@app.get("/api/project")
def api_project(name: str) -> dict:
    p = _resolve_writable(str(PROJECT_DIR / f"{_project_name(name)}.json"))
    if not p.exists():
        raise HTTPException(404, "no such project")
    return json.loads(p.read_text())


@app.get("/api/download")
def api_download(path: str):
    p = _resolve_writable(path)
    if not p.exists():
        raise HTTPException(404, f"{p} not found")
    return FileResponse(p, filename=p.name, media_type="application/octet-stream")


# --------------------------------------------------------------------------
# static
# --------------------------------------------------------------------------

# There is no bundler and no content hash in these filenames, so a browser
# given no Cache-Control applies heuristic caching and can serve a stale UI for
# hours after an upgrade — with the server sitting there reporting the new
# version. An upgrade nobody can see is worse than re-sending 90 KB over
# loopback, so ask for a fetch every time. (FileResponse sends an ETag but
# does not answer If-None-Match — only StaticFiles does — so this really is a
# refetch, not a 304.)
NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/")
def index():
    f = STATIC_DIR / "index.html"
    if not f.exists():
        return JSONResponse({"error": f"{f} missing"}, status_code=500)
    return FileResponse(f, media_type="text/html", headers=NO_CACHE)


@app.get("/app.js")
def appjs():
    f = STATIC_DIR / "app.js"
    if not f.exists():
        return JSONResponse({"error": f"{f} missing"}, status_code=500)
    return FileResponse(f, media_type="application/javascript", headers=NO_CACHE)
