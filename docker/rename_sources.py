#!/usr/bin/env python3
"""Rename source files to include language suffix.

Before:  Episode 313 Title.mp4  +  Episode 313 Title(1).mp4
After:   Episode 313 Title.eng.mp4  +  Episode 313 Title.jpn.mp4

Run from the directory containing the source files:
    python3 rename_sources.py [directory]  (default: current directory)

Use --dry-run to preview without making changes.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

MEDIA_EXTS = {".mkv", ".mp4", ".avi", ".m4v"}


def get_audio_lang(path: Path) -> str | None:
    """Return the language tag of the first audio stream, or None."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", str(path)],
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    data = json.loads(result.stdout)
    for s in data.get("streams", []):
        if s.get("codec_type") == "audio":
            return s.get("tags", {}).get("language") or None
    return None


def strip_variant_suffix(stem: str) -> str:
    """Remove trailing (1), (2), etc. from filename stem."""
    return re.sub(r"\s*\(\d+\)\s*$", "", stem).rstrip()


def extract_episode_number(name: str) -> int | None:
    m = re.search(r"[Ee]pisode\s+(\d+)", name)
    return int(m.group(1)) if m else None


def already_has_lang_suffix(path: Path) -> bool:
    suffixes = path.suffixes
    if len(suffixes) >= 2:
        lang = suffixes[-2].lstrip(".").lower()
        return lang in ("jpn", "jap", "eng", "dub")
    return False


def rename_files(directory: Path, dry_run: bool) -> None:
    files = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS
        and not already_has_lang_suffix(p)
    )

    if not files:
        print("No files need renaming.")
        return

    # Group by episode number, then by base name (without variant suffix).
    # Key: (episode_number, clean_base_name)  → list of paths
    from collections import defaultdict
    groups: dict[tuple, list[Path]] = defaultdict(list)
    ungrouped: list[Path] = []

    for p in files:
        epnum = extract_episode_number(p.name)
        if epnum is not None:
            base = strip_variant_suffix(p.stem)
            groups[(epnum, base)].append(p)
        else:
            ungrouped.append(p)

    renames: list[tuple[Path, Path]] = []  # (old, new)
    warnings: list[str] = []

    for (epnum, base), group in sorted(groups.items()):
        ext = group[0].suffix  # assume all files in a group share the extension

        if len(group) == 1:
            # Only one file for this episode — use its stream tag.
            p = group[0]
            lang = get_audio_lang(p)
            if lang in ("jpn", "jap"):
                suffix_lang = "jpn"
            elif lang in ("eng",):
                suffix_lang = "eng"
            else:
                warnings.append(f"  WARNING: {p.name} has unknown lang tag {lang!r} — skipping")
                continue
            new_name = f"{base}.{suffix_lang}{ext}"
            renames.append((p, p.parent / new_name))

        elif len(group) == 2:
            a, b = sorted(group, key=lambda p: p.name)
            lang_a = get_audio_lang(a)
            lang_b = get_audio_lang(b)

            # Both have distinct, known tags — use them directly.
            if lang_a in ("jpn", "jap") and lang_b == "eng":
                renames.append((a, a.parent / f"{base}.jpn{ext}"))
                renames.append((b, b.parent / f"{base}.eng{ext}"))
            elif lang_a == "eng" and lang_b in ("jpn", "jap"):
                renames.append((a, a.parent / f"{base}.eng{ext}"))
                renames.append((b, b.parent / f"{base}.jpn{ext}"))
            else:
                # Tags ambiguous or identical — fall back to (N) = jpn convention.
                # The file whose stem ends with (N) is treated as Japanese.
                has_variant = [p for p in group if re.search(r"\(\d+\)\s*$", p.stem)]
                no_variant  = [p for p in group if not re.search(r"\(\d+\)\s*$", p.stem)]
                if has_variant and no_variant:
                    warnings.append(
                        f"  NOTE: ep{epnum} both tagged {lang_a!r}/{lang_b!r} — "
                        f"using (N)=jpn convention"
                    )
                    renames.append((has_variant[0], has_variant[0].parent / f"{base}.jpn{ext}"))
                    renames.append((no_variant[0],  no_variant[0].parent  / f"{base}.eng{ext}"))
                else:
                    warnings.append(
                        f"  WARNING: ep{epnum} — cannot determine language, skipping: "
                        + ", ".join(p.name for p in group)
                    )
        else:
            warnings.append(
                f"  WARNING: ep{epnum} has {len(group)} files — skipping: "
                + ", ".join(p.name for p in group)
            )

    for p in ungrouped:
        warnings.append(f"  NOTE: no episode number found in {p.name!r} — skipping")

    # Print plan
    print(f"{'DRY RUN — ' if dry_run else ''}Renaming {len(renames)} file(s):\n")
    for old, new in renames:
        print(f"  {old.name}")
        print(f"    → {new.name}")

    for w in warnings:
        print(w)

    if dry_run:
        print("\nDry run complete. Run without --dry-run to apply.")
        return

    if not renames:
        return

    # Two-phase rename to avoid collisions (a→b, b→a case).
    # Phase 1: rename all to a temp name.
    tmps: list[tuple[Path, Path]] = []
    for old, new in renames:
        tmp = old.parent / (old.name + ".renaming_tmp")
        old.rename(tmp)
        tmps.append((tmp, new))

    # Phase 2: rename from temp to final.
    for tmp, new in tmps:
        tmp.rename(new)

    print(f"\nDone. {len(renames)} file(s) renamed.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("directory", nargs="?", default=".",
                    help="Directory containing source files (default: current directory)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be renamed without making changes")
    args = ap.parse_args()

    d = Path(args.directory).resolve()
    if not d.is_dir():
        print(f"ERROR: not a directory: {d}", file=sys.stderr)
        sys.exit(1)

    rename_files(d, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
