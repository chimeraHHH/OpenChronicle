# OC-Vida Reply Rescue v1

This frozen development benchmark evaluates OpenChronicle's clean-room Reply
Rescue contract. It is not a Vida benchmark and contains no Vida prompts,
outputs, private implementation details, or claimed Vida scores.

The 17 synthetic cases contain 13 valid manual snapshots and four invalid
inputs. They cover recipient/reply-all ambiguity, participant identity, quoted
prompt and schema injection, secret handling, missing attachments, unconfirmed
money and legal promises, explicit commitments, multilingual replies, unsafe
style instructions, NUL input, invalid modes, and size bounds. The committed
metric contract separates reply usefulness from hard safety gates.

`required_claim_ledger_rate` is deliberately narrow: it checks that fixture
claims explicitly required for review appear in the claim ledger. It is not a
semantic proof that every generated statement is supported. Provider promotion
still requires human claim review and the hard zero-unsupported-commitment
contract; the deterministic grader must not impersonate that judgment.

The default comparator copies the conversation into the reply body. It makes no
model call and deliberately fails question, warning, claim-ledger, secret, and
injection gates. It is a reproducible floor, not Vida or a quality baseline.

```bash
uv run python -m openchronicle.evaluation.reply_rescue \
  --dataset benchmarks/vida-reply-rescue-v1/fixtures/cases.json \
  --contract benchmarks/vida-reply-rescue-v1/json/metric_contract.json \
  --output scratch/vida-reply-rescue-report.json
```

Add one or more `--corpus path/to/provider-results.json` arguments to compare
complete recorded provider runs. Raw provider corpora and generated reports
belong under ignored `scratch/` until provenance, cost, and run conditions are
reviewed for publication.

To run the frozen cases through the exact production template, JSON mode,
no-tool call, and output validator, explicitly enable `[reply_rescue]` and
configure `[models.reply_rescue]`, then run:

```bash
uv run python -m openchronicle.evaluation.reply_rescue \
  --dataset benchmarks/vida-reply-rescue-v1/fixtures/cases.json \
  --contract benchmarks/vida-reply-rescue-v1/json/metric_contract.json \
  --run-configured-provider \
  --provider-output scratch/reply-rescue-provider-corpus.json \
  --output scratch/vida-reply-rescue-provider-report.json
```

The runner rejects invalid cases before model egress. Every accepted case
records a closed error code, latency, model identity, provider location, and
template version/digest. Provider exceptions are never copied into the corpus;
an output and an error cannot both claim the same accepted case.
