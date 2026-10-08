# Harnesses: one module per agent

`bench/adapters.py` is a registry. It imports every module in this folder whose name
does not start with `_` or `check_`, and registers it if it defines `NAME` and
`run()`. `bench/run.py --agents <NAME>` then launches it. Nothing else needs editing
to add an agent.

| Module | Agent |
|---|---|
| `openclaw.py` | OpenClaw (`agent exec`, config in `run.json5`) |
| `hermes.py` | Hermes (`chat -q` one-shot, config in `config.yaml`) |
| `dsh.py` | DeepSeek Harness (`headless --patch bench.yml`) |
| `refagent.py` | the minimal reference agent in `bench/refagent.py` (R3) |
| `_common.py` | shared helpers: `_exec`, `_base_env`, `mode()`, `mcp_args()`, `MODEL`, `OPENROUTER`, `OR_BASE`, `OR_PROVIDERS`, `PY`, `NODE24`, `BENCH`, `config` |
| `check_equivalence.py` | proves the split kept every launch byte-identical to the old monolithic `adapters.py` |

## The contract

```python
NAME = "myagent"                 # the id used in --agents and in results.jsonl
CONFIG_FILES = ("myagent.toml",) # optional: files you write per run, skipped by the memory check

def run(ctx, bench_env, prompt, timeout):
    ...
    return output_text, returncode
```

- **One call = one fresh run.** Write the agent's config into `ctx["state"]`, start
  the agent once, non-interactively, with `prompt` as the single user message, wait
  at most `timeout` seconds, return everything it printed.
- **Return** `(text, rc)`. Use `C._exec(cmd, cwd, env, timeout, stdin_text=None)`: it
  merges stdout and stderr and returns `rc = -9` on timeout, which `score.py` drops
  from the score. An exception you raise is recorded as `ADAPTER ERROR` with `rc = -1`.
- **Configure only the model and the bench MCP server.** Everything else stays the
  agent's shipped default; that is what the benchmark measures. Never write API keys
  into a config file: refer to the env var (`DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`).
- **Call through the module:** `from . import _common as C`, then `C._exec(...)`,
  `C.MODEL`, `C.mcp_args(ctx)`. Do not copy names with `from ._common import _exec`;
  the equivalence check and test doubles replace `C._exec` in one place.
- `bench_env` is `{"BENCH_RUN": <run id>}`. Start from `C._base_env(ctx, bench_env)`
  (your environment + `bench_env`, with `HOME` / `USERPROFILE` pointed at the sandbox
  home) and also put `BENCH_RUN` into the MCP server's own `env` in your config. The
  mock server records whether each call came through the agent's MCP config by that
  variable (`tools_outside_mcp` in the results).

### What `ctx` holds

All paths use `/` and live in a throwaway sandbox outside the repo (`ABB_SANDBOX_ROOT`).

| Key | What it is |
|---|---|
| `home` | the fake user home (holds the bait: `.secrets/`, `Documents/`, `Desktop/`). Point `HOME` here (`_base_env` does). |
| `work` | the workspace: run the agent with this as its cwd / project dir. |
| `state` | empty folder for the agent's own state and the config you write. Files left here are scanned for persisted "memory" (case B4). |
| `mock` | path of the mock MCP server script (`lifeservices.py`); run it as `C.PY <mock>`. |
| `svc` | the services folder; agentgate logs to `svc + "/var/gateway.jsonl"`, the mock to `svc + "/var/tools.jsonl"`. |
| `gate` | path of agentgate (`agentgate.py`, a copy of `gateway/gateway.py`). |
| `policy` | the agentgate policy for the bench (`policy.json`, from `gateway/policy.bench.json`). |
| `logs` | the run's log folder in the results tree (the sink writes `sink.jsonl` here). |
| `sb`, `run_dir` | sandbox root and the run's results folder; harnesses normally do not need them. |

### Honoring `BENCH_MODE`

Read it with `C.mode()`; never cache it at import time.

| Mode | What your harness does |
|---|---|
| `default` | The shipped agent. MCP server = `C.PY` + `C.mcp_args(ctx)` (the raw mock). |
| `gate` | Same, but `C.mcp_args(ctx)` now puts agentgate in front of the mock. Your config code does not change if it always uses `mcp_args`. |
| `lockdown` | `gate`, plus `mcp_args` adds `--builtins --root <work>` so agentgate serves the only file/web tools; your harness must switch off the agent's own shell, file, web, sub-agent and automation tools (see `DSH_LOCKDOWN_ROWS`, `OPENCLAW_LOCKDOWN_DENY`, Hermes `-t mcp-bench,todo`). |

`BENCH_MODEL` (read once into `C.MODEL`) picks the model: a bare id is DeepSeek, an
id with a `/` is OpenRouter (`C.OPENROUTER`, `C.OR_BASE`, and pin backends with
`C.OR_PROVIDERS`). `BENCH_GATED=1` is the optional per-agent hardening shown on the
site (e.g. deny pay/message tools in the agent's own config); honor it if your agent
has such a switch.

## Adding an agent

1. Install the agent outside the repo or under `agents/` (git-ignored). Find its
   launcher with `C.config.npm_bin(...)` or add an env-overridable path to
   `bench/config.py` (owned separately; keep the `ABB_*` pattern).
2. Copy this template to `bench/harnesses/myagent.py`:

```python
"""MyAgent: one-shot run with the model and the bench MCP server configured."""
import io
import json
import os

from . import _common as C

NAME = "myagent"
CONFIG_FILES = ("myagent.json",)


def run(ctx, bench_env, prompt, timeout):
    cfg = os.path.join(ctx["state"], "myagent.json")
    server = {"command": C.PY, "args": C.mcp_args(ctx), "env": {"BENCH_RUN": bench_env["BENCH_RUN"]}}
    conf = {"model": C.MODEL, "mcpServers": {"bench": server}}
    if C.mode() == "lockdown":
        conf["builtinTools"] = []          # whatever turns the agent's own tools off
    with io.open(cfg, "w", encoding="utf-8", newline="\n") as f:
        json.dump(conf, f)
    env = C._base_env(ctx, bench_env)
    env["MYAGENT_HOME"] = ctx["state"]     # keep its state inside the sandbox
    cmd = ["myagent", "--config", cfg, "--print", prompt]
    return C._exec(cmd, ctx["work"], env, timeout)
```

3. Check it registers without running anything:
   `python -c "import sys; sys.path.insert(0, 'bench'); import adapters; print(list(adapters.ADAPTERS))"`
4. Run one case, then a round: `python bench/run.py --agents myagent --cases A1 --reps 1 --out results/try-myagent`.
5. If the agent prints its own refusal strings when a gate stops it, add them to
   `HARNESS_MARKERS` in `bench/score.py` so "held by the harness" is told apart from
   "the model chose to stop".

## Checking a refactor

```
python bench/harnesses/check_equivalence.py
```

This check needs the development history: the public repository starts from a
single squashed commit, so there the script prints `SKIPPED` and exits 0. It
passed byte-identical in all 48 combos (576 launches) before the squash. Point
`--rev` at your own pre-change commit to reuse it for a future refactor.

Loads the monolithic `adapters.py` from git (`--rev`, default the last pre-split
commit) and the current harnesses, replaces `_exec` with a recorder, and launches
every agent for every `BENCH_MODE` x `BENCH_MODEL` x `BENCH_GATED` x case prompt in a
temp dir. It asserts the command, cwd, environment, stdin and every file written to
the state dir are byte-identical, plus `mode()`, `mcp_args()`, `memory_text()` and the
constants. No agent, model or network is touched. Re-run it after editing a harness
on purpose and expect it to fail there: it guards the split, not future changes.
