# Local Postgres storage (step 4a)

The local service and CI pin `pgvector/pgvector:pg17` to digest
`sha256:7d0a41464797aeb9862dfdc0bbf1526b82cfd324cdd8036df19e3545f4a9ccb8`,
with a named data volume and
`127.0.0.1:5432` binding. pgvector is not enabled or used yet. Reference contracts,
history and policy remain files. The gateway still writes JSONL; monthly spending
and LangGraph checkpoints are not stored in Postgres in this step.

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
with the same keys). Supply a unique run ID, timezone-aware start/finish times,
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
decisions supersede old ones. No decision writer or approval action is part of
this step. Deliberate schema changes by an owner/superuser can remove protection;
the app cannot do so.

Database tests create a unique temporary database, migrate it, then drop that
database in fixture teardown. They never clear the configured database's tables.
The owner URL must permit CREATE DATABASE and creation of the app role if absent.
Without `DATABASE_URL` local database tests skip; with `CI=true` the missing URL
fails. CI supplies an isolated service using the same Postgres image.
