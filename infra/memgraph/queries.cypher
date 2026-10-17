// ============================================================================
// Reference Cypher queries (Memgraph / openCypher)
// ============================================================================

// --- 2-hop layered money-mule path extraction --------------------------------
// Money in, forwarded within 5 minutes through an intermediary.
MATCH (src:Account {vpa_id: $src_vpa})-[t1:TRANSFERRED]->(mule:Account)
      -[t2:TRANSFERRED]->(dest:Account)
WHERE t1.timestamp_ms >= $window_start
  AND t2.timestamp_ms >= t1.timestamp_ms
  AND t2.timestamp_ms - t1.timestamp_ms <= 300000
RETURN src.vpa_id AS src, mule.vpa_id AS mule, dest.vpa_id AS dest,
       t1.amount AS in_amount, t2.amount AS out_amount,
       (t2.timestamp_ms - t1.timestamp_ms) AS pass_through_ms
ORDER BY pass_through_ms ASC;

// --- Fan-out / fan-in mule detector (24h window) ------------------------------
// Accounts paying many distinct accounts (fan-out) or being paid by many
// distinct accounts (fan-in) are strong mule indicators.
MATCH (a:Account)-[t:TRANSFERRED]->(b:Account)
WHERE t.timestamp_ms >= $window_start
WITH a, count(DISTINCT b) AS out_degree
WHERE out_degree >= 5
RETURN a.vpa_id, out_degree ORDER BY out_degree DESC;

MATCH (b:Account)<-[t:TRANSFERRED]-(a:Account)
WHERE t.timestamp_ms >= $window_start
WITH b, count(DISTINCT a) AS in_degree
WHERE in_degree >= 5
RETURN b.vpa_id, in_degree ORDER BY in_degree DESC;

// --- Shared-device cluster (account takeover / synthetic identity) ------------
MATCH (a1:Account)-[:USED_DEVICE]->(d:Device)<-[:USED_DEVICE]-(a2:Account)
WHERE a1.vpa_id <> a2.vpa_id
RETURN d.device_hash, collect(a1.vpa_id) + collect(a2.vpa_id) AS accounts
LIMIT 50;

// --- 2-hop neighbourhood extraction (used by feature-worker) ------------------
MATCH (a:Account {vpa_id: $vpa})-[*1..2]-(n)
RETURN DISTINCT n LIMIT 100;

MATCH (a:Account {vpa_id: $vpa})-[*1..2]-(n1)-[r]-(n2)
RETURN n1, type(r) AS rel, properties(r) AS props, n2 LIMIT 300;
