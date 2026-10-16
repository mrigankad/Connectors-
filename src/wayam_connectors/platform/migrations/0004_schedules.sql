-- Recurring job schedules per connection partition. The scheduler tick enqueues a job when due and the
-- partition has no active job; it never runs adapters itself.
CREATE TABLE sync_schedule (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connection_id uuid NOT NULL,
    product text NOT NULL,
    job_type text NOT NULL,
    resource_type text NOT NULL,
    resource_id text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    interval_seconds integer NOT NULL CHECK (interval_seconds >= 60),
    enabled boolean NOT NULL DEFAULT true,
    next_run_at timestamptz NOT NULL DEFAULT now(),
    last_enqueued_at timestamptz,
    last_job_id uuid,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES connection(tenant_id, id)
);
CREATE UNIQUE INDEX sync_schedule_partition ON sync_schedule (tenant_id, connection_id, job_type, resource_type, COALESCE(resource_id, ''));
CREATE INDEX sync_schedule_due ON sync_schedule (next_run_at) WHERE enabled;
ALTER TABLE sync_schedule ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_schedule FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON sync_schedule
    USING (tenant_id::text = current_setting('wayam.tenant_id', true)
           OR current_setting('wayam.bypass', true) IN ('worker_claim', 'migration', 'admin', 'test'))
    WITH CHECK (tenant_id::text = current_setting('wayam.tenant_id', true)
           OR current_setting('wayam.bypass', true) IN ('worker_claim', 'migration', 'admin', 'test'));
