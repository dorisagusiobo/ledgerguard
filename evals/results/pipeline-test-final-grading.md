# Manual grading: held-out pipeline notes

Source: [pipeline-test-final.json](pipeline-test-final.json), live `test` split,
seed 7, recorded 2026-11-05 at revision `aa70fd2`, pipeline run
`825e9c3a6bba4448a0d0d8306e960e2a`. Graded 2026-11-05 against the five questions
in [`investigator_eval.py`](../../src/ledgerguard/investigator_eval.py).

## Method

P = pass; R = revise. A strict overall pass requires all five checks. This is
manual editorial grading by the coding assistant, not an independent human study
or an automated score. The same rubric as the [dev grading](investigator-dev-full-grading.md)
is used: certainty must concern support for the finding, rather than intent or
cause. The structured uncertainty field counts. Usefulness asks whether the note speeds
a decision. Sentence/character targets are reported as presentation defects, not
automatic failures of usefulness or accuracy.

Read every saved summary alongside its saved finding, calculation, citation and
source evidence. Cross-checked seed-7 synthetic fixtures regenerated offline,
including split-purchase dates, amounts and distinct POs, plus the model-visible
payload and read-only tool schemas. No model calls or new hold-out evaluation were
made. Audit logs retain tool names and row counts, not historical arguments or
returned rows; exact search windows and negative-search claims cannot be fully
verified. Regenerated data is a cross-check, not a historical tool transcript.

Monthly spending aggregates establish neither invoice-level arithmetic/PO history
nor a unit price or billing cadence. The unusual-amount rule is an invoice total
above **three times the maximum historical monthly total**; its reported excess
is not an unpaid balance, a unit remainder, or evidence of three months' billing.
The investigator receives clause IDs and redacted calculations, not full clause
text or item descriptions. That explains some limitations without making the
notes' unsupported conclusions correct.

## Grades

| Invoice suffix | Accurate | Evidence named | Finding certainty | No fraud accusation | Useful | Reason / required revision |
| --- | --- | --- | --- | --- | --- | --- |
| 0003 | R | P | P | P | R | Correct duplicate pair, date, PO and $156 total. Monthly totals cannot establish no prior invoice anomalies or absence of mitigating context; remove that claim. |
| 0004 | R | P | P | P | R | Correct duplicate pair and $208 total. Remove the broad no-prior-anomalies claim; shorten four sentences to three. |
| 0043 | P | P | P | P | P | Correct ($14 − $12) × 14 = $28, clause ID and $196 invoice total. Medium uncertainty explicitly concerns independent contract verification. Four sentences and the irrelevant monthly-history aside need editing. |
| 0044 | P | P | R | P | R | Correct $202 versus $195 and $7 gap. Medium uncertainty is about cause while arithmetic support is clear; separate those and avoid introducing intentional-overcharge speculation. Monthly history only shows stable spending. |
| 0045 | P | P | P | P | P | Correct $1,056, missing PO and POL-1; low uncertainty fits. Four sentences and the unnecessary monthly-total comparison obscure a simple policy finding. |
| 0046 | R | P | P | P | R | Correct $2,530 + $2,760 = $5,290, two-day gap and distinct POs; separate orders are explicitly a possibility. A two-invoice sum versus monthly spend does not establish the claimed significant departure. Four sentences. |
| 0048 | R | P | R | P | R | Correct $676 threshold excess, but $400 monthly spend is not a unit rate and the rule does not test whole-unit multiples. Medium uncertainty fails to distinguish the clear threshold breach from unknown cause. Five sentences. |
| 0049 | R | P | P | P | R | POL-4 prohibits cash equivalents; it is not an amount-anomaly rule. Comparing a $182 invoice with monthly spend is misleading. Name the prohibited-item evidence if available, otherwise explicitly state that it cannot be verified here. Five sentences. |
| 0051 | R | P | P | P | R | Correct ($14 − $12) × 13 = $26 and explicit contract-verification uncertainty. A $182 invoice is not a notable departure merely because monthly totals were $2,000; remove that inference. |
| 0052 | P | P | R | P | P | Correct $98 versus $91 and $7 gap; correctly admits history has no comparable line-item data. Medium uncertainty concerns clerical versus intentional cause, not the clearly supported arithmetic finding. |
| 0053 | P | P | P | P | P | Correct $1,188, missing PO, POL-1 and low uncertainty. Shorten four sentences and remove the monthly-spend aside. |
| 0054 | R | P | P | P | R | Correct $2,920 + $2,560 = $5,480, two-day gap, distinct POs and individual amounts below $5,000. A monthly aggregate does not establish this pair is a spending spike. Five sentences. |
| 0056 | R | P | R | P | R | $326 is excess over the $1,200 rule threshold. Three monthly amounts plus a remainder is an arithmetic decomposition, not evidence of billing cadence. Explain the actual rule and separate uncertainty about cause. Five sentences. |
| 0057 | R | P | P | P | R | $78 and lack of duplicates do not support a POL-4 violation; the saved finding cites holiday gift certificates. Explain the evidence limit instead of claiming amount-based support or unspecified thresholds. Five sentences; 536 characters also exceeds the requested under-500 target, though it passes the hard 600-character schema. |

Strict all-five pass: **3/14** (0043, 0045, 0053). Accuracy: **5/14**; evidence named: **14/14**;
finding certainty: **10/14**; avoids fraud accusations: **14/14**; usefulness
under this strict rubric: **4/14**. Naming evidence does not mean it is relevant
or supports the conclusion. These grades concern saved notes, not detection.
Several notes contain useful arithmetic or duplicate facts despite needing revision.

## Interpretation

The pipeline completed **14/14 investigations with zero referrals**. That is a
reliability result, not evidence that all notes are ready for reviewers. The
underlying findings caught **14/14** planted problems with correct scored amounts
and citations. Only three notes pass all five checks. The notes remain the weak part: irrelevant historical comparisons,
confusion between finding certainty and intent, failure to explain semantic policy
evidence, and verbosity. No note accuses anyone of fraud, but that check alone
would miss these issues. No source report or summary was edited to improve a grade.
