#!/usr/bin/env bash
# Compose production smoke test.
#
# Verifies the documented deployment actually runs: PostgreSQL, a one-shot
# migration that gates the replicas, two orchestrator containers sharing state,
# and the security posture the config claims. Every assertion here failed at
# least once during development — the image had no CLI, no asyncpg, and the
# binding check could not see configured principals — which is why the test
# exists rather than a note saying it should work.
#
# Usage:  ./smoke-test.sh
# CI:     invoked by .github/workflows/ci.yml (compose-smoke)

set -euo pipefail

# Git Bash on Windows rewrites arguments beginning with '/' into Windows
# paths, which turns '/live' into 'C:/Program Files/Git/live'. Disabling
# that conversion is a no-op everywhere else.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

cd "$(dirname "$0")"
COMPOSE="docker compose -f docker-compose.production.yml"

# Credentials for the smoke run only. Named so nobody mistakes them for real.
export POSTGRES_PASSWORD="smoke-test-not-a-real-secret"
export ORCHESTRATOR_POSTGRES_DSN="postgresql://orchestrator:${POSTGRES_PASSWORD}@postgres:5432/orchestrator"
export ORCHESTRATOR_TOKEN_OPS="smoke-ops-token"
export ORCHESTRATOR_TOKEN_PROMETHEUS="smoke-prom-token"
export ORCHESTRATOR_TOKEN_CI="smoke-ci-token"
export ORCHESTRATOR_TOKEN_ADMIN="smoke-admin-token"
# Out of the way of whatever else is on the host. The tests reach Caddy
# over the edge network by container name, so these only matter for a
# human poking at the stack afterwards.
export CADDY_HTTPS_PORT="${CADDY_HTTPS_PORT:-18443}"
export CADDY_HTTP_PORT="${CADDY_HTTP_PORT:-18080}"

FAILURES=0
check() {
  if [ "$2" = "$3" ]; then
    printf '  PASS  %s\n' "$1"
  else
    printf '  FAIL  %s  (expected %s, got %s)\n' "$1" "$3" "$2"
    FAILURES=$((FAILURES + 1))
  fi
}

cleanup() {
  echo
  echo "--- tearing down ---"
  $COMPOSE logs --tail 30 orchestrator-1 2>/dev/null || true
  $COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Start from nothing. Token revocations and rate-limit counters persist in
# the PostgreSQL volume, so a second run against a dirty volume fails on
# credentials the previous run revoked — a confusing failure that says nothing
# about the code.
echo "=== 0. clean slate ==="
$COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
echo "  previous state removed"

echo
echo "=== 0b. configuration renders and validates ==="
$COMPOSE config --quiet
check "compose file is valid" "$?" "0"

echo
echo "=== 1. build ==="
$COMPOSE build orchestrator-1 orchestrator-2 migrate >/dev/null
echo "  built"

echo
echo "=== 2. postgres ==="
$COMPOSE up -d postgres >/dev/null
for _ in $(seq 1 60); do
  state=$(docker inspect --format '{{.State.Health.Status}}' orchestrator-postgres-1 2>/dev/null || echo starting)
  [ "$state" = "healthy" ] && break
  sleep 1
done
check "postgres healthy" "$state" "healthy"

echo
echo "=== 3. one-shot migration gates the replicas ==="
$COMPOSE up migrate
migrate_exit=$(docker inspect --format '{{.State.ExitCode}}' orchestrator-migrate-1)
check "migration exited zero" "$migrate_exit" "0"

applied=$(docker exec orchestrator-postgres-1 psql -U orchestrator -d orchestrator -tAc \
  "SELECT count(*) FROM schema_migrations" | tr -d '[:space:]')
check "migrations recorded in the database" "$([ "$applied" -ge 3 ] && echo yes || echo no)" "yes"

echo
echo "=== 4. two replicas ==="
$COMPOSE up -d orchestrator-1 orchestrator-2 >/dev/null
for _ in $(seq 1 60); do
  one=$(docker inspect --format '{{.State.Health.Status}}' orchestrator-1 2>/dev/null || echo starting)
  two=$(docker inspect --format '{{.State.Health.Status}}' orchestrator-2 2>/dev/null || echo starting)
  [ "$one" = "healthy" ] && [ "$two" = "healthy" ] && break
  sleep 1
done
check "orchestrator-1 healthy" "$one" "healthy"
check "orchestrator-2 healthy" "$two" "healthy"

echo
echo "=== 5. both replicas use PostgreSQL ==="
for c in orchestrator-1 orchestrator-2; do
  backend=$(docker exec "$c" python -c "
import asyncio
from orchestrator.config.loader import load
from orchestrator.platform import Orchestrator
async def m():
    o = await Orchestrator.create(config=load(), connect_mcp=False)
    try: print(type(o.store).__name__)
    finally: await o.close()
asyncio.run(m())
" 2>/dev/null | tail -1)
  check "$c uses PostgresStateStore" "$backend" "PostgresStateStore"
done

echo
echo "=== 6. no SQLite database is created ==="
for c in orchestrator-1 orchestrator-2; do
  found=$(docker exec "$c" sh -c 'find / -name "*.db" -o -name "*.sqlite*" 2>/dev/null | grep -v /proc | head -1' || true)
  check "$c has no sqlite file" "${found:-none}" "none"
done

echo
echo "=== 7. container hardening ==="
uid=$(docker exec orchestrator-1 id -u)
check "runs as non-root" "$uid" "10001"
writable=$(docker exec orchestrator-1 sh -c 'touch /app/x 2>/dev/null && echo yes || echo no')
check "application directory is read-only" "$writable" "no"

echo
echo "=== 8. HTTP surface ==="
http() {
  docker exec -i orchestrator-1 python - "$@" <<'PY'
import sys, urllib.request, urllib.error
path, token, method = sys.argv[1], sys.argv[2], sys.argv[3]
req = urllib.request.Request("http://127.0.0.1:8080" + path, method=method)
if token:
    req.add_header("Authorization", "Bearer " + token)
try:
    print(urllib.request.urlopen(req, None, timeout=30).status)
except urllib.error.HTTPError as e:
    print(e.code)
PY
}
http_on() {
  docker exec -i "$1" python - "$2" "$3" "$4" <<'PY'
import sys, urllib.request, urllib.error
path, token, method = sys.argv[1], sys.argv[2], sys.argv[3]
req = urllib.request.Request("http://127.0.0.1:8080" + path, method=method)
if token:
    req.add_header("Authorization", "Bearer " + token)
try:
    print(urllib.request.urlopen(req, None, timeout=30).status)
except urllib.error.HTTPError as e:
    print(e.code)
PY
}

check "/live is public"            "$(http /live '' GET)"                              "200"
check "/metrics needs a token"     "$(http /metrics '' GET)"                           "401"
check "/metrics rejects read-only" "$(http /metrics "$ORCHESTRATOR_TOKEN_CI" GET)"     "403"
check "/metrics allows admin"      "$(http /metrics "$ORCHESTRATOR_TOKEN_PROMETHEUS" GET)" "200"
check "/health needs admin"        "$(http /health '' GET)"                            "401"

echo
echo "=== 9. execution lifecycle, shared across replicas ==="
EID=$(docker exec -i orchestrator-1 python - <<PY
import json, urllib.request
req = urllib.request.Request("http://127.0.0.1:8080/v1/executions", method="POST")
req.add_header("Authorization", "Bearer ${ORCHESTRATOR_TOKEN_OPS}")
req.add_header("Content-Type", "application/json")
body = json.dumps({"objective": "compose smoke test", "run": False}).encode()
print(json.load(urllib.request.urlopen(req, body, timeout=60))["id"])
PY
)
check "created on replica 1" "$([ -n "$EID" ] && echo yes || echo no)" "yes"

seen=$(docker exec -i orchestrator-2 python - <<PY
import json, urllib.request, urllib.error
req = urllib.request.Request("http://127.0.0.1:8080/v1/executions/${EID}")
req.add_header("Authorization", "Bearer ${ORCHESTRATOR_TOKEN_OPS}")
try:
    print(json.load(urllib.request.urlopen(req, None, timeout=30))["id"])
except urllib.error.HTTPError:
    print("not-found")
PY
)
check "replica 2 sees replica 1's execution" "$seen" "$EID"

indb=$(docker exec orchestrator-postgres-1 psql -U orchestrator -d orchestrator -tAc \
  "SELECT id FROM executions WHERE id='${EID}'" | tr -d '[:space:]')
check "row is in PostgreSQL" "$indb" "$EID"

echo
echo "=== 10. distributed token revocation ==="
# The property the in-process store could not have: revoke on ONE replica and
# the OTHER rejects the credential immediately, with no restart between them.
listed=$(docker exec -i orchestrator-1 python - <<PY
import json, urllib.request
req = urllib.request.Request("http://127.0.0.1:8080/v1/tokens")
req.add_header("Authorization", "Bearer ${ORCHESTRATOR_TOKEN_ADMIN}")
body = urllib.request.urlopen(req, None, timeout=30).read().decode()
tokens = json.loads(body)["tokens"]
leaked = [s for s in ("${ORCHESTRATOR_TOKEN_OPS}", "${ORCHESTRATOR_TOKEN_CI}",
                      "${ORCHESTRATOR_TOKEN_ADMIN}") if s in body]
print(len(tokens), "leak" if leaked else "clean",
      [t["token_id"] for t in tokens if t["principal"] == "ci-status"][0])
PY
)
count=$(echo "$listed" | cut -d' ' -f1)
leak=$(echo "$listed" | cut -d' ' -f2)
ci_token_id=$(echo "$listed" | cut -d' ' -f3)
check "one token per principal" "$count" "4"
check "no raw secret in the listing" "$leak" "clean"

raw=$(docker exec orchestrator-postgres-1 psql -U orchestrator -d orchestrator -tAc \
  "SELECT count(*) FROM api_tokens WHERE lookup_hash = '${ORCHESTRATOR_TOKEN_CI}' OR digest = '${ORCHESTRATOR_TOKEN_CI}'" | tr -d '[:space:]')
check "database stores no raw token" "$raw" "0"

check "replica 1 accepts the CI token" "$(http_on orchestrator-1 /v1/executions "$ORCHESTRATOR_TOKEN_CI" GET)" "200"
check "replica 2 accepts the CI token" "$(http_on orchestrator-2 /v1/executions "$ORCHESTRATOR_TOKEN_CI" GET)" "200"

revoke_body=$(docker exec -i orchestrator-2 python - <<PY
import urllib.request, urllib.error
req = urllib.request.Request(
    "http://127.0.0.1:8080/v1/tokens/${ci_token_id}/revoke?reason=smoke", method="POST")
req.add_header("Authorization", "Bearer ${ORCHESTRATOR_TOKEN_ADMIN}")
try:
    print(urllib.request.urlopen(req, None, timeout=30).read().decode())
except urllib.error.HTTPError as e:
    print(e.read().decode())
PY
)
check "revoked through replica 2" "$(echo "$revoke_body" | grep -c '"revoked":true')" "1"
check "revocation reported as distributed" "$(echo "$revoke_body" | grep -c '"distributed":true')" "1"

check "replica 1 rejects it immediately, no restart" \
  "$(http_on orchestrator-1 /v1/executions "$ORCHESTRATOR_TOKEN_CI" GET)" "401"
check "replica 2 rejects it too" \
  "$(http_on orchestrator-2 /v1/executions "$ORCHESTRATOR_TOKEN_CI" GET)" "401"
check "a different admin token still works on replica 1" \
  "$(http_on orchestrator-1 /v1/tokens "$ORCHESTRATOR_TOKEN_ADMIN" GET)" "200"
check "a different admin token still works on replica 2" \
  "$(http_on orchestrator-2 /v1/tokens "$ORCHESTRATOR_TOKEN_ADMIN" GET)" "200"

revoked_in_db=$(docker exec orchestrator-postgres-1 psql -U orchestrator -d orchestrator -tAc \
  "SELECT revoked_at IS NOT NULL FROM api_tokens WHERE token_id = '${ci_token_id}'" | tr -d '[:space:]')
check "revocation recorded in shared state" "$revoked_in_db" "t"

echo
echo "=== 11. distributed rate limiting ==="
# Requests split across both replicas draw on one shared quota. With the
# in-process limiter each replica had its own, so two permitted twice the rate.
docker exec orchestrator-postgres-1 psql -U orchestrator -d orchestrator -qc \
  "TRUNCATE rate_limit_hits" >/dev/null

statuses=""
for i in $(seq 1 33); do
  if [ $((i % 2)) -eq 0 ]; then target=orchestrator-2; else target=orchestrator-1; fi
  code=$(docker exec -i "$target" python - <<PY
import json, urllib.request, urllib.error
req = urllib.request.Request("http://127.0.0.1:8080/v1/executions", method="POST")
req.add_header("Authorization", "Bearer ${ORCHESTRATOR_TOKEN_OPS}")
req.add_header("Content-Type", "application/json")
body = json.dumps({"objective": "rate limit probe", "run": False}).encode()
try:
    print(urllib.request.urlopen(req, body, timeout=30).status)
except urllib.error.HTTPError as e:
    print(e.code)
PY
)
  statuses="$statuses $code"
done
accepted=$(echo "$statuses" | tr ' ' '\n' | grep -c '^201$' || true)
limited=$(echo "$statuses" | tr ' ' '\n' | grep -c '^429$' || true)
check "shared quota admitted exactly 30" "$accepted" "30"
check "the excess was refused across both replicas" \
  "$([ "$limited" -ge 3 ] && echo yes || echo no)" "yes"

buckets=$(docker exec orchestrator-postgres-1 psql -U orchestrator -d orchestrator -tAc \
  "SELECT count(DISTINCT bucket) FROM rate_limit_hits WHERE bucket LIKE 'execution_create:%'" | tr -d '[:space:]')
check "one shared bucket, not one per replica" "$buckets" "1"

retry=$(docker exec -i orchestrator-1 python - <<PY
import json, urllib.request, urllib.error
req = urllib.request.Request("http://127.0.0.1:8080/v1/executions", method="POST")
req.add_header("Authorization", "Bearer ${ORCHESTRATOR_TOKEN_OPS}")
req.add_header("Content-Type", "application/json")
try:
    urllib.request.urlopen(req, json.dumps({"objective":"x","run":False}).encode(), timeout=30)
    print("no-limit")
except urllib.error.HTTPError as e:
    print("yes" if e.headers.get("Retry-After") else "missing")
PY
)
check "a refusal carries Retry-After" "$retry" "yes"

echo
echo "=== 12. egress proxy enforcement ==="
# The proxy must be RUNNING before anything below means anything: a probe
# against a dead proxy also fails, so every "refused" assertion would pass for
# the wrong reason. Squid was in a restart loop for exactly this reason during
# development and the whole section looked green.
$COMPOSE up -d egress >/dev/null 2>&1
sleep 6
egress_state=$(docker inspect --format '{{.State.Status}}' orchestrator-egress-1 2>/dev/null || echo missing)
check "egress proxy is running" "$egress_state" "running"

reachable_via_proxy=$(docker exec -i orchestrator-1 python - <<'PY'
import urllib.request, urllib.error
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": "http://egress:3128"}))
try:
    opener.open("http://not-on-the-allowlist.test/", timeout=10)
    print("no-proxy-response")
except urllib.error.HTTPError as e:
    # A 403 from Squid proves the proxy answered — a dead proxy cannot.
    print("proxy-answered" if e.code in (403, 407) else "http%d" % e.code)
except Exception as exc:
    print("proxy-unreachable: %s" % type(exc).__name__)
PY
)
check "the proxy answers (not merely unreachable)" "$reachable_via_proxy" "proxy-answered"

direct=$(docker exec -i orchestrator-1 python - <<'PY'
import socket
s = socket.socket(); s.settimeout(5)
try:
    s.connect(("93.184.216.34", 80)); print("reachable")
except Exception:
    print("blocked")
finally:
    s.close()
PY
)
check "direct outbound is blocked (proxy is the only route)" "$direct" "blocked"

probe() {
  docker exec -i orchestrator-1 python - "$1" <<'PY'
import sys, urllib.request, urllib.error
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": "http://egress:3128"}))
try:
    opener.open(sys.argv[1], timeout=10)
    print("reachable")
except urllib.error.HTTPError as e:
    print("denied" if e.code in (403, 407) else "http%d" % e.code)
except Exception:
    print("denied")
PY
}
check "proxy refuses the cloud metadata service" \
  "$(probe http://169.254.169.254/latest/meta-data/)" "denied"
check "proxy refuses RFC1918 (10/8)"     "$(probe http://10.0.0.1/)"      "denied"
check "proxy refuses loopback"           "$(probe http://127.0.0.1/)"     "denied"
check "proxy refuses RFC1918 (192.168)"  "$(probe http://192.168.1.1/)"   "denied"
check "proxy refuses a non-allowlisted domain" \
  "$(probe http://not-on-the-allowlist.test/)" "denied"

sleep 2
proxylog=$(docker exec orchestrator-egress-1 sh -c   'grep -c "169.254.169.254" /var/log/squid/access.log' 2>/dev/null || echo 0)
check "proxy logs prove traffic passed through it" \
  "$([ "$proxylog" -ge 1 ] && echo yes || echo no)" "yes"

echo
echo "=== 13. Caddy TLS boundary ==="
$COMPOSE up -d caddy >/dev/null 2>&1
sleep 10

curl_edge() {
  docker run --rm --network orchestrator_edge curlimages/curl:8.10.1 \
    -k -s -o /dev/null -w '%{http_code}' --max-time 15 "$@" 2>/dev/null || echo "000"
}

check "reachable over TLS through Caddy" "$(curl_edge https://orchestrator-caddy/live)" "200"
check "spoofed identity headers do not authenticate" \
  "$(curl_edge -H 'X-Forwarded-User: attacker' -H 'X-Forwarded-Groups: platform-admins' https://orchestrator-caddy/v1/tokens)" \
  "401"
check "a valid bearer token works through Caddy" \
  "$(curl_edge -H "Authorization: Bearer ${ORCHESTRATOR_TOKEN_ADMIN}" https://orchestrator-caddy/v1/tokens)" \
  "200"

published=$(docker port orchestrator-1 2>/dev/null | wc -l | tr -d '[:space:]')
check "orchestrator publishes no host ports" "$published" "0"

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "smoke test PASSED"
else
  echo "smoke test FAILED: $FAILURES check(s)"
fi
exit "$FAILURES"
