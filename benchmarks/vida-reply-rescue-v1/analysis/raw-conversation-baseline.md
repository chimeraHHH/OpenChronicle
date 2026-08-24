# Raw conversation comparator

The deterministic `raw_conversation` comparator uses the production source
validator, rejects invalid snapshots, and otherwise copies the supplied
conversation into a schema-valid reply artifact with empty review ledgers.

This is intentionally unsafe and unhelpful. It establishes that the evaluator
detects quoted injection markers and secret echo, and that admission/schema
validity alone cannot satisfy the Reply Rescue contract. It is neither a Vida
measurement nor a model-quality claim.
