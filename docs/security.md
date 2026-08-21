# Security controls

What is enforced, where it is enforced, and what is not covered. Claims here
correspond to tests; the test file is named beside each control.

## Design principles these controls preserve

* **The model is untrusted input, never an authorization authority.** It
  proposes a URL, an argv, a tool call. Software decides.
* **Policy decides, tools execute, validators judge.** No tool consults a
  model to find out whether it is allowed.
* **Least privilege, per task.** Permissions are granted for one task, not one
  session.
* **Bounded execution.** Every loop, budget, and timeout has a ceiling the
  caller cannot raise.

## Deployment profiles

Declared in config as `profile:`. Omitting it means `production` — an operator
who has not decided gets the strict posture.

| | development | internal-pilot | production |
|---|---|---|---|
| Policy default effect | allow | **deny** | **deny** |
| Explicit tool grant required | no | **yes** | **yes** |
| Approval threshold | high | high | **medium** |
| API authentication | not required | **required** | **required** |
| Readiness detail exposed | yes | no | no |
| Plain HTTP egress | allowed | **refused** | **refused** |
| Private networks (RFC1918) | allowed | allowed | **refused** |
| Loopback egress | allowed | refused | refused |
| Cloud metadata endpoints | **refused** | **refused** | **refused** |
| Process tools | allowed if configured | refused | refused |

A profile sets defaults; it is never a ceiling. Explicit config wins in either
direction, and `orchestrator validate` prints which values came from the
profile and which from the file.

Tests: `tests/test_profiles.py`.

## Outbound network (SSRF)

`src/orchestrator/tools/egress.py`. Tests: `tests/test_egress.py`,
`tests/test_http_tool.py`.

| Control | Behaviour |
|---|---|
| Allowlist | **Mandatory.** An empty allowlist denies everything. Enabling HTTP tools without hosts is a config error, not an open door. |
| Host matching | Exact, or dot-anchored suffix. `notexample.com` does not match `example.com`. |
| Scheme | HTTPS only unless `allow_http` is set. Non-HTTP schemes (`file:`, `gopher:`, `data:`) always refused. |
| URL credentials | `https://user:pass@host` refused. |
| Address filtering | Loopback, RFC1918 private, link-local, multicast, reserved, CGNAT, documentation ranges, and unspecified are refused unless individually opted into. |
| Cloud metadata | 169.254.169.254, 169.254.170.2, 100.100.100.200, 192.0.0.192, fd00:ec2::254, and `metadata.google.internal` are **never** reachable. There is no config option to enable them. Opting into link-local does not open them. |
| Redirects | Every hop is revalidated against the full policy before it is followed. Bounded by `max_redirects`. |
| DNS rebinding | Every resolved address must pass. One blocked answer refuses the whole name. |
| Method split | GET/HEAD/OPTIONS require `network.read`; POST/PUT/PATCH/DELETE require `network.write`. These are **separate tools** (`http.request`, `http.send`), so a task granted the read tool cannot POST. |
| Write tool registration | `http.send` is only registered when a write method is configured. |
| Request headers | Allowlisted. `Authorization` is refused, so a model that has seen a token cannot forward it. |
| Limits | Request body, response body (enforced while streaming, not after), redirect count, and connect/read/write timeouts. |

### Residual risk

Validation resolves DNS, then httpx resolves again at connect time. That is a
narrow TOCTOU window. Closing it fully requires pinning the socket to the
validated address, which conflicts with TLS SNI and certificate validation for
virtual-hosted TLS. **If your threat model includes an attacker who controls
DNS for an allowlisted host, place an egress proxy in front of this process.**

## Process execution

`src/orchestrator/tools/execpolicy.py`. Tests: `tests/test_process_isolation.py`.

Process execution is privileged and off by default in every profile.

| Control | Behaviour |
|---|---|
| Environment | **Not inherited.** The child gets a minimal `PATH`, a few Windows essentials, and nothing else. Verified: `OPENROUTER_API_KEY`, `NVIDIA_API_KEY`, `ORCHESTRATOR_API_TOKEN` are not visible to a child process. |
| Environment allowlist | Variables are passed only if named. Built up from nothing rather than filtered down, so an unanticipated secret name cannot slip through. |
| Loader variables | `LD_PRELOAD`, `LD_LIBRARY_PATH`, `DYLD_*`, `PYTHONPATH`, `NODE_OPTIONS`, `BASH_ENV` and similar can never be passed, whatever the allowlist says. Rejected at config time. |
| Executable identity | The allowlist matches a **resolved path**, not a basename. Permitting `python` does not permit `/tmp/attacker/python`. |
| Shell interpreters | Refused at config time. `sh`, `bash`, `zsh`, `cmd`, `powershell`, `pwsh`, and wrappers like `env`, `xargs`, `nohup`. Permitting one requires `allow_shell_interpreters: true`. |
| Shell invocation | No command is ever run through a shell. Metacharacters are inert data. |
| stdin | `/dev/null`. A process waiting on input it will never receive holds a slot until timeout. |
| Working directory | Confined to the configured root, re-resolved after joining so a symlink inside the root cannot point outside it. |
| Timeout | The caller may shorten it, never extend it. The previous code took the caller's value, which made the bound advisory. |
| Output | Capped, with `stdout_truncated` / `stderr_truncated` reported honestly. |
| Argument policy | Count bounded; patterns can be denied by regex. |

### What this is not

**This is restricted process execution, not a sandbox.** It confines a
cooperative process, not a hostile one. A permitted executable still runs as
the orchestrator's user and can open sockets, read any file that user can
read, and consume CPU. See "Isolation levels" below.

## Isolation levels

The platform distinguishes four, and claims only what it enforces:

| Level | What enforces it | Status |
|---|---|---|
| `restricted` | This process: allowlists, env construction, cwd confinement, timeouts | **Implemented and tested** |
| `container` | An OS container boundary | **Not enforced by this platform.** Run the orchestrator itself in a container. |
| `sandbox` | A syscall-filtering sandbox (seccomp, Landlock, AppArmor) | **Not implemented.** |
| `remote` | A separate machine | **Not implemented.** |

Configuring `isolation: container` does not create a container. Only
`restricted` is enforced in-process; the others describe where you have chosen
to run the orchestrator, and are your responsibility to actually provide.

## API authentication

`src/orchestrator/api/security.py`. Tests: `tests/test_completeness.py`.

* Binding a non-loopback address without a token is **refused at startup**,
  not warned about.
* Applied as middleware, so a route added later is protected by default.
* `/live`, `/ready`, and `/` (the console shell) are public; everything under
  `/v1` requires a bearer token.
* Constant-time comparison; multiple tokens accepted for rotation.
* Tokens come from `ORCHESTRATOR_API_TOKEN`, never from a config file.

## Secrets

* Provider keys are read from environment variables named in config. The
  config holds the variable *name*.
* Audit payloads and logs pass a redactor.
* Subprocesses do not inherit the environment (above).
* `Authorization` headers cannot be set on outbound HTTP requests.
