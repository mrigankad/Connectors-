-- Knowledge access: source principals (users/groups) and ACL freshness per document.
CREATE TABLE source_principal (
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connection_id uuid NOT NULL,
    principal_id text NOT NULL,
    kind text NOT NULL, -- user | group | link | unknown
    display_name text,
    email text,
    member_of text[] NOT NULL DEFAULT '{}',
    observed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, connection_id, principal_id),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES connection(tenant_id, id)
);

-- Per-document ACL evidence state: when the full ACL set was last confirmed from the source.
CREATE TABLE document_acl_state (
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connection_id uuid NOT NULL,
    document_id text NOT NULL,
    acl_version text NOT NULL,
    confirmed_at timestamptz NOT NULL,
    deleted boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id, connection_id, document_id),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES connection(tenant_id, id)
);

-- Wayam principal <-> source principal mapping (product user identity to source identity), per connection.
CREATE TABLE principal_mapping (
    tenant_id uuid NOT NULL REFERENCES tenant(id),
    connection_id uuid NOT NULL,
    wayam_principal text NOT NULL,
    source_principal_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, connection_id, wayam_principal),
    FOREIGN KEY (tenant_id, connection_id) REFERENCES connection(tenant_id, id)
);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['source_principal','document_acl_state','principal_mapping'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format($p$
            CREATE POLICY tenant_isolation ON %I
            USING (tenant_id::text = current_setting('wayam.tenant_id', true)
                   OR current_setting('wayam.bypass', true) IN ('worker_claim', 'migration', 'admin', 'test'))
            WITH CHECK (tenant_id::text = current_setting('wayam.tenant_id', true)
                   OR current_setting('wayam.bypass', true) IN ('worker_claim', 'migration', 'admin', 'test'))$p$, t);
    END LOOP;
END $$;
