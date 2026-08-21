# ADR-011: The core depends only on the standard library

**Status:** accepted

## Context

The obvious choice for the domain model is a validation library such as Pydantic:
less code, better errors, free serialisation.

But a dependency in the core is a dependency in every deployment - including
air-gapped ones, constrained ones, and ones that already pin an incompatible
version. The platform is also meant to be embeddable, and an embedder inherits
whatever the core imports.

## Decision

The core (`core/`, `agents/`, `planning/`, `validation/`, `recovery/`,
`context/`, `tools/`, `mcp/`, `observability/`) imports only the standard
library. The domain model is dataclasses plus a small reflection-based serialiser
(`core/domain/serde.py`).

Everything else is an extra: `cli`, `api`, `yaml`, `http`.

Optional dependencies degrade rather than fail. YAML config falls back to JSON;
`jsonschema` falls back to a structural check; OpenTelemetry falls back to
nothing; HTTP providers raise a clear message naming the extra to install.

## Consequences

**Good.** `pip install universal-orchestrator` pulls nothing. No version conflicts
for embedders. MCP over stdio works in a bare install.

**Cost.** About 150 lines of serialisation code a library would have provided, and
error messages less rich than Pydantic's. The serialiser is directly tested.

## Alternatives rejected

- **Pydantic in the core.** Good library, wrong layer. It is used in the API,
  where FastAPI already requires it.
- **Optional Pydantic with a fallback.** Two code paths for one job.
