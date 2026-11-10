# Expected conclusions (specified separately from the data, per reviewer request)

## Conclusions a conforming reader draws from these fixtures alone
1. case1 vs case2: authorization-without-obligations and authorization-with-obligations are distinguishable (obligations: [] vs [{...}]).
2. case2: an obligation is RECORDED; fulfillment evidence is absent (fulfilled=null) — the reader must report "recorded, not evidenced".
3. case3: the VETO is correlated to the attempted call by args_digest; the reader cannot conclude non-execution without side-effect observations.
4. case4: an unsupported profile yields a distinct diagnostic (E_UNSUPPORTED_PROFILE); decision stays UNKNOWN, never PASS.
5. Diagnostics are disjoint: profile problems (E_UNSUPPORTED_PROFILE) vs chain problems (E_INVALID_CHAIN_LINK) vs evidence problems (E_EVIDENCE_UNAVAILABLE) vs policy-omitted args (E_POLICY_OMITTED_ARGS).

## Conclusions a reader must NOT claim
- That a PASS implies obligation fulfillment.
- That a VETO implies non-execution.
- That an absent diagnostics array (case1-3, empty) is the same as an unanswerable check (case4) — empty means "no diagnostics raised", null/unsupported means "the question cannot be answered under this profile".

## Pinned revision
See manifest.json (sha256 of every file + fixture revision tag). Any re-run of the
comparison must cite the manifest digest, so reviewers test the same material.
