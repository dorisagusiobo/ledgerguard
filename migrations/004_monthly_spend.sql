CREATE TABLE spend_limits (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    monthly_limit NUMERIC(14,2) NOT NULL CHECK (monthly_limit >= 0 AND monthly_limit <> 'NaN')
);
INSERT INTO spend_limits (monthly_limit) VALUES (10.00);
CREATE TABLE monthly_spend (
    month DATE PRIMARY KEY CHECK (EXTRACT(DAY FROM month) = 1),
    reserved NUMERIC(20,10) NOT NULL DEFAULT 0 CHECK (reserved >= 0 AND reserved <> 'NaN'),
    spent NUMERIC(20,10) NOT NULL DEFAULT 0 CHECK (spent >= 0 AND spent <> 'NaN')
);
REVOKE ALL ON spend_limits, monthly_spend FROM PUBLIC, ledgerguard_app;
GRANT SELECT ON spend_limits, monthly_spend TO ledgerguard_app;
GRANT INSERT (month), UPDATE (reserved, spent) ON monthly_spend TO ledgerguard_app;
