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
- `search_activity_evidence`
- `propose_procedure_candidate`
- `propose_memory_candidate`
- `commit`

It has no direct append, create, compact, file, shell, browser, messaging, or
other side-effect tool. `propose_memory_candidate` may stage a reviewed append
or supersede operation, but never mutates Markdown. The constrained procedure
tool stages only a text workflow/checklist/template in `procedure-*`; explicit
user-authored procedures need direct evidence and observed/inferred procedures
need cited canonical event entries from at least two independent sessions. It
does not execute or authorize computer actions. Screen-derived and retrieved
strings are explicitly treated as untrusted quoted data.

Each proposal must cite one or more evidence tokens that were present in the
current context or returned by a read tool. Unknown tokens are rejected. A
classifier run is keyed by its session, source window, and source hashes;
proposal slots are stable within that run. If a provider retry changes wording,
the first durable proposal remains canonical and a replay mismatch is recorded
instead of creating a duplicate review card.

The candidate separately binds explicitly cited claim-support references and
the complete prompt-visible input closure. The first keeps review/source UX
precise; the second remains authoritative for policy changes and transitive
forget. Claim support cannot name a source outside the full closure, and either
projection changing blocks approval.

Classifier-created candidates also carry typed fact semantics: a normalized
global `subject_key`, `assertion_kind` (`user_asserted`, `observed`, or
`inferred`), and optional `valid_from`/exclusive `valid_to`. These values are
review-visible and digest-bound. Approval stores them in the canonical Markdown
provenance frame; compaction round-trips the frame, while current recall excludes
scheduled and expired facts. A typed supersede must preserve the subject slot,
and another active proposal with that slot conflicts even if it targets a
different Markdown file.

Published Memory can export the authorized current projection as JSON or
human-readable Markdown. Python assembles and digest-binds the content without a
model call; the native shell validates the closed payload and writes a new
private local file only after the user chooses a save path. Export never uploads
or changes the canonical memory store.

Published Memory also supports a direct user correction path. The form submits
the selected entry's stable revision digest with edited content and tags. A
matching request deterministically supersedes the old canonical entry, retains
that entry as provenance-linked history, and preserves typed subject, assertion,
and valid-time metadata. A concurrent Markdown change produces a version
conflict instead of an overwrite. This path is entirely local and does not use
a model. The desktop can also request that selected current fact's authorized
immutable lineage. This bounded, revision-bound read returns clean newest-first
versions and their source identities only on demand; superseded text never
enters the default current-memory snapshot.

The same current-fact identity supports explicit permanent forget. The preview
walks from the selected revision to the oldest provenance-linked predecessor,
then collects every downstream correction, reviewed proposal, candidate-owned
container, and dependent Daily Wrap. Commit reconstructs and digest-compares
that closure before the native confirmation and crash-resumable tombstones are
written. All versions in the chain are removed together, so deletion cannot
restore a superseded predecessor as current.

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
openchronicle memory adoptions
openchronicle memory screen-adoption <adoption-id>
openchronicle memory show <candidate-id>
openchronicle memory edit <candidate-id> --content "..." --tags tag1,tag2
openchronicle memory approve <candidate-id>
openchronicle memory reject <candidate-id> --reason "..."
openchronicle memory forget <candidate-id> --yes
```

The adoption screen is an explicit, no-tool classifier call over one exact
digest-bound Prompt/Reply Rescue output. It reuses the production procedure
validator and can only stage a pending candidate. The command discloses the
configured provider first; source deletion invalidates the candidate, and a
separate `memory approve` remains required to publish local Markdown.

Pending proposal plaintext and mutation commands are intentionally not exposed
over MCP. MCP remains read-only.

## True purge semantics

An explicit `forget` first computes the transitive candidate → accepted entry →
Daily Wrap item/revision closure, plus any now-empty candidate-created target
file that is safe to unlink. It commits content-free tombstones for every
affected artifact in the same immediate SQLite transaction. Only then does it
remove:

1. derived wrap revisions and item/whole-wrap provenance;
2. the accepted Markdown entry;
3. their FTS projections;
4. unchanged empty candidate-owned Markdown files and their file projections;
5. candidate plaintext and provenance;
6. the tombstones after all prior steps succeed.

If the process dies, every normal daemon startup (even when Daily Wrap is
disabled) or a later trusted memory command resumes the authorized plan. While
a tombstone exists, `append_entry_once` and `rebuild-index` refuse to resurrect
that entry or file from Markdown. Candidate-file projections are removed in
the same transaction as purge intent, so a crash cannot expose a sensitive
description or tag through an older direct SQLite reader. A provenance-bearing
append that waited behind the purge must revalidate its source and fails closed
after the source disappears.
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
Files created during candidate approval carry an owner ID and a digest bound to
their original stable frontmatter and path. Every verified affected file is
included in the preview's `files` / `counts.memory_files`. Empty files are
unlinked. If unrelated entries or freeform content survive, their body is kept
while owner markers are removed, description is made generic, tags are rebuilt
from surviving canonical entries, and the file projection is replaced.
Pre-existing files are never deleted or metadata-sanitized; only canonical
entries proven to be inside the reviewed closure are removed. A symlink,
non-regular path, changed ownership metadata, or any damaged provenance frame
fails closed. Finalization is revalidated under the normal store/file locks,
and crash replay retains the tombstone until Markdown and SQLite agree.
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

All privacy-sensitive public reads use one canonical review-operation →
capture-store fence through response serialization. Provider calls retain the
review-operation fence for their full duration but hold the capture-store lock
only while taking and revalidating authoritative snapshots. Explicit capture
cleanup also takes review → capture, so application-managed memory/timeline/
capture cleanup either commits before model egress or waits for it to finish;
ordinary capture persistence can continue during slow network I/O. Publication
then repeats current-source validation under the short capture lock. The locks
do not freeze arbitrary external filesystem editors, so compaction additionally
compares its exact pre-call Markdown snapshot before writeback.

Provenance-free memory is a policy root only with an explicit reserved
`oc-origin:manual-v1` heading marker. Automation-origin and legacy unmarked
entries are quarantined. Every provider-derived artifact still requires at
least one live authorized observation or explicit manual root even when capture
policy currently has no exclusions; an unrestricted policy is not permission
to infer missing ancestry.

## Daily Wrap contract

Inputs are selected by an IANA-timezone-aware local day window and include
intersecting timeline blocks, session records, and event-daily entries. The
input digest binds:

- local date, timezone, and exact UTC boundaries (including 23/25-hour DST days);
- source IDs, paths, timestamps, and content hashes;
- timeline coverage and open/unreduced-session gaps;
- current capture-policy digest, including bundle/app/title exclusions and both
  URL allow/exclude lists;
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
the last successful output while recording the failed attempt. Claiming an
existing row changes only scheduler state; its published window, workflow,
coverage, output, digest, and revision switch together with the new immutable
revision in the final transaction. A lease that is
automatically raised to cover the configured timeout/retry budget prevents
concurrent workers from duplicating provider calls. The final publish checks
both lease token and input digest. The input is recomputed after the provider
call, and the publish transaction revalidates the current hash of every
whole-wrap and item-level source. A stale or concurrently purged result is
discarded.

Every record visible to the remote Wrap prompt is retained in the whole-wrap
source closure, and each item inherits that complete closure; model-selected
citations remain a display subset, not the authorization boundary. The mutable
canonical job output is exact-bound to one immutable revision row, its input
digest, output, coverage, and source set. Public reads verify that the parent
and revision edges still agree and repeat leaf authorization before returning,
so editing only a job row or laundering parent edges cannot publish a stale
Wrap. A typed empty Wrap has its own exact schema and binding checks.

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

Public Daily Wrap reads expose only the authorized immutable revision: its
canonical day/window identity, workflow and coverage versions, published input
digest, revision number, and grounded output. The mutable scheduler job state
(active input digest, running/failed state, attempts, leases, errors, and job
timestamps) remains internal. Consequently, a failed or in-flight refresh does
not alter the public last-known-good card or leak unbound operational metadata.

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
Repairing an invalid cached revision uses a compare-and-swap against the row
that was actually rejected. If another caller has already repaired it, the
losing caller re-authorizes the newer revision only when it publishes the same
input digest; a different digest is retried or reported busy instead of being
returned as the requested result.

## Privacy and failure posture

- Capture exclusions are applied before collection and re-evaluated through
  retained observation provenance before Daily Wrap synthesis under every
  policy configuration. Unverifiable derived records are omitted and create a
  coverage gap even when no exclusion is configured. Under active URL policy,
  retained provenance is valid only for schema-v5,
  policy-v3 `url_metadata_only` observations with one explicit HTTP(S) URL;
  `ContextService` re-evaluates that URL against the current lists. Legacy,
  malformed, content-bearing, scheme-less, or newly denied observations fail
  closed rather than becoming derived evidence.
- Coverage that does not span the full day, an open session, an unreduced
  session, excluded evidence, or a rejected model item produces `partial`.
- Stored job errors contain only an exception class and generic label, not a
  provider message that could repeat sensitive prompt content.
- The LLM compactor accepts only currently authorized manual roots and live
  provenance-bearing derivatives. Automation roots, unmarked legacy entries,
  invalid origins/frames, stale projections, and policy-denied branches are
  refused before egress. Exact provenance lists survive, and any entry cited
  by downstream memory has a byte-frozen body.
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
- Candidate operations support append/create-append and deterministic reviewed
  supersede. Supersede binds an exact old entry revision, preserves both sides'
  evidence, and restores the previous current value if the replacement is purged.
- Provenance-aware leaf compaction is implemented; it deliberately cannot
  rewrite bodies that downstream memory cites. Cross-entry semantic merging is
  not attempted.
- There is no notification/outbox card yet, so exactly-once semantics currently
  cover the canonical job and MCP/CLI read surface, not a system notification.
- Retroactive policy re-evaluation needs retained raw observation metadata. If
  it has expired, the affected derived source is omitted rather than guessed.
