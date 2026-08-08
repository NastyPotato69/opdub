#!/usr/bin/env bash
#
# opdub installer.
#
# Sets up a Python environment and ffmpeg without needing root. Everything it
# installs is recorded in a manifest so uninstall.sh can remove exactly what
# was added and nothing else.
#
#   ./install.sh                 # detect what is missing, install only that
#   ./install.sh --verify        # ...then run the test suite and the scorer
#   ./install.sh --dry-run       # print the plan, change nothing
#   ./install.sh --help
#
set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
MANIFEST="$REPO/.opdub-install.manifest"

PREFIX="${OPDUB_PREFIX:-$HOME/.local}"
VENV="${OPDUB_VENV:-$REPO/.venv}"
PYTHON_VERSION="3.11"

DRY_RUN=0
FORCE=0
VERIFY=0
WANT_DEV=1
SKIP_FFMPEG=0
SKIP_PYTHON=0

UV_INSTALL_URL="https://astral.sh/uv/install.sh"
FFMPEG_BASE="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"

# ---------------------------------------------------------------- output ----

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

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '    %swould run:%s %s\n' "$DIM" "$Z" "$*"
  else
    "$@"
  fi
}

usage() {
  cat <<EOF
${B}opdub installer${Z}

  ./install.sh [options]

Options:
  --prefix DIR     where to put ffmpeg/uv binaries   (default: $PREFIX)
  --venv DIR       where to create the virtualenv    (default: $VENV)
  --skip-ffmpeg    do not install ffmpeg even if missing
  --skip-python    do not create the virtualenv
  --no-dev         skip pytest (installed by default so --verify works)
  --force          reinstall components that are already present
  --verify         run the test suite and the fixture scorer afterwards
  --dry-run        print what would happen, change nothing
  -h, --help       this message

Environment:
  OPDUB_PREFIX, OPDUB_VENV  same as --prefix / --venv

Nothing is installed system-wide and root is never required. What gets
installed is recorded in .opdub-install.manifest; ./uninstall.sh reads that
file and removes only those items.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)      PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
    --venv)        VENV="${2:?--venv needs a directory}"; shift 2 ;;
    --skip-ffmpeg) SKIP_FFMPEG=1; shift ;;
    --skip-python) SKIP_PYTHON=1; shift ;;
    --no-dev)      WANT_DEV=0; shift ;;
    --force)       FORCE=1; shift ;;
    --verify)      VERIFY=1; shift ;;
    --dry-run)     DRY_RUN=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             die "unknown option: $1 (try --help)" ;;
  esac
done

# -------------------------------------------------------------- manifest ----

manifest_add() {
  # manifest_add <kind> <value>
  [ "$DRY_RUN" = 1 ] && return 0
  mkdir -p "$(dirname "$MANIFEST")"
  if [ ! -f "$MANIFEST" ]; then
    {
      echo "# opdub install manifest"
      echo "# Written by install.sh on $(date -u '+%Y-%m-%dT%H:%M:%SZ')."
      echo "# uninstall.sh removes ONLY what is listed here."
    } > "$MANIFEST"
  fi
  grep -qxF "$1=$2" "$MANIFEST" 2>/dev/null || echo "$1=$2" >> "$MANIFEST"
}

# ---------------------------------------------------------------- checks ----

have() { command -v "$1" >/dev/null 2>&1; }

# Return 0 if the given interpreter is >= 3.11 and can build a venv.
python_usable() {
  local py="$1"
  "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null \
    && "$py" -m venv --help >/dev/null 2>&1
}

detect_platform() {
  local os arch
  os="$(uname -s)"
  arch="$(uname -m)"
  case "$os" in
    Linux) ;;
    Darwin) echo "darwin"; return 0 ;;
    *) echo "unsupported"; return 0 ;;
  esac
  case "$arch" in
    x86_64|amd64)  echo "linux64" ;;
    aarch64|arm64) echo "linuxarm64" ;;
    *)             echo "unsupported" ;;
  esac
}

download() {
  # download <url> <dest>
  if have curl; then
    run curl -fsSL --retry 3 -o "$2" "$1"
  elif have wget; then
    run wget -q -O "$2" "$1"
  else
    die "need curl or wget to download $1"
  fi
}

# ------------------------------------------------------------------ start ---

printf '\n%sopdub installer%s\n' "$B" "$Z"
info "repo:   $REPO"
info "venv:   $VENV"
info "prefix: $PREFIX"
[ "$DRY_RUN" = 1 ] && warn "dry run — nothing will be changed"
echo

[ -f "$REPO/requirements.txt" ] || die "requirements.txt not found — run this from the opdub checkout"

PLATFORM="$(detect_platform)"
run mkdir -p "$PREFIX/bin"

# ------------------------------------------------------------- 1. ffmpeg ----

step "ffmpeg / ffprobe"

if [ "$SKIP_FFMPEG" = 1 ]; then
  info "skipped (--skip-ffmpeg)"
elif have ffmpeg && have ffprobe && [ "$FORCE" = 0 ]; then
  ok "already installed: $(command -v ffmpeg)"
  info "$(ffmpeg -version 2>/dev/null | head -1)"
elif [ "$PLATFORM" = "darwin" ]; then
  warn "on macOS this script does not download binaries."
  info "install it yourself with:  brew install ffmpeg"
  info "then re-run ./install.sh"
elif [ "$PLATFORM" = "unsupported" ]; then
  warn "no static build available for $(uname -s)/$(uname -m)."
  info "install ffmpeg with your package manager, then re-run ./install.sh"
else
  info "not found — fetching a static build ($PLATFORM)"
  TMPD="$(mktemp -d)"
  trap 'rm -rf "$TMPD"' EXIT
  TARBALL="ffmpeg-master-latest-${PLATFORM}-gpl.tar.xz"
  download "$FFMPEG_BASE/$TARBALL" "$TMPD/ff.tar.xz"
  if [ "$DRY_RUN" = 0 ]; then
    tar xf "$TMPD/ff.tar.xz" -C "$TMPD"
    SRCD="$(find "$TMPD" -maxdepth 1 -type d -name 'ffmpeg-*' | head -1)"
    [ -n "$SRCD" ] || die "unexpected archive layout in $TARBALL"
    for b in ffmpeg ffprobe; do
      install -m 0755 "$SRCD/bin/$b" "$PREFIX/bin/$b"
      manifest_add bin "$PREFIX/bin/$b"
      ok "installed $PREFIX/bin/$b"
    done
  else
    info "would install ffmpeg and ffprobe into $PREFIX/bin"
  fi
  rm -rf "$TMPD"
  trap - EXIT
fi

export PATH="$PREFIX/bin:$PATH"
echo

# ------------------------------------------------------------- 2. python ----

step "Python environment"

if [ "$SKIP_PYTHON" = 1 ]; then
  info "skipped (--skip-python)"
else
  if [ -d "$VENV" ] && [ "$FORCE" = 1 ]; then
    warn "removing existing venv (--force)"
    run rm -rf "$VENV"
  fi

  if [ -x "$VENV/bin/python" ] && [ "$FORCE" = 0 ]; then
    ok "venv already exists: $VENV"
  else
    SYS_PY=""
    for cand in python3.13 python3.12 python3.11 python3; do
      if have "$cand" && python_usable "$cand"; then SYS_PY="$cand"; break; fi
    done

    if [ -n "$SYS_PY" ]; then
      info "using system interpreter: $($SYS_PY -V 2>&1)"
      run "$SYS_PY" -m venv "$VENV"
      manifest_add venv "$VENV"
      ok "created $VENV"
    else
      info "no system Python >= $PYTHON_VERSION with venv support — using uv"
      if ! have uv; then
        info "installing uv into $PREFIX/bin"
        if [ "$DRY_RUN" = 0 ]; then
          # The installer honours these and drops uv/uvx into $PREFIX/bin.
          UV_INSTALL_DIR="$PREFIX/bin" CARGO_HOME="$PREFIX" \
            sh -c "curl -LsSf $UV_INSTALL_URL | sh" >/dev/null 2>&1 \
            || die "uv install failed — see $UV_INSTALL_URL"
          for b in uv uvx; do
            [ -x "$PREFIX/bin/$b" ] && manifest_add bin "$PREFIX/bin/$b"
          done
          ok "installed uv"
        else
          info "would install uv from $UV_INSTALL_URL"
        fi
      else
        ok "uv already installed: $(command -v uv)"
      fi
      run uv venv --python "$PYTHON_VERSION" "$VENV"
      manifest_add venv "$VENV"
      manifest_add uv_python "$PYTHON_VERSION"
      ok "created $VENV"
    fi
  fi

  step "Dependencies"
  # ${arr[@]+...} keeps `set -u` happy when no dev packages were requested.
  DEV_PKGS=()
  [ "$WANT_DEV" = 1 ] && DEV_PKGS=(pytest)

  if have uv; then
    run uv pip install --python "$VENV" -q -r "$REPO/requirements.txt" \
      ${DEV_PKGS[@]+"${DEV_PKGS[@]}"}
  else
    run "$VENV/bin/python" -m pip install -q --upgrade pip
    run "$VENV/bin/python" -m pip install -q -r "$REPO/requirements.txt" \
      ${DEV_PKGS[@]+"${DEV_PKGS[@]}"}
  fi
  ok "installed numpy, scipy, fastapi, uvicorn$([ "$WANT_DEV" = 1 ] && echo ', pytest')"
fi
echo

# ------------------------------------------------------------- 3. check -----

step "Checking the install"

FAIL=0
if [ "$DRY_RUN" = 1 ]; then
  info "skipped (dry run)"
else
  for b in ffmpeg ffprobe; do
    if have "$b"; then ok "$b: $(command -v "$b")"
    else warn "$b not on PATH — opdub cannot do any media I/O without it"; FAIL=1; fi
  done

  if [ -x "$VENV/bin/python" ]; then
    if "$VENV/bin/python" -c 'import numpy, scipy, fastapi, uvicorn' 2>/dev/null; then
      ok "python: $("$VENV/bin/python" -V 2>&1) with all dependencies"
    else
      warn "virtualenv is missing dependencies"; FAIL=1
    fi
    if "$VENV/bin/python" -c 'import opdub.server, opdub.plan, opdub.cli' 2>/dev/null; then
      ok "opdub imports cleanly"
    else
      warn "opdub failed to import"; FAIL=1
    fi
  elif [ "$SKIP_PYTHON" = 0 ]; then
    warn "no interpreter at $VENV/bin/python"; FAIL=1
  fi
fi
echo

# ------------------------------------------------------------ 4. verify -----

if [ "$VERIFY" = 1 ] && [ "$DRY_RUN" = 0 ]; then
  step "Running the test suite"
  "$VENV/bin/python" -m pytest "$REPO/tests" -q || FAIL=1
  echo

  if [ -f "$REPO/fixtures/ground_truth.json" ] && [ -d "$REPO/out/edls" ]; then
    step "Scoring the fixtures"
    # Never rewrites fixtures or the scorer; it only grades existing EDLs.
    "$VENV/bin/python" "$REPO/score.py" \
      "$REPO/fixtures/ground_truth.json" --edl-dir "$REPO/out/edls" || FAIL=1
  else
    step "Scoring the fixtures"
    warn "no fixtures/ground_truth.json or out/edls — skipping"
    info "generate them with: $VENV/bin/python make_fixtures.py fixtures --sources 6 --edits 3"
  fi
  echo
fi

# -------------------------------------------------------------- 5. done -----

if [ "$FAIL" != 0 ]; then
  printf '%sInstall finished with problems.%s See the warnings above.\n\n' "$Y" "$Z"
  exit 1
fi

printf '%sDone.%s\n\n' "$G" "$Z"

case ":$PATH:" in
  *":$PREFIX/bin:"*) ;;
  *) printf '%sAdd this to your shell profile:%s\n    export PATH="%s/bin:$PATH"\n\n' \
       "$Y" "$Z" "$PREFIX" ;;
esac

cat <<EOF
${B}Start the web UI:${Z}
    OPDUB_MEDIA=/workspace/onepace:/workspace/onepiece \\
    OPDUB_OUT=/workspace/out \\
        $VENV/bin/uvicorn opdub.server:app --port 8000

  then open http://localhost:8000

${B}Or use the CLI:${Z}
    $VENV/bin/python -m opdub.cli --help

${B}To remove everything this script installed:${Z}
    ./uninstall.sh

EOF
