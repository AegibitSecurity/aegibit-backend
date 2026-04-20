-- db_hardening.sql
-- PostgreSQL-only. Run once against production DB.
-- Prevents hard-deletes on deals and audit_logs at the database level.
-- Even a superuser cannot bypass this without dropping the trigger first.

-- ── Prevent hard-deletes on deals ────────────────────────────────────────────

CREATE OR REPLACE FUNCTION prevent_deals_hard_delete()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'Hard-delete on deals is forbidden. Use soft-delete (is_deleted=true). '
        'Row id=%. If you must purge, drop this trigger first and document why.',
        OLD.id;
END;
$$;

DROP TRIGGER IF EXISTS trg_prevent_deals_delete ON deals;
CREATE TRIGGER trg_prevent_deals_delete
    BEFORE DELETE ON deals
    FOR EACH ROW EXECUTE FUNCTION prevent_deals_hard_delete();


-- ── Prevent hard-deletes on audit_logs ───────────────────────────────────────

CREATE OR REPLACE FUNCTION prevent_audit_logs_delete()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'Deleting audit_log rows is forbidden — the log is append-only. '
        'Row id=%. Contact the DBA if this is a legitimate data correction.',
        OLD.id;
END;
$$;

DROP TRIGGER IF EXISTS trg_prevent_audit_delete ON audit_logs;
CREATE TRIGGER trg_prevent_audit_delete
    BEFORE DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION prevent_audit_logs_delete();


-- ── Prevent UPDATE on audit_logs (tamper-proofing) ───────────────────────────

CREATE OR REPLACE FUNCTION prevent_audit_logs_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'Updating audit_log rows is forbidden — the log is immutable. '
        'Row id=%.',
        OLD.id;
END;
$$;

DROP TRIGGER IF EXISTS trg_prevent_audit_update ON audit_logs;
CREATE TRIGGER trg_prevent_audit_update
    BEFORE UPDATE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION prevent_audit_logs_update();
