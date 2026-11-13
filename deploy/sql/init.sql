-- Scout-MCP Postgres schema.
--
-- This is the bare skeleton the support-copilot uses: customers, orders,
-- tickets, documents. The interesting bit is Row-Level Security — every
-- table has a policy that matches `tenant_id` to the `app.tenant_id`
-- session variable set by the MCP server before each query.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ── Core tables ──────────────────────────────────────────────────────────
CREATE TABLE customers (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    tier        TEXT NOT NULL DEFAULT 'standard',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE orders (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    customer_id  TEXT NOT NULL REFERENCES customers(id),
    status       TEXT NOT NULL,
    total_cents  BIGINT NOT NULL,
    currency     TEXT NOT NULL DEFAULT 'USD',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_orders_customer ON orders(customer_id, created_at DESC);

CREATE TABLE documents (
    id         TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    url        TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Row-level security ───────────────────────────────────────────────────
-- The MCP server runs `SET LOCAL app.tenant_id = $1` before every query;
-- the RLS policy below enforces that rows from other tenants are invisible
-- regardless of what SQL the agent tries to run.

ALTER TABLE customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders    ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;

-- Row-level security does not apply to superusers, and the compose POSTGRES_USER
-- is one. By default it also skips the table owner. So the server connects as a
-- separate role that owns nothing, can only SELECT, and cannot bypass RLS.
-- FORCE makes the policies apply to the owner as well.
ALTER TABLE customers FORCE ROW LEVEL SECURITY;
ALTER TABLE orders    FORCE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;

-- Dev password. Change it, and the DSN in .env, for any real deployment.
CREATE ROLE scout_app LOGIN PASSWORD 'scout_app'
    NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
GRANT USAGE ON SCHEMA public TO scout_app;
GRANT SELECT ON customers, orders, documents TO scout_app;

CREATE POLICY tenant_isolation_customers ON customers
    USING (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY tenant_isolation_orders ON orders
    USING (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY tenant_isolation_documents ON documents
    USING (tenant_id = current_setting('app.tenant_id', true));

-- ── Sample data for two tenants ──────────────────────────────────────────
INSERT INTO customers (id, tenant_id, name, email, tier) VALUES
    ('CUST-1001', 'acme',   'Alicia Rivera', 'alicia@example.com', 'gold'),
    ('CUST-1002', 'acme',   'Ben Wallace',   'ben@example.com',    'standard'),
    ('CUST-2001', 'globex', 'Cho Nakamura',  'cho@example.com',    'gold');

INSERT INTO orders (id, tenant_id, customer_id, status, total_cents) VALUES
    ('o_9001', 'acme',   'CUST-1001', 'delivered',         12900),
    ('o_9002', 'acme',   'CUST-1001', 'refund_pending',    4900),
    ('o_9003', 'acme',   'CUST-1002', 'shipped',           29900),
    ('o_9101', 'globex', 'CUST-2001', 'delivered',         8900);

INSERT INTO documents (id, tenant_id, title, body, url) VALUES
    ('doc_refund_policy', 'acme',
     'Refund policy',
     'Refunds are issued within 5 business days of receiving the returned item.',
     'https://acme.example/refunds'),
    ('doc_shipping', 'acme',
     'Shipping timelines',
     'Standard shipping is 3-5 business days. Express is 1-2.',
     'https://acme.example/shipping'),
    ('doc_globex_sla', 'globex',
     'Support SLA',
     'Gold customers get a first response within 2 hours.',
     'https://globex.example/sla');

-- ── Keycloak's own database ──────────────────────────────────────────────
-- A separate role and database, so the identity provider shares the Postgres
-- server but nothing else: it cannot read scout data, and scout_app cannot read
-- its tables. Dev password; change it for any real deployment.
CREATE ROLE keycloak LOGIN PASSWORD 'keycloak'
    NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
CREATE DATABASE keycloak OWNER keycloak;

-- Postgres lets every role connect to every database by default. Close that: each
-- application role may connect to its own database only.
REVOKE CONNECT ON DATABASE scout FROM PUBLIC;
GRANT CONNECT ON DATABASE scout TO scout_app;
REVOKE CONNECT ON DATABASE keycloak FROM PUBLIC;
GRANT CONNECT ON DATABASE keycloak TO keycloak;
