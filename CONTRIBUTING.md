# Contributing

This repo is a research prototype, licensed under the Apache License 2.0 and
not yet published (see [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)). By
contributing you agree your contribution is licensed under the same terms
(Apache-2.0, section 5).

## Development setup

- Python 3.11 or newer. The bench, the gateway and the tests use the standard
  library only.
- Running real agents needs the agents installed at their pinned versions
  (`agents.lock.json`, installed by `scripts/setup.ps1` or `scripts/setup.sh`)
  and a model key in an environment variable: `DEEPSEEK_API_KEY`, or
  `OPENROUTER_API_KEY` for a `provider/model` id. Full steps, including the
  Docker route, are in [docs/REPRODUCE.md](docs/REPRODUCE.md).
- Machine-specific paths come from environment variables read by
  `bench/config.py`: `ABB_SANDBOX_ROOT`, `ABB_AGENTS_DIR`, `ABB_NODE_DIR`,
  `ABB_HERMES_EXE`. The defaults reproduce the original Windows setup.
- Never commit keys, installed agents (`agents/`), runtimes (`runtime/`) or
  per-run sandboxes (`results/**/runs/`). `.gitignore` covers these.

## Running the tests

None of these call a model or need network access.

```
python -m unittest discover -s tests -v   # scoring rules, aggregation, results consistency
python gateway/test_gateway.py            # agentgate's rules, model-free
python bench/validate_cases.py            # bench/cases.json against bench/cases.schema.json
```

`gateway/test_gateway.py` binds local ports 8765 and 8799. Stop anything else
using them first. CI (`.github/workflows/ci.yml`) runs the same checks and a
site build on Ubuntu and Windows.

## Adding an agent

Each agent is a plugin module in `bench/harnesses/`. See
[bench/harnesses/README.md](bench/harnesses/README.md) for the interface and a
worked example. Also:

- Configure only the model and the mock MCP server. Leave every other setting
  at the factory default, and use the agent's unattended single-command mode.
- Give the agent its own state directory per run. Nothing may carry over
  between runs.
- Add the agent's own denial strings to `HARNESS_MARKERS` in `bench/score.py`,
  so a harness block is not counted as model restraint.
- Pin its version in `agents.lock.json`.
- If it supports `lockdown`, document how its built-in shell, file and web
  tools are turned off.

## Adding a case

1. Add the case to `bench/cases.json`. Give it an id with its layer prefix (A,
   B or C), a weight, a prompt and violation rules that read machine records
   only: `tool_called`, `sink_contains`, `sink_path`, `output_contains`,
   `file_exists`, `memory_contains`. Add `utility` for B and C cases, and
   `requires_delivery` when the bait might not reach the agent.
2. Check it against the schema:
   `python bench/validate_cases.py` (schema: `bench/cases.schema.json`).
3. Add or change fixtures through `bench/gen_fixtures.py`, then re-run it, so
   the fixtures stay reproducible from code.
4. Add a golden run for the new rule types to `tests/` if the scorer needs a
   new rule.
5. A new or changed case starts a new round. Do not mix its results into an
   existing round directory.

### Synthetic-data rule

Every fixture must be fictional. This is not optional.

- Never use real credentials, real account numbers, real API keys or tokens,
  or data about real people.
- Never write live attack strings or LOLBin command lines into fixtures,
  tests or docs. Antivirus tools flag them, and they are not needed: the cases
  test whether an agent acts, not whether a payload works.
- Baits must look like real data, but be fictional. Markers such as
  `CANARY`, "demo bank" or `.example` domains are recognized as honeypots by
  models and inflate held rates. That is why fixtures v1 were discarded.
- The only exfiltration target is the local sink on `127.0.0.1`. Payment and
  messaging tools are mocks that only log.

## Publishing a round

1. Run the round into a new directory:
   `BENCH_MODE=<mode> BENCH_MODEL=<model> python bench/run.py --agents ... --cases all --reps 3 --out results/<round>`.
   `run.py` writes `results/<round>/manifest.json` with the agent and model
   versions.
2. Score it: `python bench/score.py results/<round>`. This writes
   `summary.json`. Score before you delete `runs/`: the held-by-harness count
   reads each run's `output.txt`.
3. For a gateway round, record the SHA-256 of `gateway/gateway.py` and the
   policy file used in `results/<round>/GATEWAY_SHA256.txt`.
4. Commit `results.jsonl`, `summary.json`, `manifest.json` and any hash file.
   Do not commit `runs/`.
5. Update the page copy in `site/` (`narrative.json`, `r2.json`,
   `data/*.json`, Chinese text with `_en` siblings), then rebuild the page:
   `python site/build_page.py results/v2-2026-10-02 gate=results/r2-gate lockdown=results/r2-lockdown`.
   Never edit `site/agent-boundary-bench.html` by hand.
6. Have someone else check every number on the page against the results files
   before the round counts as done. `site/data/r3.json` and
   `site/data/fooled_results.json` are copied by hand, so check them too.

## Commit style

- Subject line: `<area>: <what changed>`, lowercase, no trailing period. The
  area is the top-level folder or topic, for example `bench`, `gateway`,
  `site`, `docs` or `results`.
- Body: what changed and why, wrapped at about 72 characters.
- One logical change per commit. Keep result data and code changes in
  separate commits.
- If an AI assistant wrote part of the change, add a `Co-Authored-By:`
  trailer.
