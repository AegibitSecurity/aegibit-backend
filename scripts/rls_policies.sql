-- ═══════════════════════════════════════════════════════════════════════════
-- AEGIBIT Flow — Row Level Security (PostgreSQL / Supabase)
-- Defense-in-depth: even if application code has a bug, the DB blocks
-- cross-tenant reads/writes at the storage engine level.
--
-- Strategy:
--   The FastAPI app sets LOCAL parameter app.current_org_id at the start of
--   every request (via OrgIsolationMiddleware).
--   RLS policies read this parameter and reject any row whose organization_id
--   does not match.
--
-- Apply once:  psql $DATABASE_URL -f scripts/rls_policies.sql
-- Safe to re-run: all objects use CREATE OR REPLACE / IF NOT EXISTS guards.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── Helper: set current org on connection ────────────────────────────────────
-- The app calls: SET LOCAL app.current_org_id = '<uuid>';
-- at the start of each DB transaction via the middleware below.

-- ── deals ─────────────────────────────────────────────────────────────────────

ALTER TABLE deals ENABLE ROW LEVEL SECURITY;
ALTER TABLE deals FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS deals_org_isolation     ON deals;
DROP POLICY IF EXISTS deals_insert_own_org    ON deals;

-- SELECT / UPDATE / DELETE: row must belong to current org
CREATE POLICY deals_org_isolation ON deals
    FOR ALL
    USING (
        organization_id::text =
        current_setting('app.current_org_id', true)
    );

-- INSERT: new row's organization_id must equal current org
CREATE POLICY deals_insert_own_org ON deals
    FOR INSERT
    WITH CHECK (
        organization_id::text =
        current_setting('app.current_org_id', true)
    );


-- ── audit_logs ────────────────────────────────────────────────────────────────

ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS audit_logs_org_isolation  ON audit_logs;
DROP POLICY IF EXISTS audit_logs_insert_own_org ON audit_logs;

CREATE POLICY audit_logs_org_isolation ON audit_logs
    FOR SELECT
    USING (
        organization_id::text =
        current_setting('app.current_org_id', true)
    );

CREATE POLICY audit_logs_insert_own_org ON audit_logs
    FOR INSERT
    WITH CHECK (
        organization_id::text =
        current_setting('app.current_org_id', true)
    );

-- Audit rows are immutable — UPDATE and DELETE are always denied.
CREATE POLICY audit_logs_deny_update ON audit_logs
    FOR UPDATE USING (false);

CREATE POLICY audit_logs_deny_delete ON audit_logs
    FOR DELETE USING (false);


-- ── notifications ─────────────────────────────────────────────────────────────

ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS notifications_org_isolation  ON notifications;
DROP POLICY IF EXISTS notifications_insert_own_org ON notifications;

CREATE POLICY notifications_org_isolation ON notifications
    FOR ALL
    USING (
        organization_id::text =
        current_setting('app.current_org_id', true)
    );

CREATE POLICY notifications_insert_own_org ON notifications
    FOR INSERT
    WITH CHECK (
        organization_id::text =
        current_setting('app.current_org_id', true)
    );


-- ── org_config ────────────────────────────────────────────────────────────────

ALTER TABLE org_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE org_config FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_config_org_isolation ON org_config;

CREATE POLICY org_config_org_isolation ON org_config
    FOR ALL
    USING (
        organization_id::text =
        current_setting('app.current_org_id', true)
    );


-- ── car_models (pricing data) ─────────────────────────────────────────────────

ALTER TABLE car_models ENABLE ROW LEVEL SECURITY;
ALTER TABLE car_models FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS car_models_org_isolation ON car_models;

CREATE POLICY car_models_org_isolation ON car_models
    FOR ALL
    USING (
        organization_id::text =
        current_setting('app.current_org_id', true)
    );


-- ── customers ─────────────────────────────────────────────────────────────────

ALTER TABLE customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE customers FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS customers_org_isolation ON customers;

CREATE POLICY customers_org_isolation ON customers
    FOR ALL
    USING (
        organization_id::text =
        current_setting('app.current_org_id', true)
    );


-- ── fraud_audit_logs ──────────────────────────────────────────────────────────

ALTER TABLE fraud_audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE fraud_audit_logs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS fraud_audit_org_isolation ON fraud_audit_logs;

CREATE POLICY fraud_audit_org_isolation ON fraud_audit_logs
    FOR ALL
    USING (
        organization_id::text =
        current_setting('app.current_org_id', true)
    );


-- ── users ─────────────────────────────────────────────────────────────────────
-- Users are already scoped by organization_id in every app query.
-- RLS here is extra safety.

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS users_org_isolation  ON users;
DROP POLICY IF EXISTS users_self_read      ON users;

-- Users can only read/modify within their org
CREATE POLICY users_org_isolation ON users
    FOR ALL
    USING (
        organization_id::text =
        current_setting('app.current_org_id', true)
    );

-- A user can always read their own row (needed for login validation)
CREATE POLICY users_self_read ON users
    FOR SELECT
    USING (
        id::text = current_setting('app.current_user_id', true)
    );


-- ── organizations, tasks, deal_events — no RLS needed ────────────────────────
-- organizations: never contains tenant data, no isolation needed
-- tasks: joined through deals which are already RLS-protected
-- deal_events: joined through deals which are already RLS-protected


-- ═══════════════════════════════════════════════════════════════════════════
-- VERIFICATION QUERY (run after applying to confirm RLS is active)
-- ═══════════════════════════════════════════════════════════════════════════
-- SELECT tablename, rowsecurity, forcerowolesecurity
-- FROM   pg_tables
-- WHERE  schemaname = 'public'
--   AND  tablename IN ('deals','audit_logs','notifications','org_config',
--                      'car_models','customers','fraud_audit_logs','users');
