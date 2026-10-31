-- Append sequence determines status; timestamps are server-generated display data.
CREATE OR REPLACE VIEW finding_review_status AS
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
    ORDER BY d.decision_id DESC
    LIMIT 1
) latest ON true;
REVOKE INSERT ON decisions FROM ledgerguard_app;
GRANT INSERT (finding_id, reviewer, decision, note) ON decisions TO ledgerguard_app;
