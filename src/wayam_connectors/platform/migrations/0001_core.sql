-- Core tenant-scoped tables with row-level security.
-- Tenant isolation: every tenant table has tenant_id and a policy comparing it to
-- current_setting('wayam.tenant_id'). FORCE RLS makes the table owner subject to it too.
-- current_setting(..., true) returns NULL when unset, so a session that forgot to set the
-- tenant sees nothing rather than everything.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE tenant (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE api_key (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    product text NOT NULL,
    key_hash bytea NOT NULL UNIQUE,
    key_prefix text NOT NULL,
    roles text[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);

CREATE TABLE connector_definition (
    connector_id text PRIMARY KEY,
    catalog_id text NOT NULL,
    adapter_version text NOT NULL,
    schema_version text NOT NULL,
    verification_level text NOT NULL,
    manifest jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE connection (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connector_id text NOT NULL REFERENCES connector_definition(connector_id),
    name text NOT NULL,
    config jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'active', -- active | blocked | revoked
    status_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    UNIQUE (tenant_id, id)
);

CREATE TABLE credential_ref (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connection_id uuid NOT NULL,
    backend text NOT NULL,
    key_id text NOT NULL,
    ciphertext bytea NOT NULL,
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    rotated_at timestamptz,
    revoked_at timestamptz,
    FOREIGN KEY (tenant_id, connection_id) REFERENCES connection(tenant_id, id)
);
CREATE UNIQUE INDEX credential_ref_current ON credential_ref (tenant_id, connection_id) WHERE revoked_at IS NULL;

CREATE TABLE capability_grant (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connection_id uuid NOT NULL,
    product text NOT NULL,
    capabilities text[] NOT NULL,
    resource_allowlist jsonb NOT NULL DEFAULT '[]'::jsonb,
    granted_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    FOREIGN KEY (tenant_id, connection_id) REFERENCES connection(tenant_id, id)
);
CREATE UNIQUE INDEX capability_grant_current ON capability_grant (tenant_id, connection_id, product) WHERE revoked_at IS NULL;

CREATE TABLE sync_job (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connection_id uuid NOT NULL,
    product text NOT NULL,
    job_type text NOT NULL,
    resource_type text NOT NULL,
    resource_id text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'queued', -- queued | leased | succeeded | failed | dead_letter | cancelled
    attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 5,
    run_after timestamptz NOT NULL DEFAULT now(),
    lease_token uuid,
    leased_by text,
    leased_until timestamptz,
    last_error jsonb,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    cancel_requested boolean NOT NULL DEFAULT false,
    FOREIGN KEY (tenant_id, connection_id) REFERENCES connection(tenant_id, id)
);
CREATE INDEX sync_job_claim ON sync_job (run_after) WHERE status IN ('queued', 'leased');
-- one active job per connection/resource partition
CREATE UNIQUE INDEX sync_job_active_partition ON sync_job (tenant_id, connection_id, job_type, resource_type, COALESCE(resource_id, ''))
    WHERE status IN ('queued', 'leased');

CREATE TABLE checkpoint (
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connection_id uuid NOT NULL,
    resource_type text NOT NULL,
    resource_id text NOT NULL DEFAULT '',
    cursor jsonb,
    cursor_version text,
    last_success_at timestamptz,
    records_total bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, connection_id, resource_type, resource_id),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES connection(tenant_id, id)
);

CREATE TABLE raw_object_ref (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connection_id uuid NOT NULL,
    object_key text NOT NULL,
    sha256 text NOT NULL,
    size bigint NOT NULL,
    content_type text,
    stored_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, connection_id, sha256),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES connection(tenant_id, id)
);

CREATE TABLE normalized_record (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connection_id uuid NOT NULL,
    product text NOT NULL,
    source_system text NOT NULL,
    source_edition text,
    source_object_type text NOT NULL,
    source_object_id text NOT NULL,
    version_key text NOT NULL, -- event_id, else source_version, else checksum
    source_version text,
    event_id text,
    operation text NOT NULL,
    schema_version text NOT NULL,
    mapping_version text,
    checksum text NOT NULL,
    source_timestamp timestamptz,
    observed_at timestamptz NOT NULL,
    provenance text NOT NULL,
    raw_object_ref text,
    acl_ref text,
    payload jsonb NOT NULL,
    extensions jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_current boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, connection_id, source_object_type, source_object_id, version_key),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES connection(tenant_id, id)
);
CREATE INDEX normalized_record_current ON normalized_record (tenant_id, connection_id, source_object_type, source_object_id) WHERE is_current;

CREATE TABLE quarantine_record (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connection_id uuid NOT NULL,
    job_id uuid,
    source_object_id text,
    provenance text NOT NULL,
    reason text NOT NULL,
    error jsonb NOT NULL,
    raw jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES connection(tenant_id, id)
);

CREATE TABLE source_acl (
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connection_id uuid NOT NULL,
    document_id text NOT NULL,
    principal_id text NOT NULL,
    role text NOT NULL,
    inherited boolean NOT NULL DEFAULT false,
    acl_version text NOT NULL,
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, connection_id, document_id, principal_id),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES connection(tenant_id, id)
);

CREATE TABLE webhook_inbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connection_id uuid NOT NULL,
    delivery_id text NOT NULL,
    event_type text,
    headers jsonb NOT NULL,
    body bytea NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL DEFAULT 'received', -- received | processed | rejected
    processed_at timestamptz,
    UNIQUE (tenant_id, connection_id, delivery_id),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES connection(tenant_id, id)
);

CREATE TABLE outbox (
    id bigserial PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connection_id uuid,
    topic text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz
);
CREATE INDEX outbox_unpublished ON outbox (id) WHERE published_at IS NULL;

CREATE TABLE action_request (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connection_id uuid NOT NULL,
    product text NOT NULL,
    action text NOT NULL,
    target text NOT NULL,
    arguments jsonb NOT NULL DEFAULT '{}'::jsonb,
    requested_by text NOT NULL,
    approval_ref text,
    status text NOT NULL DEFAULT 'proposed', -- proposed | approved | rejected | executed | failed
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    FOREIGN KEY (tenant_id, connection_id) REFERENCES connection(tenant_id, id)
);

CREATE TABLE audit_event (
    id bigserial PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    actor text NOT NULL,
    action text NOT NULL,
    target text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Row-level security -------------------------------------------------------
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'connection','credential_ref','capability_grant','sync_job','checkpoint','raw_object_ref',
        'normalized_record','quarantine_record','source_acl','webhook_inbox','outbox','action_request','audit_event'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$
            CREATE POLICY tenant_isolation ON %I
            USING (
                tenant_id::text = current_setting('wayam.tenant_id', true)
                OR current_setting('wayam.bypass', true) IN ('worker_claim', 'migration', 'admin', 'test')
            )
            WITH CHECK (
                tenant_id::text = current_setting('wayam.tenant_id', true)
                OR current_setting('wayam.bypass', true) IN ('worker_claim', 'migration', 'admin', 'test')
            )$p$, t);
    END LOOP;
END $$;

-- api_key is read during authentication, before a tenant is known: only the auth path may read it.
ALTER TABLE api_key ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_key FORCE ROW LEVEL SECURITY;
CREATE POLICY api_key_auth ON api_key
    USING (current_setting('wayam.bypass', true) IN ('auth', 'admin', 'test')
           OR tenant_id::text = current_setting('wayam.tenant_id', true))
    WITH CHECK (current_setting('wayam.bypass', true) IN ('auth', 'admin', 'test')
           OR tenant_id::text = current_setting('wayam.tenant_id', true));
