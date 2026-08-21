# Deployment runbook

Three supported shapes. Each names what it is for and what it is not.

---

## 1. Local single-user

One person, one machine, their own data.

```bash
pip install -e ".[api,yaml,http]"
orchestrator init .
# edit .orchestrator/config.yaml -> profile: development
orchestrator serve
```

Opens on `127.0.0.1:8080`. No token required — the boundary being relied on is
the loopback interface, and `orchestrator serve` prints which posture it is in
on startup.

Suitable for: personal use, development, evaluation.
**Not** suitable for: anyone else's data, anything reachable from a network.

---

## 2. Internal deployment behind a TLS/auth proxy

A team, inside one organisation, behind something that already authenticates.

```yaml
# .orchestrator/config.yaml
profile: internal-pilot

api:
  tenancy: single
  principals:
    - id: ops-console
      token_env: ORCHESTRATOR_TOKEN_OPS
      scopes: [executions.read, executions.write, approvals.respond]
    - id: ci-status
      token_env: ORCHESTRATOR_TOKEN_CI
      scopes: [executions.read]

policy:
  rules:
    - {kind: tool, subject: fs.read_file, effect: allow, reason: "reads the catalogue"}
    - {kind: tool, subject: http.request, effect: allow, reason: "fetches source pages"}

tools:
  http:
    enabled: true
    allowed_hosts: [api.internal.example, docs.example.com]
    allowed_methods: [GET, HEAD]
```

```bash
export ORCHESTRATOR_TOKEN_OPS="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
export ORCHESTRATOR_TOKEN_CI="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"

docker build -t orchestrator:local .
docker run -d --name orchestrator \
  -p 127.0.0.1:8080:8080 \
  -v orchestrator-data:/data \
  -e ORCHESTRATOR_TOKEN_OPS -e ORCHESTRATOR_TOKEN_CI \
  -e OLLAMA_HOST=http://ollama:11434 \
  --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  orchestrator:local
```

Then front it with nginx/Caddy/Traefik terminating TLS, and set
`trusted_proxies` so forwarded client addresses are believed only from that
proxy.

**The proxy is responsible for:** TLS, rate limiting, and (optionally) SSO.
Rate limiting is deliberately not solved in-process — see below.

---

## 3. Production, multiple instances

```
            ┌──────────────┐
  clients ─▶│  TLS + auth  │  proxy: TLS, rate limit, optional OIDC
            │    proxy     │
            └──────┬───────┘
                   │  X-Forwarded-For (trusted_proxies)
        ┌──────────┴──────────┐
        ▼                     ▼
   orchestrator          orchestrator      stateless replicas
        │                     │
        └──────────┬──────────┘
                   ▼
          ┌─────────────────┐
          │   PostgreSQL    │   ◀── required for >1 instance
          └─────────────────┘
```

```yaml
profile: production
api:
  tenancy: multi
  principals:
    - {id: acme-bot,  token_env: ORCHESTRATOR_TOKEN_ACME,  scopes: [admin], tenant: acme}
    - {id: globex-bot, token_env: ORCHESTRATOR_TOKEN_GLOBEX, scopes: [admin], tenant: globex}
data:
  default_classification: confidential
  enforce_egress_policy: true
```

### Multi-instance, with PostgreSQL

```yaml
storage:
  backend: postgres
  postgres:
    dsn_env: ORCHESTRATOR_POSTGRES_DSN
    max_connections: 10
```

```bash
export ORCHESTRATOR_POSTGRES_DSN="postgresql://orchestrator:$(cat /run/secrets/db)@postgres:5432/orchestrator"
export POSTGRES_PASSWORD="$(cat /run/secrets/db)"
export ORCHESTRATOR_TOKEN_OPS="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
export ORCHESTRATOR_TOKEN_PROMETHEUS="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"

# Apply migrations once, before rolling instances.
orchestrator migrate --apply

docker compose -f deployment/docker-compose.production.yml up -d
```

A worked stack is in [docker-compose.production.yml](docker-compose.production.yml):
two orchestrator replicas, PostgreSQL, Caddy terminating TLS and rate
limiting, a Squid egress proxy, and a nightly `pg_dump`. Nothing in it
contains a credential.

**Still true: SQLite is single-node.** Running this topology on SQLite
corrupts state. The backend is the thing that makes the replicas safe.

#### Rolling upgrades and the schema

Migrations are idempotent and take an advisory lock, so two instances starting
together converge. A database at a version newer than a binary is **refused at
startup** — which is what you want during a rollback: the old binary declines
to run rather than misreading rows the new one wrote. Plan a rollback that
crosses a migration accordingly.

## Backup and restore

### PostgreSQL (multi-instance)

```bash
# Consistent online dump.
docker compose -f deployment/docker-compose.production.yml exec -T postgres   pg_dump -U orchestrator orchestrator | gzip > orchestrator-$(date +%F).sql.gz

# Restore into an empty database, then let migrations bring it current.
gunzip -c orchestrator-2026-08-20.sql.gz   | docker compose exec -T postgres psql -U orchestrator orchestrator
orchestrator migrate --apply
```

The compose stack runs a nightly `pg_dump` with 14-day retention. Verify a
restore before you need it:

```bash
psql -U orchestrator orchestrator -c "SELECT count(*) FROM executions;"
orchestrator migrate            # should report up to date
```

### SQLite (single-node)

State lives in one SQLite file plus its WAL.

```bash
# Back up consistently, without stopping the service.
docker exec orchestrator sh -c \
  'python -c "import sqlite3,sys; \
   src=sqlite3.connect(\"/data/.orchestrator/state.db\"); \
   dst=sqlite3.connect(\"/data/backup.db\"); \
   src.backup(dst); dst.close(); src.close()"'
docker cp orchestrator:/data/backup.db ./state-$(date +%F).db
```

Do **not** copy `state.db` with `cp` while the service is running: the WAL
means the copy can be inconsistent. `sqlite3`'s backup API is online-safe.

Restore: stop the service, replace `state.db`, remove any `-wal`/`-shm`
alongside it, start.

Verify a restore before you need it:

```bash
sqlite3 state-2026-08-20.db "PRAGMA integrity_check; SELECT COUNT(*) FROM executions;"
```

---

## Revoking a token

Revocation is immediate on the instance that receives it, and the token store
holds only salted digests, so the value cannot be recovered from a dump.

```python
store.revoke("tok_1a2b3c4d", reason="leaked in a build log")
```

**Revocation reaches every replica immediately.** Token state lives in
PostgreSQL, so the write *is* the revocation — there is no broadcast, no cache
to invalidate, and no window. `smoke-test.sh` §10 revokes through replica 2
and asserts replica 1 rejects the credential on its next request.

```bash
curl -sk -H "Authorization: Bearer $ORCHESTRATOR_TOKEN_ADMIN"   https://orchestrator.example.com/v1/tokens          # ids and metadata, no secrets

curl -sk -X POST -H "Authorization: Bearer $ORCHESTRATOR_TOKEN_ADMIN"   "https://orchestrator.example.com/v1/tokens/tok_1a2b3c4d/revoke?reason=leaked"
```

**One caveat, and it matters.** A token whose value comes from an environment
variable is re-seeded when a replica restarts. Revoking it stops it working
now, on every replica, and a rolling restart does *not* resurrect it — the
upsert deliberately leaves `revoked_at` alone. But the variable is still its
source of truth, so remove it from the environment and redeploy to be finished
with it. The audit trail records `token.revoked` and every subsequent
`token.rejected`.

## Rotating a token

Both are accepted during the changeover, so there is no window where neither
works:

```bash
export ORCHESTRATOR_TOKEN_OPS="new-token,old-token"
# restart, move callers to the new token, then:
export ORCHESTRATOR_TOKEN_OPS="new-token"
# restart again
```

---

## Health checks

| Endpoint | Auth | Use |
|---|---|---|
| `/live` | none | Liveness. Restart on failure. |
| `/ready` | none | Load balancer. **Detail only for an authenticated admin.** |
| `/metrics` | `admin` | Prometheus scrape. |

Do not point a container restart policy at `/ready`: it depends on model
providers, and restarting on a provider blip turns a partial outage into a
crash loop.

---

## Incident response

**A token may be compromised.** Remove it from the principal's `token_env`,
restart. Audit trail: search for `api.unauthorized` and `api.forbidden` events
with that principal.

**Unexpected egress.** `orchestrator audit <id> | grep policy` shows every
model-egress decision with provider, classification, and approval reference.
`tools.http.allowed_hosts` is the ground truth for what was reachable.

**Suspected data exposure.** Objectives and results are in the state database
and the audit trail; metrics contain none of it by construction. Provider
egress decisions name which provider received data at what classification.

---

## Operational checklist before going live

```bash
orchestrator validate            # posture, migration notices, config problems
python -m pytest tests/ -q       # 701 tests; 42 skip without PostgreSQL
```

Then confirm by hand:

- [ ] `orchestrator validate` prints the profile you intended
- [ ] `ORCHESTRATOR_API_TOKEN` or per-principal tokens are set and are not in any file
- [ ] `tools.http.allowed_hosts` lists only hosts you meant
- [ ] `tools.process.enabled` is false, or its allowlist is exact paths
- [ ] TLS terminates at a proxy; the orchestrator port is not exposed directly
- [ ] Rate limiting is configured **at the proxy**
- [ ] `/data` is a durable volume with a tested restore
- [ ] A retention schedule exists (`orchestrator prune`)
- [ ] Every external model provider has a `data_policy` with an approval reference
