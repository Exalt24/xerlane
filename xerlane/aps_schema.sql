-- xerlane APS (Autodesk Platform Services) schema. Run by xerlane.aps.init_schema as the table owner role inside the xerlane schema, after schema.sql.
-- Same rules as schema.sql: every table carries tenant_id, part of every key, and has FORCED row level security: a connection sees a row only when
-- tenant_id equals the app.tenant_id setting of its current transaction. No setting, no rows.
-- No foreign key points at an XER table on purpose: the link to a task is the task_code value, so the XER tables can be reloaded, replaced or wiped on their own.

-- One row per model object of one model view. object_id is APS's non-persistent id of the translation; external_id is the persistent one when the source design has it.
CREATE TABLE IF NOT EXISTS aps_object (
    tenant_id    text   NOT NULL CHECK (tenant_id <> ''),
    model_urn    text   NOT NULL CHECK (model_urn <> ''),
    view_guid    text   NOT NULL CHECK (view_guid <> ''),
    object_id    bigint NOT NULL CHECK (object_id >= 0),
    external_id  text,
    name         text,
    category     text,
    task_code    text   CHECK (task_code <> ''),
    properties   jsonb  NOT NULL DEFAULT '{}',
    content_hash text   NOT NULL CHECK (length(content_hash) = 64),
    PRIMARY KEY (tenant_id, model_urn, view_guid, object_id)
);
CREATE INDEX IF NOT EXISTS aps_object_task_code ON aps_object (tenant_id, task_code) WHERE task_code IS NOT NULL;

-- One row per load that changed anything in a view (an object added, changed or removed, or a new row set aside).
CREATE TABLE IF NOT EXISTS aps_load_run (
    tenant_id      text        NOT NULL CHECK (tenant_id <> ''),
    run_id         bigserial,
    model_urn      text        NOT NULL,
    view_guid      text        NOT NULL,
    payload_sha256 text        NOT NULL,
    loaded_at      timestamptz NOT NULL DEFAULT now(),
    counts         jsonb       NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, run_id)
);

CREATE TABLE IF NOT EXISTS aps_change_event (
    tenant_id   text   NOT NULL CHECK (tenant_id <> ''),
    event_id    bigserial,
    run_id      bigint NOT NULL,
    kind        text   NOT NULL CHECK (kind IN ('OBJECT_ADDED', 'OBJECT_CHANGED', 'OBJECT_REMOVED')),
    model_urn   text   NOT NULL,
    view_guid   text   NOT NULL,
    object_id   bigint NOT NULL,
    external_id text,
    name        text,
    fields      text,
    old_hash    text,
    new_hash    text,
    PRIMARY KEY (tenant_id, event_id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES aps_load_run (tenant_id, run_id) ON DELETE CASCADE
);

-- Items that failed validation. Never dropped: the raw item stays (a short stub when it is too large or cannot be stored), with the rule that stopped it.
CREATE TABLE IF NOT EXISTS aps_quarantine (
    tenant_id      text   NOT NULL CHECK (tenant_id <> ''),
    quarantine_id  bigserial,
    model_urn      text   NOT NULL,
    view_guid      text   NOT NULL,
    payload_sha256 text   NOT NULL,
    item_index     bigint NOT NULL,
    reason_code    text   NOT NULL,
    reason         text   NOT NULL,
    raw            jsonb  NOT NULL,
    PRIMARY KEY (tenant_id, quarantine_id),
    UNIQUE (tenant_id, model_urn, view_guid, payload_sha256, item_index)
);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['aps_object', 'aps_load_run', 'aps_change_event', 'aps_quarantine'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)) WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
    END LOOP;
END $$;

-- An APS object next to the XER task that carries the same task_code. security_invoker makes the view run with the privileges of whoever queries it, so row level
-- security of both tables applies to the caller's tenant (without it the view would run as its owner). The tenant is also in the join condition. A task_code is unique
-- only inside one project, so an object can match one task per project: filter on proj_id when more than one project is loaded.
CREATE OR REPLACE VIEW aps_object_task WITH (security_invoker = true) AS
SELECT o.tenant_id, o.model_urn, o.view_guid, o.object_id, o.external_id, o.name, o.category, o.task_code,
       t.proj_id, t.task_id, t.task_name
FROM aps_object o
JOIN task t ON t.tenant_id = o.tenant_id AND t.task_code = o.task_code;
