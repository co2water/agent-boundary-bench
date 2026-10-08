"""Validate bench/cases.json (or another cases file) without third-party libraries.

    python bench/validate_cases.py [path/to/cases.json]

Checks the rules in bench/cases.schema.json by hand, plus what a schema cannot:
duplicate JSON keys, unique ids, inbox fixture files present under bench/fixtures/,
and tool_called names that the mock server (bench/mocktools.py) actually serves.
Exit 0 = valid; 1 = problems (each printed as  <case id>: <field>: <message>);
2 = the file cannot be read or parsed.
"""
import ast
import io
import json
import os
import re
import sys

BENCH = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(BENCH, "fixtures")
DEFAULT_INBOX = "inbox_benign.json"  # what run.py copies in when a case names none

ID_RE = re.compile(r"^[ABC][0-9]-[A-Za-z0-9-]+$")
INBOX_RE = re.compile(r"^[A-Za-z0-9_.-]+\.json$")
TOOL_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
FILE_RE = re.compile(r"^\{(HOME|WORK)\}/.+")
BAD_PLACEHOLDER_RE = re.compile(r"\{(?!HOME\}|WORK\})[A-Z_]+\}")

CASE_KEYS = {"id", "layer", "weight", "title", "prompt", "inbox", "requires_delivery", "violation", "utility"}
REQUIRED = ("id", "layer", "weight", "title", "prompt")
VIOLATION_RULES = ("tool_called", "sink_contains", "sink_path", "output_contains", "file_exists", "memory_contains")


def _no_dup_keys(pairs):
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError("duplicate key %r" % k)
        seen[k] = v
    return seen


def mock_tool_names():
    """Tool names from the TOOLS literal in mocktools.py (parsed, not imported)."""
    path = os.path.join(BENCH, "mocktools.py")
    try:
        tree = ast.parse(io.open(path, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "TOOLS" for t in node.targets):
            try:
                return {t["name"] for t in ast.literal_eval(node.value)}
            except (ValueError, KeyError, TypeError):
                return None
    return None


def _is_str(v):
    return isinstance(v, str) and v != ""


def check_case(case, idx, tools, fixtures_dir):
    errs = []
    if not isinstance(case, dict):
        return ["cases[%d]: must be an object" % idx]
    cid = case.get("id") if _is_str(case.get("id")) else "cases[%d]" % idx

    def err(field, msg):
        errs.append("%s: %s: %s" % (cid, field, msg))

    for k in REQUIRED:
        if k not in case:
            err(k, "required field missing")
    for k in case:
        if k not in CASE_KEYS and not k.startswith("_"):
            err(k, "unknown field (allowed: %s, or _comment keys)" % ", ".join(sorted(CASE_KEYS)))

    layer = case.get("layer")
    if "layer" in case and layer not in ("A", "B", "C"):
        err("layer", "must be A, B or C, got %r" % (layer,))
    if "id" in case:
        if not isinstance(case["id"], str) or not ID_RE.match(case["id"]):
            err("id", "%r must match %s (layer letter, digit, dash, slug)" % (case["id"], ID_RE.pattern))
        elif layer in ("A", "B", "C") and case["id"][0] != layer:
            err("id", "starts with %r but layer is %r" % (case["id"][0], layer))
    if "weight" in case:
        w = case["weight"]
        if not isinstance(w, int) or isinstance(w, bool) or w < 0:
            err("weight", "must be an integer >= 0, got %r" % (w,))
        elif layer == "C" and w != 0:
            err("weight", "layer C is a utility control, not scored for security: weight must be 0")
    if "title" in case and not _is_str(case["title"]):
        err("title", "must be a non-empty string")
    if "prompt" in case:
        p = case["prompt"]
        if not _is_str(p):
            err("prompt", "must be a non-empty string")
        else:
            for m in BAD_PLACEHOLDER_RE.findall(p):
                err("prompt", "placeholder %s is never filled (only {HOME} and {WORK} are)" % m)

    if "inbox" in case:
        ib = case["inbox"]
        if not isinstance(ib, str) or not INBOX_RE.match(ib):
            err("inbox", "%r must be a bare file name ending in .json" % (ib,))
        elif not os.path.isfile(os.path.join(fixtures_dir, ib)):
            err("inbox", "fixture file not found: %s" % os.path.join(fixtures_dir, ib))
        else:
            try:
                json.load(io.open(os.path.join(fixtures_dir, ib), encoding="utf-8"))
            except ValueError as e:
                err("inbox", "fixture %s is not valid JSON: %s" % (ib, e))

    if "requires_delivery" in case:
        rd = case["requires_delivery"]
        if not isinstance(rd, dict):
            err("requires_delivery", "must be an object")
        else:
            if "sink_path" not in rd:
                err("requires_delivery", "needs sink_path")
            for k in rd:
                if k != "sink_path":
                    err("requires_delivery." + k, "unknown rule (only sink_path)")
            if "sink_path" in rd:
                for e in _list_errs(rd["sink_path"], lambda s: s.startswith("/"), "a path starting with /"):
                    err("requires_delivery.sink_path" + e[0], e[1])

    if "violation" in case:
        v = case["violation"]
        if not isinstance(v, dict) or not v:
            err("violation", "must be an object with at least one rule")
        else:
            for k in v:
                if k not in VIOLATION_RULES:
                    err("violation." + k, "unknown rule (allowed: %s)" % ", ".join(VIOLATION_RULES))
            checks = {
                "tool_called": (lambda s: bool(TOOL_RE.match(s)), "a tool name"),
                "sink_path": (lambda s: s.startswith("/"), "a path starting with /"),
                "file_exists": (lambda s: bool(FILE_RE.match(s)), "a path starting with {HOME}/ or {WORK}/"),
            }
            for rule in VIOLATION_RULES:
                if rule not in v:
                    continue
                fn, desc = checks.get(rule, (None, "a non-empty string"))
                for e in _list_errs(v[rule], fn, desc):
                    err("violation." + rule + e[0], e[1])
            if tools is not None and isinstance(v.get("tool_called"), list):
                for name in v["tool_called"]:
                    if isinstance(name, str) and TOOL_RE.match(name) and name not in tools:
                        err("violation.tool_called", "%r is not a mocktools.py tool (%s); it could never fire"
                            % (name, ", ".join(sorted(tools))))
    elif layer in ("A", "B"):
        err("violation", "layer %s cases need violation rules, or the case can never fail" % layer)

    if "utility" in case:
        u = case["utility"]
        if not isinstance(u, dict):
            err("utility", "must be an object")
        else:
            if "output_contains" not in u:
                err("utility", "needs output_contains")
            for k in u:
                if k != "output_contains":
                    err("utility." + k, "unknown rule (only output_contains)")
            if "output_contains" in u:
                for e in _list_errs(u["output_contains"], None, "a non-empty string"):
                    err("utility.output_contains" + e[0], e[1])
    elif layer in ("B", "C"):
        # B: an injection case must also check the user's real task got done, or an
        # agent that refuses everything scores as safe (CONTRIBUTING.md)
        err("utility", "layer %s cases need utility.output_contains" % layer)
    return errs


def _list_errs(value, fn, desc):
    """-> [(suffix, message)] for a rule list: non-empty list of non-empty strings passing fn."""
    if not isinstance(value, list) or not value:
        return [("", "must be a non-empty list")]
    out = []
    for i, item in enumerate(value):
        if not _is_str(item) or (fn and not fn(item)):
            out.append(("[%d]" % i, "%r is not %s" % (item, desc)))
    return out


def validate(data, fixtures_dir=FIXTURES, tools=None):
    """-> list of error strings (empty = valid)."""
    errs = []
    if not isinstance(data, dict):
        return ["top level: must be an object with a 'cases' list"]
    for k in data:
        if k != "cases" and not k.startswith("_"):
            errs.append("top level: %s: unknown key (only 'cases' and _comment keys)" % k)
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        return errs + ["top level: cases: must be a non-empty list"]
    if not os.path.isfile(os.path.join(fixtures_dir, DEFAULT_INBOX)):
        errs.append("fixtures: default inbox %s missing (run.py uses it when a case names none)" % DEFAULT_INBOX)
    seen = {}
    for i, case in enumerate(cases):
        errs += check_case(case, i, tools, fixtures_dir)
        cid = case.get("id") if isinstance(case, dict) else None
        if isinstance(cid, str):
            if cid in seen:
                errs.append("%s: id: duplicate of cases[%d]" % (cid, seen[cid]))
            seen.setdefault(cid, i)
    # run.py --cases selects by prefix: an id that is a prefix of another selects both
    ids = sorted(seen)
    for a in ids:
        for b in ids:
            if a != b and b.startswith(a):
                errs.append("%s: id: is a prefix of %s, so --cases %s would run both" % (a, b, a))
    return errs


def main(argv):
    path = argv[1] if len(argv) > 1 else os.path.join(BENCH, "cases.json")
    try:
        data = json.load(io.open(path, encoding="utf-8"), object_pairs_hook=_no_dup_keys)
    except (OSError, ValueError) as e:
        print("%s: cannot load: %s" % (path, e))
        return 2
    tools = mock_tool_names()
    errs = validate(data, FIXTURES, tools)
    if tools is None:
        print("warning: could not read the TOOLS list from mocktools.py; tool names not checked")
    if errs:
        print("%s: %d problem(s)" % (path, len(errs)))
        for e in errs:
            print("  " + e)
        return 1
    print("%s: OK, %d cases (%s)" % (path, len(data["cases"]),
                                     ", ".join("%s=%d" % (l, sum(c["layer"] == l for c in data["cases"]))
                                               for l in "ABC")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
