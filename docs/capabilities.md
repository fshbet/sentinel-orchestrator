# Capabilities

A capability is an opaque string naming something that can be done. It is the
routing currency of the platform.

```yaml
capabilities:
  - id: analysis
    description: "Reads supplied material and draws conclusions from it."
    requirements:
      tools: ["fs.read_file"]
      skills: []
      permissions: ["fs.read"]
      model_capabilities: [long_context]
```

## Why not roles

Roles encode what work looks like. `developer` implies code; `analyst` implies
documents. Either makes the framework domain-specific.

Capabilities are supplied by whoever knows the domain: configuration, a plugin,
or the planner. **The core ships none.**

## How they route

1. A task declares `required_capabilities`.
2. An agent advertises `capabilities`.
3. The selector matches and scores.
4. The requirements of the matched capabilities are unioned into the agent's
   scope - so declaring a capability grants exactly the tools and permissions it
   says it needs, and nothing else.

## Declaring at runtime

```python
platform.capabilities.declare(
    "structured-extraction",
    "Extracts structured records from unstructured input.",
    tools=["fs.read_file"],
    permissions=["fs.read"],
)
```

The planner may reference a capability that does not exist yet; unknown ones are
dropped from generated tasks rather than being invented into existence.

## Not the same as a skill

A capability says what kind of work an agent can take on, and grants the tools
and permissions it declares. A **skill** says how the work should be approached
and grants nothing. Both are configuration; only one is an enforcement boundary.
See [skills.md](skills.md).

## Inspecting

```bash
orchestrator capabilities --json
```
