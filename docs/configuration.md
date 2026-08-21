# Configuration

Configuration is layered: built-in defaults, then discovered files, then files
you name, then environment overrides. `orchestrator validate` prints the
effective result, the profile, and where each security-relevant value came
from.

## Profile

```yaml
profile: production   # development | internal-pilot | production
```

**Omitting this means `production`.** An operator who has not decided gets the
strict posture. A profile fills in anything you leave unset; anything you set
explicitly always wins, in either direction.

Full posture table: [security.md](security.md#deployment-profiles).

### Migration from a pre-profile config

Three defaults tightened. `orchestrator validate` names the ones your config
relies on:

```
defaults that changed and this config does not set:
  - policy.default_effect: default changed from 'allow' to 'deny'
  - policy.require_explicit_tool_grant: changed from False to True
  - tools.http.allowed_hosts: an empty allowlist used to mean 'any host'
                              and now means 'none'
```

To keep the previous behaviour exactly, declare `profile: development`. To
adopt the new posture, add explicit policy rules for the tools you use.

## Policy

```yaml
policy:
  default_effect: deny              # from the profile if unset
  require_explicit_tool_grant: true # from the profile if unset
  approval_threshold: medium        # risk at or above this needs a human
  deny_threshold: critical          # risk above this is refused outright
  rules:
    - kind: tool
      subject: fs.read_file
      effect: allow
      reason: "reads the source catalogue"
    - kind: tool
      subject: process.run
      effect: deny
      reason: "no process execution in this deployment"
```

Under `require_explicit_tool_grant: true`, a tool with no matching `allow`
rule is refused. Missing permissions are always a denial, never an approval
prompt — otherwise the escalation path is "ask, and hope somebody clicks yes".

## HTTP tools (egress)

```yaml
tools:
  http:
    enabled: true
    allowed_hosts:              # REQUIRED. Empty means nothing is permitted.
      - api.example.com
    allowed_methods: [GET, HEAD]   # default: GET, HEAD, OPTIONS
    max_redirects: 3
    max_request_bytes: 1048576
    max_response_bytes: 5242880
    connect_timeout: 10
    timeout: 30
    total_timeout: 60

    # Each of these is opt-in and separate. Needing one does not grant others.
    allow_http: false                # plain HTTP. Development only.
    allow_private_networks: false    # RFC1918
    allow_loopback: false
    allow_link_local: false
```

Enabling HTTP tools without `allowed_hosts` is a **configuration error**, not
an open door.

`GET`/`HEAD`/`OPTIONS` register `http.request` and need `network.read`.
`POST`/`PUT`/`PATCH`/`DELETE` register a *separate* `http.send` tool needing
`network.write`. If no write method is configured, `http.send` does not exist.

**Cloud metadata endpoints have no configuration path.** There is no key that
enables 169.254.169.254 or `metadata.google.internal`.

## Process tools

Off by default in every profile. Privileged.

```yaml
tools:
  process:
    enabled: true
    root: ./workspace
    allowed_commands:            # resolved paths, not basenames
      - /usr/bin/git
      - /usr/local/bin/ruff
    environment_allowlist:       # nothing else reaches the child
      - BUILD_ID
    denied_argument_patterns:
      - "--no-verify"
    max_arguments: 64
    timeout: 120
    max_output_bytes: 100000
    allow_shell_interpreters: false
```

Rejected at startup: a shell in `allowed_commands`, or a loader variable
(`LD_PRELOAD`, `PYTHONPATH`, `NODE_OPTIONS`, …) in `environment_allowlist`.

The child process inherits **nothing**. It receives a minimal `PATH`, a few
Windows essentials, and whatever `environment_allowlist` names.

## API

```yaml
api:
  tenancy: single              # or: multi
  principals:
    - id: ci-runner
      token_env: ORCHESTRATOR_TOKEN_CI    # the NAME of a variable
      scopes: [executions.read]
      name: "CI status checks"
    - id: ops-console
      token_env: ORCHESTRATOR_TOKEN_OPS
      scopes: [executions.read, executions.write, approvals.respond]
    - id: acme-bot
      token_env: ORCHESTRATOR_TOKEN_ACME
      scopes: [admin]
      tenant: acme             # requires tenancy: multi

  rate_limit:
    enabled: false             # prefer a reverse proxy
    backend: memory            # memory | none
    requests: 60
    window_seconds: 60
```

Scopes: `executions.read`, `executions.write`, `approvals.respond`,
`audit.read`, `admin` (implies the rest). Unset scopes default to
`executions.read` only.

**Tokens never appear in this file.** `token_env` names an environment
variable. A principal whose variable is unset is skipped, not fatal.

`ORCHESTRATOR_API_TOKEN` still works and still means full access, so existing
deployments keep working.

## Data classification and model egress

```yaml
data:
  default_classification: confidential   # public | internal | confidential | restricted
  enforce_egress_policy: true            # from the profile if unset

models:
  providers:
    - type: ollama                 # inferred `local` — runs on your hardware
      base_url: http://localhost:11434

    - type: openai_compatible
      name: openrouter
      data_policy:
        disposition: approved      # local | approved | prohibited
        max_classification: internal
        approval_reference: "VENDOR-114"

    - type: openai_compatible
      name: some-other-vendor
      data_policy:
        disposition: prohibited
```

A provider with **no** `data_policy` is treated as *unapproved*, not as
approved-for-public.

Enforcement is on when the profile asks for it **and** at least one provider
is declared in config. A provider passed programmatically to
`Orchestrator.create(providers=[...])` is the caller's own object and is not
gated — refusing it would break every embedding use while protecting nothing.

Approving an external provider is a legal, privacy, and vendor-review
decision. This enforces the answer; it does not supply it.

## Retention

```bash
orchestrator prune --older-than-days 90            # dry run (the default)
orchestrator prune --older-than-days 90 --apply
```

Only `completed`, `failed`, and `cancelled` are eligible — derived from the
enum, so a status added later defaults to protected. Anything running, paused,
waiting, or `cancelling` is kept regardless of age.

## Environment variables

| Variable | Purpose |
|---|---|
| `ORCHESTRATOR_API_TOKEN` | Legacy full-access token. Comma-separated to rotate. |
| `ORCHESTRATOR_TOKEN_*` | Per-principal tokens, named by `token_env`. |
| `ORCHESTRATOR_CONSOLE_PATH` | Serve a customised console. |
| `OPENROUTER_API_KEY`, `NVIDIA_API_KEY`, … | Provider keys, named by `api_key_env`. |

A key pasted into `api_key_env` instead of a variable *name* is a common
mistake and ends up in version control. `orchestrator validate` reports it.

## Storage

```yaml
storage:
  backend: sqlite            # sqlite (default, single-node) | postgres | memory
  path: .orchestrator/state.db

  # Multi-instance. Required for more than one replica.
  postgres:
    dsn_env: ORCHESTRATOR_POSTGRES_DSN   # the NAME of a variable
    min_connections: 1
    max_connections: 10
    command_timeout: 30
    apply_migrations: true     # false to migrate explicitly before rollout
```

`storage.postgres.dsn` is **rejected**: a DSN carries a password and a config
file gets committed. Name a variable instead.

```bash
orchestrator migrate            # status
orchestrator migrate --apply    # apply pending migrations
```

## Token lifetimes

```yaml
api:
  principals:
    - id: contractor
      token_env: ORCHESTRATOR_TOKEN_CONTRACTOR
      scopes: [executions.read]
      lifetime_days: 30          # or lifetime_hours
```

Tokens are held only as salted digests. Expiry and not-before are checked on
every use with a 60-second clock-skew allowance. Revocation is immediate on
the instance that receives it — see the runbook for the multi-replica caveat.

## External identity

```yaml
api:
  proxy_identity:
    enabled: true
    trusted_proxies: ["10.0.0.9"]      # REQUIRED when enabled
    user_header: x-forwarded-user
    groups_header: x-forwarded-groups
    tenant_header: x-forwarded-tenant
    group_scopes:
      platform-admins: [admin]
      viewers: [executions.read]
    default_scopes: []
```

Enabling this without `trusted_proxies` is a configuration error: without it,
any caller could set the header and become any user.

There is no in-process OIDC JWT validation, deliberately — see the note in
`api/tokens.py`. Put an OIDC-aware proxy in front and let it assert identity
through these headers.

## Rate limiting

```yaml
api:
  rate_limit:
    enabled: false      # prefer a reverse proxy
    backend: memory     # memory (single instance) | none
    requests: 60
    window_seconds: 60
```

`InMemoryRateLimiter` reports `distributed = False`. Behind a load balancer,
N replicas permit N times the configured rate and a restart forgets every
counter. Put the limit at the proxy.
