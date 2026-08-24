# Runtime reliability audit

The Stage 0 reliability harness produces machine-verifiable evidence for the
10,000-capture replay and 24-hour storage-soak portions of issue #2. It uses
synthetic, deterministic capture records and makes no model or network calls.
It is only one part of the acceptance surface: **issue #2 remains open** until
a qualifying 24-hour run, production daemon-queue evidence, and downstream
cascade replay/recovery evidence all exist.

## Runtime correctness boundary

Focused runtime tests currently exercise these fail-closed contracts outside
the synthetic soak worker:

- One daemon-generation, suspend-aware monotonic wall clock supplies capture
  persistence, session boundaries, and timeline production. A capture's exact
  timestamp is assigned under the capture-store lock and reused by the
  post-write session hook. The first frame after wake cuts a stale session
  before starting the next one.
- A sparse per-window root binds the complete capture manifest, current delayed-
  egress policy, typed outcome, block identity, and raw lifecycle. Child
  receipts bind each exact path/observation/hash/timestamp while raw is live or
  retiring.
- Retention and size cleanup are all-or-none per receipted window. The durable
  `live → retiring → retired` protocol commits tombstones/FTS removal before
  unlink, resumes partial unlinks after restart, and drops child manifests only
  after every old member is absent. Screenshot stripping remains per-file.
- Late evidence behind the watermark rewinds coverage. It can re-materialize a
  block only while no durable downstream/session progress depends on the old
  block. Otherwise raw evidence is retained and the watermark remains stalled.
- An invalid timeline projection is a durable coverage gap: no receipt is
  issued and coverage cannot advance past it.
- For populated windows, block projection, provenance, root outcome, child
  manifest, and watermark are one SQLite publication. A final source/policy
  change rolls the publication back rather than exposing partial coverage.
- The producer synchronously validates only `live`/`retiring` roots whose raw
  manifests can still be replayed. A durable 256-row cursor incrementally
  audits historical blocks for missing upgrade roots, and retained child
  receipts use batched instant-key anti-joins. Retired root/block/source proof
  is validated fail-closed when evidence is read. This keeps the minute tick
  bounded instead of deeply revalidating all history without weakening the
  consumer trust boundary.

These tests establish safety behavior, not end-to-end automatic recovery.
There is no implemented and validated cascade that invalidates and rebuilds
reducer entries, classifier state, Daily Wraps, and other dependents after a
consumed block receives late evidence. Therefore “fail closed” must not be read
as “eventually converges without intervention.”

## Short smoke

Run a small smoke during ordinary development:

```bash
uv run python scripts/run_runtime_reliability_audit.py \
  --report /tmp/openchronicle-runtime-smoke.json \
  --replay-count 100 \
  --soak-seconds 10 \
  --sample-interval-seconds 1 \
  --work-interval-seconds 0.25

uv run python scripts/verify_runtime_reliability_report.py \
  /tmp/openchronicle-runtime-smoke.json --expect incomplete
```

`incomplete` here is expected. A short smoke can establish functional
convergence and exercise resource sampling, but it cannot satisfy the 10,000 +
24-hour acceptance gate.

## Acceptance run

Run the long audit on a stable local or self-hosted macOS runner:

```bash
uv run python scripts/run_runtime_reliability_audit.py \
  --report /tmp/openchronicle-runtime-24h.json \
  --replay-count 10000 \
  --soak-hours 24 \
  --sample-interval-seconds 30 \
  --work-interval-seconds 1

uv run python scripts/verify_runtime_reliability_report.py \
  /tmp/openchronicle-runtime-24h.json --expect storage-complete
```

The soak deadline and both observed durations use monotonic time. Storage
completion requires at least 86,400 observed seconds in both the parent
monitor and the worker, a successful worker exit, SQLite/FTS integrity, at
least 95% coverage for every required metric, and every sample staying within
the versioned limits in `tests/runtime/reliability/manifest.json`. Sampling is
scheduled against fixed monotonic target times. The verifier independently
checks the first sample, strict ordering, maximum adjacent gap, and terminal
gap, so a declared interval or a single sample cannot fake continuous
coverage.

The monitor samples worker RSS, CPU, file descriptors, threads, descendants,
database/WAL/SHM/capture-buffer sizes and counts, and owned temp files/bytes. It
writes deterministic local capture fixtures, exercises SQLite projection
writes and checkpoints, and performs cleanup plus reconciliation at shutdown.
This is a storage/runtime soak; it does not claim macOS Accessibility
permission coverage, provider/network coverage, production daemon queue
coverage, or downstream cascade replay coverage.

If a sampled resource crosses its manifest limit, the monitor stops its owned
worker immediately and records a failed report instead of allowing a runaway
24-hour process to keep consuming resources.

The monitor starts the worker in a new process session, retains its exact
`Popen` handle, and only sends TERM/KILL to that still-live owned session. The
worker environment removes likely credential variables and test failpoint
authorization. Temporary audit roots are private and deleted after the report
has been written.

## Report semantics

- `storage_harness_complete=true`: functional checks passed, replay count is
  at least 10,000, and the bounded storage soak lasted at least 24 hours.
- top-level `complete`: reserved for a later report schema that also verifies
  the production daemon queue. Schema v1 cannot emit this state.
- top-level `incomplete`: checks passed but the scale/duration is below storage
  acceptance, or storage acceptance passed while production queue evidence is
  still absent.
- `failed`: evidence is malformed, a recovery/integrity check failed, a
  process failed, metric coverage is insufficient, or a bound was exceeded.

The verifier recomputes resource maxima, timeline coverage, and status from
raw samples. It enforces a closed key schema and rejects fields capable of
carrying capture text, prompts, credentials, or provider content.

## Trust boundary

The JSON report is unsigned. The verifier establishes only that it follows the
closed schema, obeys the versioned policy, and is internally consistent with
the deterministic fixture algorithm and its raw samples. It does **not** prove
who ran the audit, that the reported samples came from a real process, or that
an attacker did not regenerate an entirely self-consistent report.

For reviewable evidence, retain the trusted runner's stdout/stderr alongside
the report artifact. Both runner and verifier print the report's SHA-256; keep
that digest in the trusted CI/self-hosted-runner log and compare it with the
stored artifact. Cryptographic runner identity, signing, and external build
attestation remain separate future work.

Accordingly, verifier output includes `internally_consistent`,
`verification_scope=closed-schema-and-internal-consistency-only`, and
`authenticity_verified=false`; `valid=true` must be read within that scope.

Even a valid `storage_harness_complete=true` report does not close #2. A trusted
24-hour artifact, real production daemon-queue soak, and downstream-level
cascade replay/recovery acceptance are still outstanding.
