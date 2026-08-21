# Memory

Five things are kept apart on purpose, because conflating them is what makes
long runs collapse:

| Store | Holds | Lifetime |
|---|---|---|
| Execution state | Tasks, validations, failures, approvals | Durable, authoritative |
| Working memory | Notes and results from this run | The run |
| Long-term memory | Facts worth carrying across runs | Indefinite |
| Project knowledge | Facts about this project | Indefinite |
| Artifacts | Produced outputs | Durable, referenced |
| Audit history | Every decision, in order | Durable, append-only |

Conversation history is the *least* durable of these, not the store of record.

## Using it

```python
platform.memory.remember(
    "deployment window",
    "Fridays are frozen.",
    tier=MemoryTier.PROJECT,
    tags=["scheduling"],
)

hits = platform.memory.recall("when can we deploy", limit=5)
```

Agents write to working memory through `orchestrator.record_note`.

## Retrieval is selective

`recall()` scores and returns the best matches, never everything. The default
scorer is lexical overlap weighted towards the key - dependency-free and
predictable. It has no notion of synonymy.

An embedding-backed scorer can be supplied without changing any caller:

```python
MemoryStore(scorer=my_embedding_scorer)
```

## Eviction

Working memory is capped (`context.max_working_memory`). When full, the least
useful entries go first: fewest retrieval hits, then oldest. A repeated key in
the same tier updates rather than accumulating.
