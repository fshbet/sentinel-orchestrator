# ADR-007: Budgeted, compacted context assembly

**Status:** accepted

## Context

Sending everything known to every model call fails three ways: it exceeds small
context windows, it costs money on large ones, and it buries the relevant signal.
The platform must work with a 4k local model and a 200k hosted one without the
caller knowing which.

## Decision

Context is a dedicated subsystem (`context/`). A caller declares what is
relevant; the manager ranks it, budgets it against the *selected* model's window,
compacts what does not fit, and only then builds the request.

Compaction is deterministic and ordered: drop the least relevant unpinned items,
fold history into a compressed note, then truncate the largest items. Only if all
of that fails is a model asked to summarise - because a summarisation call is
itself a model call with its own cost and failure mode.

Truncation always marks the gap. Large values become artifact or task-result
*references* rather than being inlined.

Token estimation is tokenizer-free and deliberately over-estimates.

## Consequences

**Good.** The platform never knowingly exceeds a window. Behaviour is consistent
across providers. Compaction is reproducible, so a failure is debuggable.

**Cost.** Over-estimation leaves some window unused. Lexical relevance ranking has
no notion of synonymy; the interface accepts an embedding-backed scorer, but
shipping one would mean a dependency and a model call in the retrieval path.

## Alternatives rejected

- **Send everything.** Fails on small windows.
- **Exact tokenizers per provider.** A dependency per provider for a bound that
  only needs to be conservative.
- **Model-summarise first.** Slower, costlier, and not reproducible.
