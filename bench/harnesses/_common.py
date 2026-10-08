"""Helpers shared by every harness module (see README.md in this folder).

Harness modules import this as ``from . import _common as C`` and call through
the module (``C._exec(...)``, ``C.MODEL``), never ``from ._common import _exec``:
the refactor-equivalence check and any test double replace ``C._exec`` in one
place, and a name copied at import time would not see that.
"""
import os
import subprocess
import sys

try:
    import config  # bench/ on sys.path: how run.py imports everything
except ImportError:  # imported as bench.harnesses._common from the repo root
    from .. import config

HERE = os.path.dirname(os.path.abspath(__file__))
# same strings the monolithic adapters.py computed (ROOT/bench, not HERE/..)
ROOT = os.path.dirname(os.path.dirname(HERE))
BENCH = os.path.join(ROOT, "bench")
PY = sys.executable.replace("\\", "/")
NODE24 = config.node_dir()
# BENCH_MODEL selects the model. A bare id (deepseek-v4-flash) = DeepSeek; an id with a
# slash (meta-llama/llama-3.1-8b-instruct) = OpenRouter. Default: the R1/R2 model.
MODEL = os.environ.get("BENCH_MODEL", "deepseek-v4-flash")
OPENROUTER = "/" in MODEL
OR_BASE = "https://openrouter.ai/api/v1"
# OpenRouter fans a model out across backends; some (Groq) reject these tool-call
# formats. Pin to backends that do Llama tool calls cleanly so results aren't backend noise.
OR_PROVIDERS = ["Fireworks", "Together", "DeepInfra", "Lambda"]


def _exec(cmd, cwd, env, timeout, stdin_text=None):
    """Run one agent process; -> (stdout + stderr text, returncode). -9 = timed out."""
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, input=stdin_text, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return (p.stdout or "") + "\n--- stderr ---\n" + (p.stderr or ""), p.returncode
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return out + "\n--- TIMEOUT ---", -9


def mode():
    """default = shipped config; gate = MCP behind agentgate; lockdown = gate + the
    harness's own shell/file/web tools off, agentgate's scoped tools instead."""
    return os.environ.get("BENCH_MODE", "default")


def mcp_args(ctx):
    """Arguments for `PY <args>` that start the bench MCP server for this mode:
    the raw mock in default, the mock behind agentgate in gate / lockdown."""
    if mode() == "default":
        return [ctx["mock"]]
    a = [ctx["gate"], "--policy", ctx["policy"], "--server", "lifeservices", "--log", ctx["svc"] + "/var/gateway.jsonl"]
    if mode() == "lockdown":
        a += ["--builtins", "--root", ctx["work"]]
    return a + ["--", PY, ctx["mock"]]


def _base_env(ctx, bench_env):
    env = dict(os.environ)
    env.update(bench_env)
    # the agent's notion of "home" is the sandbox home
    env["HOME"] = ctx["home"]
    env["USERPROFILE"] = ctx["home"].replace("/", "\\")
    return env
