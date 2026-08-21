# Context engineering

## The rule

Nothing hands a model "everything we know". A caller declares what is relevant,
the manager ranks it, budgets it against the selected model's window, compacts
what does not fit, and only then builds the request.

## Budget

```
usable input = (context_window - reserved_output) * (1 - safety_margin)
```

`reserved_output` is the model's max output, capped at half the window.
`safety_margin` defaults to 10%, covering estimation error.

Estimation is tokenizer-free (~3.6 characters per token) and deliberately
over-estimates. Exactness would mean a tokenizer dependency per provider for a
bound that only needs to be conservative.

## Compaction order

Deterministic, so a failure is reproducible:

1. **Drop** the least relevant unpinned items, lowest relevance first.
2. **Fold** remaining history into one compressed note.
3. **Truncate** the largest items, pinned ones last.
4. **Summarise** with a model - only if a summariser is configured and steps 1-3
   were not enough. A summarisation call is itself a model call with its own cost
   and failure mode, so it is the last resort, not the first.

Truncation always marks the gap (`[... N characters omitted ...]`). Silent loss
is worse than visible loss.

Compaction is audited as `context.compacted` with what was dropped, truncated,
and summarised.

## Relevance

| Item | Relevance | Pinned |
|---|---|---|
| Objective | 1.0 | yes |
| The task itself | 1.0 | yes |
| Success criteria | 0.9 | yes |
| Constraints | 0.95 | yes |
| Upstream task results | 0.85 | no |
| Assumptions | 0.6 | no |
| Artifact list | 0.55 | no |
| Recalled memory | 0.5 | no |
| History | 0.3-0.6, recent higher | no |

Pinned items are never dropped. They may still be truncated as a last resort,
visibly.

## References over inlining

Values over ~4000 characters become artifact or task-result references. A 40MB
result does not have to pass through a prompt to be usable downstream.
