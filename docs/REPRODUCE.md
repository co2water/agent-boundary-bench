# Reproduce

This page tells you how to re-run the R1-R3 rounds with the same agents, runtimes
and models. Every step reads from one file, `agents.lock.json`.

## 1. Lockfile

`agents.lock.json` (in the repo root) records what produced the published results:

| What | Pinned |
|---|---|
| OS / Python | Windows 11, native; Python 3.14 (3.14.4). The bench itself needs 3.11+ |
| OpenClaw | npm `openclaw@2026.9.7`, installed in `agents/openclaw` with `"allowScripts": {"openclaw": true}` |
| DeepSeek Harness | npm `@deepseek-ai/dsh@0.2.0-rc.2`, installed in `agents/dsh` |
| Hermes Agent | 0.21.0 from its official installer (a git install), upstream commit `25d954c2` |
| Node for OpenClaw | portable Node 24.21.0 win-x64, sha256 `158f7685...e541` (OpenClaw needs `>=24.16 <25` or `>=26.1`) |
| R1, R2 model | `deepseek-v4-flash` on the DeepSeek API |
| R3 model | `meta-llama/llama-3.1-8b-instruct` on OpenRouter, provider order Fireworks, Together, DeepInfra, Lambda |

The lockfile also maps each round to its `BENCH_MODE`, its agents and its
`results/` folder. `scripts/npm/<agent>/` holds the exact `package.json` and
`package-lock.json` of each npm install, so the full dependency tree is pinned too.

## 2. Setup

```
# Windows
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 --dry-run   # show the plan
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -WithNode   # do it

# Linux / macOS / Git Bash
scripts/setup.sh --dry-run
scripts/setup.sh                # add --with-node in Git Bash on Windows
```

The scripts do these steps:

- They install OpenClaw and DeepSeek Harness at the pinned versions into
  `$ABB_AGENTS_DIR` (default `agents/`). They use `npm ci` with the recorded
  lockfiles. You can run them again: an agent that is already at its pinned version
  is skipped.
- With `-WithNode` / `--with-node` (Windows only), they download the portable Node
  into `runtime/`. They check its sha256 against the lockfile, and they stop and
  delete the archive on a mismatch. Without the flag, they only print the steps.
- For Hermes, they only print instructions. Install it with its official installer,
  check out commit `25d954c2` in the installed source, reinstall it into its venv,
  and confirm the version is 0.21.0. Your own Hermes config is not used, because
  each run gets a fresh `HERMES_HOME`.

To run on Linux in a container instead, see `docker/README.md` (no Hermes there).

## 3. Environment variables

Paths come from `bench/config.py`. Set a variable only if the default does not
fit your machine:

| Variable | Default | Purpose |
|---|---|---|
| `ABB_SANDBOX_ROOT` | `C:/abx` on Windows, `/tmp/abx` elsewhere | per-run throwaway sandboxes; keep it outside the repo |
| `ABB_AGENTS_DIR` | `<repo>/agents` | where setup installs the npm agents |
| `ABB_NODE_DIR` | `<repo>/runtime/node-v24.21.0-win-x64` | Node put first on PATH for OpenClaw |
| `ABB_HERMES_EXE` | `%LOCALAPPDATA%/hermes/hermes-agent/venv/Scripts/hermes.exe` (Linux: `~/.hermes/hermes-agent/venv/bin/hermes`) | the Hermes executable |

Run settings:

| Variable | Values |
|---|---|
| `BENCH_MODEL` | `deepseek-v4-flash` (default; R1/R2) or `meta-llama/llama-3.1-8b-instruct` (R3). An id with a `/` uses OpenRouter |
| `BENCH_MODE` | `default` (R1), `gate` or `lockdown` (R2), `default` or `lockdown` (R3) |

Keys go in environment variables only, never in a file in the repo:

- `DEEPSEEK_API_KEY` for the DeepSeek models.
- `OPENROUTER_API_KEY` for any `provider/model` id.

On Windows, `bench/run.py` also reads both keys from `HKCU\Environment` if the
current shell started before you set them.

## 4. Run

First, check the gateway with no model and no network:

```
python gateway/test_gateway.py
```

Then run the rounds. Each line is one round (agent x case x rep). Use a new `--out` folder for each one:

```
# R1: factory defaults
BENCH_MODE=default  python bench/run.py --agents openclaw,hermes,dsh --cases all --reps 3 --out results/my-r1
# R2: gateway, then gateway + narrowed built-ins
BENCH_MODE=gate     python bench/run.py --agents openclaw,hermes,dsh --cases all --reps 3 --out results/my-r2-gate
BENCH_MODE=lockdown python bench/run.py --agents openclaw,hermes,dsh --cases all --reps 3 --out results/my-r2-lockdown
# R3: a gullible model on the reference agent
BENCH_MODEL=meta-llama/llama-3.1-8b-instruct BENCH_MODE=default  python bench/run.py --agents refagent --cases all --reps 3 --out results/my-r3-default
BENCH_MODEL=meta-llama/llama-3.1-8b-instruct BENCH_MODE=lockdown python bench/run.py --agents refagent --cases all --reps 3 --out results/my-r3-lockdown
```

PowerShell sets the variables first, for example `$env:BENCH_MODE = "gate"`, and then
runs the same command. Each run calls the model API, so it costs money.

## 5. Score

```
python bench/score.py results/my-r1
```

The score comes only from machine logs: the mock tool calls, the sink, the file
system and canary strings. Compare your scores with the published ones in
`results/<round>/summary.json`. With 3 reps per cell, expect differences of a few points.

## 6. Manifest

Each `bench/run.py` invocation adds one entry to `<out>/manifest.json`. A re-run
into the same folder adds a new entry and keeps the old ones. An entry records:

- an ISO timestamp, the platform and the Python version
- `BENCH_MODEL`, `BENCH_MODE` and the other `BENCH_*` / `ABB_*` settings
- the agents, cases, reps and timeout
- the sha256 of `gateway/gateway.py`, `gateway/policy.bench.json` and `bench/cases.json`
- the git commit and whether the tree has uncommitted changes (with `results/` excluded)
- the full `agents.lock.json`

The manifest drops any variable or argument whose name contains KEY, TOKEN, SECRET
or PASSWORD. To see what a run would record, without writing anything, run
`python bench/manifest.py`.

When you report a result, include `manifest.json`. The R2 and R3 runs used these
gateway and policy files. agentgate 0.3.0 (tag `agentgate-v0.3.0`) changed
`gateway/gateway.py` (version strings, the command line, the demo, and starting
Windows `.cmd` upstreams; the decision code is unchanged), so its hash differs.
0.3.1 also rewrote the block and reason messages in English and Chinese, so the
`reason` field in new gateway logs reads differently from the R2/R3 logs; the
decisions are the same. 0.3.2 changed `gateway/gateway.py` again: it cleans upstream
tool metadata, hash-chains the `--log` (new fields `args_sha256`, `seq`, `prev`,
`hash`, `chain_restart`; `tool`, `verdict` and `reason`, which are all the bench
reads, are unchanged), hard-denies hard-linked files for the file tools and
re-checks paths at execution. The decisions for the bench's cases are the same, and
`gateway/fooled_agent.py` still blocks 10 of 10. 0.3.3 only changed the command
line (`verify-log --help`). For a byte-exact R2/R3 gateway,
check out the public commit before the `agentgate-v0.3.0` tag:

```
ee38e8d9e908b2efa2bc7b5f25d48f67fc2778f1ff41b721d98abf1edf754ddd  gateway/gateway.py
4b0e83fee4bfb6a2745c9d2aba80ad55440250a6eb31dba249d29b574e067de4  gateway/policy.bench.json
```
