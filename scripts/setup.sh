#!/usr/bin/env bash
# agent-boundary-bench setup for Linux, macOS and Git Bash on Windows.
#
# Reads agents.lock.json and installs the npm agents (OpenClaw, DeepSeek Harness)
# at the pinned versions into $ABB_AGENTS_DIR (default <repo>/agents). Safe to run
# again: an agent already at the pinned version is left alone.
#
# Hermes Agent and the portable Node are NOT installed automatically; the script
# prints what to do. --with-node downloads the pinned portable Node (Windows only)
# and refuses to unpack it unless its sha256 matches the lockfile.
#
#   scripts/setup.sh [--dry-run] [--with-node]
#
#   --dry-run    print every action, change nothing, download nothing
#   --with-node  (Git Bash on Windows) fetch + verify the portable Node from the lockfile
set -euo pipefail

usage() {
  sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

DRY_RUN=0
WITH_NODE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --with-node) WITH_NODE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCK="$REPO/agents.lock.json"
AGENTS_DIR="${ABB_AGENTS_DIR:-$REPO/agents}"

say()  { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# run CMD...: execute, or only print it in --dry-run
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run]'; printf ' %q' "$@"; printf '\n'
  else
    "$@"
  fi
}

is_windows() {
  case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) return 0 ;; *) return 1 ;; esac
}

# ------------------------------------------------------------------ prerequisites
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || die "Python 3.11+ is required (python3 or python on PATH)"
[ -f "$LOCK" ] || die "lockfile not found: $LOCK"

say "agent-boundary-bench setup"
say "  repo        $REPO"
say "  lockfile    $LOCK"
say "  agents dir  $AGENTS_DIR  (ABB_AGENTS_DIR)"
say "  python      $("$PY" -c 'import platform; print(platform.python_version())')  (lockfile: $("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["platform"]["python"])' "$LOCK"))"
[ "$DRY_RUN" -eq 1 ] && say "  mode        DRY RUN: nothing is changed or downloaded"
say ""

# lockq EXPR: evaluate a Python expression with `lock` bound to the parsed lockfile
lockq() {
  "$PY" -c 'import json, sys
lock = json.load(open(sys.argv[1], encoding="utf-8"))
print(eval(sys.argv[2]))' "$LOCK" "$1"
}

# installed_version DIR PACKAGE: version in DIR/node_modules/PACKAGE/package.json, or empty
installed_version() {
  "$PY" -c 'import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])
except Exception:
    pass' "$1/node_modules/$2/package.json"
}

# node_in_range VERSION: the OpenClaw engine range from the lockfile, >=24.16.0 <25 || >=26.1.0
node_in_range() {
  local major minor _rest
  IFS=. read -r major minor _rest <<< "${1#v}"
  case "$major$minor" in *[!0-9]*|"") return 1 ;; esac
  if [ "$major" -eq 24 ] && [ "$minor" -ge 16 ]; then return 0; fi
  if [ "$major" -eq 26 ] && [ "$minor" -ge 1 ]; then return 0; fi
  if [ "$major" -gt 26 ]; then return 0; fi
  return 1
}

sha256_of() {
  "$PY" -c 'import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
print(h.hexdigest())' "$1"
}

# ------------------------------------------------------------------ portable Node
NODE_VER="$(lockq 'lock["runtimes"]["node_openclaw"]["version"]')"
NODE_URL="$(lockq 'lock["runtimes"]["node_openclaw"]["url"]')"
NODE_SHA="$(lockq 'lock["runtimes"]["node_openclaw"]["sha256"]')"
NODE_ARCHIVE="$(lockq 'lock["runtimes"]["node_openclaw"]["archive"]')"
NODE_RANGE="$(lockq 'lock["runtimes"]["node_openclaw"]["required_range"]')"
NODE_DEFAULT_DIR="$REPO/$(lockq 'lock["runtimes"]["node_openclaw"]["install_dir"]')"
NODE_DIR="${ABB_NODE_DIR:-$NODE_DEFAULT_DIR}"

node_dir_version() {
  local exe=""
  if [ -f "$NODE_DIR/node.exe" ]; then exe="$NODE_DIR/node.exe"
  elif [ -x "$NODE_DIR/node" ]; then exe="$NODE_DIR/node"
  elif [ -x "$NODE_DIR/bin/node" ]; then exe="$NODE_DIR/bin/node"
  fi
  [ -n "$exe" ] && "$exe" -p 'process.versions.node' 2>/dev/null || true
}

say "== Node for OpenClaw (needs $NODE_RANGE; lockfile pins $NODE_VER)"
HAVE_NODE_DIR="$(node_dir_version)"
if [ -n "$HAVE_NODE_DIR" ]; then
  say "  found Node $HAVE_NODE_DIR in $NODE_DIR"
  node_in_range "$HAVE_NODE_DIR" || warn "Node $HAVE_NODE_DIR in $NODE_DIR is outside $NODE_RANGE"
elif [ "$WITH_NODE" -eq 1 ]; then
  is_windows || die "--with-node fetches the win-x64 build from the lockfile; on Linux/macOS install Node $NODE_RANGE yourself (see below)"
  RUNTIME_DIR="$(dirname "$NODE_DEFAULT_DIR")"
  ARCHIVE_PATH="$RUNTIME_DIR/$NODE_ARCHIVE"
  say "  downloading $NODE_URL"
  run mkdir -p "$RUNTIME_DIR"
  run curl -fL --retry 3 -o "$ARCHIVE_PATH" "$NODE_URL"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "[dry-run] verify sha256($NODE_ARCHIVE) == $NODE_SHA, else delete it and stop"
  else
    GOT_SHA="$(sha256_of "$ARCHIVE_PATH")"
    if [ "$GOT_SHA" != "$NODE_SHA" ]; then
      rm -f "$ARCHIVE_PATH"
      die "sha256 mismatch for $NODE_ARCHIVE: got $GOT_SHA, lockfile says $NODE_SHA (archive deleted)"
    fi
    say "  sha256 OK ($NODE_SHA)"
  fi
  run "$PY" -m zipfile -e "$ARCHIVE_PATH" "$RUNTIME_DIR"
  run rm -f "$ARCHIVE_PATH"
  [ -n "${ABB_NODE_DIR:-}" ] && [ "$ABB_NODE_DIR" != "$NODE_DEFAULT_DIR" ] && \
    warn "unpacked to $NODE_DEFAULT_DIR, but ABB_NODE_DIR points to $ABB_NODE_DIR; unset it or point it there"
  NODE_DIR="$NODE_DEFAULT_DIR"
  HAVE_NODE_DIR="$(node_dir_version)"
else
  SYS_NODE="$(node -p 'process.versions.node' 2>/dev/null || true)"
  if [ -n "$SYS_NODE" ] && node_in_range "$SYS_NODE"; then
    say "  no portable Node in $NODE_DIR; system Node $SYS_NODE satisfies $NODE_RANGE, OpenClaw will use it"
  else
    say "  no suitable Node found (portable: none in $NODE_DIR; system: ${SYS_NODE:-none})."
    say "  To reproduce the published runs (Windows):"
    say "    1. Download $NODE_URL"
    say "    2. Check its sha256 is $NODE_SHA"
    say "       (python -c \"import hashlib;print(hashlib.sha256(open('$NODE_ARCHIVE','rb').read()).hexdigest())\")"
    say "    3. Unzip it so that $NODE_DEFAULT_DIR/node.exe exists, or set ABB_NODE_DIR to the unzipped folder"
    say "    or re-run this script with --with-node to do 1-3 with the checksum enforced."
    say "  On Linux/macOS: install Node $NODE_RANGE (e.g. $NODE_VER) and either put it on PATH"
    say "  or set ABB_NODE_DIR to the folder that contains the node binary."
  fi
fi
say ""

# ------------------------------------------------------------------ npm agents
say "== npm agents -> $AGENTS_DIR"
NPM_ROWS="$("$PY" -c 'import json, sys
lock = json.load(open(sys.argv[1], encoding="utf-8"))
for a in lock["agents"]:
    i = a.get("install") or {}
    if i.get("method") != "npm":
        continue
    print("\t".join([a["id"], a["display_name"], a["package"], a["version"], i["dir"],
                     i.get("node") or "system", i.get("npm_lock") or "-",
                     json.dumps(i.get("package_json_extra") or {})]))' "$LOCK")"

# npm_in DIR USE_NODE_DIR ARGS...: run npm in DIR, with the portable Node first on PATH if asked
npm_in() {
  local dir="$1" use_node_dir="$2"; shift 2
  if [ "$DRY_RUN" -eq 1 ]; then
    if [ "$use_node_dir" = 1 ]; then
      printf '[dry-run] (cd %q && PATH=%q:$PATH npm' "$dir" "$NODE_DIR"
    else
      printf '[dry-run] (cd %q && npm' "$dir"
    fi
    printf ' %q' "$@"; printf ')\n'
    return 0
  fi
  (
    cd "$dir"
    if [ "$use_node_dir" = 1 ]; then export PATH="$NODE_DIR:$PATH"; fi
    command -v npm >/dev/null 2>&1 || die "npm not found on PATH"
    npm "$@" < /dev/null
  )
}

FAILED=0
while IFS=$'\t' read -r id name pkg ver dir node npmlock extra; do
  [ -n "$id" ] || continue
  target="$AGENTS_DIR/$dir"
  have="$(installed_version "$target" "$pkg")"
  if [ "$have" = "$ver" ]; then
    say "  ok    $name: $pkg@$ver already in $target"
    continue
  fi
  if [ -n "$have" ]; then
    say "  update $name: found $pkg@$have, lockfile pins $ver"
  else
    say "  install $name: $pkg@$ver -> $target"
  fi

  use_node_dir=0
  if [ "$node" = "node_openclaw" ]; then
    if [ -n "$HAVE_NODE_DIR" ]; then
      use_node_dir=1
    else
      SYS_NODE="$(node -p 'process.versions.node' 2>/dev/null || true)"
      node_in_range "${SYS_NODE:-0.0.0}" || warn "$name needs Node $NODE_RANGE; installing with system Node ${SYS_NODE:-none} may fail (see the Node section above)"
    fi
  fi

  run mkdir -p "$target"
  if [ "$npmlock" != "-" ] && [ -f "$REPO/$npmlock/package-lock.json" ] && [ -f "$REPO/$npmlock/package.json" ]; then
    # exact transitive tree recorded from the published runs
    run cp "$REPO/$npmlock/package.json" "$REPO/$npmlock/package-lock.json" "$target/"
    npm_in "$target" "$use_node_dir" ci --no-audit --no-fund
  else
    if [ "$DRY_RUN" -eq 1 ]; then
      say "[dry-run] write $target/package.json (private, merged with $extra)"
    else
      "$PY" -c 'import json, os, sys
path, name, extra = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
pj = {}
if os.path.isfile(path):
    with open(path, encoding="utf-8") as f:
        pj = json.load(f)
pj.setdefault("name", name)
pj.setdefault("version", "1.0.0")
pj.setdefault("private", True)
for k, v in extra.items():
    if isinstance(v, dict) and isinstance(pj.get(k), dict):
        pj[k].update(v)
    else:
        pj[k] = v
with open(path, "w", encoding="utf-8", newline="\n") as f:
    json.dump(pj, f, indent=2)
    f.write("\n")' "$target/package.json" "$id" "$extra"
    fi
    npm_in "$target" "$use_node_dir" install --save-exact --no-audit --no-fund "$pkg@$ver"
  fi

  if [ "$DRY_RUN" -eq 0 ]; then
    have="$(installed_version "$target" "$pkg")"
    if [ "$have" = "$ver" ]; then
      say "  ok    $name: $pkg@$ver installed"
    else
      warn "$name: expected $pkg@$ver after install, found '${have:-nothing}'"
      FAILED=1
    fi
  fi
done <<< "$NPM_ROWS"
say ""

# ------------------------------------------------------------------ Hermes (manual)
H_VER="$(lockq '[a for a in lock["agents"] if a["id"] == "hermes"][0]["version"]')"
H_COMMIT="$(lockq '[a for a in lock["agents"] if a["id"] == "hermes"][0]["commit"]')"
H_REPO="$(lockq '[a for a in lock["agents"] if a["id"] == "hermes"][0]["repository"]')"
if is_windows; then
  H_DEFAULT="${LOCALAPPDATA:-}/hermes/hermes-agent/venv/Scripts/hermes.exe"
else
  H_DEFAULT="$HOME/.hermes/hermes-agent/venv/bin/hermes"
fi
H_EXE="${ABB_HERMES_EXE:-$H_DEFAULT}"
say "== Hermes Agent $H_VER (commit $H_COMMIT) - manual install"
if [ -f "$H_EXE" ]; then
  H_SRC="$(dirname "$(dirname "$(dirname "$H_EXE")")")"
  H_HEAD="$(git -C "$H_SRC" rev-parse --short=8 HEAD 2>/dev/null || true)"
  say "  found $H_EXE"
  if [ -n "$H_HEAD" ]; then
    H_LOCAL="$(lockq '[a for a in lock["agents"] if a["id"] == "hermes"][0].get("local_commit") or ""')"
    if [ "${H_HEAD:0:${#H_COMMIT}}" = "$H_COMMIT" ]; then
      say "  source checkout at $H_HEAD (matches lockfile)"
    elif [ -n "$H_LOCAL" ] && [ "${H_HEAD:0:${#H_LOCAL}}" = "$H_LOCAL" ]; then
      say "  source checkout at $H_HEAD (the local install R1-R3 used: upstream $H_COMMIT + carried commits)"
    else
      warn "Hermes source checkout is at $H_HEAD, lockfile pins $H_COMMIT"
    fi
  else
    say "  could not read the source commit (no git checkout at $H_SRC)"
  fi
else
  say "  not found at $H_EXE"
fi
say "  To reproduce the published runs:"
say "    1. Install Hermes Agent with its official installer (git install) from $H_REPO"
say "    2. In the installed source checkout: git fetch && git checkout $H_COMMIT,"
say "       then reinstall it into its venv as the upstream README describes"
say "    3. Confirm the version is $H_VER, and set ABB_HERMES_EXE if hermes is not at"
say "       $H_DEFAULT"
say "  Each bench run gives Hermes a fresh HERMES_HOME, so your own Hermes config is not used."
say ""

# ------------------------------------------------------------------ next steps
say "== Next"
say "  Keys are read from the environment only; never put them in a file in this repo:"
say "    export DEEPSEEK_API_KEY=...      # R1/R2 model deepseek-v4-flash"
say "    export OPENROUTER_API_KEY=...    # R3 model meta-llama/llama-3.1-8b-instruct"
say "  Optional paths (bench/config.py): ABB_SANDBOX_ROOT ABB_AGENTS_DIR ABB_NODE_DIR ABB_HERMES_EXE"
say "  See docs/REPRODUCE.md for the run and scoring commands."

if [ "$FAILED" -ne 0 ]; then
  die "one or more agents did not install at the pinned version"
fi
