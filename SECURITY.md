# Security

## Reporting a vulnerability

Report privately, not as a public issue. Include what you did, what happened,
and what you expected. You will get an acknowledgement within three working
days and an assessment within ten.

## The threat model, stated plainly

**This platform runs tools.** Depending on configuration those tools read
files, spawn processes, and make network requests, and they do so with
arguments a language model chose. Every control below follows from that one
fact.

The model is treated as an untrusted input source, not as an authority. It
proposes; the software disposes. A model that asks to read `/etc/shadow` is
not a security incident, it is a request that gets denied — and the design
goal is that denial happens in code, not in a prompt.

### What is defended

| Threat | Control | Tests |
|---|---|---|
| Unauthenticated remote execution | The API refuses to bind a non-loopback address without a token. The process does not start. | `test_completeness.py` |
| Over-broad credentials | Scoped principals: `executions.read/write`, `approvals.respond`, `audit.read`, `admin`. Enforced centrally; an unmapped route requires `admin`. | `test_api_authorization.py` |
| Cross-tenant access | Ownership checked on the fetched object, not the query. Wrong tenant returns 404, not 403. | `test_api_authorization.py` |
| SSRF to internal services | Mandatory host allowlist; loopback, RFC1918, link-local, multicast, reserved, CGNAT all refused unless individually opted into. | `test_egress.py` |
| SSRF to cloud metadata | 169.254.169.254 and peers are unreachable. **No configuration option enables them.** | `test_egress.py`, `test_adversarial.py` |
| SSRF via redirect | Every hop revalidated against the full policy before it is followed. | `test_http_tool.py` |
| DNS rebinding | Every resolved address must pass; one blocked answer refuses the name. (Residual TOCTOU — see below.) | `test_egress.py` |
| Data exfiltration over HTTP | Read and write are separate tools with separate permissions. `http.send` is not registered unless a write method is configured. `Authorization` headers cannot be set. | `test_adversarial.py` |
| Credential theft via subprocess | The child inherits **no** environment. Verified that provider keys and the API token are invisible to it. | `test_process_isolation.py` |
| Arbitrary execution via allowlist | Executables matched by resolved path, not basename. Shell interpreters refused at config time. No command runs through a shell. | `test_process_isolation.py` |
| Library injection | `LD_PRELOAD`, `DYLD_*`, `PYTHONPATH`, `NODE_OPTIONS`, `BASH_ENV` can never be passed. Rejected at config time. | `test_process_isolation.py` |
| Workspace escape | Paths confined and re-resolved after joining, so a symlink inside the root cannot point outside. | `test_adversarial.py` |
| Prompt injection reaching tools | Tool grants are per-task and checked at call time by policy, never by the model. Missing permissions are a denial, not an approval prompt. | `test_adversarial.py` |
| Permission escalation | A task cannot claim a permission it was not granted; narrowing a scope cannot widen it. | `test_adversarial.py` |
| Undeclared third-party data egress | Providers carry a declared disposition. An undeclared provider is *unapproved*, not approved-for-public. | `test_dataflow.py` |
| Secrets in logs and audit | Redaction by key name **and** by credential shape inside string values (vendor prefixes, JWTs, PEM blocks, URL credentials). | `test_dataflow.py` |
| Secrets in error responses | Errors are sanitised of paths and credentials; unexpected failures return a request id, never a traceback. | `test_api_authorization.py` |
| Oversized request bodies | Counted as the stream is read, not trusted from `Content-Length`. | `test_api_authorization.py` |
| Metrics as an exposure route | Labels come from closed sets. An objective, prompt, execution id, or tenant cannot become a label. `/metrics` requires admin. | `test_metrics.py`, `test_metrics_integration.py` |
| Health endpoint disclosure | `/health` and `/v1/health` require admin. `/ready` returns status only to unauthenticated callers; `/live` touches no dependency. | `test_health_exposure.py` |
| Credential lifetime | Tokens carry expiry and not-before, are stored as salted digests, and revoke without a restart. | `test_token_api.py` |
| Revocation across replicas | Token state lives in PostgreSQL. Revoking on one replica is honoured by every other on the next request — no restart, no cache to expire. | `test_shared_tokens.py` · `smoke-test.sh` §10 |
| Raw tokens at rest | Only a deterministic lookup digest and a per-row salted digest are stored. The raw value reaches neither the database, the logs, nor the audit trail. | `test_shared_tokens.py::test_the_database_never_contains_the_raw_token` · `smoke-test.sh` §10 |
| Rate limiting across replicas | Counted in the shared database, keyed on the authenticated principal, with stricter budgets for authentication failures, token administration, and execution creation. | `test_shared_tokens.py` · `smoke-test.sh` §11 |
| Outbound egress enforcement | Orchestrator containers have no route off the host; Squid is the only path out and refuses loopback, RFC1918, link-local, CGNAT, multicast, metadata addresses, and non-allowlisted domains. | `smoke-test.sh` §12 |
| TLS boundary | Caddy terminates TLS and strips client-supplied identity headers. The orchestrator publishes no host ports. | `smoke-test.sh` §13 |
| Identity header spoofing | Proxy identity headers are believed only from a listed trusted peer. | `test_token_lifecycle.py` |
| SQL injection in state queries | Every value is a bound parameter; only placeholder punctuation is interpolated. | `test_postgres_store.py` |
| Runaway cost or loops | Every loop bounded; a caller may shorten a limit, never extend it. | `test_engine.py` |

### What is not defended, and you should plan for

* **Legacy `ORCHESTRATOR_API_TOKEN` cannot be permanently removed at
  runtime.** It is registered in the shared store like any other token and
  *can* be revoked by id, taking effect on every replica immediately — but the
  environment variable remains its source of truth, so the next restart
  re-seeds it. Removing it for good means removing the variable and
  redeploying. Configured `api.principals` have the same runtime revocation
  and the same caveat; a revoked token is not resurrected by a restart (the
  upsert does not clear `revoked_at`), but a *new* row is created if you
  change the token value.
* **The in-process rate limiter remains single-instance.** It is still the
  default for deployments without a shared database, and still reports
  `distributed = False` for that reason. The production Compose stack uses
  `backend: postgres`, which is shared and verified across two replicas.
* **DNS rebinding: the application window remains, and the deployment
  closes it.** The orchestrator still resolves twice — validation and connect
  — and claims no pinning. In the Compose stack that gap is closed by routing:
  the containers have no route off the host, and Squid re-checks the
  destination when it opens the socket. That is a property of the deployment,
  not of this platform, and it disappears if you run without the proxy.
  Original wording follows.

* **DNS rebinding has a residual TOCTOU window, and no pinning is claimed.**
  Validation resolves the name and checks every address; httpx resolves again
  when it opens the socket. An earlier version of this codebase described a
  `pin_dns` setting and a `pinned_transport` — **neither existed**, and
  `describe()` reported `dns_pinning: true` to operators reading their
  posture. Both are removed; the gap is now reported as
  `dns_rebinding_toctou: open`. **If an attacker controlling DNS for an
  allowlisted host is in your threat model, put an egress proxy in front —
  `deployment/squid.conf` is a worked example.**
* **Restricted process execution is not a sandbox.** It confines a cooperative
  process. A permitted executable still runs as the orchestrator's user.
* **`container`, `sandbox`, and `remote` isolation are not enforced.** Only
  `restricted` is implemented in-process. Configuring another value does not
  create that boundary — see [docs/security.md](docs/security.md#isolation-levels).
* **SQLite is single-node.** Multi-instance deployment requires the
  PostgreSQL backend (`storage.backend: postgres`), which is implemented
  and tested. Running two instances against one SQLite file corrupts state.
* **Transport is your responsibility.** The server speaks plain HTTP.
* **AI output correctness is not guaranteed.** The platform provides
  evidence-based validation, confidence reporting, an adversarial reviewer,
  and human approval gates for high-impact actions. None of that makes model
  output correct; it makes the basis for trusting it inspectable.

## Configuring authentication

```bash
export ORCHESTRATOR_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
orchestrator serve --host 0.0.0.0
```

Rotate without downtime by accepting both tokens during the changeover:

```bash
export ORCHESTRATOR_API_TOKEN="new-token,old-token"
```

Restart, move callers to the new token, then drop the old one and restart
again.

Binding loopback with no token is allowed and is the intended single-user
desktop setup. `orchestrator serve` prints which posture it is running in on
startup, so it is visible rather than assumed.

If another layer in front of this process already authenticates callers, say
so explicitly:

```bash
orchestrator serve --host 0.0.0.0 --i-have-my-own-authentication
```

The flag is verbose on purpose. It should be uncomfortable to type by
accident.

## Handling credentials

Provider API keys are read from environment variables named in the config:

```yaml
providers:
  - type: openai_compatible
    api_key_env: OPENROUTER_API_KEY   # the NAME of a variable, never the key
```

Putting a key directly in `api_key_env` is a common and understandable mistake
— the field takes a variable name, and a key placed there ends up in version
control. `orchestrator validate` reports it.

## Retention

Audit trails are the record of what the system did and why. They are also
personal data if your objectives contain any.

```bash
orchestrator prune --older-than-days 90            # dry run, the default
orchestrator prune --older-than-days 90 --apply
```

Only completed, failed, and cancelled runs are eligible. Anything still
running, paused, or waiting on a person is kept regardless of age.
