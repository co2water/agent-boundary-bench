"""Per-agent launch adapters. Each run gets its own state dir; only the model
and the mock MCP server are configured, everything else is the shipped default.

This module is a registry: every agent's launcher lives in its own module under
bench/harnesses/ (NAME + run(ctx, bench_env, prompt, timeout)), found here
automatically. See bench/harnesses/README.md to add one. The public API is
unchanged: run(), memory_text(), mode(), mcp_args(), ADAPTERS and the constants.
"""
import importlib
import io
import os
import pkgutil

if __package__:  # imported as bench.adapters
    harnesses = importlib.import_module(__package__ + ".harnesses")
else:  # bench/ on sys.path, as run.py does it
    import harnesses

_common = importlib.import_module(harnesses.__name__ + "._common")

# re-exported for code that imported them from here
ROOT = _common.ROOT
BENCH = _common.BENCH
PY = _common.PY
NODE24 = _common.NODE24
MODEL = _common.MODEL
OPENROUTER = _common.OPENROUTER
OR_BASE = _common.OR_BASE
OR_PROVIDERS = _common.OR_PROVIDERS
mode = _common.mode
mcp_args = _common.mcp_args
_exec = _common._exec  # a reference only: patch harnesses._common._exec to intercept launches
_base_env = _common._base_env


def _discover():
    """{NAME: module} for every bench/harnesses/*.py that is not private (_x) or a tool
    script (check_x) and defines both NAME and run()."""
    found = {}
    for info in sorted(pkgutil.iter_modules(harnesses.__path__), key=lambda i: i.name):
        if info.ispkg or info.name.startswith(("_", "check_")):
            continue
        mod = importlib.import_module(harnesses.__name__ + "." + info.name)
        name, fn = getattr(mod, "NAME", None), getattr(mod, "run", None)
        if not isinstance(name, str) or not callable(fn):
            continue
        if name in found:
            raise RuntimeError("two harness modules claim NAME=%r: %s and %s"
                               % (name, found[name].__name__, mod.__name__))
        found[name] = mod
    return found


HARNESSES = _discover()
ADAPTERS = {name: mod.run for name, mod in HARNESSES.items()}

HERMES_EXE = HARNESSES["hermes"].HERMES_EXE if "hermes" in HARNESSES else None
if "dsh" in HARNESSES:
    DSH_LOCKDOWN_ROWS = HARNESSES["dsh"].DSH_LOCKDOWN_ROWS
if "openclaw" in HARNESSES:
    OPENCLAW_LOCKDOWN_DENY = HARNESSES["openclaw"].OPENCLAW_LOCKDOWN_DENY


def run(agent, ctx, bench_env, prompt, timeout):
    return ADAPTERS[agent](ctx, bench_env, prompt, timeout)


# Files where an agent keeps state that survives into later sessions.
# Transcripts and session logs are excluded: they naturally quote what the
# agent read, which is not the same as remembering it.
_SKIP_DIRS = {"sessions", "logs", "cache", "audio_cache", "node_modules", "transcripts", "trajectories",
              "pending"}  # pending = staged, awaiting approval: gated, not persisted
_SKIP_EXT = {".jsonl", ".zst", ".zstd", ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite-wal", ".log"}
# per-run config the bench itself writes (bench.yml, run.json5, prompt.txt): each harness
# declares its own in CONFIG_FILES; they carry the prompt, not anything the agent remembered
_SKIP_NAMES = frozenset(f for mod in HARNESSES.values() for f in getattr(mod, "CONFIG_FILES", ()))


def memory_text(ctx):
    chunks = []
    roots = [(ctx["state"], None), (ctx["work"], {"README.md", "2026-09.csv"})]
    for root, skip_names in roots:
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in _SKIP_DIRS]
            at_root = os.path.normpath(d) == os.path.normpath(root)
            for name in files:
                if skip_names and at_root and name in skip_names:
                    continue  # the fixture files themselves, which carry the bait
                if os.path.splitext(name)[1].lower() in _SKIP_EXT or name in _SKIP_NAMES:
                    continue
                path = os.path.join(d, name)
                try:
                    if os.path.getsize(path) > 2_000_000:
                        continue
                    with io.open(path, encoding="utf-8", errors="ignore") as f:
                        chunks.append(f.read())
                except OSError:
                    pass
    return "\n".join(chunks)
