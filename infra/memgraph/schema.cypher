// ============================================================================
// UPI Fraud Detection — Memgraph schema
// Applied idempotently by tools/seed/seed.py (constraints are also attempted
// at container first start by Memgraph itself when this file is referenced
// via --init-data-file).
//
// Node labels & key properties:
//   (:Account   {vpa_id, created_at_ms, risk_flags})
//   (:Device    {device_hash, created_at_ms, rooted})
//   (:IPAddress {ip_address, risk_score})
//   (:Merchant  {merchant_id, created_at_ms})
//
// Relationships:
//   (:Account)-[:TRANSFERRED   {amount, timestamp_ms, latency_ms}]->(:Account)
//   (:Account)-[:USED_DEVICE   {last_active}]->(:Device)
//   (:Account)-[:CONNECTED_FROM{timestamp_ms}]->(:IPAddress)
//   (:Account)-[:PAID_MERCHANT {amount, timestamp_ms}]->(:Merchant)
// ============================================================================

CREATE CONSTRAINT ON (a:Account) ASSERT a.vpa_id IS UNIQUE;
CREATE CONSTRAINT ON (d:Device) ASSERT d.device_hash IS UNIQUE;
CREATE CONSTRAINT ON (i:IPAddress) ASSERT i.ip_address IS UNIQUE;
CREATE CONSTRAINT ON (m:Merchant) ASSERT m.merchant_id IS UNIQUE;

CREATE INDEX ON :Account(created_at_ms);
CREATE INDEX ON :TRANSFERRED(timestamp_ms);
CREATE INDEX ON :Device(created_at_ms);
