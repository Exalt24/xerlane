-- xerlane cost schema: cost lines and commitments from CSV exports. Run by xerlane.db.init_db as the table owner role inside the xerlane schema, after schema.sql.
-- Same rules as schema.sql: every table carries tenant_id, part of every key, and has FORCED row level security: a connection sees a row only when
-- tenant_id equals the app.tenant_id setting of its current transaction. No setting, no rows.
-- No foreign key points at an XER table on purpose: the link to a task is the task_code value, so the schedule can be reloaded without touching the money, and the
-- report says which cost lines have lost their task instead of the database refusing the reload.

-- One row per line of a cost export: budget, forecast and actual for one cost code of one project, optionally tied to a schedule activity by its task code.
CREATE TABLE IF NOT EXISTS cost_line (
    tenant_id   text    NOT NULL CHECK (tenant_id <> ''),
    proj_id     bigint  NOT NULL,
    line_id     bigint  NOT NULL CHECK (line_id >= 0),
    cost_code   text    NOT NULL CHECK (cost_code <> ''),
    description text,
    task_code   text    CHECK (task_code <> ''),
    period      text    CHECK (period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    budget      numeric(18, 4) NOT NULL,
    forecast    numeric(18, 4) NOT NULL,
    actual      numeric(18, 4) NOT NULL,
    PRIMARY KEY (tenant_id, proj_id, line_id)
);
-- No secondary index on cost_line: the primary key (tenant_id, proj_id, line_id) already serves the project filter every report statement starts with, and a measured
-- comparison with three candidate indexes (docs/query-tuning.md) showed no gain worth the cost on every load.

-- One row per commitment (subcontract or purchase order) of a commitment export.
CREATE TABLE IF NOT EXISTS commitment (
    tenant_id     text    NOT NULL CHECK (tenant_id <> ''),
    proj_id       bigint  NOT NULL,
    commitment_id bigint  NOT NULL CHECK (commitment_id >= 0),
    vendor        text    NOT NULL CHECK (vendor <> ''),
    cost_code     text    NOT NULL CHECK (cost_code <> ''),
    description   text,
    committed     numeric(18, 4) NOT NULL CHECK (committed >= 0),
    invoiced      numeric(18, 4) NOT NULL CHECK (invoiced >= 0),
    status        text    NOT NULL CHECK (status IN ('OPEN', 'CLOSED', 'VOID')),
    issued        date,
    PRIMARY KEY (tenant_id, proj_id, commitment_id)
);

-- One row per load that changed anything.
CREATE TABLE IF NOT EXISTS cost_load_run (
    tenant_id     text        NOT NULL CHECK (tenant_id <> ''),
    run_id        bigserial,
    kind          text        NOT NULL CHECK (kind IN ('cost', 'commitment')),
    source_label  text        NOT NULL,
    source_sha256 text        NOT NULL,
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    counts        jsonb       NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, run_id)
);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['cost_line', 'commitment', 'cost_load_run'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)) WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
    END LOOP;
END $$;
