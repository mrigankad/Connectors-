-- Objects observed during a full crawl. Deletions are inferred only after the crawl completes entirely.
CREATE TABLE crawl_seen (
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connection_id uuid NOT NULL,
    crawl_id uuid NOT NULL,
    source_object_type text NOT NULL,
    source_object_id text NOT NULL,
    seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, connection_id, crawl_id, source_object_type, source_object_id),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES connection(tenant_id, id)
);
ALTER TABLE crawl_seen ENABLE ROW LEVEL SECURITY;
ALTER TABLE crawl_seen FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON crawl_seen
    USING (tenant_id::text = current_setting('wayam.tenant_id', true)
           OR current_setting('wayam.bypass', true) IN ('worker_claim', 'migration', 'admin', 'test'))
    WITH CHECK (tenant_id::text = current_setting('wayam.tenant_id', true)
           OR current_setting('wayam.bypass', true) IN ('worker_claim', 'migration', 'admin', 'test'));
