# Skills

A skill is reusable knowledge or procedure an agent can be given: how to
approach a kind of problem, what to check, what the house conventions are.

It is **content, not code**. A skill cannot grant a permission, call a tool, or
change what an agent is allowed to do. It can only tell an agent how to think
about work it was already authorised to do. That separation is what keeps
skills safe to load from a directory.

This is the one place Markdown belongs in an executable system: skills are prose
for a model to read, and prose is what Markdown is for.

## Writing one

```markdown
---
id: careful-reading
version: 2.1.0
description: How to read source material without over-claiming.
tags: [analysis]
applies_to: [analysis]
requires: [evidence-first]
---

Separate what the source states from what it implies.

Quote before paraphrasing. Where the source is ambiguous, say so rather than
choosing the reading that suits the conclusion.
```

Front matter plus body. Every field is optional except the id, and even that
falls back to the filename. YAML and JSON are also accepted, for skills that are
generated rather than written.

| Field | Means |
|---|---|
| `id` | How it is referenced. Defaults to the filename. |
| `version` | Retained; executions pin what they used. |
| `description` | One line, shown in listings. |
| `tags` | Free-form labels. |
| `applies_to` | Capability ids this skill is relevant to. |
| `requires` | Other skills this one builds on, resolved transitively. |

## Loading

```yaml
skills:
  directories: [".orchestrator/skills"]
  definitions:
    - id: house-style
      description: How results are written here.
      content: "State what is unverified before what is."
```

Directories are searched recursively for `.md`, `.yaml`, and `.json`. Plugins
register skills through `registry.add_skill(...)`.

```bash
orchestrator skills --json
```

**The platform ships none.** A shipped skill would be an assumption about what
work looks like, which is exactly what the capability system exists to avoid.

## Giving them to an agent

```yaml
agents:
  definitions:
    - id: analyst
      capabilities: [analysis]
      skills: [careful-reading, house-style]
```

A task can also ask for one directly:

```yaml
metadata:
  skills: [careful-reading]
```

The runtime composes what the agent and task declare, resolves everything those
skills require, and puts the result in front of the model as part of its
instructions.

## Composition

`requires` is resolved transitively and emitted in dependency order, so a skill
reads after the ones it builds on:

```
top requires middle, middle requires base
  → base, middle, top
```

A cycle in `requires` does not hang and does not fail. The back edge is dropped
and every skill is still delivered, because imperfectly ordered guidance is
better than none.

## A missing skill is a warning, not a failure

Guidance is not capability. If a declared skill is not registered, the task runs
without it and the gap is recorded:

```
plan.verified  {"warnings": [{"code": "unknown_skill", ...}]}
```

`orchestrator validate` reports the same thing for agent definitions before
anything runs. This is deliberate: an agent missing a piece of advice should
still work, whereas an agent missing a *tool* should not silently proceed.

## Versioning

Every version ever registered is retained, and re-registering the same id and
version with different content raises — publish a new version instead.

An execution pins the version of every skill at start:

```python
execution.workflow.skill_versions   # {"careful-reading": "2.1.0", ...}
```

so a completed run can be replayed against exactly the guidance it had (spec
section 69). Resolution honours those pins, which means editing a skill does not
change what an in-flight execution sees.

## Skills versus capabilities versus tools

Three different things, easily conflated:

| | Answers | Grants |
|---|---|---|
| **Capability** | What kind of work is this? | Routing, plus the tools and permissions it declares |
| **Tool** | What can be done? | The ability to act |
| **Skill** | How should this be approached? | Nothing |

A capability says an agent *can* do analysis. A tool lets it read the file. A
skill tells it how to read carefully. Only the first two are enforcement
boundaries.
