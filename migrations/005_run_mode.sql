-- Provenance for saved replay runs; older callers retain the local default.
ALTER TABLE runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'local'
    CHECK (mode IN ('local', 'replay', 'live'));
