-- xerlane web schema: who may log in to the web app, their sessions, and failed logins. Run by xerlane.db.init_db as the table owner role, after schema.sql.
-- Same rule as every other table: tenant_id is part of the key and row level security is FORCED. Two tables need one extra door, and only one:
--   app_user and login_attempt can be read by the login transaction, which does not know the tenant yet, through the setting app.login_email (the address being tried);
--   app_session can be read through app.session_hash (the hash of the cookie being checked).
-- The web server sets those only inside the one transaction that needs them, from values it just received, and sets app.tenant_id from the row it found, never from a request.

CREATE TABLE IF NOT EXISTS app_user (
    tenant_id     text        NOT NULL CHECK (tenant_id <> ''),
    user_id       bigserial,
    email         text        NOT NULL CHECK (email = lower(email) AND email ~ '^[^@[:space:]]+@[^@[:space:]]+$' AND length(email) <= 254),
    password_hash text        NOT NULL CHECK (password_hash LIKE 'scrypt$%'),
    role          text        NOT NULL CHECK (role IN ('viewer', 'admin')),
    disabled      boolean     NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id),
    UNIQUE (email)
);

CREATE TABLE IF NOT EXISTS app_session (
    tenant_id    text        NOT NULL CHECK (tenant_id <> ''),
    session_hash text        PRIMARY KEY CHECK (length(session_hash) = 64),
    user_id      bigint      NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    FOREIGN KEY (tenant_id, user_id) REFERENCES app_user (tenant_id, user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS app_session_user ON app_session (tenant_id, user_id);

CREATE TABLE IF NOT EXISTS login_attempt (
    email text        NOT NULL,
    at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_attempt_email ON login_attempt (email, at);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['app_user', 'app_session', 'login_attempt'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
    END LOOP;
END $$;

CREATE POLICY tenant_isolation ON app_user
    USING (tenant_id = current_setting('app.tenant_id', true) OR email = current_setting('app.login_email', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY tenant_isolation ON app_session
    USING (tenant_id = current_setting('app.tenant_id', true) OR session_hash = current_setting('app.session_hash', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY tenant_isolation ON login_attempt
    USING (email = current_setting('app.login_email', true))
    WITH CHECK (email = current_setting('app.login_email', true));
