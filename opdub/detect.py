"""Best-effort automatic assignment of source files and audio tracks.

This is the only place in the project that guesses, and it exists purely to
save typing. Every proposal it returns lands in the UI as a *filled-in form*
the operator can see and override, never as a decision that reaches the
pipeline unseen. When the evidence is weak it declines rather than picking:
an unassigned row is obvious, a confidently wrong pairing is not.

Two shapes of source library are supported:

  separate    one language per file, so files must be paired with each other
              ("Ep 313.jpn.mkv" + "Ep 313.eng.mkv")
  multitrack  one file per episode carrying several audio tracks, so only the
              tracks need identifying

Every proposal carries a `reason` string explaining what it matched on, which
the UI shows next to the row.
"""
from __future__ import annotations

import re
from typing import Any

# Tokens that identify a language in a filename or a stream title. Ordered
# longest-first within each language so "japanese" wins over "jap".
JPN_TOKENS = ("japanese", "japan", "jpn", "jap", "jp", "subbed", "sub", "ja")
ENG_TOKENS = ("english", "eng", "dubbed", "dub", "en")

# ISO-639 values seen in real stream tags.
JPN_TAGS = {"jpn", "ja", "jap", "japanese"}
ENG_TAGS = {"eng", "en", "english"}

# Numbers that appear in filenames but never mean "episode".
_RESOLUTIONS = {2160, 1440, 1080, 720, 576, 480, 360, 240}
_CODEC_NOISE = {264, 265, 8, 10, 16}


def _stem(name: str) -> str:
    """Filename without any extension chain (handles 'Ep 1.jpn.mkv')."""
    out = name
    while True:
        base, dot, ext = out.rpartition(".")
        if not dot or len(ext) > 5 or not ext.isalnum():
            return out
        out = base


def _clean(name: str) -> str:
    """Strip the parts of a filename that reliably contain no episode number."""
    s = _stem(name)
    # Underscores are word characters, so "Ep0313_eng" has no \b anywhere in
    # it and every marker pattern would silently miss. Treat them as spaces.
    s = s.replace("_", " ")
    s = re.sub(r"\[[^\]]*\]", " ", s)          # [Group] / [ABCD1234]
    s = re.sub(r"\(\d{1,2}\)\s*$", " ", s)     # "Title(1)" duplicate marker
    s = re.sub(r"\b\d{3,4}[pi]\b", " ", s, flags=re.I)   # 1080p / 720i
    s = re.sub(r"\bx?26[45]\b", " ", s, flags=re.I)      # x264 / h265
    s = re.sub(r"\b\d{1,2}bit\b", " ", s, flags=re.I)
    return s


# Ordered strongest-evidence first. Each yields (season_or_None, episode).
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bS(\d{1,2})[\s._-]*E(\d{1,4})\b", re.I), "se"),
    (re.compile(r"\b(?:episodes?|epis|ep)[\s._#-]*(\d{1,4})\b", re.I), "ep"),
    (re.compile(r"#\s*(\d{1,4})\b"), "ep"),
    (re.compile(r"\bE(\d{2,4})\b"), "ep"),
    # Anime release style: "Show - 1071 [1080p]" or "Show - 1071v2"
    (re.compile(r"[-–—]\s*(\d{1,4})\s*(?:v\d+)?\s*(?:$|[\[(])"), "ep"),
]


def episode_key(name: str) -> tuple[int | None, int] | None:
    """Parse (season, episode) from a filename, or None if nothing looks like one.

    Handles the conventions these libraries actually use: 'Episode 313',
    'S21E1071', 'Show - 1071 [1080p]', 'Ep.313', '#313', 'E313', and a bare
    trailing number. Years and resolutions are never mistaken for episodes,
    which matters for names like 'Some Show 1999 Episode 313 ...'.
    """
    s = _clean(name)

    for pat, kind in _PATTERNS:
        m = pat.search(s)
        if not m:
            continue
        if kind == "se":
            return int(m.group(1)), int(m.group(2))
        return None, int(m.group(1))

    # Fallback: the last standalone number that cannot be something else.
    candidates: list[int] = []
    for m in re.finditer(r"\b(\d{1,4})\b", s):
        v = int(m.group(1))
        if 1900 <= v <= 2100:            # a year
            continue
        if v in _RESOLUTIONS or v in _CODEC_NOISE:
            continue
        candidates.append(v)
    if candidates:
        return None, candidates[-1]
    return None


def _token_language(text: str) -> str | None:
    """Language implied by words in a filename or stream title."""
    low = re.sub(r"[^a-z0-9]+", " ", text.lower())
    words = set(low.split())
    for t in ENG_TOKENS:
        if t in words:
            return "eng"
    for t in JPN_TOKENS:
        if t in words:
            return "jpn"
    return None


def stream_language(stream: dict) -> tuple[str | None, str]:
    """Language of one audio stream from its tag, then its title.

    Returns (language, reason). A stream tag is strong evidence; a title is
    weaker but still explicit, and both beat anything in the filename.
    """
    tag = (stream.get("language") or "").strip().lower()
    if tag in JPN_TAGS:
        return "jpn", f"stream {stream.get('index')} tagged {tag!r}"
    if tag in ENG_TAGS:
        return "eng", f"stream {stream.get('index')} tagged {tag!r}"

    title = stream.get("title") or ""
    if title:
        lang = _token_language(title)
        if lang:
            return lang, f"stream {stream.get('index')} titled {title!r}"
    return None, ""


def file_language(probe: dict) -> tuple[str | None, str]:
    """Language of a whole file, for the one-language-per-file layout.

    Order: the audio stream's own tag, then the filename. A file whose single
    stream is tagged is the clearest case there is; the filename is the
    fallback for untagged rips.
    """
    audio = probe.get("audio") or []
    if len(audio) == 1:
        lang, why = stream_language(audio[0])
        if lang:
            return lang, why

    # Several streams, but all the tagged ones agree.
    tagged = {}
    for s in audio:
        lang, why = stream_language(s)
        if lang:
            tagged.setdefault(lang, why)
    if len(tagged) == 1:
        lang, why = next(iter(tagged.items()))
        return lang, why

    name_lang = _token_language(probe.get("name") or "")
    if name_lang:
        return name_lang, f"filename says {name_lang!r}"
    return None, ""


def _first_audio(probe: dict) -> int | None:
    audio = probe.get("audio") or []
    return audio[0]["index"] if audio else None


def _label(probe: dict, key: tuple[int | None, int] | None) -> str:
    if key is None:
        return _stem(probe.get("name") or "episode")
    season, ep = key
    return f"S{season:02d}E{ep:02d}" if season is not None else f"Episode {ep}"


def assign_multitrack(files: list[dict]) -> dict:
    """Identify the jpn and eng tracks inside each multi-track file.

    One file is one episode here, so nothing has to be paired — only the
    tracks need naming.
    """
    pairs: list[dict] = []
    unmatched: list[dict] = []

    for pr in files:
        audio = pr.get("audio") or []
        if pr.get("error"):
            unmatched.append({"path": pr["path"], "why": pr["error"]})
            continue
        if len(audio) < 2:
            unmatched.append({
                "path": pr["path"],
                "why": f"only {len(audio)} audio track — needs at least two "
                       f"in multi-track mode",
            })
            continue

        found: dict[str, tuple[int, str]] = {}
        for s in audio:
            lang, why = stream_language(s)
            if lang and lang not in found:
                found[lang] = (s["index"], why)

        if "jpn" in found and "eng" in found:
            key = episode_key(pr.get("name") or "")
            pairs.append({
                "path": pr["path"],
                "label": _label(pr, key),
                "episode": key[1] if key else None,
                "jpn": {"path": pr["path"], "stream": found["jpn"][0]},
                "dub": {"path": pr["path"], "stream": found["eng"][0]},
                "reason": f"{found['jpn'][1]}; {found['eng'][1]}",
            })
        else:
            missing = [x for x in ("jpn", "eng") if x not in found]
            unmatched.append({
                "path": pr["path"],
                "why": f"could not identify the {', '.join(missing)} track "
                       f"from tags or titles — pick it by hand",
            })

    pairs.sort(key=lambda p: (p["episode"] is None, p["episode"] or 0, p["label"]))
    return {"pairs": pairs, "unmatched": unmatched}


def assign_separate(files: list[dict]) -> dict:
    """Pair one-language-per-file sources with each other by episode number."""
    groups: dict[Any, list[dict]] = {}
    ungrouped: list[dict] = []

    for pr in files:
        if pr.get("error") or not (pr.get("audio") or []):
            ungrouped.append({"path": pr["path"],
                              "why": pr.get("error") or "no audio streams"})
            continue
        key = episode_key(pr.get("name") or "")
        if key is None:
            ungrouped.append({"path": pr["path"],
                              "why": "no episode number found in the filename"})
            continue
        groups.setdefault(key, []).append(pr)

    pairs: list[dict] = []
    unmatched: list[dict] = list(ungrouped)

    for key, members in sorted(groups.items(), key=lambda kv: (kv[0][0] or 0, kv[0][1])):
        detected: dict[str, list[tuple[dict, str]]] = {"jpn": [], "eng": []}
        unknown: list[dict] = []
        for pr in members:
            lang, why = file_language(pr)
            if lang:
                detected[lang].append((pr, why))
            else:
                unknown.append(pr)

        jpn = detected["jpn"][0] if len(detected["jpn"]) == 1 else None
        eng = detected["eng"][0] if len(detected["eng"]) == 1 else None

        # Exactly two files, one identified: the other must be its counterpart.
        # This is the common "Title.mp4 + Title(1).mp4" rip, where only one of
        # the two carries a usable tag.
        if len(members) == 2 and len(unknown) == 1:
            if jpn and not eng:
                eng = (unknown[0], "the only other file for this episode")
            elif eng and not jpn:
                jpn = (unknown[0], "the only other file for this episode")

        if jpn and eng and jpn[0]["path"] != eng[0]["path"]:
            label = _label(jpn[0], key)
            pairs.append({
                "label": label,
                "episode": key[1],
                "jpn": {"path": jpn[0]["path"], "stream": _first_audio(jpn[0])},
                "dub": {"path": eng[0]["path"], "stream": _first_audio(eng[0])},
                "reason": f"episode {key[1]}: {jpn[1]}; {eng[1]}",
            })
            placed = {jpn[0]["path"], eng[0]["path"]}
            for pr in members:
                if pr["path"] not in placed:
                    unmatched.append({
                        "path": pr["path"],
                        "why": f"extra file for episode {key[1]}",
                    })
        else:
            for pr in members:
                lang, _ = file_language(pr)
                if len(members) == 1:
                    why = f"episode {key[1]} has no counterpart file"
                elif not lang:
                    why = (f"episode {key[1]}: cannot tell which language this "
                           f"file is — no stream tag and nothing in the name")
                else:
                    why = (f"episode {key[1]}: {len(members)} files, ambiguous "
                           f"which pair to make")
                unmatched.append({"path": pr["path"], "why": why})

    return {"pairs": pairs, "unmatched": unmatched}


def auto_assign(files: list[dict], mode: str) -> dict:
    """Propose pairings. `mode` is 'separate' or 'multitrack'."""
    if mode == "multitrack":
        return assign_multitrack(files)
    if mode == "separate":
        return assign_separate(files)
    raise ValueError(f"unknown mode {mode!r}")
