# Saved evaluation evidence

These are the tracked copies of the evaluation evidence, preserved byte-for-byte.
They preserve the historical result, not a promise that another live invocation
would reproduce it. No held-out model responses were recorded for replay.

| Artifact pair | Provenance | Audit reconciliation |
| --- | --- | --- |
| `pipeline-test-final.json` + `pipeline-test-final-audit.jsonl` | Live held-out `test`, seed 7, 2026-11-05 UTC, revision `aa70fd2` | 153 audit rows; 69 model completions; cost $0.2915775 |
| `investigator-dev-full.json` + `investigator-dev-full-audit.jsonl` | Investigator-only `dev`, seed 7, 2026-10-31 UTC, revision `40ffb3a` | 283 audit rows; combined Nova/Sonnet cost $0.2540950 |

[manifest.json](manifest.json) records byte sizes and SHA-256 hashes of the four
original files. The two `*-grading.md` files are editorial assessments, kept
separate from the immutable source reports. See [the evaluation write-up](../../docs/EVALUATION.md).

The reports contain synthetic invoice evidence and reviewer-facing summaries.
The audits contain call metadata, tool names, counts, usage, cost and outcomes;
they contain no prompts, tool arguments/results, credentials or private model
reasoning. Artifact contents and schema fields were reviewed before copying.
They are evaluation evidence, not model inputs or CI fixtures. Keep them out of
prompts and replay inputs; the existing dev response recording remains unchanged.
