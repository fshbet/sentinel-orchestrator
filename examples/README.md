# Examples

Deliberately generic. The platform ships no domain examples, because a domain
example is an assumption about what work looks like, and everyone would inherit
it (see `docs/research/ORCHESTRATION_RESEARCH.md`).

| File | Shows |
|---|---|
| `embedded.py` | Driving the platform from Python, including a host-supplied model |
| `external_worker.py` | A task worker in any language, over the subprocess adapter |
| `plugin_example.py` | A plugin adding a capability, a tool, and a validator |
| `approval_flow.py` | Handling a run that stops to ask a human |

Run any of them with the repository's `src/` on the path:

```bash
PYTHONPATH=src python examples/embedded.py
```

The generic workflow *shapes* live in `workflows/` at the repository root:
sequential, parallel, evaluator-optimizer, orchestrator-worker, research, and
problem-solving.
