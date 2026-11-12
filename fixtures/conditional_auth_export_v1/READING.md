# Reading instructions — conditional-auth-v1 synthetic export fixtures

Purpose: test what a reader OUTSIDE the runtime can establish from the exported record alone, per the audit-contract review scoped in google/adk-python#6099.

## How to read each case

| Case | What it tests | Reader should establish | Reader should NOT establish |
|---|---|---|---|
| case1 | authorization WITHOUT obligations | decision=PASS; obligations field explicitly empty | that any obligation ever existed |
| case2 | authorization WITH typed obligation | decision=PASS; obligation obl-1 exists, due=before, fulfilled=null (not yet evaluable) | that the obligation was fulfilled — recording an obligation is distinct from evidence of fulfillment |
| case3 | refusal correlated to attempted call | decision=VETO tied to args_digest c4d9... | that non-execution occurred — missing tool response alone does not prove it; check side-effect observations separately |
| case4 | unsupported profile / distinct diagnostics | diagnostic E_UNSUPPORTED_PROFILE; decision and obligations are null | any answer about authorization at all — an unanswerable question must not become a PASS |
| case5 (v1.1.0) | obligations key absent (policy-driven omission) | diagnostic E_POLICY_OMITTED_FIELD; decision stays null | that the omission was policy-driven rather than a lossy export — the record alone cannot establish it; also NOT equivalent to case1's empty array |

## The narrow question this fixture answers

Under profile conditional-auth-v1, a reader outside the runtime CAN distinguish
authorization-without-obligations (case1) from authorization-with-obligations (case2)
**iff** the obligations field is explicitly exported (empty array vs non-empty).
If the export format drops the field, byte-integrity checks still pass while the
semantic distinction is silently lost — which is the failure mode the schema rule
("MUST be present even when empty") is designed to prevent. case5 (added v1.1.0)
shows the diagnostic path for that failure mode: the field absent entirely, distinct
from an explicit empty array and from case4's null-under-unsupported-profile.

## Boundaries

- Synthetic data only; no proprietary engine, no real user data.
- Semantic states PASS/DEGRADE/VETO are illustrative; naming decisions belong to the assignees.
- Unsupported conclusions stay UNKNOWN (case4 and case5 show the diagnostic paths).
