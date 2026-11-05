-- Resolve audit trails for live, replayed and failed runs without requiring a saved run.
CREATE VIEW run_audit AS
SELECT COALESCE(a.pipeline_run_id, split_part(a.run_id, ':', 1)) AS resolved_run_id,
       NULLIF(split_part(a.run_id, ':', 2), '') AS document_id,
       a.*
FROM audit_log a;
GRANT SELECT ON run_audit TO ledgerguard_app;
