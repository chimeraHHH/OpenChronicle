# Runtime reliability evidence

`manifest.json` is the versioned acceptance policy for the storage/runtime
portion of GitHub issue #2. `storage_harness_complete` is true only when the
same run contains both:

- an exact replay of at least 10,000 deterministic capture JSON files; and
- a healthy soak observed for at least 86,400 monotonic seconds, sampled no
  less often than the manifest permits and within every resource bound.

A successful short run is deliberately `incomplete`. A 10k + 24-hour run can
set `storage_harness_complete=true`, but schema v1 still leaves the top-level
status `incomplete`: its synchronous worker has no production daemon queue and
therefore cannot provide the queue-depth evidence required to close issue #2.
This prevents a synthetic constant zero from becoming a false acceptance.

The replay injects two missing search projections, one stale projection, one
recognized orphan capture temp, and one durable capture-file tombstone. It
then uses production cleanup/reconciliation APIs, compares exact identifier
sets through SHA-256 digests, runs SQLite and FTS integrity checks, and repeats
reconciliation to prove idempotence.

Verification regenerates the deterministic source and expected-visible
digests from `requested_capture_count`, checks exact reconcile/fault counts,
checks fixture/final storage accounting and checkpoint state, and applies the
same manifest ceilings to replay and soak metrics. Worker duration and
cycle/index accounting must also agree exactly with the parent envelope.

The verifier checks monotonic sample coverage (first sample, every adjacent
gap, terminal gap, and strict ordering); a declared interval or a single
sample cannot stand in for 24 hours of observations.

Reports contain metrics, counts, identifier-set digests, and fixed status
envelopes only. They never include capture fields, prompts, provider output,
environment values, or exception messages. Reports are intentionally not
checked into this directory.

Reports are unsigned. Verification proves closed-schema policy compliance and
internal consistency only, not origin or truthfulness. Preserve the trusted
runner log containing the printed artifact SHA-256 and compare it with the
retained JSON artifact. A party able to replace both the report and its trusted
log can construct a new self-consistent report; this verifier is not a digital
signature or remote attestation system.
