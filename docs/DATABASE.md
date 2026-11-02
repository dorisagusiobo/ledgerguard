# Local Postgres storage (steps 4a and 4b)

The local service and CI pin `pgvector/pgvector:pg17` to digest
`sha256:7d0a41464797aeb9862dfdc0bbf1526b82cfd324cdd8036df19e3545f4a9ccb8`,
with a named data volume and
`127.0.0.1:5432` binding. pgvector is not enabled or used yet. Reference contracts,
history and policy remain files. Audit records, UTC-month spending and investigator
checkpoints can now be persisted in Postgres. No reviewer pause is implemented.

`.env` contains generated owner/app passwords and connection URLs; it is ignored
and must never be displayed or committed. `.env.example` contains placeholders
only. The owner URL is `DATABASE_URL`; runtime storage uses `APP_DATABASE_URL`.
The Compose initialization script creates the login `ledgerguard_app` on the
first initialization of an empty volume. Changing a password in `.env` does not
change an already initialized database role's password.

With Docker Desktop running, start the local service with `docker compose up -d
--wait`. Export `DATABASE_URL` securely into the process environment, then run:

```sh
uv run python -m ledgerguard.db migrate
```

Alternatively, let uv supply the generated environment file directly to the
process without displaying its contents:

```sh
uv run --env-file .env python -m ledgerguard.db migrate
```

Only the owner runs migrations. Applied SQL files are immutable: the runner
compares SHA-256 hashes, refuses missing/changed history or out-of-order additions,
and applies pending migrations and history records in one transaction. It uses a
transaction advisory lock to serialize concurrent runners. Migration 002 replaces
the stored finding status with a decision-derived view. Add the next numbered
migration rather than editing an applied file.

`save_run(conn, result, metadata)` accepts a `RunMetadata` instance (or a mapping
with the same keys). Supply `result.pipeline_run_id` as the run ID (a mismatch
raises before saving), timezone-aware start/finish times,
model IDs, optional git revision, and `document_hashes` containing each submitted
document's SHA-256 hex digest, including failed/unattempted PDFs. The caller
computes hashes from the PDFs; PDF bytes are never passed to the store.

Use an idle app-role connection. `save_run` commits every result row in one
transaction or rolls it all back. Reusing a run ID raises rather than overwriting
prior evidence. Per-document budgets and skipped stages are retained in `runs`.
Optional `audit_records` imports existing JSONL gateway records in that same
transaction; it does not change gateway logging. Only recognized gateway fields
are accepted, excluding prompts and tool results.

`load_findings(conn, run_id)` returns ordered `StoredFinding` entries with a
persistent `finding_id`, the original `Finding`, and its list of investigator
`Note`s. Note indexes remain relative to each group identified by the first
invoice number. NUMERIC columns return `Decimal`, including unit prices.

Finding status comes from `finding_review_status`: `pending` with no decision,
otherwise `approved` or `rejected` from the highest decision ID (append order).
Migration 003 makes timestamps display-only: the app can insert only finding ID,
reviewer, decision and note; time comes from the server default. `findings` has
no writable status column, and the app can only SELECT the view. Saving a result cannot set or override its review status.

The app role has INSERT/SELECT only on `audit_log` and `decisions`. UPDATE/DELETE
row triggers and TRUNCATE statement triggers also reject owner mutations. New
decisions supersede old ones. The review API appends decisions without financial
actions. Deliberate schema changes by an owner/superuser can remove protection;
the app cannot do so.

Database tests create a unique temporary database, migrate it, then drop that
database in fixture teardown. They never clear the configured database's tables.
The owner URL must permit CREATE DATABASE and creation of the app role if absent.
Without `DATABASE_URL` local database tests skip; with `CI=true` the missing URL
fails. CI supplies an isolated service using the same Postgres image.


## Save a recorded replay for review

Migration 005 adds run provenance (`mode`, default `local`). Apply pending
migrations with the direct owner connection before using replay persistence.
`pipeline_eval --save` is accepted only alongside `--replay`; `--save --live`
is rejected before creating a provider client. A complete successful replay
is saved atomically through `pooled_connection(runtime_database_url())`, which
must operate as `ledgerguard_app`. Its metadata records `mode=replay` and the
recording header's git commit. PDF hashes, evidence, notes, budgets and the
replay's gateway audit records are retained. No PDF bytes are stored.

The replay invokes no live model. Reported usage and dollar amounts come from
the recording; they are not new provider charges. Saving never makes review
decisions or changes accounting records. A duplicate run ID fails rather than
replacing existing evidence.

## Runtime audit and monthly budget

Pass `audit=PostgresSink(APP_DATABASE_URL)` and
`monthly=MonthlyLedger(APP_DATABASE_URL)` to `Gateway`, or supply those same
objects to `run_pipeline`. Runtime connections must operate as `ledgerguard_app`;
owner sessions are rejected. The `audit_path=` shortcut still selects
`JsonlSink(path)` for existing/offline callers; provide exactly one destination.
Persistent monthly enforcement is enabled by supplying the ledger; offline fakes
can continue using in-memory invoice/batch budgets without a database.

The Postgres sink uses independent autocommitted connections and reuses
`store.append_audit`. A model attempt has a durable authorization row before the
provider is called, followed by a success/error record. Tool attempts retain their
authorization/completion records. The JSONL sink retains its existing format: one
final record per model attempt, with the file opened before the call.
Do not import those same Postgres records again through `RunMetadata.audit_records`.
Database audit failures raise `OSError` and stop work, including before invocation;
they are not converted to ordinary model-error referrals. Rollback of a run save
cannot remove already committed runtime audit records.

Migration 004 seeds the single owner-managed monthly limit at $10.00. The app can
read `spend_limits`, insert only the month into `monthly_spend`, and update only
its reserved/spent counters. Before each model call a short transaction inserts
and locks the UTC-month row, then reserves the estimated maximum cost. Monthly
blocks take priority over invoice/batch blocks and stop the whole pipeline.
Successful calls charge actual usage; client errors release the reservation;
invalid usage charges the full reservation. Reconciliation always uses the month
captured when reserving, even across a UTC month boundary.

**Crash behavior:** if the process dies after reserving but before reconciling,
that reservation stays counted. There is no automatic expiry or blind release.
The system may stop early but cannot reuse that reserved capacity. An owner must
verify actual provider usage before repairing stranded reservations. As with the
existing gateway, reported usage above the estimate is charged in full and can
exceed the reservation; estimates are not a tokenizer guarantee.

## Investigator checkpoints

After SQL migrations, `db migrate` runs `PostgresSaver.setup()` as the migration
owner on an autocommit connection (the library creates indexes concurrently).
A separate advisory lock serializes setup. Application code never runs setup.
The command grants SELECT/INSERT/UPDATE on `checkpoints`, `checkpoint_blobs` and
`checkpoint_writes`, and no DELETE; `checkpoint_migrations` remains owner-only.
The existing owner-only `runs` lifecycle is unchanged: runs are still saved once
at the end, with no new in-progress writer in this step.

Open a `PostgresSaver` with the app connection, without calling `setup()`, and pass
it as `checkpointer=` to `investigate` or `run_pipeline`. The default investigator
thread ID is `<gateway.run_id>:<invoice_number>`. Each pipeline invocation gets a
new UUID hex `pipeline_run_id` unless one is supplied. Gateway audit IDs are
`<pipeline_run_id>:<document_id>`, so checkpoint threads also stay distinct across
runs over the same PDFs. An explicit `thread_id` can be
supplied to `investigate`. State uses plain lists/dicts, including notes and review
outcomes, and round-trips with strict serialization without application classes.
The caller still receives the public `Investigation` dataclass and `Note` models.

LangGraph owns BYTEA blob columns for serialized state. These are the only binary
columns; no PDFs or raw extraction document blocks enter investigator state.
Reference data stays in files and pgvector is still unused. The review API
appends decisions without interrupting or resuming the investigator.


### Retention

Audit records omit prompts and tool results, but checkpoints retain the
investigator's whole conversation: findings, tool results and notes. The app role
cannot DELETE checkpoints and there is currently no retention cleanup, so this
state is kept indefinitely. This supports investigator recovery and is appropriate
for synthetic data. A retention policy is required before introducing real data.


## Review API

`ledgerguard.api:app` exposes exactly five routes: GET `/health`, GET `/runs`,
GET `/runs/{run_id}/findings`, GET `/findings/{finding_id}`, and POST
`/findings/{finding_id}/decisions`. Interactive docs and OpenAPI routes are disabled.
The health check is public; every other route requires
`Authorization: Bearer <REVIEW_API_TOKEN>`. Configure `APP_DATABASE_URL`,
`REVIEW_API_TOKEN` and `REVIEW_REVIEWER` in the server environment. Only the
environment supplies reviewer identity; the request can supply only `decision`
and `note`. Missing configuration fails closed.

The API uses the app role, reads review status from the decision-derived view,
and commits each new decision before returning 201. It does not resume a graph
or run any action after approval. Money is returned as strings with two decimal
places, unit prices with four; times are ISO 8601 UTC. Validation errors contain
only locations/messages, and database failures return a fixed 503 body without
connection details. Database-backed API tests run with the rest of the CI suite.

After supplying the environment, start the local API on loopback:

```sh
uv run --env-file .env uvicorn ledgerguard.api:app --host 127.0.0.1 --port 8000
```
