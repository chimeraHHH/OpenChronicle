# JSON Resume interoperability contract

This contract pins the public JSON Resume v1 schema and records the current
reference implementations inspected for behavior. It is not a compatibility
claim for every JSON Resume theme or résumé builder.

The pinned upstream schema bytes have SHA-256
`a07eedd3d86ac5bb61d72e136d788269e35baf42c35391e15d0be39b3dc5a4bd`.
A production export containing the OpenChronicle extension passed a direct
`jsonschema.Draft7Validator` check against those exact bytes on 2026-08-09.

Import accepts the canonical schema's extension-friendly objects, but unknown
paths, unmapped values, contact fields, external URLs, and third-party
references enter review or omission ledgers instead of silently disappearing.
Known fields produce either exact-field candidates or visibly labeled
deterministic composites. Neither form enters the master profile without an
explicit candidate selection and a new reviewed provenance binding.

Export starts from one immutable selected projection, never the full master
profile. Summary, skill, certificate, and language facts use the narrow
standard mapping. Facts that cannot be represented without inventing an
employer, institution, or project identity remain in an explicit loss ledger
and a namespaced OpenChronicle extension. The upstream schema permits such
extensions, but other tools may ignore them; the export warning says so.

The tests under `tests/test_json_resume_interop.py` cover malformed and
duplicate-key JSON, unknown fields, injection-shaped text, contact/reference
omission, exact and composite admission, review tampering, projection-only
export, standard loss reporting, privacy/ownership warnings, and untrusted
OpenChronicle-extension round trips.
