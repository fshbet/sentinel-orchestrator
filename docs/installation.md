# Installation

The core has **no third-party dependencies**. Everything beyond it is an extra.

```bash
pip install -e .                 # core only: engine, MCP over stdio, plugins
pip install -e ".[cli]"          # + orchestrator command line
pip install -e ".[api]"          # + REST API
pip install -e ".[http]"         # + HTTP model providers, HTTP MCP transport
pip install -e ".[yaml]"         # + YAML configuration files
pip install -e ".[all]"          # everything
pip install -e ".[dev]"          # + pytest
```

Python 3.11 or newer. Windows, Linux, and macOS are all supported; nothing in
the codebase assumes a platform.

## What each extra buys you

| Extra | Enables | Without it |
|---|---|---|
| `cli` | `orchestrator` command | Use the Python API |
| `api` | `orchestrator serve` | Use the CLI or the Python API |
| `http` | OpenAI-compatible, Anthropic, Ollama providers; HTTP MCP | stdio MCP still works |
| `yaml` | `config.yaml` | `config.json` works identically |

Optional pieces degrade rather than fail. `jsonschema` improves the schema
validator when present; OpenTelemetry is exported to when present and ignored
when not.

## Verifying the install

```bash
orchestrator health --json
orchestrator validate
```

`validate` checks configuration, agents, tools, workflows, and plugins without
running anything, and exits non-zero if something is wrong.
