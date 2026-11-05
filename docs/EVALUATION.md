# Evaluation results and note review

LedgerGuard's saved held-out run catches the planted problems, but its reviewer
notes still need revision. Completion, detection and note quality are measured
separately below. All documents are synthetic; no new live evaluation was run to
write this report.

## Held-out pipeline: 2026-11-05

Source: [pipeline-test-final.json](../evals/results/pipeline-test-final.json),
revision `aa70fd2`, seed 7, full `test` split (`limit=null`), live mode.
Extraction and policy use Nova 2 Lite; investigation uses Sonnet 4.6. These are
historical results for that revision, not a fresh evaluation of later changes.

| Measure | Saved result |
| --- | --- |
| PDFs selected / successfully extracted / exact | 19 / 19 / 19 |
| Rules-only baseline | 12/14 problems; 100% precision, 85.7% recall |
| Full pipeline | 14/14 problems; 100% precision and recall |
| Amount / citation accuracy | 100% / 100%, as scored against planted answers |
| Clean invoices wrongly flagged | 0/1 |
| Investigator groups completed | 14/14 |
| Referrals / skipped stages | 0 / none |
| Model calls / tool attempts | 69 / 42 |
| Total cost / cost per document | $0.2915775 / approximately $0.01535 |
| Wall time / time per document | 135.5 seconds / 7.13 seconds |
| Largest per-invoice run | 5 model calls, $0.0294448; within configured limits |
| Strict manual note pass | 3/14 saved notes |

Each of duplicates, contract overcharges, arithmetic errors, split purchases and
unusual amounts contributes two planted cases. Policy violations contribute four;
the policy checker adds the two POL-4 cases missed by rules alone. The scorer uses
PDF filenames for document identity and complete selected answer groups. Amount
and citation scores assess the findings, not every assertion in investigator prose.

The [audit log](../evals/results/pipeline-test-final-audit.jsonl) has 153 rows:
69 successful model calls plus authorization/completion pairs for 42 tools. Summed
recorded cost equals the report's $0.2915775. Stage costs also reconcile:

| Stage | Model calls | Cost | Provider-call time |
| --- | --- | --- | --- |
| Extraction | 19 | $0.0206296 | 58.42 seconds |
| Policy | 19 | $0.0081839 | 12.16 seconds |
| Investigation | 31 | $0.2627640 | 64.60 seconds |

Stage times measure provider calls; wall time includes pipeline work. Investigator
calls account for approximately 90% of reported cost. Tool audit rows are not
additional model calls or additional copies of the model charges.

## Manual note grading

The five questions in [`investigator_eval.py`](../src/ledgerguard/investigator_eval.py)
ask whether the note is accurate, names evidence, expresses certainty, avoids fraud
accusations and helps a reviewer decide faster. The coding assistant graded the
saved notes; this is an editorial assessment, not independent human validation.
A strict pass requires all five checks, with certainty about the finding.
Sentence/character targets are noted separately, not treated as automatic
usefulness failures. Per-note reasons and
verification limits are in the linked grading tables.

| Saved evaluation | Completion | All-five note passes | Coverage |
| --- | --- | --- | --- |
| [Earlier investigator dev, Nova](../evals/results/investigator-dev-full-grading.md) | 12/12 | 4/12 | Rules-only findings; 12 groups |
| [Earlier investigator dev, Sonnet](../evals/results/investigator-dev-full-grading.md) | 10/12 | 2/10 available notes | Two summary-schema referrals; 2/12 selected groups have a passing note |
| [Held-out pipeline, Sonnet](../evals/results/pipeline-test-final-grading.md) | 14/14 | 3/14 | Rules and semantic policy findings; 14 groups |

These runs differ in code revision, task scope and split. They do not establish
that Sonnet's quality declined or that Nova is preferable. The old dev report is
from 2026-10-31, revision `40ffb3a`, with costs $0.0531400 for Nova and $0.200955
for Sonnet. All 12 groups' findings remained unchanged under both models. The
architecture's separate 12/12 Sonnet completion result came from another dev run;
it must not be substituted for this earlier report's 10/12.

The held-out notes all name evidence and avoid fraud accusations. Five pass the
accuracy check and ten express certainty appropriately under this rubric. Three
pass every check (0043, 0045, 0053); four are useful under the rubric. Ten exceed
the requested three sentences, and INV-2026-0057
has 536 characters: valid under the hard 600-character schema, but above the
under-500 wording target. No summaries were truncated or rewritten for grading.

The recurring substantive problems are:

- Monthly spending totals are treated as invoice-level history, unit prices or
  billing cadence. They cannot establish past arithmetic correctness, PO handling
  or the cause of an unusual charge.
- Notes sometimes express uncertainty about clerical error versus intent despite
  a clearly supported arithmetic discrepancy. Finding support and cause should
  be stated separately.
- The POL-4 notes discuss amount and duplicate searches instead of the prohibited
  cash-equivalent evidence. The investigator's redacted input lacks the item text
  and full clause; it should state that limit rather than invent amount-based support.

The single schema-repair path can improve completion after successful tool data,
within existing turn and gateway limits. It does not validate narrative accuracy,
fix semantic evidence visibility or guarantee concise, useful prose. The held-out
audit log shows consecutive Sonnet calls with no tool between them for
INV-2026-0043, INV-2026-0046 and INV-2026-0054, indicating that the repair fired on
3 of 14 groups (31 investigation calls instead of 28). This is inferred because
no repair event is logged. Of those three notes, 0043 passes the strict grading;
0046 and 0054 do not. Those three investigations would likely have been referrals
without the repair.

## What this establishes, and what remains

The saved test result meets the 90% precision/recall detection targets on this
small synthetic split. It does not establish production accuracy: there is one
seed, 19 simple PDFs, only one clean test invoice and two semantic test cases.
The dev grading provides useful failure examples, not another held-out estimate.
No new hold-out call was made after inspecting or grading the test notes.

Reports and audits preserve outputs and usage, not full historical tool payloads
or a held-out response recording. Exact search-window assertions cannot be
reconstructed from audit metadata. Seed-7 regeneration checks the underlying
fixtures but is not a recovered call transcript. The held-out evaluation also does
not verify deployment, authentication or signed-in review through the web UI.

Keep this test run fixed as historical evidence. Narrative improvements should be
developed and graded on dev cases; a future final quality claim needs fresh,
unseen cases and independent reviewer assessment. See the
[evidence manifest](../evals/results/manifest.json) for integrity hashes and the
[evidence notes](../evals/results/README.md).
