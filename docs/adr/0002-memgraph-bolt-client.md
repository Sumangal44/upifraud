# ADR-0002: Memgraph via Bolt (neo4rs), not the HTTP API

**Status**: Accepted · **Date**: 2026-10

## Context
The feature-worker extracts a 2-hop neighbourhood subgraph per transaction
under a ~20 ms budget. Memgraph's old REST/HTTP query endpoint (port 7444) is
deprecated/removed in current versions; the supported programmatic interface
is the **Bolt protocol on 7687** (openCypher, Neo4j-compatible).

## Decision
Talk to Memgraph over Bolt using [`neo4rs`](https://docs.rs/neo4rs) 0.8, the
async Neo4j Rust driver. Memgraph accepts any credentials, so the driver's
auth handshake is a no-op. Queries are parameterized (`.param(...)`) — no
string interpolation of user-controlled VPA values.

## Consequences
- ✅ One round-trip per query; two queries (nodes + edges) per transaction.
- ✅ Pure Rust, no native client libs; parameter binding avoids Cypher
  injection.
- ⚠️ neo4rs targets Neo4j's Bolt versions; Memgraph implements Bolt 4.x+. If a
  future driver/server combination breaks negotiation, the fallback is the
  low-level pure-Rust `bolt-client` crate.
- ⚠️ The two-query neighbourhood read is eventually consistent with writes; the
  graph feeds a risk score, so sub-second staleness is acceptable.

## Alternatives
Memgraph HTTP API (gone), `bolt-client` raw messages (more code, same
wire-protocol risk), syncing edges to a non-graph store (loses multi-hop
traversal).
