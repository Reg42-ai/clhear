#!/usr/bin/env bash
# Walkthrough: one text, two organisations, two blueprints.
#
# Needs a configured model (see the README), the API and a worker:
#   export CLHEAR_LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=...
#   export CLHEAR_LOCAL_SOURCES_DIR="$PWD/examples/security-baseline"
#   clhear serve &  clhear worker &
#   examples/security-baseline/run.sh
set -euo pipefail
API="${CLHEAR_API:-http://127.0.0.1:8000}"
post() { curl -sf -X "$1" "$API$2" -H 'content-type: application/json' ${3:+-d "$3"}; }

post POST /v1/sources '{"key": "baseline", "adapter": "local_text", "name": "Example security baseline",
  "kind": "standard", "jurisdiction": "EU", "locator": {"path": "baseline.txt"}}' >/dev/null
echo "== What CLHEAR read (first clauses)"
post POST /v1/sources/baseline/test-fetch | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["clauses"], "clauses"); [print(" ", c["clause_ref"], "|", c["text"][:90]) for c in d["preview"]]'

post POST /v1/scopes '{"name": "security-baseline", "sources": ["baseline"]}' >/dev/null
python3 - "$API" <<'PY'
import json, sys, urllib.request
api = sys.argv[1]
profiles = json.load(open("examples/security-baseline/profiles.json"))
for pid, body in profiles.items():
    req = urllib.request.Request(f"{api}/v1/profiles/{pid}", data=json.dumps(body).encode(), method="PUT",
                                 headers={"content-type": "application/json"})
    print("profile", pid, json.load(urllib.request.urlopen(req))["validation"])
PY

RUN=$(post POST /v1/runs '{"scope": "security-baseline", "profiles": ["payments-startup", "research-lab"]}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
echo "== Run $RUN"
while :; do
  STATUS=$(post GET "/v1/runs/$RUN" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d.get("release_id") or "", d.get("error") or "")')
  case "$STATUS" in succeeded*|failed*) break ;; esac
  sleep 5
done
echo "$STATUS"
RELEASE=$(echo "$STATUS" | awk '{print $2}')

for pid in payments-startup research-lab; do
  echo "== Blueprint for $pid"
  post GET "/v1/releases/$RELEASE/blueprints/$pid" | python3 -c '
import json, sys
b = json.load(sys.stdin)
print("measures:")
for item in b["items"]:
    print(" -", item["name"], "(" + item["basis"] + ")")
print("duties:")
for c in b["coverage"]:
    print(" ", c["state"].ljust(8), c["clause_ref"].ljust(10), c["duty"][:90])
for n in b["not_applicable"]:
    print("  n/a     ", n["clause_ref"].ljust(10), "needs", n["because"][0]["requires"])
print("minimal:", b["minimality"]["minimal"])'
done
