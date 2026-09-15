#!/usr/bin/env bash
# Query the Microsoft Learn MCP server over raw JSON-RPC.
#
# The server is declared in .mcp.json, but its tools are not loaded in every session (a
# non-interactive session never gets to approve it). CLAUDE.md section 0 requires the docs check
# regardless, so this wrapper removes the excuse.
#
# Usage:
#   scripts/mslearn.sh search "Agent Framework Python structured output"
#   scripts/mslearn.sh code   "WorkflowBuilder checkpoint" python
#   scripts/mslearn.sh fetch  "https://learn.microsoft.com/agent-framework/workflows/checkpoints"
set -euo pipefail

ENDPOINT="https://learn.microsoft.com/api/mcp"
ACCEPT="application/json, text/event-stream"
export PYTHONIOENCODING=utf-8

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  exit 64
}

[ $# -ge 2 ] || usage
verb="$1"; shift

case "$verb" in
  search) tool="microsoft_docs_search";        args=$(printf '{"query":"%s"}' "$1") ;;
  fetch)  tool="microsoft_docs_fetch";         args=$(printf '{"url":"%s"}' "$1") ;;
  code)
    tool="microsoft_code_sample_search"
    if [ $# -ge 2 ]; then
      args=$(printf '{"query":"%s","language":"%s"}' "$1" "$2")
    else
      args=$(printf '{"query":"%s"}' "$1")
    fi
    ;;
  *) usage ;;
esac

# MCP requires initialize -> notifications/initialized -> tools/call on one session id.
sid=$(curl -sS -X POST "$ENDPOINT" \
  -H "Content-Type: application/json" -H "Accept: $ACCEPT" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"atlas","version":"1.0"}}}' \
  --max-time 45 -D - -o /dev/null | tr -d '\r' | awk 'tolower($1)=="mcp-session-id:"{print $2}')

if [ -z "$sid" ]; then
  echo "error: Microsoft Learn MCP did not return a session id" >&2
  exit 1
fi

curl -sS -X POST "$ENDPOINT" \
  -H "Content-Type: application/json" -H "Accept: $ACCEPT" -H "mcp-session-id: $sid" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' --max-time 30 -o /dev/null

curl -sS -X POST "$ENDPOINT" \
  -H "Content-Type: application/json" -H "Accept: $ACCEPT" -H "mcp-session-id: $sid" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" \
  --max-time 120 \
| sed -n 's/^data: //p' \
| python -c '
import json, sys

raw = sys.stdin.read().strip()
if not raw:
    print("error: empty response from Microsoft Learn MCP", file=sys.stderr)
    raise SystemExit(1)

payload = json.loads(raw)
if "error" in payload:
    print("MCP error:", payload["error"], file=sys.stderr)
    raise SystemExit(1)

for chunk in payload.get("result", {}).get("content", []):
    text = chunk.get("text", "")
    try:
        parsed = json.loads(text)
    except Exception:
        print(text)
        continue
    items = parsed.get("results", parsed) if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        print(text)
        continue
    for item in items:
        print("###", item.get("title"))
        print("URL:", item.get("contentUrl") or item.get("url"))
        print(item.get("content") or item.get("code") or "")
        print("---")
'
