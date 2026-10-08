# Security policy

## Reporting a vulnerability in agentgate

Please report security problems in agentgate privately. Do not open a public
issue.

Use GitHub's private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**. The report is visible
only to the maintainer until a fix is published.

Please include:

- the agentgate version or commit;
- the policy file you used;
- the tool call or sequence of calls that gets past a rule, and what you
  expected the gateway to do instead;
- whether it needs a model, or reproduces model-free (a script like
  `gateway/fooled_agent.py` or a check in `gateway/test_gateway.py` is ideal).

Use synthetic data only in reports. Do not send real credentials, real
personal data or working attack payloads against third-party systems.

This is a research prototype maintained on a best-effort basis. There is no
guaranteed response time.

## Scope

In scope:

- `gateway/gateway.py` and the shipped policy files
  (`gateway/policy.assistant.json`, `gateway/policy.bench.json`);
- bypasses of the rules described in
  [gateway/THREAT_MODEL.md](gateway/THREAT_MODEL.md): capability classes,
  unknown-tool handling, the approval page (CSRF, origin and host checks,
  single use), taint and secret_seen, the URL token check, workspace scoping,
  sensitive-path and write rules, and fetch restrictions.

Out of scope:

- the limits that the threat model already lists as not defended, unless you
  show a practical way to close one (DNS rebinding is a known open issue);
- the agents under test (OpenClaw, Hermes Agent, DeepSeek Harness). Report
  their issues to their own projects;
- the benchmark harness in `bench/` and the results page in `site/`. These
  are test tooling that runs on a local machine with synthetic data. Ordinary
  bugs there are welcome as normal issues.
