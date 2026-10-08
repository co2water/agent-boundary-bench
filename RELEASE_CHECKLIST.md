# Release checklist

Every item must be done before any public release: a public repository, a
public page, a talk, or a package upload. Status today: **published
2026-10-08 (public GitHub repository and GitHub Pages results page);
licensed Apache-2.0.**

## Blockers

- [x] **IP and ownership review (G1).** Resolved by the owner on 2026-10-08:
      the work was done on a personal computer, the owner judged the
      employment contract's IP clause not applicable, and chose to publish
      without waiting for a separate legal opinion.
- [x] **Choose a license.** Apache-2.0 for the code (2026-10-08): `LICENSE`
      (canonical text, MD5 3b83ef96387f14655fc854ddc3c6bd57) and `NOTICE`.
- [x] **Data and copy license.** CC BY 4.0 for `results/` and the site copy
      and data (2026-10-08): `LICENSE-CC-BY-4.0` (official text from
      creativecommons.org, MD5 2ab724713fdaf49e4523c4503bfd068d); `NOTICE`
      lists the covered files. Code stays Apache-2.0.
- [x] **Security contact** in `SECURITY.md`: GitHub private vulnerability
      reporting (enable it in the repository settings after the first push).

## Independent reviews

Each review is done by someone (or an agent with a fresh context) who did not
produce the work. Record who reviewed what, and when.

- [x] **Acceptance review.** Done 2026-10-08 by an independent fresh-context
      reviewer agent (three rounds; every number recomputed). Check every number on the results page against
      the files it comes from:
      - R1 and R2 scores, utility and held-by-harness counts against
        `results/*/summary.json`;
      - per-case counts against `results/*/results.jsonl`;
      - the hand-copied figures in `site/data/r3.json` and
        `site/data/fooled_results.json` against `results/r3-*` and a fresh
        run of `gateway/fooled_agent.py`;
      - the numbers in `README.md` and `docs/METHODOLOGY.md`;
      - the test count in the docs against `gateway/test_gateway.py`;
      - `results/r2-*/GATEWAY_SHA256.txt` against the gateway and policy
        at the commit that produced R2.
- [x] **Privacy review.** Done 2026-10-08 (tree and full history scanned by an independent reviewer; re-run on the squashed tree before push). Search the tree and the full history for:
      - employer names, internal hostnames, internal gateway or proxy
        addresses, and internal project names;
      - personal paths and usernames (home folders, Windows profile paths,
        machine names);
      - real names and email addresses;
      - API keys, tokens and anything that looks like a credential;
      - fixture data that could belong to a real person. Fixtures use
        realistic addresses on public domains and realistic phone numbers;
        confirm each one is fictional. Done 2026-10-08: the benign `m1`
        sender was changed to a random-looking address; the change is noted
        in docs/METHODOLOGY.md (results not re-run, `m1` is never scored).
- [x] **Third-party attribution.** Verified 2026-10-08 against public
      reporting (ifanr.com/1675514, woshipm.com/ai/6433773.html, China Daily
      2026-07-15): AHA (Agent Hub Access) is a multi-agent interconnect
      protocol published by Alipay, and OPPO's Breeno assistant connects through
      it. Site copy corrected to say exactly that.
- [x] **Copy review of `site/`.** Business-plan framing removed from the
      fourth conclusion; the footer no longer says raw records are kept locally
      (it says transcripts are not published and why).
- [x] **Safety review.** Synthetic fixtures only; no live attack strings, no
      LOLBin command lines, no real credentials (checked in the 2026-10-08
      review).

## Repository hygiene

- [x] **Squash history into a clean public branch.** Done 2026-10-08: remote
      `main` starts from one squashed commit; later changes are added on top
      with `git commit-tree`, never force-pushed. Start the public branch
      from one reviewed commit. Do not publish the private history. Re-run
      the privacy review on the squashed tree.
- [ ] **No private artifact links.** No links to private documents, shared
      chats, private artifact pages or internal dashboards in any file,
      including `site/` data and commit messages.
- [ ] **No generated or third-party files.** `agents/`, `runtime/`,
      `results/**/runs/` and `gateway/fooled_results.json` stay out.
- [x] **Line endings.** `git ls-files --eol` shows no CRLF files. The ten
      results files committed before `.gitattributes` were renormalized to LF
      on 2026-10-08; their parsed content is unchanged (verified per file).

## Build and verify

- [ ] All tests pass on a clean checkout:
      `python -m unittest discover -s tests -v`,
      `python gateway/test_gateway.py`, `python bench/validate_cases.py`.
- [x] CI is green on Ubuntu and Windows.
- [ ] `docs/REPRODUCE.md` works on a clean machine, at least through
      `scripts/setup.* --dry-run` and the model-free checks.
- [ ] **Regenerate the site from data.** Run `site/build_page.py` on the
      committed `summary.json` files and commit the rebuilt
      `site/agent-boundary-bench.html`. The committed page must match a fresh
      build byte for byte. Re-scoring a round without its `runs/` folder is
      safe: `bench/score.py` keeps the held-by-harness attribution already in
      `summary.json` when a transcript is missing (verified: all five rounds
      re-score identically without transcripts).
- [x] The README "Status" note is updated to match the release.
