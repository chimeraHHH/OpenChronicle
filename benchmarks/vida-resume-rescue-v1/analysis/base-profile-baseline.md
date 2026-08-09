# Safe base-profile comparator

The deterministic `base_profile` comparator uses the production source
validators, rejects malformed source envelopes, excludes facts in unresolved
conflicts, retains every other reviewed fact, and marks every extracted job
requirement as `missing_evidence`.

This comparator is intentionally untailored. It establishes that exact factual
preservation alone cannot satisfy relevant-fact selection or requirement
mapping gates. It is neither a Vida measurement nor evidence of ATS or hiring
performance.
