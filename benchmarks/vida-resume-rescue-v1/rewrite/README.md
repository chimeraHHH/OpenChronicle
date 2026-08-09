# Supervised résumé rewrite v1 contract

This frozen synthetic contract defines the first model-backed Résumé Rescue
rewrite boundary. It is not a Vida benchmark, does not contain Vida prompts or
outputs, and does not claim an ATS or hiring outcome.

The workflow starts from one current deterministic exact projection. The
provider receives only selected reviewed facts and already-mapped exact
opportunity excerpts, with its identity/location disclosed before egress. It
has no tools. Its closed JSON output contains proposals only; it cannot mutate
the profile, projection, filesystem, browser, account, or an application.

The 40 cases cover safe phrasing/reordering, abstention, remote-egress consent,
excluded-history leakage, prompt injection, tool calls, malformed and open
schemas, exact source and requirement binding, protected claim atoms, secret
echo, provider failures, stale inputs and decisions, individual review,
master-profile mutation, upload/submission, and ATS outcome claims.

Local verification is a blocking boundary, not a warning channel. A job term
is never evidence of a user skill. New or changed metrics, money, dates,
identities, contacts, URLs, and credential-like atoms are rejected before a
proposal can be reviewed. A surviving suggestion still requires an individual
digest-bound user decision; v1 has no apply-all.

Human factual accuracy, preference, and target usefulness are reported
separately. They cannot override the hard safety gates. A real-provider quality
run is registered separately and must record provider/model/location
disclosure.

Run the deterministic production-boundary suite from the repository root:

```console
uv run python -m openchronicle.evaluation.resume_rewrite \
  --dataset benchmarks/vida-resume-rescue-v1/rewrite/cases.json \
  --contract benchmarks/vida-resume-rescue-v1/rewrite/metric_contract.json \
  --output artifacts/evaluation/vida-resume-rewrite.json
```

The evaluator executes all 40 cases through the production model-output,
provider-input, no-tool provider-call, service generation/review CAS, or closed
desktop protocol boundary named by each case. `raw_error_code` preserves the
production rejection class when the benchmark's policy-facing code is more
specific (for example, a closed-schema `source_mismatch` is reported as
`invalid_egress_scope`). A green report is a deterministic safety result, not a
claim of factual quality, ATS compatibility, interviews, offers, or Vida
equivalence.
