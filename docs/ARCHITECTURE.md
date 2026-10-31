# LedgerGuard architecture

This document describes the planned Version 1. Built so far: the synthetic data and PDF
generator, extractor, rule checks, policy checker, bounded investigator and read-only
tools, scorer, per-step and pipeline evaluations, and governance gateway. Postgres
persistence, an independent audit sink, a persistent monthly ledger and investigator
checkpoints are implemented, along with the five-route review API and a local Next.js
review screen. Deployment is not built yet, and pgvector clause retrieval is deferred
(see step 3).

## 1. Problem

Accounts payable teams check vendor invoices against spending policies and contracts,
but manual checks miss duplicate charges, overbilling and purchases split to avoid
approval limits. LedgerGuard extracts each invoice, checks the charges, and reports
every finding with a dollar amount, source evidence and the exact policy or contract
clause. A person reviews every finding; LedgerGuard never pays or changes financial
records on its own.

## 2. User

Version 1 has one role: a **reviewer**. The reviewer sees invoices, findings, evidence,
citations and confidence in a review queue, and approves or rejects each finding with a
note. Approving a finding records a decision; it does not trigger a payment.

## 3. Flow

```text
Invoice PDF → Extractor → Rule checks → Policy checker → Investigator → Review queue → Human decision
                                                          (flagged only)
              └──────── Governance gateway: every model/tool call ────────┘
                        allow / block / needs approval · log · budget
```

1. **Extractor — one AI call plus code.** The model reads the PDF into a strict schema
   (vendor, invoice number, dates, currency, line items, totals) with the page and source
   text for each field. Code validates the schema and the arithmetic and sends uncertain
   extractions to review.
2. **Rule checks — code.** Explicit, testable rules check totals, exact and near
   duplicates, contract rate differences, approval thresholds, possible split purchases,
   and amounts far above a vendor's usual spending. Amounts use decimal arithmetic.
3. **Policy checker — code plus one AI call.** Code selects the clauses that need
   judgment (clauses enforced by the rule checks are excluded so findings are not
   double-counted), then one model call compares the invoice's line items with those
   clauses. The model returns only a clause ID, a line number and a confidence; code
   builds the citation, amount and evidence from stored data, so a citation cannot be
   invented. Low confidence or invalid output goes to review. The synthetic policy has
   one judgment clause, so every judgment clause is sent; pgvector retrieval will replace
   this once the policy is large enough to need search.
4. **Investigator — the one agent, flagged invoices only.** For invoices with at least one
   finding, the agent uses a small set of read-only tools (vendor history, related
   invoices, duplicate search) to gather evidence and explain uncertainty. It writes one
   note per finding and never adds, removes or changes findings. Notes may reference only
   invoice numbers from the findings or from tool results in that run. It has at most
   6 model turns and 10 tool calls, and the last turn must submit notes. An unusual
   amount is not treated as proof of fraud.
5. **Review queue — application code.** Validates findings, amounts, evidence and
   citations, saves them, and shows the page number and exact source text. Version 1
   shows no page image because the database deliberately stores no PDFs.
6. **Human decision — the reviewer.** The reviewer approves or rejects each finding
   through the API. A finding's status is its latest decision, or pending if none exists.
   The database prevents decisions from being edited or deleted. Nothing runs after
   approval in Version 1.

**Governance gateway — code, around every model and tool call.** It checks the tool is on
the allowed list, validates arguments, checks the budget, returns allow, block or needs
approval, and writes the result to the audit log before anything runs.

## 4. Technology

| Component | Purpose |
| --- | --- |
| Python + FastAPI | Rule checks, financial calculations, and the review API for runs, findings and decisions |
| LangGraph | The bounded investigator loop and its checkpoints |
| Amazon Bedrock (Converse API) | Model calls through the gateway; the model for each step is chosen by evaluation. Extraction and policy checking use Amazon Nova 2 Lite; investigation uses Claude Sonnet 4.6 |
| Postgres + pgvector | Records, audit log and LangGraph checkpoints, plus clause search once the policy needs it (Neon free tier, or local Docker) |
| Next.js | Review queue and evidence viewer |
| AWS Lambda or App Runner | Hosting for the API; choose during deployment based on run length |
| Terraform | Reproducible infrastructure and the AWS Budgets configuration |
| GitHub Actions | Tests and the synthetic evaluation suite on every change |
| Claude Code skills and hooks | Development guardrails; runtime safety is enforced by the application |

## 5. Data

All data is synthetic: fabricated invoices, a spending policy, vendor contracts and
vendor histories, including clean invoices and invoices with planted errors and known
expected findings. Version 1 supports USD only; other currencies go to review.

| Table | Main contents |
| --- | --- |
| `documents` | Invoice, policy and contract files: type, content hash, version, status |
| `vendors` | Synthetic vendors and their name variants |
| `invoices` | Vendor, invoice number, dates, totals, document reference, status |
| `line_items` | Description, quantity, unit price, amount, page and source text |
| `contract_rates` | Agreed rates per vendor and item, with effective dates |
| `clauses` | Policy and contract passages, clause IDs, page, version and embedding |
| `findings` | Type, invoices affected, amount at issue, calculation, evidence, citation, confidence, status |
| `decisions` | Finding, reviewer, approve or reject, note, timestamp |
| `audit_log` | Append-only record of model and tool calls, gateway results, usage and decisions |
| `runs` | Run status, model and prompt versions, token usage, cost and errors |

If no clause applies, the finding says "no applicable clause identified"; the system
never invents a citation.

## 6. Safety and governance

- **No financial actions.** The AI cannot pay invoices, issue refunds, edit bank details,
  or change contracts, policies or accounting records. No such tools exist in the system.
- **A person decides.** Every finding needs the reviewer's approval or rejection.
- **Untrusted documents.** Invoice text is data, never instructions.
- **Evidence or review.** Missing evidence or uncertain extraction goes to review instead
  of producing a confident, unsupported finding.
- **Audit trail.** Every model call, tool call, gateway result and decision is logged.
  The application cannot edit or delete audit entries. Secrets are never logged.
- **Tool limits.** Each run allows at most 10 tool-call attempts, including blocked
  attempts. Allowed calls append authorization before execution and completion
  afterward, linked by a call ID; blocked calls append one record. Argument values
  and tool results are never logged.
- **Run definition.** A run is one invoice through extraction, policy checking and investigation, sharing the $0.50 and 20-call caps; live evaluations require a separate explicit batch budget passed as `--max-cost`, in addition to invoice and monthly limits.
- **Cost limits.** $10 per month for model calls, $0.50 and at most 20 model calls per
  run, enforced in code before each call. The monthly limit wins: when it is reached, no
  more model calls run until the next month. AWS Budgets alerts at 80% and 100% are a
  backup, because alerts do not stop spending.

## 7. How we measure it

A versioned evaluation set of clean invoices and planted errors (duplicates, overcharges,
policy violations, split purchases, extraction errors). Expected answers are kept apart
from what the model sees.

| Metric | What it shows |
| --- | --- |
| Recall | Share of planted problems the system catches |
| Precision | Share of raised findings that are correct |
| False flags on clean invoices | How much unnecessary review it creates |
| Amount and citation accuracy | Whether each finding has the right amount and a clause that supports it |
| Extraction accuracy | Whether vendor, dates, line items and totals match the ground truth |
| Governance tests | Whether blocked calls are denied and the budget stop works |
| Cost and time per invoice | Whether it is practical to run |

Targets: at least 90% precision and 90% recall on held-out invoices, and every governance
test passing. These are goals, not results.

**Development results so far (`dev` split, seed 7; not held-out results).** Each step was
evaluated on its own:

| Step | Result |
| --- | --- |
| Rules only | 12 of 14 planted problems caught, 100% precision, 0 of 20 clean invoices flagged |
| Extraction (Nova 2 Lite) | 38 of 38 invoices extracted exactly; planted arithmetic errors preserved; $0.0011 per invoice |
| Rules + policy checker (Nova 2 Lite) | 14 of 14 caught, 100% precision, 0 of 20 clean invoices flagged; $0.0004 per invoice |
| Investigator (Claude Sonnet 4.6) | 12 of 12 investigations completed, findings unchanged, expected tool used every time; $0.0165 per investigation |

`dev` contains only two semantic policy cases, so the policy result is weak evidence.
One of them, "Store credit - staff rewards", is arguably not a cash equivalent; Sonnet
did not flag it. The label was kept as planted rather than changed after seeing results.
Investigator notes are graded by hand against a checklist; there is no automatic quality
score. CI runs the tests and a small evaluation with
recorded model responses; live-model evaluations run before releases within a set budget.

## 8. Decision log

| Decision | Why | Alternative considered |
| --- | --- | --- |
| LangGraph | Explicit state, a bounded investigator loop and checkpoints | Plain Python, which needs custom state and checkpoint handling |
| Review status derived from append-only decisions, no LangGraph interrupt | Nothing runs after approval in Version 1, so a paused graph would add complexity without behavior. Add an interrupt only if a step must run after approval. | LangGraph interrupt |
| Postgres + pgvector | One database for records, audit log, checkpoints and clause search | OpenSearch Serverless; a second service, and costly at this scale |
| Bedrock Converse API | One request format for every model, so models can be compared on the same evaluation | The Anthropic SDK, which works with Claude models only |
| Amazon Nova 2 Lite for extraction | On the 38 `dev` invoices it matched Claude Sonnet 4.6 on every extraction and detection measure at about 1/13 the cost ($0.0011 vs $0.0138 per invoice). The synthetic PDFs are clean, one page and one line item, so harder invoices need re-evaluation | Claude Sonnet 4.6, kept available through the gateway |
| Amazon Nova 2 Lite for policy checking | With the rules it caught 14 of 14 on `dev` with no false flags at $0.0004 per invoice; Sonnet caught 13 of 14 at $0.0034. The deciding case is debatable and `dev` has only two semantic cases | Claude Sonnet 4.6 |
| Claude Sonnet 4.6 for investigation | Its notes cite the evidence and separate certainty from intent. Nova 2 Lite completed every investigation but wrote unsupported claims, such as explaining away an unusual amount with low uncertainty and claiming the vendor split amounts to avoid detection. Notes are the only model text a reviewer reads, so quality outweighs the 4× cost ($0.0165 vs $0.0044 per investigation) | Amazon Nova 2 Lite |
| Send all judgment clauses; defer pgvector | Only one synthetic clause needs judgment, so retrieval would add a database without changing results | pgvector retrieval now |
| Synthetic data only | Planted errors give known answers, and no real company data is exposed | Real invoices, which are out of scope |
| A person approves every finding | Accountability stays with finance; model mistakes cannot cause financial actions | Fully automatic decisions |
| Budget caps in application code | Stops spending before a call is made | AWS Budgets alone, which only alerts |
| One agent, only on flagged invoices | Only the investigation has steps that can't be known in advance; everything else is cheaper and testable as fixed steps | Several agents (extractor, policy, investigator, reviewer) |

## Later

Not part of Version 1: a PDF page viewer, auditor and admin roles, a nightly schedule with email summaries,
spending forecasts, MCP tool servers, a reviewer agent, expense receipts and
multi-currency support, and document IDs so same-number resubmissions can be reported
as duplicates.
