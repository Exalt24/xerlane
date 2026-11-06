-- xerlane schema. Run by xerlane.db.init_db as the table owner role inside the xerlane schema.
-- Every table carries tenant_id, part of every key, and has FORCED row level security: a connection sees a row only when
-- tenant_id equals the app.tenant_id setting of its current transaction. No setting, no rows.

CREATE TABLE IF NOT EXISTS calendar (
    tenant_id    text    NOT NULL CHECK (tenant_id <> ''),
    clndr_id     bigint  NOT NULL,
    clndr_name   text,
    clndr_type   text,
    day_hr_cnt   numeric,
    week_hr_cnt  numeric,
    default_flag text,
    extra        jsonb   NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, clndr_id)
);

CREATE TABLE IF NOT EXISTS rsrc (
    tenant_id       text    NOT NULL CHECK (tenant_id <> ''),
    rsrc_id         bigint  NOT NULL,
    rsrc_name       text,
    rsrc_short_name text,
    rsrc_type       text,
    clndr_id        bigint,
    extra           jsonb   NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, rsrc_id),
    FOREIGN KEY (tenant_id, clndr_id) REFERENCES calendar (tenant_id, clndr_id) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS project (
    tenant_id       text    NOT NULL CHECK (tenant_id <> ''),
    proj_id         bigint  NOT NULL,
    proj_short_name text    NOT NULL,
    plan_start_date timestamp,
    plan_end_date   timestamp,
    last_recalc_date timestamp,
    clndr_id        bigint,
    extra           jsonb   NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, proj_id),
    FOREIGN KEY (tenant_id, clndr_id) REFERENCES calendar (tenant_id, clndr_id) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS projwbs (
    tenant_id       text    NOT NULL CHECK (tenant_id <> ''),
    wbs_id          bigint  NOT NULL,
    proj_id         bigint  NOT NULL,
    parent_wbs_id   bigint,
    wbs_short_name  text    NOT NULL,
    wbs_name        text,
    proj_node_flag  text,
    seq_num         bigint,
    extra           jsonb   NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, wbs_id),
    FOREIGN KEY (tenant_id, proj_id) REFERENCES project (tenant_id, proj_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (tenant_id, parent_wbs_id) REFERENCES projwbs (tenant_id, wbs_id) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS task (
    tenant_id          text    NOT NULL CHECK (tenant_id <> ''),
    task_id            bigint  NOT NULL,
    proj_id            bigint  NOT NULL,
    wbs_id             bigint  NOT NULL,
    clndr_id           bigint  NOT NULL,
    task_code          text    NOT NULL,
    task_name          text,
    task_type          text,
    status_code        text,
    target_drtn_hr_cnt numeric CHECK (target_drtn_hr_cnt >= 0),
    remain_drtn_hr_cnt numeric CHECK (remain_drtn_hr_cnt >= 0),
    target_start_date  timestamp,
    target_end_date    timestamp,
    early_start_date   timestamp,
    early_end_date     timestamp,
    late_start_date    timestamp,
    late_end_date      timestamp,
    act_start_date     timestamp,
    act_end_date       timestamp,
    total_float_hr_cnt numeric,
    free_float_hr_cnt  numeric,
    extra              jsonb   NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, task_id),
    UNIQUE (tenant_id, proj_id, task_code) DEFERRABLE INITIALLY DEFERRED,
    CHECK (target_end_date >= target_start_date),
    CHECK (early_end_date >= early_start_date),
    CHECK (late_end_date >= late_start_date),
    CHECK (act_end_date >= act_start_date),
    FOREIGN KEY (tenant_id, proj_id) REFERENCES project (tenant_id, proj_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (tenant_id, wbs_id) REFERENCES projwbs (tenant_id, wbs_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (tenant_id, clndr_id) REFERENCES calendar (tenant_id, clndr_id) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS taskpred (
    tenant_id     text    NOT NULL CHECK (tenant_id <> ''),
    task_pred_id  bigint  NOT NULL,
    task_id       bigint  NOT NULL,
    pred_task_id  bigint  NOT NULL,
    proj_id       bigint  NOT NULL,
    pred_proj_id  bigint  NOT NULL,
    pred_type     text    NOT NULL CHECK (pred_type IN ('PR_FS', 'PR_SS', 'PR_FF', 'PR_SF')),
    lag_hr_cnt    numeric,
    extra         jsonb   NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, task_pred_id),
    FOREIGN KEY (tenant_id, task_id) REFERENCES task (tenant_id, task_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (tenant_id, pred_task_id) REFERENCES task (tenant_id, task_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (tenant_id, proj_id) REFERENCES project (tenant_id, proj_id) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS taskrsrc (
    tenant_id     text    NOT NULL CHECK (tenant_id <> ''),
    taskrsrc_id   bigint  NOT NULL,
    task_id       bigint  NOT NULL,
    rsrc_id       bigint,
    proj_id       bigint  NOT NULL,
    target_cost   numeric CHECK (target_cost >= 0),
    target_qty    numeric CHECK (target_qty >= 0),
    remain_cost   numeric CHECK (remain_cost >= 0),
    remain_qty    numeric CHECK (remain_qty >= 0),
    act_reg_cost  numeric CHECK (act_reg_cost >= 0),
    act_reg_qty   numeric CHECK (act_reg_qty >= 0),
    act_ot_cost   numeric CHECK (act_ot_cost >= 0),
    act_ot_qty    numeric CHECK (act_ot_qty >= 0),
    extra         jsonb   NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, taskrsrc_id),
    FOREIGN KEY (tenant_id, task_id) REFERENCES task (tenant_id, task_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (tenant_id, rsrc_id) REFERENCES rsrc (tenant_id, rsrc_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (tenant_id, proj_id) REFERENCES project (tenant_id, proj_id) DEFERRABLE INITIALLY DEFERRED
);

-- One row per load that changed anything, and the changes it made to an already loaded project.
CREATE TABLE IF NOT EXISTS load_run (
    tenant_id     text        NOT NULL CHECK (tenant_id <> ''),
    run_id        bigserial,
    proj_id       bigint      NOT NULL,
    kind          text        NOT NULL CHECK (kind IN ('initial', 'update')),
    source_label  text        NOT NULL,
    source_sha256 text        NOT NULL,
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    counts        jsonb       NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, run_id)
);

CREATE TABLE IF NOT EXISTS change_event (
    tenant_id  text   NOT NULL CHECK (tenant_id <> ''),
    event_id   bigserial,
    run_id     bigint NOT NULL,
    kind       text   NOT NULL CHECK (kind IN ('TASK_ADDED', 'TASK_REMOVED', 'FINISH_MOVED', 'FLOAT_CHANGED')),
    task_id    bigint NOT NULL,
    task_code  text,
    field      text,
    old_value  text,
    new_value  text,
    PRIMARY KEY (tenant_id, event_id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES load_run (tenant_id, run_id) ON DELETE CASCADE
);

-- Rows that failed validation. Never dropped: the raw fields stay, with the rule that stopped them.
CREATE TABLE IF NOT EXISTS quarantine (
    tenant_id     text   NOT NULL CHECK (tenant_id <> ''),
    quarantine_id bigserial,
    proj_id       bigint,
    source_label  text   NOT NULL,
    source_sha256 text   NOT NULL,
    table_name    text   NOT NULL,
    line_no       bigint NOT NULL,
    reason_code   text   NOT NULL,
    reason        text   NOT NULL,
    raw           jsonb  NOT NULL,
    PRIMARY KEY (tenant_id, quarantine_id),
    UNIQUE (tenant_id, source_label, source_sha256, table_name, line_no)
);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['calendar', 'rsrc', 'project', 'projwbs', 'task', 'taskpred', 'taskrsrc', 'load_run', 'change_event', 'quarantine'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)) WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
    END LOOP;
END $$;
