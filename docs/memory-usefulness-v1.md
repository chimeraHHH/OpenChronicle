# Memory usefulness report v1

## Outcome

OpenChronicle can now answer a narrow, auditable question locally:

> Which exact reviewed memory revisions conditioned Prompt Rescue outputs, and
> which distinct output revisions did the user explicitly mark as used?

Run:

```bash
openchronicle memory usefulness
openchronicle memory usefulness --json
```

The implementation preserves memory, Prompt Rescue, adoption, provenance, and
ranking business state while reading SQLite and Markdown. Like other local CLI
commands, first-run initialization can create the default config, log, and
SQLite schema files. It adds no domain table, LLM call, background worker,
ranking signal, decay rule, consolidation pass, or computer-use capability.

## Trusted join

The report follows this chain:

```text
exact memory_entry revision
    -> prompt_rescue provenance edge + memory_context_json
    -> prompt_rescue output identity
    -> immutable artifact_adoption digest
```

The memory revision identity is the complete tuple:

```text
kind + path + id + timestamp + content_hash
```

For every ready Prompt Rescue job, the report validates the job projection and
requires its durable provenance sources to exactly equal:

```text
[prompt_rescue_input, *memory_context_refs]
```

The comparison includes timestamp and content hash, not only SQLite edge keys.
A mismatch quarantines the conditioned output instead of returning a partly
trusted usefulness row.

Prompt Rescue joins adoption records only by:

```text
artifact_kind = prompt_rescue
artifact_id = prompt_rescue_job.id
```

It deliberately does not require a historical adoption version or digest to
equal the job's current version or digest. Edits preserve immutable adopted
artifact snapshots, and editing back to an old digest replays the original row.

## Metrics

For each exact memory revision:

- `conditioned_output_count`: distinct ready Prompt Rescue jobs that carried
  the revision in their exact durable context.
- `unedited_adoption_count`: immutable adoption rows whose output had not been
  edited before adoption.
- `edited_adoption_count`: immutable adoption rows whose output had been edited;
  these remain ambiguous and do not count as strong positives.
- `dependent_artifacts`: output IDs, current output digest/version, and exact
  adoption IDs/digests/versions without artifact text.
- `current_status`: `current`, `superseded`, `expired`, or `missing`.

An adoption row represents one distinct adopted artifact digest, not every use
event. Repeated **I used this** actions for the same digest are idempotent.

The global report also includes a no-memory descriptive arm, exact-revision
tracking coverage, quarantined output count, and invalid-row counts. Adoption
rates are associations only. They are not randomized uplift, negative feedback,
or causal credit for any one memory when a job used multiple revisions.

## Revision state

The report resolves status in this order:

1. an unresolvable path, entry, timestamp, hash, or provenance frame is
   `missing`;
2. an exact historical body with a complete verified supersession chain is
   `superseded`; a forged marker or isolated strike is `missing` with
   `invalid_supersede_chain`;
3. an exact unsuperseded revision beyond `valid_to` is `expired`;
4. an exact active, authorized, current revision is `current`;
5. other revisions that cannot currently be re-authorized are `missing`.

For superseded entries, hashing uses the unstruck indexed body, so the Markdown
`~~...~~` control wrapper does not turn a valid historical revision into a false
`missing` result. `current_status_reason` exposes the bounded classification
reason without returning text.

## Privacy and non-goals

The JSON contains identifiers, timestamps, hashes, counts, status, and adoption
metadata. It never contains:

- memory body;
- rough prompt, target, audience, constraints, or selection binding;
- generated or edited artifact body;
- adoption artifact snapshot.

No adoption means only “no immutable adoption record was observed.” It is not a
rejection. The report does not reinforce memory merely because it was retrieved,
does not use model self-confidence, and does not automatically hide old memory.

## Research basis

The design follows the lightest reusable ideas from current systems rather than
copying their infrastructure:

- [Hindsight dry-run](https://github.com/vectorize-io/hindsight/blob/3295716cafcc593b6a2cdebd03dd71373b091859/skills/hindsight-docs/references/developer/api/mental-models.md#dry-run-preview-a-refresh-before-it-happens)
  separates retrieved facts from used facts and exposes the outcome.
- [GitHub Copilot's memory system](https://github.blog/ai-and-ml/github-copilot/building-an-agentic-memory-system-for-github-copilot/)
  supports citation plus read-time verification instead of trusting a stale
  offline projection.
- [MemOS local retrieval](https://github.com/MemTensor/MemOS/blob/be68e2fb5370866bd5e2b188bb3d22bd13b49e09/apps/memos-local-plugin/core/retrieval/ALGORITHMS.md)
  separates result value from similarity; v1 observes result value but does not
  feed it back into ranking.
- [Engram](https://github.com/blakestone-x/engram/tree/fce46b1aaf1f52d9fbe97202989b7ed88782b0b6)
  keeps Markdown authoritative and preserves deprecated history.

Automatic usefulness reranking remains deferred until there are enough real or
frozen adoption trajectories for held-out comparison. Even then, usefulness can
only be tested as a bounded late tie-break, not as permission to auto-promote or
delete memory.
