"""`agentgate demo`: watch agentgate hold a payment until you approve it.

A scripted agent talks MCP to agentgate, which wraps a mock payment server
(demo_server.py). Reading mail goes straight through; the payment waits on
agentgate's local confirmation page until you approve or deny it there.
No API key, no network, synthetic data, and no money moves.

    agentgate demo                  open the confirmation page and wait for you
    agentgate demo --no-approver    no confirmation page: the payment is denied at once
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
PAYMENT = {"merchant": "Sunny Cafe", "amount_twd": 1200, "memo": "coffee beans"}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Agent:
    """The scripted 'agent': a minimal MCP client over the gateway's stdio."""

    def __init__(self, cmd):
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.i = 0
        self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "agentgate-demo", "version": "1"}})

    def call(self, method, params):
        self.i += 1
        self.p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": self.i, "method": method, "params": params}) + "\n").encode())
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("agentgate exited")
            msg = json.loads(line.decode("utf-8"))
            if msg.get("id") == self.i:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message"))
                return msg["result"]

    def tool(self, name, args):
        r = self.call("tools/call", {"name": name, "arguments": args})
        text = " ".join(c.get("text", "") for c in r.get("content", []) if c.get("type") == "text")
        return bool(r.get("isError")), text

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(5)
        except Exception:
            self.p.kill()


def run(argv=None):
    ap = argparse.ArgumentParser(prog="agentgate demo", description=__doc__.split("\n\n")[1])
    ap.add_argument("--no-approver", action="store_true", help="run without the confirmation page (payment denied at once)")
    ap.add_argument("--port", type=int, default=0, help="port for the confirmation page (default: a free one)")
    ap.add_argument("--wait", type=float, default=90, help="seconds to wait for your decision (default 90)")
    ap.add_argument("--no-browser", action="store_true", help="print the page address instead of opening a browser")
    a = ap.parse_args(argv)
    try:
        # gateway messages include Chinese; a pipe (Git Bash, CI) would get the legacy code page
        if sys.stdout.isatty():
            sys.stdout.reconfigure(errors="backslashreplace")
        else:
            sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass

    port = 0 if a.no_approver else (a.port or _free_port())
    cmd = [sys.executable, os.path.join(HERE, "gateway.py"), "--policy", os.path.join(HERE, "policy.assistant.json"),
           "--server", "lifeservices"]
    if port:
        cmd += ["--approve-port", str(port), "--approve-wait", str(a.wait)]
    cmd += ["--", sys.executable, os.path.join(HERE, "demo_server.py")]

    print("agentgate demo: a scripted agent -> agentgate -> a mock payment server.")
    print("Synthetic data only. No API key, nothing leaves this machine, no money moves.\n")
    agent = Agent(cmd)
    try:
        listed = [t["name"] for t in agent.call("tools/list", {}).get("tools", [])]
        print("1. Tools the agent can see:", ", ".join(listed))

        blocked, text = agent.tool("mail_list", {})
        print("\n2. Agent reads the inbox (data class, policy: allow)")
        print("   ->", "BLOCKED: " + text if blocked else "allowed: " + text[:120])

        print("\n3. Agent tries to pay: pay_merchant(merchant=%r, amount_twd=%d, memo=%r)"
              % (PAYMENT["merchant"], PAYMENT["amount_twd"], PAYMENT["memo"]))
        print("   money class, policy: confirm -> agentgate holds the call.")
        if port:
            url = "http://127.0.0.1:%d/" % port
            print("   Open %s and press 批准這一次 (approve once) or 拒絕 (deny)." % url)
            print("   The page shows the real arguments. Waiting up to %d s..." % a.wait)
            headless = sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
            if not (a.no_browser or headless):
                t = threading.Timer(0.8, lambda: webbrowser.open(url))
                t.daemon = True  # a console browser must not keep the demo alive
                t.start()
        else:
            print("   No confirmation page is running (--no-approver), so nobody can approve it.")
        start = time.time()
        blocked, text = agent.tool("pay_merchant", PAYMENT)
        if blocked:
            why = ("you denied it, or nobody approved it in time" if port else "nobody can approve it, so agentgate denied it (fail closed)")
            print("   -> BLOCKED after %.0f s: %s." % (time.time() - start, why))
            print("      What the agent is told:\n      %s" % text)
        else:
            print("   -> APPROVED by you. The mock server answered: %s" % text)
    finally:
        agent.close()
    repo = "https://github.com/co2water/agent-boundary-bench/blob/main/gateway/"
    print("\nWrap your own MCP server the same way: %sREADME.md#wrap-your-own-mcp-server" % repo)
    print("Prototype: not for real payment tools yet. What it does not defend: %sTHREAT_MODEL.md" % repo)
    return 0


if __name__ == "__main__":
    sys.exit(run())
