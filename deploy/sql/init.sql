-- Atlas-MCP Postgres schema.
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
     'https://acme.example/shipping');
