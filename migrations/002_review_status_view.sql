-- Review status is derived solely from append-only human decisions.
REVOKE UPDATE (status) ON findings FROM ledgerguard_app;
DROP INDEX findings_queue;
ALTER TABLE findings DROP COLUMN status;
CREATE INDEX findings_queue ON findings (run_id, position);

CREATE VIEW finding_review_status AS
SELECT f.finding_id,
       CASE latest.decision
           WHEN 'approve' THEN 'approved'
           WHEN 'reject' THEN 'rejected'
           ELSE 'pending'
       END AS status
FROM findings f
LEFT JOIN LATERAL (
    SELECT d.decision
    FROM decisions d
    WHERE d.finding_id = f.finding_id
    ORDER BY d.time DESC, d.decision_id DESC
    LIMIT 1
) latest ON true;
GRANT SELECT ON finding_review_status TO ledgerguard_app;
