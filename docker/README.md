# agent-boundary-bench in Docker (Linux)

`docker/Dockerfile` builds a Linux image with Python 3.14, Node 24.21.0 and the two
npm agents pinned in `agents.lock.json` (OpenClaw 2026.9.7 and DeepSeek Harness
0.2.0-rc.2). They are installed by `scripts/setup.sh`, which runs `npm ci` from the
recorded lockfiles in `scripts/npm/<agent>/`, so the whole dependency tree matches
the published runs, not only the top-level versions.

The published rounds (R1-R3) ran on native Windows 11. The image is a second,
independent way to run the bench. Its numbers are not the published numbers.
Compare them with care.

## Build

Run from the repo root. The build context is the repo, and the Dockerfile is in `docker/`:

```
docker build -f docker/Dockerfile -t agent-boundary-bench --build-arg GIT_COMMIT=$(git rev-parse HEAD) .
```

- `GIT_COMMIT` is optional. The image does not contain `.git`, so this build arg is
  how `manifest.json` records the commit (as `ABB_GIT_COMMIT`).
- `NODE_VERSION` (default `24.21.0`) and `PYTHON_VERSION` (default `3.14`) are
  build args. OpenClaw needs Node `>=24.16.0 <25 || >=26.1.0`.
- `docker/Dockerfile.dockerignore` keeps `agents/`, `runtime/`, `results/`, `.git`
  and secret-shaped files (`*.env`, `*.key`, `*.pem`, `.credentials*`) out of the
  build context. BuildKit (the default builder) reads it.

## Run

The default command runs the gateway checks. They need no model, no key and no network:

```
docker run --rm agent-boundary-bench
```

A benchmark round. The key is passed from your shell environment at run time.
`-e NAME` with no value copies the variable from the host, so the key does not
appear on the command line or in the image:

```
export DEEPSEEK_API_KEY=...        # R1/R2 model (deepseek-v4-flash)
mkdir -p results/docker-run
docker run --rm \
  -e DEEPSEEK_API_KEY \
  -e BENCH_MODE=lockdown \
  -v "$PWD/results:/opt/agent-boundary-bench/results" \
  agent-boundary-bench \
  python bench/run.py --agents openclaw,dsh --cases all --reps 3 --out results/docker-run

python bench/score.py results/docker-run
```

For R3 (OpenRouter), pass `-e OPENROUTER_API_KEY -e BENCH_MODEL=meta-llama/llama-3.1-8b-instruct`
and use `--agents refagent`.

Notes:

- The container runs as the unprivileged user `bench` (uid 10001). A bind-mounted
  `results/` folder must be writable by that uid. Alternatively, add `--user "$(id -u):$(id -g)"`.
- Sandboxes are created under `/tmp/abx` (`ABB_SANDBOX_ROOT`) inside the container.
  They are deleted after each run. An archived copy goes to `results/<run>/runs/`.
- The bench needs outbound network access only for the model API. The "outside
  world" sink listens on `127.0.0.1` inside the container.
- Every invocation adds an entry to `results/<run>/manifest.json`. The entry has the
  platform, the settings, the file hashes, the commit and the lockfile. API keys
  are never recorded.

## What the image does not cover

- **Hermes Agent.** Hermes is not installed. It comes from its own installer (a git
  install into a venv, commit `25d954c2` for the published runs), and that
  installer is not scripted here. Leave `hermes` out of `--agents`. If you install
  Hermes in a derived image, set `ABB_HERMES_EXE`. Also check that the Hermes
  adapter's path handling works on Linux, because it was only run on Windows.
- **API keys.** Keys are never baked into the image (no `ENV` or `ARG` holds one).
  Pass them with `-e` at run time. Do not use `--build-arg` for keys, because build
  args are kept in the image history.
- **The portable Windows Node** (`node-v24.21.0-win-x64`). The image uses the
  official Linux Node of the same version instead.
- **Building the results page** (`site/`). Run it on the host.
- **Windows-only behaviour** (for example, reading keys from `HKCU\Environment`, or
  the Windows-specific agent paths). This behaviour is not exercised in the image.
