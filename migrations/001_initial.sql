-- Immutable once applied: add a new numbered migration for subsequent changes.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ledgerguard_app') THEN
        CREATE ROLE ledgerguard_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;
    END IF;
END
$$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO ledgerguard_app;

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL CHECK (finished_at >= started_at),
    model_ids JSONB NOT NULL,
    git_revision TEXT,
    status TEXT NOT NULL CHECK (status IN ('completed', 'needs_review', 'stopped', 'failed')),
    stopped_reason TEXT,
    skipped_stages TEXT[] NOT NULL DEFAULT '{}',
    budgets JSONB NOT NULL DEFAULT '{}'
);
CREATE TABLE documents (
    run_id TEXT NOT NULL REFERENCES runs,
    document_id TEXT NOT NULL,
    pdf_sha256 TEXT NOT NULL CHECK (pdf_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (run_id, document_id)
);
CREATE TABLE invoices (
    invoice_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    vendor_name TEXT NOT NULL,
    po_number TEXT,
    invoice_date DATE NOT NULL,
    due_date DATE NOT NULL,
    currency TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    subtotal NUMERIC(14,2) NOT NULL CHECK (subtotal >= 0 AND subtotal <> 'NaN'),
    tax NUMERIC(14,2) NOT NULL CHECK (tax >= 0 AND tax <> 'NaN'),
    total NUMERIC(14,2) NOT NULL CHECK (total >= 0 AND total <> 'NaN'),
    UNIQUE (run_id, document_id),
    UNIQUE (run_id, invoice_number),
    FOREIGN KEY (run_id, document_id) REFERENCES documents
);
CREATE TABLE line_items (
    invoice_id BIGINT NOT NULL REFERENCES invoices,
    position INTEGER NOT NULL CHECK (position >= 0),
    description TEXT NOT NULL,
    quantity NUMERIC NOT NULL CHECK (quantity > 0 AND quantity NOT IN ('NaN', 'Infinity')),
    unit_price NUMERIC(14,4) NOT NULL CHECK (unit_price >= 0 AND unit_price <> 'NaN'),
    amount NUMERIC(14,2) NOT NULL CHECK (amount >= 0 AND amount <> 'NaN'),
    source_page INTEGER NOT NULL CHECK (source_page >= 1),
    source_text TEXT NOT NULL,
    PRIMARY KEY (invoice_id, position)
);
CREATE TABLE findings (
    finding_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs,
    position INTEGER NOT NULL CHECK (position >= 0),
    type TEXT NOT NULL CHECK (type IN ('duplicate', 'contract_overcharge', 'policy_violation',
        'split_purchase', 'unusual_amount', 'arithmetic_error', 'extraction_issue')),
    invoice_numbers TEXT[] NOT NULL CHECK (cardinality(invoice_numbers) > 0),
    amount_at_issue NUMERIC(14,2) NOT NULL CHECK (amount_at_issue >= 0 AND amount_at_issue <> 'NaN'),
    calculation TEXT NOT NULL,
    evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) > 0),
    citation JSONB,
    no_clause_reason TEXT,
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    CHECK ((citation IS NOT NULL AND no_clause_reason IS NULL) OR
           (citation IS NULL AND no_clause_reason IS NOT NULL AND no_clause_reason = 'no applicable clause identified')),
    UNIQUE (run_id, position)
);
CREATE TABLE notes (
    finding_id BIGINT PRIMARY KEY REFERENCES findings,
    finding_index INTEGER NOT NULL CHECK (finding_index >= 0),
    summary TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 600),
    related_invoices TEXT[] NOT NULL,
    uncertainty TEXT NOT NULL CHECK (uncertainty IN ('low', 'medium', 'high'))
);
CREATE TABLE referrals (
    referral_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs,
    document_id TEXT,
    invoice_number TEXT,
    stage TEXT NOT NULL CHECK (stage IN ('extraction', 'rules', 'policy', 'investigation', 'budget')),
    reason TEXT NOT NULL,
    CHECK (num_nonnulls(document_id, invoice_number) = 1),
    FOREIGN KEY (run_id, document_id) REFERENCES documents
);
CREATE TABLE decisions (
    decision_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    finding_id BIGINT NOT NULL REFERENCES findings,
    reviewer TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    note TEXT NOT NULL,
    time TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE audit_log (
    audit_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pipeline_run_id TEXT REFERENCES runs,
    time TIMESTAMPTZ NOT NULL,
    run_id TEXT NOT NULL,
    model TEXT,
    decision TEXT NOT NULL CHECK (decision IN ('allow', 'block')),
    outcome TEXT NOT NULL,
    reason TEXT,
    scope TEXT,
    input_tokens BIGINT CHECK (input_tokens >= 0),
    output_tokens BIGINT CHECK (output_tokens >= 0),
    cost NUMERIC(20,10) CHECK (cost >= 0 AND cost <> 'NaN'),
    reserved_cost NUMERIC(20,10) CHECK (reserved_cost >= 0 AND reserved_cost <> 'NaN'),
    call_id TEXT,
    tool TEXT,
    phase TEXT,
    row_count INTEGER CHECK (row_count >= 0)
);
CREATE INDEX findings_queue ON findings (run_id, status, position);
CREATE INDEX audit_log_run ON audit_log (run_id, time);
CREATE INDEX decisions_finding ON decisions (finding_id, decision_id);

CREATE FUNCTION reject_append_only_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '42501';
END
$$;
CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION reject_append_only_mutation();
CREATE TRIGGER decisions_no_update_delete BEFORE UPDATE OR DELETE ON decisions
    FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
CREATE TRIGGER decisions_no_truncate BEFORE TRUNCATE ON decisions
    FOR EACH STATEMENT EXECUTE FUNCTION reject_append_only_mutation();
-- Fire even for sessions operating as a replication target.
ALTER TABLE audit_log ENABLE ALWAYS TRIGGER audit_log_no_update_delete;
ALTER TABLE audit_log ENABLE ALWAYS TRIGGER audit_log_no_truncate;
ALTER TABLE decisions ENABLE ALWAYS TRIGGER decisions_no_update_delete;
ALTER TABLE decisions ENABLE ALWAYS TRIGGER decisions_no_truncate;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, ledgerguard_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, ledgerguard_app;
GRANT SELECT, INSERT ON runs, documents, invoices, line_items, findings, notes, referrals,
    decisions, audit_log TO ledgerguard_app;
-- Step 5 may update review status; decisions themselves remain immutable.
GRANT UPDATE (status) ON findings TO ledgerguard_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ledgerguard_app;
REVOKE ALL ON FUNCTION reject_append_only_mutation() FROM PUBLIC;
