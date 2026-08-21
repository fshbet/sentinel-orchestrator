# Policies

## What can be governed

| Kind | Subject matched against |
|---|---|
| `tool` | Tool id |
| `mcp_tool` | `server:tool` |
| `mcp_server` | Server id |
| `model` | Model id |
| `agent` | Agent id |
| `adapter` | Adapter name |
| `filesystem` | Path or pattern |
| `network` | Host or pattern |

## Rule shape

```yaml
policy:
  rules:
    - kind: tool
      subject: "fs.*"          # glob
      effect: allow            # allow | deny | require_approval
      max_risk: medium         # optional ceiling for matches
      required_permissions: ["fs.read"]
      reason: "read access to the workspace is expected"
      priority: 10             # higher wins; longer patterns win within a tier
```

## Precedence

Most specific first: `priority * 1000 + len(subject) - wildcards * 10`. An
explicit `deny` beats everything regardless.

## Worked examples

**Read-only run.** Nothing may modify anything:

```yaml
policy:
  default_effect: deny
  rules:
    - {kind: tool, subject: "fs.read_file", effect: allow}
    - {kind: tool, subject: "fs.list_directory", effect: allow}
    - {kind: tool, subject: "orchestrator.*", effect: allow}
```

**Everything dangerous needs a human:**

```yaml
policy:
  approval_threshold: medium
```

**Refuse outright rather than asking:**

```yaml
policy:
  deny_threshold: high
```

**Explicit grants only.** Nothing runs unless a rule names it:

```yaml
policy:
  require_explicit_tool_grant: true
  rules:
    - {kind: tool, subject: "fs.read_file", effect: allow}
```

**Restrict one MCP server:**

```yaml
mcp:
  policies:
    - server: production-db
      max_risk: low
      deny_tools: ["*write*", "*delete*", "*drop*"]
      require_approval_above: none
```

## Versioning

`policy.version` is recorded on every execution, so a past run can be tied to the
policy that governed it.

## Auditing

Every decision is recorded. Refusals appear as `tool.denied` and `mcp.denied`
with the reason and the risk assessment.
