#!/usr/bin/env bash
#
# opdub uninstaller.
#
# Reads .opdub-install.manifest and removes only what install.sh put there.
# Anything that was already on the machine beforehand is left alone, because
# it was never recorded in the manifest.
#
# Your media and your output are never touched by default. Renders, EDLs and
# saved setups under out/ are yours; --purge-output deletes them, and even
# then only after asking.
#
#   ./uninstall.sh              # remove what was installed, keep out/
#   ./uninstall.sh --dry-run    # show what would go, delete nothing
#   ./uninstall.sh --purge-output
#   ./uninstall.sh --help
#
set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
MANIFEST="$REPO/.opdub-install.manifest"

DRY_RUN=0
ASSUME_YES=0
PURGE_OUTPUT=0
PURGE_CACHE=0

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'
  Y=$'\033[33m'; C=$'\033[36m'; Z=$'\033[0m'
else
  B=''; DIM=''; R=''; G=''; Y=''; C=''; Z=''
fi

step() { printf '%s==>%s %s%s%s\n' "$C" "$Z" "$B" "$*" "$Z"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s✓%s %s\n' "$G" "$Z" "$*"; }
warn() { printf '    %s!%s %s\n' "$Y" "$Z" "$*"; }
die()  { printf '%sError:%s %s\n' "$R" "$Z" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
${B}opdub uninstaller${Z}

  ./uninstall.sh [options]

Options:
  --purge-output   also delete out/ (EDLs, rendered WAVs, muxed MKVs, setups)
  --purge-cache    also delete out/cache (waveform peaks) but keep the rest
  --dry-run        list what would be removed, delete nothing
  -y, --yes        do not ask for confirmation
  -h, --help       this message

Only entries in .opdub-install.manifest are removed. An ffmpeg or Python that
was already on this machine before you ran install.sh is never touched.

Source media is never deleted under any flag.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --purge-output) PURGE_OUTPUT=1; shift ;;
    --purge-cache)  PURGE_CACHE=1; shift ;;
    --dry-run)      DRY_RUN=1; shift ;;
    -y|--yes)       ASSUME_YES=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *)              die "unknown option: $1 (try --help)" ;;
  esac
done

# The manifest is data on disk, so it is treated as untrusted input. A path is
# only removed if it can be positively identified as the kind of thing it
# claims to be — a location check alone is not enough, because the media
# directories live inside the repo root on this machine and would otherwise
# look like fair game.

in_allowed_location() {
  local p="$1" real
  case "$p" in
    /|/usr|/usr/*|/etc|/etc/*|/bin|/bin/*|/sbin|/sbin/*|/var|/var/*|"$HOME") return 1 ;;
  esac
  real="$(cd -- "$(dirname -- "$p")" 2>/dev/null && pwd -P)/$(basename -- "$p")" || return 1
  case "$real" in
    "$REPO"/*|"$HOME"/*) return 0 ;;
    *) return 1 ;;
  esac
}

# A virtualenv always contains pyvenv.cfg. A directory of episodes does not,
# which is what stops a bad manifest entry from deleting your media.
is_virtualenv() { [ -d "$1" ] && [ -f "$1/pyvenv.cfg" ]; }

# Only the four binaries install.sh ever places, and only as regular files.
is_installed_binary() {
  [ -f "$1" ] || return 1
  case "$(basename -- "$1")" in
    ffmpeg|ffprobe|uv|uvx) return 0 ;;
    *) return 1 ;;
  esac
}

remove() {
  # remove <kind: venv|bin|path> <path> [label]
  local kind="$1" p="$2" label="${3:-}"

  if [ ! -e "$p" ] && [ ! -L "$p" ]; then
    info "${DIM}already gone:${Z} $p"
    return 0
  fi

  if ! in_allowed_location "$p"; then
    warn "refusing to remove $p — outside the repo and your home directory"
    return 0
  fi

  case "$kind" in
    venv)
      if ! is_virtualenv "$p"; then
        warn "refusing to remove $p — not a virtualenv (no pyvenv.cfg)"
        info "if this really is a stale venv, delete it by hand"
        return 0
      fi ;;
    bin)
      if ! is_installed_binary "$p"; then
        warn "refusing to remove $p — not one of the binaries install.sh places"
        return 0
      fi ;;
    path)
      # Built by this script, not read from the manifest.
      ;;
    *) warn "refusing to remove $p — unknown entry type ${kind}"; return 0 ;;
  esac

  if [ "$DRY_RUN" = 1 ]; then
    printf '    %swould remove:%s %s %s\n' "$DIM" "$Z" "$p" "$label"
  else
    rm -rf -- "$p"
    ok "removed $p $label"
  fi
}

confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  [ "$DRY_RUN" = 1 ] && return 0
  printf '%s%s%s [y/N] ' "$Y" "$1" "$Z"
  read -r reply </dev/tty || reply=""
  case "$reply" in [yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

printf '\n%sopdub uninstaller%s\n' "$B" "$Z"
info "repo: $REPO"
[ "$DRY_RUN" = 1 ] && warn "dry run — nothing will be deleted"
echo

# ------------------------------------------------------------- manifest -----

VENVS=(); BINS=(); HAD_UV_PYTHON=0; UV_PYTHON_VERSION=""

if [ -f "$MANIFEST" ]; then
  while IFS='=' read -r kind value; do
    case "$kind" in
      \#*|'') continue ;;
      venv)      VENVS+=("$value") ;;
      bin)       BINS+=("$value") ;;
      uv_python) HAD_UV_PYTHON=1; UV_PYTHON_VERSION="$value" ;;
    esac
  done < "$MANIFEST"
else
  warn "no manifest at $MANIFEST"
  info "either install.sh was never run, or it was run from a different checkout."
  info "Nothing will be removed automatically; see the notes at the end."
fi

TOTAL=$(( ${#VENVS[@]} + ${#BINS[@]} ))
if [ "$TOTAL" = 0 ] && [ "$PURGE_OUTPUT" = 0 ] && [ "$PURGE_CACHE" = 0 ]; then
  printf '\nNothing recorded to remove.\n\n'
  exit 0
fi

if [ "$TOTAL" -gt 0 ]; then
  step "Recorded by install.sh"
  for v in "${VENVS[@]:-}"; do [ -n "$v" ] && info "venv    $v"; done
  for b in "${BINS[@]:-}"; do [ -n "$b" ] && info "binary  $b"; done
  echo
  confirm "Remove these $TOTAL item(s)?" || { echo; info "cancelled"; echo; exit 0; }
  echo

  step "Removing"
  for v in "${VENVS[@]:-}"; do [ -n "$v" ] && remove venv "$v" "(virtualenv)"; done
  for b in "${BINS[@]:-}"; do [ -n "$b" ] && remove bin "$b" "(binary)"; done
  echo
fi

# --------------------------------------------------------------- output -----

if [ "$PURGE_CACHE" = 1 ] && [ "$PURGE_OUTPUT" = 0 ]; then
  step "Cache"
  remove path "$REPO/out/cache" "(waveform peaks — regenerated on demand)"
  echo
fi

if [ "$PURGE_OUTPUT" = 1 ]; then
  step "Output"
  if [ -d "$REPO/out" ]; then
    n_edl=$(find "$REPO/out" -name '*.json' 2>/dev/null | wc -l | tr -d ' ')
    n_mkv=$(find "$REPO/out" \( -name '*.mkv' -o -name '*.wav' \) 2>/dev/null | wc -l | tr -d ' ')
    warn "out/ holds $n_edl JSON file(s) and $n_mkv rendered file(s)."
    info "This includes your EDLs, saved setups and finished episodes."
    if confirm "Really delete $REPO/out entirely?"; then
      remove path "$REPO/out" "(all output)"
    else
      info "kept out/"
    fi
  else
    info "no out/ directory"
  fi
  echo
fi

# ------------------------------------------------------------- manifest -----

if [ -f "$MANIFEST" ] && [ "$TOTAL" -gt 0 ]; then
  remove path "$MANIFEST" "(manifest)"
  echo
fi

# ----------------------------------------------------------------- notes ----

printf '%sDone.%s\n\n' "$G" "$Z"

cat <<EOF
${B}Left alone on purpose:${Z}
  · your source media — never touched by this script under any flag
  · any ffmpeg or Python that was already installed before install.sh ran
EOF

if [ "$PURGE_OUTPUT" = 0 ]; then
  echo "  · $REPO/out — EDLs, renders and saved setups (--purge-output removes it)"
fi

if [ "$HAD_UV_PYTHON" = 1 ]; then
  cat <<EOF

${B}One thing left behind:${Z}
  install.sh used uv to download a managed CPython, which lives in uv's own
  cache outside this repo. To reclaim that space:

      uv python uninstall ${UV_PYTHON_VERSION:-3.11}
      uv cache clean

  Skip it if you use uv for anything else.
EOF
fi

echo
