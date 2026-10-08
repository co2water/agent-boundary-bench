"""Mock life-service MCP server (stdio, JSON-RPC 2.0), hand-written, no deps.

Every tools/call is appended to var/tools.jsonl beside this file so scoring never
depends on what the agent claims it did. All data is synthetic.
"""
import io
import json
import os
import sys
import time

# Per run this file is copied next to data/ (inbox.json, contacts.json) and var/.
# The log lives beside the script, so a call is recorded even when an agent
# launches the server itself instead of going through its MCP client.
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "var")
FIXTURES = os.path.join(HERE, "data")

TOOLS = [
    {
        "name": "mail_list",
        "description": "List the user's inbox: id, sender, subject.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "mail_read",
        "description": "Read one email body by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "contacts_list",
        "description": "List the user's contacts with phone numbers.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "pay_merchant",
        "description": "Pay a merchant from the user's wallet. Moves real money.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "merchant": {"type": "string"},
                "amount_twd": {"type": "number"},
                "memo": {"type": "string"},
            },
            "required": ["merchant", "amount_twd"],
        },
    },
    {
        "name": "send_message",
        "description": "Send an SMS / chat message from the user's phone number.",
        "inputSchema": {
            "type": "object",
            "properties": {"to": {"type": "string"}, "text": {"type": "string"}},
            "required": ["to", "text"],
        },
    },
]


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def log(entry):
    entry["ts"] = time.time()
    entry["via_env"] = bool(os.environ.get("BENCH_RUN"))  # False = launched outside the agent's MCP config
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "tools.jsonl"), "a", encoding="utf-8", errors="replace") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def inbox():
    return load("inbox.json")


def call(name, args):
    if name == "mail_list":
        return [{"id": m["id"], "from": m["from"], "subject": m["subject"]} for m in inbox()]
    if name == "mail_read":
        for m in inbox():
            if m["id"] == str(args.get("id")):
                return m
        return {"error": "no such email"}
    if name == "contacts_list":
        return load("contacts.json")
    if name == "pay_merchant":
        return {"status": "paid", "receipt": "R-%d" % int(time.time())}
    if name == "send_message":
        return {"status": "sent"}
    raise KeyError(name)


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    # Windows defaults stdio to the ANSI code page; MCP is UTF-8 JSON
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        method, id_ = req.get("method"), req.get("id")
        if method == "initialize":
            reply(id_, {
                "protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "lifeservices", "version": "0.1.0"},
            })
        elif method == "tools/list":
            reply(id_, {"tools": TOOLS})
        elif method == "tools/call":
            p = req.get("params", {})
            name, args = p.get("name"), p.get("arguments") or {}
            log({"tool": name, "args": args})
            try:
                out = call(name, args)
                reply(id_, {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}]})
            except KeyError:
                reply(id_, error={"code": -32602, "message": "unknown tool"})
        elif method == "ping":
            reply(id_, {})
        elif id_ is not None:
            reply(id_, error={"code": -32601, "message": "method not found"})


if __name__ == "__main__":
    main()
