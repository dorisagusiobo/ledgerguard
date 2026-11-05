# LedgerGuard

LedgerGuard audits synthetic USD invoices against contracts and spending policies,
then sends evidence-backed findings and investigator notes to a human reviewer.
It never pays invoices or changes financial records. Approving a finding appends
a decision; it triggers no financial action.

The pipeline reads PDFs with Amazon Nova 2 Lite, runs deterministic checks with
`Decimal` arithmetic, checks judgment-based policy clauses with Nova, and uses a
bounded Claude Sonnet 4.6 investigator with read-only tools. Every model and tool
call goes through a governance gateway. Postgres stores results, append-only audit
records and decisions; a FastAPI API and Next.js screen support review.

On the saved held-out synthetic evaluation (seed 7, 19 PDFs), it caught **14/14
planted problems**, with **100% scored precision, amount accuracy and citation
accuracy**, and exact extraction on **19/19** documents. The run cost
**$0.2915775** and took **135.5 seconds**. Only **one clean invoice** was in this
split, so the zero-false-flag result has limited coverage.

Detection and note quality are separate. All 14 investigations completed, but
**3/14 notes passed every check** in the strict manual grading: unsupported
historical comparisons, certainty about cause rather than finding support, and
verbosity still need work. All avoided fraud accusations. See the
[evaluation write-up](docs/EVALUATION.md) and [saved evidence](evals/results/README.md)
for the per-note grades, denominators, provenance and limitations.

## Try it offline

Python 3.12+ and uv are required. These commands use no paid services:

```sh
uv sync
uv run pytest -q
uv run python -m ledgerguard.synthetic --seed 7 --out data/synthetic
uv run python -m ledgerguard.evaluate --data data/synthetic --split dev
```

Generation requires an empty output directory (a `.gitkeep` is allowed). The
last command evaluates deterministic rules, not the live AI pipeline. It catches
12/14 dev problems; the two semantic policy cases require the policy checker.
Expected answers are scoring inputs only, never model inputs. Database tests skip
locally without `DATABASE_URL`; the ordinary offline suite uses fakes and the
committed dev recording, with no AWS calls.

For local review, follow [database setup](docs/DATABASE.md) and the
[web app instructions](web/README.md). Supply configuration through the environment;
never commit credentials. [Deployment instructions](infra/terraform/README.md)
cover the Lambda/Cognito/S3 infrastructure. Signed-in review on the deployed site
was verified on 2026-11-04 and 2026-11-05, with decisions saved under the reviewer's
verified email. A deployed 3-invoice `dev` smoke run cost $0.0209925, a reviewer
decision was saved, and a re-sent event was refused with no spend. A deployed
38-invoice `dev` batch caught 14/14 expected findings with no extras, cost
$0.2884795, and produced one referral. Both batches ran at revision `6775e4a`;
the deployed code is now `aa70fd2`, which has not processed a batch. The held-out
run itself was local and does not verify deployment.

## Project map

- [`src/ledgerguard/`](src/ledgerguard/) — pipeline, gateway, rules, tools, API and evaluations.
- [`tests/`](tests/) — offline tests and isolated Postgres integration tests.
- [`evals/`](evals/) — dev replay recording and preserved evaluation evidence.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — scope, design, governance and decisions.
- [`docs/EVALUATION.md`](docs/EVALUATION.md) — results, manual grades and remaining limitations.
- [`web/`](web/) and [`infra/`](infra/) — review screen and deployment.

Version 1 uses synthetic data only. It has no PDF page-image viewer, no financial
actions and no multi-currency processing. Clause retrieval with pgvector is deferred.
