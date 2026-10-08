"""Mock life-services MCP server for `agentgate demo` (stdio, standard library only).

Same tool names as the policy's "lifeservices" server. Every value is synthetic:
the mail, the contacts and the "payment", which only returns a fake receipt.
Nothing is sent anywhere and no money moves.
"""
import io
import json
import sys
import time

MAIL = [
    {"id": "m1", "from": "billing@sunny-cafe.example", "subject": "Invoice: coffee beans, NT$1,200",
     "body": "Hi, this is a demo invoice for 1 kg of coffee beans. Amount due: NT$1,200. (Synthetic demo data.)"},
]
CONTACTS = [{"name": "Demo Contact", "phone": "+886-900-000-000"}]

TOOLS = [
    {"name": "mail_list", "description": "List recent emails (id, from, subject).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "mail_read", "description": "Read one email body by id.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "contacts_list", "description": "List the user's contacts with phone numbers.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "pay_merchant", "description": "Pay a merchant from the user's wallet. Moves real money.",
     "inputSchema": {"type": "object", "properties": {"merchant": {"type": "string"}, "amount_twd": {"type": "number"},
                                                      "memo": {"type": "string"}},
                     "required": ["merchant", "amount_twd"]}},
    {"name": "send_message", "description": "Send an SMS / chat message from the user's phone number.",
     "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}, "text": {"type": "string"}},
                     "required": ["to", "text"]}},
]


def run_tool(name, args):
    if name == "mail_list":
        return [{k: m[k] for k in ("id", "from", "subject")} for m in MAIL]
    if name == "mail_read":
        return next((m for m in MAIL if m["id"] == args.get("id")), {"error": "no such mail"})
    if name == "contacts_list":
        return CONTACTS
    if name == "pay_merchant":
        return {"status": "paid (demo: no money moved)", "receipt": "DEMO-%d" % int(time.time())}
    if name == "send_message":
        return {"status": "sent (demo: nothing was sent)"}
    raise KeyError(name)


def main():
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    for raw in io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8"):
        raw = raw.strip()
        if not raw:
            continue
        req = json.loads(raw)
        method, id_ = req.get("method"), req.get("id")
        params = req.get("params") or {}
        if id_ is None:
            continue  # notifications
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                      "serverInfo": {"name": "agentgate-demo-lifeservices", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                data = run_tool(params.get("name"), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
            except KeyError:
                result = {"isError": True, "content": [{"type": "text", "text": "unknown tool"}]}
        else:
            out.write(json.dumps({"jsonrpc": "2.0", "id": id_, "error": {"code": -32601, "message": "method not found"}}) + "\n")
            out.flush()
            continue
        out.write(json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}, ensure_ascii=False) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
