# Stage 1: provenance, review-first memory, and Daily Wrap

This document defines the implemented Stage 1 backend contract for this fork.
It is a clean-room design inspired only by publicly described product behavior.
It does not claim to reproduce Vida's private schemas, prompts, scheduling, UI,
or algorithms.

## Public research boundary

Vida publicly describes Daily Wrap as an automatic summary of a day's progress
and accomplishments. Its public material also describes cross-application
context and user controls for viewing, editing, deleting, pausing, and excluding
memory sources. The public pages do not disclose Daily Wrap's internal fields,
trigger time, source selection, classifier, prompt, storage, or notification
semantics. Those details below are independent OpenChronicle design decisions.

Primary sources:

- [Vida public product page](https://web-prod.vida.app/)
- [Vida product site](https://vida.app/)
- [Vida privacy policy](https://vida.app/privacy/)
- [Vida terms](https://vida.app/terms/)

## Implemented data flow

```text
observation JSON
  -> timeline_block
  -> session + event memory_entry
  -> memory_candidate (pending/conflict)
  -> reviewed durable memory_entry

event memory_entry + timeline_block + session
  -> daily_wrap_item
  -> one canonical daily_wrap per (local_date, IANA timezone, scope)
```

Every edge is projected into SQLite. Long-term Markdown entries also carry a
machine-readable `oc-provenance` comment so `rebuild-index` can reconstruct the
memory-entry projection. The comment is excluded from the parsed body and FTS.

Timeline block creation and observation-edge insertion share one SQLite
savepoint. A block replay resolves the row that actually persisted for its
time window before repairing edges, so `INSERT OR IGNORE` cannot produce an
edge to a discarded random ID.

Full index rebuild parses every visible Markdown entry first, then resolves
memory-entry dependencies to a fixed point. It is independent of filename
order: a `project-*` derivative can safely depend on a lexically later
`user-*` source. Missing, changed, cyclic, or tombstoned dependencies remain
unindexed rather than being revived.

## Review-first memory

The classifier receives only these tools:

- `read_memory`
- `search_memory`
- `propose_memory_candidate`
- `commit`

It has no append, create, supersede, compact, file, shell, browser, messaging,
or other side-effect tool. Screen-derived and retrieved strings are explicitly
treated as untrusted quoted data.

Each proposal must cite one or more evidence tokens that were present in the
current context or returned by a read tool. Unknown tokens are rejected. A
classifier run is keyed by its session, source window, and source hashes;
proposal slots are stable within that run. If a provider retry changes wording,
the first durable proposal remains canonical and a replay mismatch is recorded
instead of creating a duplicate review card.

Proposal creation revalidates every cited source while holding an immediate
SQLite write transaction. The candidate row, conflict decision, and provenance
edges commit atomically; a matching replay can repair a missing edge left by a
legacy writer, but cannot silently replace different evidence. Candidate
content is also rejected if it contains a canonical entry heading or an
`oc-provenance` marker, so quoted screen text cannot manufacture a second
Markdown record.

Candidate lifecycle:

```text
pending <-> conflict
   |           |
   +-> applying -> accepted
   +-> rejected
```

Editing, approval, and rejection use optimistic version checks. Approval uses a
deterministic entry ID and `append_entry_once`. A replay must match the existing
body, tags, and provenance exactly; mismatches fail instead of being silently
accepted. All proposal/review/purge mutations, every provenance-bearing
deterministic append, and full provenance rebuilds share a re-entrant
cross-process local lock. Dependency hashes are checked inside that fence. This
closes approval-versus-forget, conflict-classification, and late derived-entry
writes against a purge closure while still leaving ordinary reads concurrent.

Trusted local commands:

```bash
openchronicle memory candidates
openchronicle memory show <candidate-id>
openchronicle memory edit <candidate-id> --content "..." --tags tag1,tag2
openchronicle memory approve <candidate-id>
openchronicle memory reject <candidate-id> --reason "..."
openchronicle memory forget <candidate-id> --yes
```

Pending proposal plaintext and mutation commands are intentionally not exposed
over MCP. MCP remains read-only.

## True purge semantics

An explicit `forget` first computes the transitive candidate → accepted entry →
Daily Wrap item/revision closure and commits content-free tombstones for every
affected artifact in the same immediate SQLite transaction. Only then does it
remove:

1. derived wrap revisions and item/whole-wrap provenance;
2. the accepted Markdown entry;
3. its FTS and file projections;
4. candidate plaintext and provenance;
5. the tombstones after all prior steps succeed.

If the process dies, every normal daemon startup (even when Daily Wrap is
disabled) or a later trusted memory command resumes the authorized plan. While
a tombstone exists, `append_entry_once` and `rebuild-index` refuse to resurrect
that entry from Markdown. A provenance-bearing append that waited behind the
purge must revalidate its source and fails closed after the source disappears.
Rejection is not purge: rejected proposals remain review history until
explicitly forgotten.
The purge plan always includes the deterministic approval entry ID, even if a
crash happened after Markdown materialization but before `applied_entry_id` was
persisted; it also detects legacy split entries that cite the candidate.
The closure combines projected SQLite edges with a scan of valid embedded
Markdown frames. This catches a cross-file derivative whose atomic Markdown
rename succeeded but whose FTS/provenance projection crashed. Legacy
`supersede_entry` replacements now embed and project an explicit dependency on
the superseded entry, so they enter the same transitive purge closure.
SQLite connections use `secure_delete`, and successful purge requests a
truncating WAL checkpoint. This is best-effort local erasure, not a promise to
erase APFS snapshots, backups, provider logs, or data already copied elsewhere.

Bulk `clean memory` and `clean captures` use the same deny-first posture:
file-level tombstones and empty search projections commit before unlinking. If
an unlink fails, the command exits nonzero and retains the tombstone, so direct
reads and index rebuilds cannot expose the leftover plaintext. Memory paths
must use the exact on-disk NFC/case spelling; aliases that macOS would otherwise
resolve to the same file are rejected before tombstone checks.
MCP and classifier reads additionally validate the full transitive chain of
embedded memory dependencies against current canonical content, not merely the
syntax of one frame or a possibly stale FTS row. Validation is cycle-safe and
fails closed when any ancestor is missing, changed, tombstoned, or cyclic.
Raw-capture read/search/current-context operations hold
the same cross-process collection lock as capture cleanup and rebuild, so a
cleanup tombstone cannot commit in the middle of a response.

## Daily Wrap contract

Inputs are selected by an IANA-timezone-aware local day window and include
intersecting timeline blocks, session records, and event-daily entries. The
input digest binds:

- local date, timezone, and exact UTC boundaries (including 23/25-hour DST days);
- source IDs, paths, timestamps, and content hashes;
- timeline coverage and open/unreduced-session gaps;
- current capture-policy digest;
- workflow version.

The remote payload is capped at 400 evenly sampled records and 2,000 characters
per record. Serialized records have a 175,000-byte budget, coverage gaps are
counted/truncated instead of carrying unbounded source IDs, and the complete
JSON payload has a hard 200,000-byte UTF-8 limit. Truncation marks coverage
`partial`. The payload contains bounded text excerpts and opaque evidence
tokens, never screenshots or a complete AX tree. Static instructions and the
JSON data are separate messages, all screen text is labeled untrusted, and the
call has no tools.

Canonical identity is `(local_date, timezone, scope)`. The same published input
digest returns the existing row without another model call. Late sources or
policy changes update the same row as `revision + 1`; revisions and their own
provenance edges are retained in a separate table. Refresh failure preserves
the last successful output while recording the failed attempt. A lease that is
automatically raised to cover the configured timeout/retry budget prevents
concurrent workers from duplicating provider calls. The final publish checks
both lease token and input digest. The input is recomputed after the provider
call, and the publish transaction revalidates the current hash of every
whole-wrap and item-level source. A stale or concurrently purged result is
discarded.

Output categories are:

- `completed`
- `progressed`
- `open`
- `blocked`
- `needs_review`

Every item has a stable ID and at least one fully serialized evidence reference.
Its `text` and `supporting_text` must be identical to an exact excerpt in a
cited record; Stage 1 intentionally does not permit model-written paraphrases.
Every accepted item is marked `untrusted_activity_quote=true`. Excerpts that
look like prompt-control instructions are rejected, including NFKD-normalized
and default-ignorable Unicode variants, common confusables, role/result
constraints, links/secrets, shell-shaped text, and clause-start direct
imperatives. Downstream readers must never execute commands or infer user
authorization from a quote.
`completed`, `open`, and `blocked` additionally require an explicit matching
signal in that excerpt. Label/value status fields recognize category-specific
labels and reject complete scalar contradiction values while preserving normal
action descriptions; unchecked checkboxes are never treated as completion.
Negated, resolved, future, conditional, disabled-state, and question-form
claims are rejected conservatively. Activity alone cannot become completion.
Unknown, missing, duplicate, or unsupported evidence is dropped and the wrap
becomes `partial`.
Provider failure or invalid JSON produces a failed attempt with no invented
natural-language fallback. An empty day produces an idempotent empty wrap
without calling a model.

Commands:

```bash
openchronicle daily-wrap run --date YYYY-MM-DD --timezone Area/City
openchronicle daily-wrap show --date YYYY-MM-DD --timezone Area/City
openchronicle daily-wrap list
openchronicle provenance trace daily_wrap <wrap-id>
```

Scheduled generation is opt-in (`enabled = false` by default). When enabled,
the daemon targets the previous local day at 00:05, performs post-schedule
startup catch-up, and rechecks failures/late evidence within a configurable
grace window. Each date has an independent supervised monitor, so a 24-hour
late-data grace period cannot skip the next calendar day's scheduled run.
Timezone, cadence, grace, and minimum lease are configurable under
`[daily_wrap]`; one-off CLI run/show commands use that configured timezone when
`--timezone` is omitted. Provider work runs on a dedicated daemon thread.
Scheduler cancellation revokes only its matching lease and returns promptly,
so shutdown does not wait for a stuck provider and a late result cannot publish.
Claim creation and cancellation share a short handshake lock: cancellation
either prevents a future claim or observes and revokes the committed claim.

## Privacy and failure posture

- Capture exclusions are applied before collection and re-evaluated through
  retained observation provenance before Daily Wrap synthesis when a policy is
  restrictive. Unverifiable derived records are omitted and create a coverage
  gap.
- Coverage that does not span the full day, an open session, an unreduced
  session, excluded evidence, or a rejected model item produces `partial`.
- Stored job errors contain only an exception class and generic label, not a
  provider message that could repeat sensitive prompt content.
- Provenance-bearing files are currently refused by the legacy LLM compactor;
  losing source edges is worse than postponing compaction.
- Daily Wrap is Suggest-only. It does not send, schedule, upload, modify files,
  create tasks, or perform any Action Plane operation.
- Enabling a cloud-backed `[models.daily_wrap]` intentionally sends the bounded
  evidence payload to that provider. Keep it disabled or use a local model if
  that egress is not acceptable.

## Verified test gates

The Stage 1 suite covers:

- fresh and legacy capture schema migration;
- atomic block/observation edges and replay dedupe;
- Markdown provenance round-trip and rebuild;
- deterministic-entry body/tag/provenance mismatch rejection;
- candidate idempotency, atomic candidate/provenance writes, run-slot replay,
  concurrent conflict classification, edit CAS, proposal/approval evidence
  revalidation, approval replay, and materialization-crash recovery;
- transitive purge, crash injection, stale-FTS cleanup, revision erasure, and
  rebuild non-resurrection, including concurrent approve/purge and late
  provenance-append fencing;
- filename-order-independent provenance rebuild, provenance-linked supersede,
  and purge discovery of Markdown-written/projection-missing derivatives;
- deny-first bulk cleanup with injected unlink failures, path-case/Unicode alias
  attempts, memory/capture in-flight read serialization, transitive
  stale-dependency and cycle exclusion, and rebuild exclusion;
- wrap grounding, unsupported-claim rejection, provider failure, stale-input
  discard, publish/purge races, last-known-good refresh, revisioning, exclusive
  leases, cancellable late providers, byte-bounded payloads and gaps,
  prompt-injection and strong-state negation isolation, policy exclusion,
  concurrent scheduler catch-up, retry cutoff, configured CLI timezone, and DST
  23/25-hour boundaries.

## Deliberate Stage 1 limitations

- The native macOS permissions/review/source-drawer shell is not built yet;
  trusted CLI commands are the current mutation UI.
- Candidate operations currently materialize append/create-append behavior.
  Deterministic reviewed supersede remains pending.
- The provenance-aware compactor remains pending; it fails closed for entries
  carrying evidence.
- There is no notification/outbox card yet, so exactly-once semantics currently
  cover the canonical job and MCP/CLI read surface, not a system notification.
- Retroactive policy re-evaluation needs retained raw observation metadata. If
  it has expired, the affected derived source is omitted rather than guessed.
