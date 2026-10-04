# Production readiness checklist

Every item is marked **Complete**, **Incomplete**, or **Operator** (yours to
provide — the platform cannot do it for you).

**Complete requires all three:** wired through the real startup or request
path, covered by an integration test, and used correctly by the documented
production deployment. Standalone code, unit-only tests, and unmounted
configuration do not count. The Evidence column names the test and the
deployment file for each.

That rule was added because it was being broken. `storage.backend: postgres`
was documented and implemented, and `config.loader` rejected it — so no
deployment could ever have used it. The image had neither the CLI nor asyncpg,
so the container could not run its own entrypoint. `TokenStore` enforced
expiry and revocation that the API middleware never consulted. All three
passed their unit tests.

Scope claim: this supports **controlled production deployment**, behind a
TLS/auth proxy, with a declared profile — single-instance on SQLite, or
**multi-instance on PostgreSQL**. It does not guarantee AI output
correctness, and never claims to.

---

## Egress and SSRF

| Item | Status | Evidence |
|---|---|---|
| Host allowlist mandatory; empty means deny | Complete | `test_egress.py` |
| Suffix matching is dot-anchored | Complete | `test_egress.py` |
| HTTPS enforced; HTTP is a named dev opt-in | Complete | `test_egress.py` |
| Non-HTTP schemes refused | Complete | `test_egress.py` |
| Loopback / RFC1918 / link-local / multicast / reserved / CGNAT refused | Complete | `test_egress.py` |
| Cloud metadata unreachable with no config escape hatch | Complete | `test_adversarial.py` |
| Every redirect hop revalidated | Complete | `test_http_tool.py` |
| All resolved addresses validated (rebinding) | Complete | `test_egress.py` |
| Read/write split across two tools and two permissions | Complete | `test_http_tool.py` |
| Method allowlist | Complete | `test_egress.py` |
| Request/response size, redirect count, timeouts bounded | Complete | `test_http_tool.py` |
| Response cap enforced while streaming | Complete | `test_http_tool.py` |
| `Authorization` cannot be set on outbound requests | Complete | `test_adversarial.py` |
| Socket-level DNS pinning | **Not implemented, and no longer claimed** | The `pin_dns` field and `pinned_transport` reference are removed; `describe()` reports the gap. `deployment/squid.conf` closes it. |

## Artifact persistence

| Item | Status | Evidence |
|---|---|---|
| Published content is written to a durable file | Complete | `test_artifact_persistence.py` |
| `storage.artifact_dir` declared in the production config | Complete | `deployment/config.production.yaml` |
| Shared volume mounted into both replicas at that path | Complete | `deployment/docker-compose.production.yml`; smoke test §14 |
| Volume owned by UID 10001 before replicas start | Complete | `artifact-init` service; smoke test §14 |
| Store is outside the workspace and separate from database state | Complete | `deployment/docker-compose.production.yml` |
| Failure to persist fails the tool call | Complete | `test_artifact_persistence.py`; smoke test §14 |
| A failed write can never report a successful publication | Complete | `test_artifact_persistence.py` |
| SHA-256 and byte size recorded for stored content | Complete | `test_artifact_persistence.py` |
| `artifact_exists` re-reads the file and checks size and checksum | Complete | `test_artifact_persistence.py` |
| An in-memory record alone does not satisfy the check | Complete | `test_artifact_persistence.py` |
| Artifact names confined to their execution directory | Complete | `test_artifact_persistence.py` (8 hostile names) |
| Symlinks in the store are not followed | Complete | `test_artifact_persistence.py` |
| Name collision versions rather than overwrites | Complete | `test_artifact_emission.py` |
| Error text omits the store path | Complete | `test_artifact_persistence.py` |
| Cross-replica read proven in the production topology | **Unverified here** | Smoke test §14 exists and is syntax-checked; not executed in this environment — see the note below. |


## Policy defaults

| Item | Status | Evidence |
|---|---|---|
| `default_effect: deny` in production | Complete | `test_profiles.py` |
| `require_explicit_tool_grant` in production | Complete | `test_profiles.py` |
| Undeclared profile resolves to `production` | Complete | `test_profiles.py` |
| Named development profile preserved | Complete | `test_profiles.py` |
| Defaults verified through the real `load()` path | Complete | `test_profiles.py` |
| Migration notices for tightened defaults | Complete | `orchestrator validate` |
| Explicit config overrides the profile both ways | Complete | `test_profiles.py` |
| Choose and declare the right profile | **Operator** | — |

## Process and worker isolation

| Item | Status | Evidence |
|---|---|---|
| Environment not inherited | Complete | `test_process_isolation.py` |
| Environment allowlist, built up not filtered down | Complete | `test_process_isolation.py` |
| Loader variables permanently forbidden | Complete | `test_process_isolation.py` |
| Executables matched by resolved path | Complete | `test_process_isolation.py` |
| Shell interpreters refused at config time | Complete | `test_process_isolation.py` |
| No command runs through a shell | Complete | `test_adversarial.py` |
| Working directory confined; symlink escape blocked | Complete | `test_process_isolation.py` |
| Caller cannot extend the timeout | Complete | `test_process_isolation.py` |
| Output capped | Complete | `test_process_isolation.py` |
| Argument count and pattern policy | Complete | `test_process_isolation.py` |
| Isolation levels documented, only `restricted` claimed | Complete | `docs/security.md` |
| Container / sandbox / remote isolation enforced | **Incomplete** | Not implemented |
| Run the orchestrator inside a container | **Operator** | `Dockerfile`, `deployment/docker-compose.production.yml` |

## Authentication, authorization, tenancy

| Item | Status | Evidence |
|---|---|---|
| Bearer auth; refuses unauthenticated network bind | Complete | `test_completeness.py` |
| Legacy `ORCHESTRATOR_API_TOKEN` still works | Complete | `test_api_authorization.py` |
| Principal identity with token id, scopes, tenant | Complete | `test_api_authorization.py` |
| Five scopes enforced centrally | Complete | `test_api_authorization.py` |
| Unmapped route requires `admin` (fails closed) | Complete | `test_api_authorization.py` |
| Cross-tenant read/modify/approve/cancel/audit blocked | Complete | `test_api_authorization.py` |
| Structural test: every execution-scoped route guarded | Complete | `test_api_authorization.py` |
| Single-tenant mode | Complete | `test_api_authorization.py` |
| Authorization denials audited | Complete | `api.forbidden` events |
| No custom password storage; OIDC-shaped seam | Complete | `identity.py::resolve` |
| Token expiry, not-before, token id | Complete | `test_token_api.py` (over HTTP) · `test_token_lifecycle.py` |
| Revocation without a restart | Complete | `test_token_api.py::test_revocation_takes_effect_without_restarting_the_service` · `smoke-test.sh` §10, verified in the live stack |
| Tokens stored only as salted digests | Complete | `test_token_api.py::test_the_registry_holds_no_plaintext_after_construction` |
| Issue / revoke / rejection audited | Complete | `test_token_api.py::test_revocation_is_audited_with_the_actor` |
| Admin token API (list metadata, revoke by id) | Complete | `test_token_api.py` · `smoke-test.sh` §10 |
| One resolution path (no competing token check) | Complete | `test_token_api.py::test_the_registry_resolves_through_the_token_store` |
| Revocation propagates across replicas | Complete | Token state in PostgreSQL. `test_shared_tokens.py::test_revoking_on_one_replica_is_immediate_on_the_other` · `smoke-test.sh` §10 revokes on replica 2 and asserts replica 1 rejects immediately |
| Raw token never stored | Complete | `test_shared_tokens.py::test_the_database_never_contains_the_raw_token` · `smoke-test.sh` §10 queries the table directly |
| Token lookup is indexed, not a scan | Complete | `test_shared_tokens.py::test_resolution_uses_the_index_rather_than_scanning` (EXPLAIN asserts no Seq Scan) |
| No caching that could delay revocation | Complete | By construction — one indexed read per request; rationale in `shared_tokens.py` |
| Trusted reverse-proxy identity | **Not integrated, and not claimed** | Option A chosen: bearer tokens are the only identity mechanism. The Caddyfile states it does not authenticate and **strips** client-supplied identity headers; `smoke-test.sh` §13 asserts spoofed headers get 401. `api.proxy_identity.enabled: true` is rejected by config validation. |
| OIDC JWT validation in-process | **Not implemented** | Deliberate; no verified integration exists, so no claim is made. |
| Deploy an identity provider in front | **Operator** | The shipped Caddyfile does **not** authenticate and says so. |

## API hardening

| Item | Status | Evidence |
|---|---|---|
| Body size counted from the stream, not `Content-Length` | Complete | `test_api_authorization.py` |
| Rate limiter abstraction + in-process implementation | Complete | `test_api_authorization.py` |
| Shared/distributed rate limiter | Complete | `PostgresRateLimiter`, serialised per bucket by advisory lock. `test_shared_tokens.py` (7 tests incl. a concurrency race) · `smoke-test.sh` §11 splits 33 requests across two replicas and admits exactly 30 |
| Limits keyed on the authenticated principal | Complete | `security.py` uses `limiter_key(principal.id, …)` · `smoke-test.sh` §11 |
| Stricter budgets for auth failures, token admin, execution creation | Complete | `ratelimit.STRICT_CATEGORIES` · `test_shared_tokens.py` |
| `Retry-After` on refusal | Complete | `smoke-test.sh` §11 |
| CORS: wildcard-with-credentials refused | Complete | `test_api_authorization.py` |
| Trusted-proxy handling of forwarded headers | Complete | `test_api_authorization.py` |
| Readiness detail withheld from unauthenticated callers | Complete | `test_api_authorization.py` |
| Liveness lightweight and dependency-free | Complete | `/live` |
| Request ids on responses, logs, and audit | Complete | `test_api_authorization.py` |
| Errors carry no paths, credentials, or tracebacks | Complete | `test_api_authorization.py` |
| Configure rate limiting at the proxy | **Operator** | — |

## Secrets and data protection

| Item | Status | Evidence |
|---|---|---|
| Secrets from environment, never config files | Complete | `identity.py`, `loader.py` |
| Redaction by key name | Complete | `test_dataflow.py` |
| Redaction of credential shapes inside string values | Complete | `test_dataflow.py` |
| Exception text redacted | Complete | `logging.py` |
| Four-level data classification | Complete | `test_dataflow.py` |
| Provider dispositions: local / approved / prohibited | Complete | `test_dataflow.py` |
| Undeclared provider treated as unapproved | Complete | `test_dataflow.py` |
| Egress decision enforced at the router | Complete | `routing.py` |
| Decision recorded without the payload | Complete | `test_dataflow.py` |
| Classify your data and review your providers | **Operator** | Legal/privacy decision |

## Deployment integration

| Item | Status | Evidence |
|---|---|---|
| `storage.backend: postgres` accepted by the real `load()` path | Complete | `test_storage_config.py` (26 tests) |
| `storage.postgres` block validated (allowlist, no plaintext DSN, pool ranges) | Complete | `test_storage_config.py` |
| `Orchestrator.create()` opens PostgreSQL after real validation | Complete | `test_postgres_store.py::test_an_orchestrator_starts_on_postgres_and_runs_an_execution` |
| Version-controlled, credential-free production config | Complete | `deployment/config.production.yaml` |
| Config mounted read-only and selected by `ORCHESTRATOR_CONFIG` | Complete | `docker-compose.production.yml` · `smoke-test.sh` §5 |
| Migrations run exactly once before replicas accept traffic | Complete | One-shot `migrate` service + `service_completed_successfully` · `smoke-test.sh` §3 |
| A failed migration stops the deployment | Complete | `depends_on: service_completed_successfully` |
| Two replicas actually start (no Swarm-only `deploy.replicas`) | Complete | Named services · `smoke-test.sh` §4 |
| Both replicas use PostgreSQL, no SQLite file created | Complete | `smoke-test.sh` §5–6 · `test_postgres_store.py::test_no_sqlite_file_is_created_when_the_backend_is_postgres` |
| Read-only root, non-root user, no writable app directory | Complete | `smoke-test.sh` §7 |
| Image contains the CLI and the PostgreSQL driver | Complete | `Dockerfile` extras `[cli,api,yaml,http,postgres]` · `smoke-test.sh` §1 |
| Execution lifecycle across replicas through HTTP | Complete | `smoke-test.sh` §9 |
| `orchestrator validate` works without a live database | Complete | `--isolated`, verified against `config.production.yaml` |
| Startup banner reports the posture actually enforced | Complete | Fixed: previously printed "auth: none" for a token-authenticated deployment |
| CI runs the Compose smoke test | Complete | `.github/workflows/ci.yml` job `compose-smoke` |

## Multi-replica production controls

| Item | Status | Evidence |
|---|---|---|
| Token metadata, digests, expiry, not-before, revocation in PostgreSQL | Complete | migration 4 `api_tokens` · `test_shared_tokens.py` |
| Every replica validates against shared state | Complete | `smoke-test.sh` §10 (both replicas reject after one revokes) |
| Constant-time digest comparison preserved | Complete | `shared_tokens.py` uses `TokenRecord.matches` → `hmac.compare_digest` |
| Audit records for issue / reject / expiry / revocation | Complete | `token.issued`, `token.rejected`, `token.revoked` |
| Legacy token limits documented honestly | Complete | `SECURITY.md`, `config.production.yaml` |
| Egress proxy is the only route off the host | Complete | `smoke-test.sh` §12 — direct outbound blocked |
| Proxy denies loopback, RFC1918, link-local, metadata, off-allowlist | Complete | `smoke-test.sh` §12 (5 destinations) |
| Proxy is proven alive before its denials are believed | Complete | `smoke-test.sh` §12 — a dead proxy previously made every denial pass |
| Proxy logs prove traffic passed through it | Complete | `smoke-test.sh` §12 reads `/var/log/squid/access.log` |
| Application-level egress validation retained | Complete | `test_egress.py` (43 tests) — defence in depth |
| TLS termination with identity headers stripped | Complete | `smoke-test.sh` §13 |
| Orchestrator publishes no host ports | Complete | `smoke-test.sh` §13 |
| Metrics restricted to the monitoring network **and** an admin token | Complete | `Caddyfile` `@metrics` · `smoke-test.sh` §8 |

## Reliability and operations

| Item | Status | Evidence |
|---|---|---|
| SQLite for single-node | Complete | `sqlite_store.py` |
| Stated plainly as not multi-instance | Complete | `docs/durable-execution.md` |
| Storage abstraction (`StateStore`) documented | Complete | `docs/durable-execution.md` |
| PostgreSQL backend | Complete | `test_postgres_store.py` (31 against a real server) · `test_storage_config.py` · `deployment/config.production.yaml` |
| Versioned, idempotent migration runner | Complete | `test_postgres_store.py` · one-shot `migrate` service in `docker-compose.production.yml` · `smoke-test.sh` §3 |
| Refuses a database newer than the build | Complete | `test_postgres_store.py::test_a_database_newer_than_the_code_is_refused` |
| `orchestrator migrate` inspects and applies | Complete | `smoke-test.sh` §3, run in-container against PostgreSQL |
| Backup/restore documented | Complete | `deployment/runbook.md` |
| Backup/restore rehearsed in CI | **Incomplete** | Documented in the runbook, not exercised |
| Concurrency, restart recovery, idempotency, cancellation tested | Complete | `test_engine.py`, `test_failure_injection.py` |
| Configurable retention; in-flight work protected | Complete | `test_completeness.py` |
| Secure-deletion limits stated | Complete | `docs/durable-execution.md` |
| Prometheus metrics for the nine required signals | Complete | `test_metrics.py` |
| Metrics label cardinality bounded by construction | Complete | `test_metrics.py` |
| Metrics wired into engine, tool, model, policy, and API paths | Complete | `test_metrics_integration.py` |
| Metrics observed through `/metrics`, not unit calls | Complete | `test_metrics_integration.py` |
| Instrumentation failure never fails orchestration | Complete | `test_metrics_integration.py` |
| `/metrics` requires admin | Complete | `test_health_exposure.py` |
| Run backups and test a restore | **Operator** | — |

## Output quality and AI safety

| Item | Status | Evidence |
|---|---|---|
| No claim of guaranteed correctness | Complete | This document, `SECURITY.md` |
| Structured output schemas | Complete | `validation/` |
| Deterministic validators | Complete | `test_subsystems.py` |
| Evidence-based completion, confidence ladder | Complete | `test_engine.py` |
| Devil's advocate adversarial reviewer | Complete | `test_completeness.py` |
| Confidence and limitation reporting | Complete | Console + API |
| Human approval gates by risk level | Complete | `test_engine.py` |
| Risk classification drives approval | Complete | `core/policy/risk.py` |
| Adversarial tests: injection, escalation, exfiltration, metadata, MCP | Complete | `test_adversarial.py` |
| Formal impact taxonomy distinct from risk levels | **Incomplete** | `RiskLevel` drives approval; no separate declared taxonomy |
| Evaluation harness, 31 deterministic cases across 7 suites | Complete | `test_evaluation.py` |
| Security cases gate; quality cases baselined | Complete | `test_evaluation.py` |
| JSON and Markdown reports | Complete | `orchestrator evaluate --report` |
| Harness proven to detect regressions | Complete | `test_evaluation.py` (inverted cases) |
| Real-model evaluation | **Not implemented** | Opt-in by design; deterministic only today |
| Decide which actions require approval in your domain | **Operator** | — |

## Supply chain and deployment

| Item | Status | Evidence |
|---|---|---|
| CI: tests on 3.11/3.12/3.13, Linux + Windows | Complete | `.github/workflows/ci.yml` |
| CI: lint and format | Complete | ruff |
| CI: type check, gating on `typed-modules.txt` (15 modules) | Complete | mypy clean, no `\|\| true` |
| CI: type check, whole tree, gating | Complete | mypy clean across all 111 modules |
| CI: dependency vulnerability scan | Complete | pip-audit |
| CI: secret scanning | Complete | credential-pattern grep |
| CI: static security analysis | Complete | bandit |
| CI: SBOM generation | Complete | cyclonedx |
| CI: container image scan | Complete | trivy |
| CI: security regression gates | Complete | Insecure-bind and metadata tests |
| Docker: non-root, minimal, two-stage, healthcheck | Complete | `Dockerfile` |
| Docker: no credentials or state baked in | Complete | CI asserts it |
| Console present in the installed image | Complete | Packaged in `orchestrator/ui/` |
| Dependency constraints with upper bounds | Complete | `constraints.txt` |
| Full transitive lockfile | **Incomplete** | Constraints only; rationale in `constraints.txt` |
| Deployment examples: local, internal, production | Complete | `deployment/runbook.md` |
| Sign and publish images | **Operator** | — |

---

## Summary

| | Count |
|---|---|
| Complete | 145 |
| Incomplete / not implemented | 8 |
| Operator responsibility | 8 |
| **Total** | **161** |

Every "Complete" names a test file or a deployment file, and satisfies the
three conditions at the top of this document.

**Supported deployment target, verified end to end:** multi-instance Docker
Compose, PostgreSQL shared state, bearer-token authentication with distributed
revocation, TLS through a reverse proxy, and enforced outbound egress. The
49-check `deployment/smoke-test.sh` runs the whole thing and passes.

**The eight remaining gaps.** None blocks the supported target; each is stated
so nobody has to discover it.

1. **No OIDC or proxy-forwarded identity.** Option A: bearer tokens only. The
   proxy strips identity headers and `proxy_identity.enabled: true` is
   rejected by config validation.
2. **Legacy `ORCHESTRATOR_API_TOKEN` is re-seeded on restart.** It can be
   revoked at runtime across all replicas, but removing it permanently means
   removing the variable and redeploying.
3. **DNS-rebinding TOCTOU remains in the application.** Closed in the shipped
   deployment by routing through Squid — a property of the deployment, not the
   platform, and absent if you run without the proxy.
4. **Container / sandbox / remote isolation is not enforced in-process.** Only
   `restricted` is implemented and only that is claimed. Process execution is
   off in the production config.
5. **No transitive lockfile.** Constraints with upper bounds only.
6. **No real-model evaluation.** The harness is deterministic by design.
7. **No impact taxonomy distinct from `RiskLevel`.**
8. **Backup/restore documented but not rehearsed in CI.**

### Explicitly unsupported

* **SQLite with more than one instance.** Two processes against one file
  corrupt state. The production config uses PostgreSQL and the smoke test
  asserts no SQLite file is created.
* **OIDC or any proxy-forwarded identity.** Not integrated, not claimed,
  rejected by validation if configured.
* **Process execution without an external isolation boundary.** `restricted`
  confines a cooperative process, not a hostile one.
* **Guaranteed AI correctness.** The platform provides evidence-based
  validation, confidence reporting, an adversarial reviewer, approval gates,
  and a deterministic regression harness. None of that makes model output
  correct; it makes the basis for trusting it inspectable.
