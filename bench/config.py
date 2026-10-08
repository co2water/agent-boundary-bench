"""Machine-specific paths, overridable by environment variables.

Every location the bench needs outside the repo comes from here, so a run on
another machine (or in Docker / Linux) only sets env vars. Defaults reproduce
the original Windows setup.

  ABB_SANDBOX_ROOT   where per-run sandboxes are created   (default C:/abx on Windows, /tmp/abx elsewhere)
  ABB_AGENTS_DIR     where the agents are installed        (default <repo>/agents)
  ABB_NODE_DIR       Node >= 24.16 used for OpenClaw       (default <repo>/runtime/node-v24.21.0-win-x64)
  ABB_HERMES_EXE     the Hermes executable                 (default %LOCALAPPDATA%/hermes/.../hermes.exe)

Read elsewhere: ABB_GIT_COMMIT (bench/manifest.py) records the source commit when
there is no .git to ask, e.g. inside the Docker image.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "bench")
WINDOWS = sys.platform == "win32"


def _env(name, default):
    v = os.environ.get(name)
    return v if v else default


def sandbox_root():
    return _env("ABB_SANDBOX_ROOT", "C:/abx" if WINDOWS else "/tmp/abx")


def agents_dir():
    return _env("ABB_AGENTS_DIR", os.path.join(ROOT, "agents"))


def node_dir():
    return _env("ABB_NODE_DIR", os.path.join(ROOT, "runtime", "node-v24.21.0-win-x64"))


def hermes_exe():
    default = os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes", "hermes-agent", "venv", "Scripts", "hermes.exe") \
        if WINDOWS else os.path.expanduser("~/.hermes/hermes-agent/venv/bin/hermes")
    return _env("ABB_HERMES_EXE", default)


def npm_bin(agent, name):
    """Path to an npm-installed agent's launcher (agents/<agent>/node_modules/.bin/<name>)."""
    return os.path.join(agents_dir(), agent, "node_modules", ".bin", name + (".cmd" if WINDOWS else ""))
