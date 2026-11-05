# Manual grading: investigator dev full

Source: [investigator-dev-full.json](investigator-dev-full.json) (2026-10-31, revision `40ffb3a`, seed 7).
Checklist: [`investigator_eval.py`](../../src/ledgerguard/investigator_eval.py) docstring.

Verified against the saved report and seed-7 fixtures on 2026-11-05; preserved here
from the local grading draft so the review is no longer confined to ignored `runs/`.
This review credits Nova 0027 as useful despite its presentation defects; the
local draft had failed it for verbosity/irrelevant context. Thus the final Nova
all-five count is 4/12 rather than the draft's 3/12.

## Method

P = passes; R = revise. Columns are the five checklist questions: accuracy,
named evidence, certainty about the finding, avoids calling anything fraud,
and reviewer usefulness. A note passes overall only if all five pass. This is a
manual editorial judgment by the coding assistant, not independent human
validation or an automated quality metric. The certainty column
concerns support for the finding, applied retrospectively to old notes.
The structured uncertainty field counts as an expression of certainty. Format
targets are noted separately; exceeding three sentences does not automatically
fail usefulness if the note still helps a reviewer decide faster.

Checked the saved summaries against current rule calculations, model-visible
payloads, read-only tool schemas, and seed-7 fixtures regenerated in a temporary
directory. No model calls were made. Exact historical tool arguments/results are
not preserved in this report, so historical search-window claims cannot be fully
verified. Regenerated fixtures are a cross-check, not a recovered call transcript.
Monthly spending totals cannot establish past invoice arithmetic, past PO compliance,
or the cause or intent behind a charge.

## Nova

| Invoice suffix | Accurate | Evidence | Certainty | No fraud accusation | Useful | Reason / required revision |
|---|---|---|---|---|---|---|
| 0001 | P | P | P | P | P | Matching amount, PO, date and counterpart; low uncertainty fits. |
| 0002 | P | P | P | P | P | Concrete duplicate match; appropriately calls it a possible duplication error. |
| 0027 | P | P | P | P | P | Correct $34 calculation and explicit contract-verification caveat help the reviewer. Remove irrelevant history/uniqueness discussion and shorten four sentences as presentation improvements. |
| 0028 | R | P | P | P | R | Correct $7 gap; lack of duplicates cannot establish an isolated error or exclude a pattern of misreporting. |
| 0029 | R | P | P | P | R | Correct missing-PO finding; $2,000 monthly totals do not establish a pattern of missing POs. |
| 0030 | P | P | P | P | P | Identifies both invoices, $5,280 combined total and seven-day window. Could improve by stating that each is individually below $5,000. |
| 0032 | R | P | R | P | R | $1,200 is the rule's three-times-monthly threshold, not evidence of three expected payments. Error/payment-processing explanation is speculative; medium conflates finding support with cause. |
| 0035 | R | P | P | P | R | Correct $60 finding, but history and absence of duplicates do not validate contracted rates or the overcharge arithmetic. Name the actual rate/quantity calculation. |
| 0036 | R | P | P | P | R | Correct $7 gap. Related-invoice results are not evidence of another invoice with the same details or that this is isolated. |
| 0037 | R | P | R | P | R | Missing PO is clear. Monthly totals do not support misclassification/error; medium reflects uncertainty about cause rather than the policy finding. |
| 0038 | R | P | P | P | R | Correct pair and $5,670 total, but “manipulation ... to avoid detection” asserts unsupported intent. State a potential split requiring review. |
| 0040 | R | P | P | P | R | Correct $578 excess, but three payments and a likely one-time adjustment/additional charge are unsupported. PO “matches” lacks a specified comparator. |

Strict all-five pass: **4/12**. All 12 name evidence. The single automated fraud
keyword hit (0037) says “rather than fraud”: it is not an accusation and passes
check 4. The intent claim in 0038 still fails accuracy/usefulness despite having
no fraud keyword.

## Sonnet

| Invoice suffix | Accurate | Evidence | Certainty | No fraud accusation | Useful | Reason / required revision |
|---|---|---|---|---|---|---|
| 0001 | P | P | P | P | P | Strong attribute match and correctly aligned low uncertainty; monthly-history aside is unnecessary. |
| 0002 | P | P | P | P | P | Concrete match plus a clearly labeled possible amended-invoice explanation. |
| 0027 | R | P | P | P | R | Correct rate, quantity, clause ID and $34 result; comparing a $238 invoice with $2,000 monthly totals does not establish an outlier. Four sentences also exceed the requested three. |
| 0028 | R | P | R | P | R | Correct $7 gap, but monthly totals cannot show “no comparable anomalies.” Medium concerns intent, while evidence clearly supports the arithmetic finding. Remove intentional-overcharge speculation. |
| 0029 | P | P | R | P | R | Clear threshold and missing PO; closing certainty statement is about administrative intent, not the finding. Five sentences exceed the requested three. |
| 0032 | R | P | R | P | R | Correct $438 excess, but this is above a three-times-maximum-monthly threshold, not a general multiple-of-$400 rule or established extra charge. Medium concerns cause; four sentences exceed three. |
| 0035 | R | P | R | P | R | Correct $60 calculation; the model-visible calculation explicitly includes 14, 12 and 30. The item description is redacted, not the numeric unit price. Monthly totals cannot establish no prior anomalies. |
| 0036 | R | P | R | P | R | Correct $7 gap, but tool rows cannot show no historic arithmetic anomalies or certify 0033 has no similar issue. Low field conflicts with the claim that the arithmetic finding cannot be confirmed; four sentences exceed three. |
| 0037 | R | P | P | P | R | Excellent distinction between high finding certainty and unknown intent. Remove inference about earlier PO handling from monthly spend; four sentences exceed three. |
| 0040 | R | P | R | P | R | Correct $578 threshold excess; flat-rate/subscription and three-month billing interpretations remain unverified. Medium does not distinguish a clear threshold breach from an unknown cause; five sentences exceed three. |
| 0030 | — | — | — | — | — | No saved valid note: summary-schema referral. Cannot grade rejected text from this JSON. |
| 0038 | — | — | — | — | — | No saved valid note: summary-schema referral. Cannot grade rejected text from this JSON. |

Strict all-five pass: **2/10 available notes**; **2/12 selected groups** have a
passing saved note under this rubric. Completion was **10/12**, a separate metric.
All 10 available notes name evidence and avoid fraud accusations.

## Historical follow-up (not a held-out result)

Both requested schema descriptions are now updated, with the hard 600-character
limit unchanged. A new Sonnet run reaching 12/12 would establish completion for
that run; it would still need note-quality grading. The description change directly
targets length headroom and uncertainty semantics, but does not by itself address
the unsupported-history/intent claims above. No paid rerun was performed here.

The later held-out pipeline evaluation is graded separately in
[pipeline-test-final-grading.md](pipeline-test-final-grading.md). It does not
replace these dev notes or retroactively improve their grades.
