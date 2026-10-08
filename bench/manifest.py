"""Run manifest: what produced a results directory.

write_manifest(out_dir, args_dict) adds one entry per bench invocation to
<out_dir>/manifest.json (a JSON list; an earlier manifest is appended to, never
overwritten). An entry records when and where the run happened, the run
arguments, the BENCH_* / ABB_* settings (home/profile prefixes replaced by ~),
hashes of the gateway, its policy and the cases, the git commit, the agent
versions actually installed (npm package versions, Hermes git HEAD and dirty
flag, the Node used for OpenClaw), and the pinned agents.lock.json content.

Secrets are never recorded: any environment variable or argument whose name
contains KEY, TOKEN, SECRET or PASSWORD is dropped, whatever its value.
"""
import datetime
import hashlib
import io
import json
import os
import platform
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = "manifest.json"
LOCKFILE = "agents.lock.json"
HASHED = ("gateway/gateway.py", "gateway/policy.bench.json", "bench/cases.json")
ENV_PREFIXES = ("BENCH_", "ABB_")
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def _is_secret(name):
    up = str(name).upper()
    return any(m in up for m in SECRET_MARKERS)


def _sha256(rel):
    path = os.path.join(ROOT, *rel.split("/"))
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _git(*args):
    try:
        p = subprocess.run(["git"] + list(args), cwd=ROOT, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def _lockfile():
    path = os.path.join(ROOT, LOCKFILE)
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return None
    except ValueError as e:
        return {"error": "agents.lock.json is not valid JSON: %s" % e}


def _scrub(value):
    """Replace the user's home / profile prefix with ~ so a shared manifest names no person."""
    if not isinstance(value, str):
        return value
    out = value
    for prefix in {os.path.expanduser("~"), os.environ.get("USERPROFILE", ""), os.environ.get("HOME", "")}:
        if not prefix or len(prefix) < 3:
            continue
        for p in {prefix, prefix.replace("\\", "/"), prefix.replace("/", "\\")}:
            i = out.lower().find(p.lower())
            while i != -1:
                out = out[:i] + "~" + out[i + len(p):]
                i = out.lower().find(p.lower())
    return out


def _env():
    return {k: _scrub(v) for k, v in sorted(os.environ.items())
            if k.upper().startswith(ENV_PREFIXES) and not _is_secret(k)}


def _npm_version(agent_id, package):
    sys.path.insert(0, os.path.join(ROOT, "bench"))
    try:
        import config
    finally:
        sys.path.pop(0)
    path = os.path.join(config.agents_dir(), agent_id, "node_modules", *package.split("/"), "package.json")
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f).get("version")
    except (OSError, ValueError):
        return None


def _installed():
    """What is actually installed on this machine (best effort), next to the pinned lockfile."""
    sys.path.insert(0, os.path.join(ROOT, "bench"))
    try:
        import config
    finally:
        sys.path.pop(0)
    out = {
        "openclaw": _npm_version("openclaw", "openclaw"),
        "dsh": _npm_version("dsh", "@deepseek-ai/dsh"),
    }
    # Hermes is a git install: <repo>/venv/{Scripts,bin}/hermes[.exe] -> <repo>
    repo = os.path.dirname(os.path.dirname(os.path.dirname(config.hermes_exe())))
    head = dirty = None
    if os.path.isdir(os.path.join(repo, ".git")):
        try:
            h = subprocess.run(["git", "-C", repo, "rev-parse", "--short=8", "HEAD"], capture_output=True, text=True, timeout=10)
            s = subprocess.run(["git", "-C", repo, "status", "--porcelain"], capture_output=True, text=True, timeout=10)
            head = h.stdout.strip() or None if h.returncode == 0 else None
            dirty = bool(s.stdout.strip()) if s.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            pass
    out["hermes"] = {"git_head": head, "git_dirty": dirty}
    node = os.path.join(config.node_dir(), "node.exe" if config.WINDOWS else "node")
    try:
        p = subprocess.run([node if os.path.isfile(node) else "node", "-v"], capture_output=True, text=True, timeout=10)
        out["node_for_openclaw"] = p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        out["node_for_openclaw"] = None
    return out


def _clean_args(args_dict):
    out = {}
    for k, v in (args_dict or {}).items():
        if _is_secret(k):
            continue
        v = v if isinstance(v, (str, int, float, bool, type(None), list)) else str(v)
        out[k] = _scrub(v)  # e.g. --out under the user's profile
    return out


def build_entry(args_dict):
    args = _clean_args(args_dict)
    agents = args.get("agents")
    if isinstance(agents, str):
        agents = [x for x in agents.split(",") if x]
    # dirty = uncommitted changes to the code/config; results/ is the run's own output
    status = _git("status", "--porcelain", "--", ".", ":(exclude)results")
    commit = _git("rev-parse", "HEAD")
    commit_source = "git" if commit else None
    if not commit and os.environ.get("ABB_GIT_COMMIT"):  # e.g. a Docker image built without .git
        commit, commit_source = os.environ["ABB_GIT_COMMIT"], "ABB_GIT_COMMIT"
    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "python": platform.python_version(),
        "bench_model": os.environ.get("BENCH_MODEL") or "deepseek-v4-flash (default)",
        "bench_mode": os.environ.get("BENCH_MODE") or "default",
        "agents": agents,
        "cases": args.get("cases"),
        "reps": args.get("reps"),
        "timeout": args.get("timeout"),
        "args": args,
        "env": _env(),
        "sha256": {rel: _sha256(rel) for rel in HASHED},
        "git_commit": commit,
        "git_commit_source": commit_source,
        "git_dirty": None if status is None else bool(status),
        "agents_installed": _installed(),
        "agents_lock": _lockfile(),
    }


def write_manifest(out_dir, args_dict):
    """Append this invocation's entry to <out_dir>/manifest.json; return the entry."""
    entry = build_entry(args_dict)
    path = os.path.join(out_dir, MANIFEST)
    entries = []
    if os.path.isfile(path):
        try:
            with io.open(path, encoding="utf-8") as f:
                old = json.load(f)
            entries = old if isinstance(old, list) else [old]
        except (OSError, ValueError):
            # keep the unreadable file for inspection instead of silently dropping it
            os.replace(path, path + ".corrupt")
    entries.append(entry)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return entry


if __name__ == "__main__":  # print the entry a run would record, without writing it
    json.dump(build_entry({}), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
