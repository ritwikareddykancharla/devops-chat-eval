"""Stateful Multi-Turn DevOps Chat & Cross-Incident Memory Graph backed by SQLite WAL."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .bedrock_client import NOVA_LITE_MODEL, converse_bedrock
from .incidents_seed import CUSTOMER_SPACES, INCIDENTS_SEED, SERVICE_EDGES

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-\.]*[a-z0-9]|[a-z0-9]+", re.IGNORECASE)
INCIDENT_ID_RE = re.compile(r"\bINC-\d{3}\b", re.IGNORECASE)

STOPWORDS: Set[str] = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "by",
    "is", "was", "were", "are", "be", "been", "this", "that", "it", "its", "did",
    "any", "what", "how", "why", "when", "where", "which", "we", "our", "last",
    "have", "had", "has", "from", "into", "across", "before", "after", "same",
    "other", "can", "will", "if", "right", "now", "about", "tell", "me", "show",
}

CHAT_SYSTEM_PROMPT = (
    "You are AWS DevOps Agent Chat operating strictly inside one isolated customer Space.\n"
    "Rules:\n"
    "1. Ground every technical claim in the provided Space Telemetry & Incident Graph Context.\n"
    "2. Cite exact incident IDs in brackets like [INC-201], exact service names, CloudWatch alarms, "
    "and exact metric numbers or UTC timestamps from the context.\n"
    "3. Explain cross-incident correlations (shared upstream/downstream dependencies or repeat failure modes) "
    "and give concrete mitigation or pre-incident prevention guidance.\n"
    "4. Never invent incident IDs, metrics, or services outside this customer Space. Keep answers concise (90-150 words)."
)


def tokenize_query(text: str) -> List[str]:
    return [
        tok.lower()
        for tok in TOKEN_RE.findall(text)
        if tok.lower() not in STOPWORDS and len(tok) > 1
    ]


class DevOpsChatMemoryStore:
    """SQLite WAL multi-tenant DevOps Space incident graph & multi-turn chat store."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        default_dir = Path(os.environ.get("DEVOPSCHATEVAL_RUNTIME_DIR", "/tmp/devops-chat-eval"))
        default_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path) if db_path else (default_dir / "devops_space.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_and_seed()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_and_seed(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS spaces (
                        space_id TEXT PRIMARY KEY,
                        account_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        region TEXT NOT NULL,
                        rds_max_connections INTEGER NOT NULL,
                        services_json TEXT NOT NULL,
                        downstream_p99_json TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS incidents (
                        incident_id TEXT PRIMARY KEY,
                        space_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        service TEXT NOT NULL,
                        upstream_json TEXT NOT NULL,
                        downstream_json TEXT NOT NULL,
                        failure_mode TEXT NOT NULL,
                        started_at_utc TEXT NOT NULL,
                        resolved_at_utc TEXT NOT NULL,
                        duration_minutes INTEGER NOT NULL,
                        deploy_sha TEXT NOT NULL,
                        alarms_json TEXT NOT NULL,
                        metrics_json TEXT NOT NULL,
                        root_cause TEXT NOT NULL,
                        mitigation TEXT NOT NULL,
                        timeline_json TEXT NOT NULL,
                        FOREIGN KEY(space_id) REFERENCES spaces(space_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_incidents_space ON incidents(space_id);
                    CREATE INDEX IF NOT EXISTS idx_incidents_service ON incidents(space_id, service);

                    CREATE TABLE IF NOT EXISTS service_edges (
                        edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        space_id TEXT NOT NULL,
                        source_service TEXT NOT NULL,
                        target_service TEXT NOT NULL,
                        relation TEXT NOT NULL,
                        UNIQUE(space_id, source_service, target_service, relation)
                    );

                    CREATE INDEX IF NOT EXISTS idx_edges_space ON service_edges(space_id);

                    CREATE TABLE IF NOT EXISTS chat_sessions (
                        session_id TEXT NOT NULL,
                        space_id TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY (session_id, space_id)
                    );

                    CREATE TABLE IF NOT EXISTS chat_turns (
                        turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        space_id TEXT NOT NULL,
                        turn_index INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        cited_incidents_json TEXT NOT NULL,
                        latency_ms REAL NOT NULL,
                        created_at REAL NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_turns_session ON chat_turns(space_id, session_id, turn_index);
                    """
                )

                for sp in CUSTOMER_SPACES:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO spaces
                        (space_id, account_id, title, region, rds_max_connections, services_json, downstream_p99_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sp["space_id"],
                            sp["account_id"],
                            sp["title"],
                            sp["region"],
                            int(sp["rds_max_connections"]),
                            json.dumps(sp["services"]),
                            json.dumps(sp["downstream_p99_ms"]),
                        ),
                    )

                for inc in INCIDENTS_SEED:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO incidents
                        (incident_id, space_id, title, severity, service, upstream_json, downstream_json,
                         failure_mode, started_at_utc, resolved_at_utc, duration_minutes, deploy_sha,
                         alarms_json, metrics_json, root_cause, mitigation, timeline_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            inc["incident_id"],
                            inc["space_id"],
                            inc["title"],
                            inc["severity"],
                            inc["service"],
                            json.dumps(inc["upstream_services"]),
                            json.dumps(inc["downstream_services"]),
                            inc["failure_mode"],
                            inc["started_at_utc"],
                            inc["resolved_at_utc"],
                            int(inc["duration_minutes"]),
                            inc["deploy_sha"],
                            json.dumps(inc["alarms"]),
                            json.dumps(inc["metrics"]),
                            inc["root_cause"],
                            inc["mitigation"],
                            json.dumps(inc["timeline"]),
                        ),
                    )

                for edge in SERVICE_EDGES:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO service_edges
                        (space_id, source_service, target_service, relation)
                        VALUES (?, ?, ?, ?)
                        """,
                        (edge["space_id"], edge["source"], edge["target"], edge["relation"]),
                    )

                conn.commit()
            finally:
                conn.close()

    def reset_sessions(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM chat_turns;")
                conn.execute("DELETE FROM chat_sessions;")
                conn.commit()
            finally:
                conn.close()

    def list_spaces(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM spaces ORDER BY space_id").fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                inc_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM incidents WHERE space_id = ?",
                    (r["space_id"],),
                ).fetchone()["c"]
                out.append(
                    {
                        "space_id": r["space_id"],
                        "account_id": r["account_id"],
                        "title": r["title"],
                        "region": r["region"],
                        "rds_max_connections": r["rds_max_connections"],
                        "services": json.loads(r["services_json"]),
                        "downstream_p99_ms": json.loads(r["downstream_p99_json"]),
                        "incident_count": inc_count,
                    }
                )
            return out
        finally:
            conn.close()

    def get_space(self, space_id: str) -> Dict[str, Any]:
        for sp in self.list_spaces():
            if sp["space_id"] == space_id:
                return sp
        raise KeyError(f"Unknown customer space_id: {space_id}")

    @staticmethod
    def _row_to_incident(r: sqlite3.Row) -> Dict[str, Any]:
        return {
            "incident_id": r["incident_id"],
            "space_id": r["space_id"],
            "title": r["title"],
            "severity": r["severity"],
            "service": r["service"],
            "upstream_services": json.loads(r["upstream_json"]),
            "downstream_services": json.loads(r["downstream_json"]),
            "failure_mode": r["failure_mode"],
            "started_at_utc": r["started_at_utc"],
            "resolved_at_utc": r["resolved_at_utc"],
            "duration_minutes": r["duration_minutes"],
            "deploy_sha": r["deploy_sha"],
            "alarms": json.loads(r["alarms_json"]),
            "metrics": json.loads(r["metrics_json"]),
            "root_cause": r["root_cause"],
            "mitigation": r["mitigation"],
            "timeline": json.loads(r["timeline_json"]),
        }

    def list_incidents(self, space_id: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            if space_id:
                self.get_space(space_id)
                rows = conn.execute(
                    "SELECT * FROM incidents WHERE space_id = ? ORDER BY incident_id",
                    (space_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM incidents ORDER BY incident_id").fetchall()
            return [self._row_to_incident(r) for r in rows]
        finally:
            conn.close()

    def get_incident(self, space_id: str, incident_id: str) -> Dict[str, Any]:
        self.get_space(space_id)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM incidents WHERE space_id = ? AND incident_id = ?",
                (space_id, incident_id.upper()),
            ).fetchone()
            if not row:
                raise KeyError(f"Incident {incident_id} not found in isolated space {space_id}")
            return self._row_to_incident(row)
        finally:
            conn.close()

    def get_space_edges(self, space_id: str) -> List[Dict[str, str]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT source_service, target_service, relation FROM service_edges WHERE space_id = ?",
                (space_id,),
            ).fetchall()
            return [
                {"source": r["source_service"], "target": r["target_service"], "relation": r["relation"]}
                for r in rows
            ]
        finally:
            conn.close()

    def get_session_turns(self, space_id: str, session_id: str) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT turn_index, role, content, cited_incidents_json, latency_ms, created_at
                FROM chat_turns
                WHERE space_id = ? AND session_id = ?
                ORDER BY turn_index ASC
                """,
                (space_id, session_id),
            ).fetchall()
            return [
                {
                    "turn_index": r["turn_index"],
                    "role": r["role"],
                    "content": r["content"],
                    "cited_incidents": json.loads(r["cited_incidents_json"]),
                    "latency_ms": r["latency_ms"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def verify_cross_space_isolation(self) -> Dict[str, Any]:
        """Run deterministic multi-tenant control/data plane isolation probes across all 3 Spaces."""
        t0 = time.perf_counter()
        probes = [
            ("space-prod-payments", "INC-206 Valkey-InventoryCluster oversold_sku_count"),
            ("space-prod-fulfillment", "INC-211 OIDC-JWKS-Origin jwks_refresh_qps"),
            ("space-prod-identity", "INC-201 Aurora-Payments-Primary checkout-api"),
        ]
        leaks = 0
        details: List[Dict[str, Any]] = []
        for target_space, foreign_query in probes:
            res = self.retrieve_context(space_id=target_space, query=foreign_query, session_id="iso-probe", top_k=5)
            foreign_hits = [
                inc["incident_id"]
                for inc in res["retrieved_incidents"]
                if inc["space_id"] != target_space
            ]
            leaks += len(foreign_hits)
            details.append(
                {
                    "queried_space": target_space,
                    "foreign_probe": foreign_query,
                    "foreign_incidents_returned": len(foreign_hits),
                    "isolated": len(foreign_hits) == 0,
                }
            )
        return {
            "probes_run": len(probes),
            "cross_tenant_leaks": leaks,
            "isolated": leaks == 0,
            "verify_ms": round((time.perf_counter() - t0) * 1000.0, 3),
            "details": details,
        }

    def retrieve_context(
        self,
        space_id: str,
        query: str,
        session_id: Optional[str] = None,
        top_k: int = 3,
    ) -> Dict[str, Any]:
        """Hybrid BM25 + 2-hop Service Topology Graph retrieval scoped strictly to `space_id`."""
        t0 = time.perf_counter()
        space = self.get_space(space_id)
        incidents = self.list_incidents(space_id=space_id)
        edges = self.get_space_edges(space_id=space_id)

        # Carry over recent session context terms for multi-turn follow-up coreference
        prior_turns = self.get_session_turns(space_id, session_id) if session_id else []
        carry_tokens: List[str] = []
        prior_incidents: Set[str] = set()
        for t in prior_turns[-4:]:
            for cid in t["cited_incidents"]:
                prior_incidents.add(cid.upper())
            if t["role"] == "user":
                carry_tokens.extend(tokenize_query(t["content"])[:10])

        raw_q_tokens = tokenize_query(query)
        # Current turn tokens get full weight (2x count), carry-over tokens get 1x weight for follow-ups
        weighted_q_tokens = raw_q_tokens * 2 + carry_tokens

        # Build BM25 corpus over space incidents
        docs_tokens: List[Counter[str]] = []
        doc_lengths: List[int] = []
        df: Counter[str] = Counter()
        for inc in incidents:
            blob = " ".join(
                [
                    inc["incident_id"],
                    inc["title"],
                    inc["service"],
                    " ".join(inc["upstream_services"]),
                    " ".join(inc["downstream_services"]),
                    inc["failure_mode"].replace("_", " "),
                    " ".join(inc["alarms"]),
                    " ".join(f"{k} {v}" for k, v in inc["metrics"].items()),
                    inc["root_cause"],
                    inc["mitigation"],
                ]
            )
            toks = tokenize_query(blob)
            c = Counter(toks)
            docs_tokens.append(c)
            doc_lengths.append(len(toks))
            for term in c.keys():
                df[term] += 1

        n_docs = max(1, len(incidents))
        avgdl = sum(doc_lengths) / float(n_docs) if doc_lengths else 1.0
        k1 = 1.5
        b = 0.75

        q_lower = query.lower()
        bm25_scores: Dict[str, float] = {}
        for idx, inc in enumerate(incidents):
            score = 0.0
            tf_map = docs_tokens[idx]
            dl = doc_lengths[idx]
            for term in weighted_q_tokens:
                if term not in tf_map:
                    continue
                n_q = df[term]
                idf = math.log(1.0 + (n_docs - n_q + 0.5) / (n_q + 0.5))
                tf = tf_map[term]
                denom = tf + k1 * (1.0 - b + b * (dl / avgdl))
                score += idf * ((tf * (k1 + 1.0)) / denom)

            # Exact identifier boosts (incident ID, primary service, downstream dependency)
            if inc["incident_id"].lower() in q_lower:
                score += 9.0
            if inc["service"].lower() in q_lower:
                score += 5.0
            for dep in inc["downstream_services"] + inc["upstream_services"]:
                if dep.lower() in q_lower:
                    score += 3.5
            if inc["incident_id"] in prior_incidents and len(raw_q_tokens) <= 14:
                score += 2.2

            bm25_scores[inc["incident_id"]] = round(score, 4)

        # Build undirected adjacency list for 2-hop topology neighbor expansion
        adj: Dict[str, List[Tuple[str, str]]] = {}
        for e in edges:
            adj.setdefault(e["source"], []).append((e["target"], e["relation"]))
            adj.setdefault(e["target"], []).append((e["source"], e["relation"]))

        # Seed anchor incidents from top BM25 hits or prior conversation anchors
        sorted_bm25 = sorted(incidents, key=lambda x: bm25_scores[x["incident_id"]], reverse=True)
        seed_incidents = [x for x in sorted_bm25[:2] if bm25_scores[x["incident_id"]] > 0]
        if not seed_incidents and prior_incidents:
            seed_incidents = [x for x in incidents if x["incident_id"] in prior_incidents][:2]
        if not seed_incidents and sorted_bm25:
            seed_incidents = [sorted_bm25[0]]

        seed_nodes: Set[str] = set()
        seed_modes: Set[str] = set()
        for sinc in seed_incidents:
            seed_nodes.add(sinc["service"])
            seed_nodes.update(sinc["downstream_services"])
            seed_modes.add(sinc["failure_mode"])

        # 1-hop neighbor services via explicit service_edges
        one_hop_nodes: Dict[str, str] = {}
        for node in list(seed_nodes):
            for nbr, rel in adj.get(node, []):
                if nbr not in seed_nodes:
                    one_hop_nodes[nbr] = f"{node} --[{rel}]--> {nbr}"

        graph_boosts: Dict[str, float] = {inc["incident_id"]: 0.0 for inc in incidents}
        graph_reasons: Dict[str, str] = {}

        seed_ids = {s["incident_id"] for s in seed_incidents}
        for inc in incidents:
            iid = inc["incident_id"]
            if iid in seed_ids:
                continue
            shared_deps = set([inc["service"]] + inc["downstream_services"] + inc["upstream_services"]) & seed_nodes
            if shared_deps:
                dep_name = sorted(shared_deps)[0]
                graph_boosts[iid] += 3.8
                graph_reasons[iid] = f"shared_dependency:{dep_name}"
            elif inc["service"] in one_hop_nodes:
                graph_boosts[iid] += 2.6
                graph_reasons[iid] = f"topology_neighbor:{one_hop_nodes[inc['service']]}"
            elif inc["failure_mode"] in seed_modes:
                graph_boosts[iid] += 2.2
                graph_reasons[iid] = f"recurring_failure_mode:{inc['failure_mode']}"

        scored_items: List[Dict[str, Any]] = []
        for inc in incidents:
            iid = inc["incident_id"]
            combined = round(bm25_scores[iid] + graph_boosts[iid], 4)
            scored_items.append(
                {
                    **inc,
                    "bm25_score": bm25_scores[iid],
                    "graph_boost": round(graph_boosts[iid], 4),
                    "combined_score": combined,
                    "retrieval_source": (
                        "bm25+graph_neighbor"
                        if bm25_scores[iid] > 0 and graph_boosts[iid] > 0
                        else ("graph_neighbor" if graph_boosts[iid] > 0 else "bm25_lexical")
                    ),
                    "graph_relation": graph_reasons.get(iid, "direct_anchor"),
                }
            )

        scored_items.sort(key=lambda x: x["combined_score"], reverse=True)
        top_hits = scored_items[: max(1, top_k)]
        retrieval_ms = round((time.perf_counter() - t0) * 1000.0, 3)

        return {
            "space": space,
            "retrieved_incidents": top_hits,
            "graph_neighbors_activated": [
                {"incident_id": x["incident_id"], "service": x["service"], "reason": x["graph_relation"]}
                for x in top_hits
                if x["graph_boost"] > 0
            ],
            "prior_turns_used": len(prior_turns),
            "retrieval_ms": retrieval_ms,
        }

    def verify_chat_citations(
        self,
        space_id: str,
        reply_text: str,
        retrieved_incidents: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Deterministically check every cited incident ID and service against isolated `space_id` ground truth."""
        space_incidents = {inc["incident_id"]: inc for inc in self.list_incidents(space_id=space_id)}
        all_incidents = {inc["incident_id"]: inc for inc in self.list_incidents(space_id=None)}
        space_info = self.get_space(space_id)

        raw_cited = [m.upper() for m in INCIDENT_ID_RE.findall(reply_text)]
        cited_unique: List[str] = []
        for c in raw_cited:
            if c not in cited_unique:
                cited_unique.append(c)

        verified_ids: List[str] = []
        cross_tenant_ids: List[str] = []
        unknown_ids: List[str] = []

        for cid in cited_unique:
            if cid in space_incidents:
                verified_ids.append(cid)
            elif cid in all_incidents:
                cross_tenant_ids.append(cid)
            else:
                unknown_ids.append(cid)

        matched_services = [
            svc for svc in space_info["services"] if svc.lower() in reply_text.lower()
        ]
        precision = (
            round(len(verified_ids) / float(len(cited_unique)), 4)
            if cited_unique
            else (1.0 if matched_services else 0.0)
        )

        return {
            "cited_incidents": cited_unique,
            "verified_incidents": verified_ids,
            "cross_tenant_incidents": cross_tenant_ids,
            "hallucinated_incidents": unknown_ids,
            "matched_space_services": matched_services,
            "citation_precision": precision,
            "grounded": len(cross_tenant_ids) == 0 and len(unknown_ids) == 0 and (len(verified_ids) > 0 or len(matched_services) > 0),
            "retrieved_anchor_ids": [inc["incident_id"] for inc in retrieved_incidents],
        }

    @staticmethod
    def _build_deterministic_fallback_reply(
        space: Dict[str, Any],
        retrieved: List[Dict[str, Any]],
    ) -> str:
        primary = retrieved[0]
        metrics_str = ", ".join(f"{k}={v}" for k, v in list(primary["metrics"].items())[:4])
        parts = [
            f"In `{space['space_id']}`, [{primary['incident_id']}] on `{primary['service']}` ({primary['severity']}, "
            f"started {primary['started_at_utc']}, {primary['duration_minutes']} min) was triggered by {primary['root_cause']}",
            f"Observed telemetry: {metrics_str}; alarms: {', '.join(primary['alarms'])}.",
        ]
        if len(retrieved) > 1:
            sec = retrieved[1]
            parts.append(
                f"Cross-incident correlation ({sec['graph_relation']}): [{sec['incident_id']}] on `{sec['service']}` "
                f"showed related impact ({sec['title']})."
            )
        parts.append(f"Proven mitigation: {primary['mitigation']}")
        return " ".join(parts)

    def chat_turn(
        self,
        space_id: str,
        message: str,
        session_id: str = "default-session",
        model_id: str = NOVA_LITE_MODEL,
        use_bedrock: bool = True,
    ) -> Dict[str, Any]:
        """Execute a stateful multi-turn DevOps chat turn with hybrid retrieval, Bedrock Nova, and citation verification."""
        clean_msg = message.strip()
        if not clean_msg:
            raise ValueError("Chat message cannot be empty")

        ctx = self.retrieve_context(space_id=space_id, query=clean_msg, session_id=session_id, top_k=3)
        space = ctx["space"]
        retrieved = ctx["retrieved_incidents"]
        prior_turns = self.get_session_turns(space_id=space_id, session_id=session_id)

        evidence_block = json.dumps(
            {
                "isolated_space_id": space["space_id"],
                "aws_region": space["region"],
                "rds_max_connections": space["rds_max_connections"],
                "downstream_p99_ms": space["downstream_p99_ms"],
                "retrieved_incidents": [
                    {
                        "incident_id": inc["incident_id"],
                        "service": inc["service"],
                        "severity": inc["severity"],
                        "failure_mode": inc["failure_mode"],
                        "started_at_utc": inc["started_at_utc"],
                        "duration_minutes": inc["duration_minutes"],
                        "deploy_sha": inc["deploy_sha"],
                        "alarms": inc["alarms"],
                        "metrics": inc["metrics"],
                        "root_cause": inc["root_cause"],
                        "mitigation": inc["mitigation"],
                        "graph_relation": inc["graph_relation"],
                    }
                    for inc in retrieved
                ],
            },
            indent=2,
        )

        # Build valid alternating Bedrock Converse messages list
        bedrock_messages: List[Dict[str, Any]] = []
        for pt in prior_turns[-6:]:
            role = "user" if pt["role"] == "user" else "assistant"
            if bedrock_messages and bedrock_messages[-1]["role"] == role:
                bedrock_messages[-1]["content"][0]["text"] += "\n" + pt["content"]
            else:
                bedrock_messages.append({"role": role, "content": [{"text": pt["content"]}]})

        augmented_user_prompt = (
            f"Customer Space Context (strictly scoped to {space['space_id']}):\n{evidence_block}\n\n"
            f"Engineer Question: {clean_msg}\n"
            "Provide a grounded DevOps response citing exact [INC-xxx] IDs, metrics, and prevention steps."
        )
        if bedrock_messages and bedrock_messages[-1]["role"] == "user":
            bedrock_messages[-1]["content"][0]["text"] += "\n\n" + augmented_user_prompt
        else:
            bedrock_messages.append({"role": "user", "content": [{"text": augmented_user_prompt}]})

        llm_res = (
            converse_bedrock(
                system_prompt=CHAT_SYSTEM_PROMPT,
                messages=bedrock_messages,
                model_id=model_id,
                max_tokens=420,
                temperature=0.1,
            )
            if use_bedrock
            else None
        )

        if llm_res and llm_res.get("text"):
            reply_text = llm_res["text"]
            bedrock_ms = llm_res["latency_ms"]
            tokens = llm_res["total_tokens"]
            cost_usd = llm_res["cost_usd"]
            engine = llm_res["engine"]
        else:
            reply_text = self._build_deterministic_fallback_reply(space, retrieved)
            bedrock_ms = 0.0
            tokens = 0
            cost_usd = 0.0
            engine = "deterministic-evidence-synthesizer"

        verification = self.verify_chat_citations(space_id, reply_text, retrieved)

        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO chat_sessions (session_id, space_id, created_at) VALUES (?, ?, ?)",
                    (session_id, space_id, now),
                )
                next_idx = len(prior_turns) + 1
                conn.execute(
                    """
                    INSERT INTO chat_turns
                    (session_id, space_id, turn_index, role, content, cited_incidents_json, latency_ms, created_at)
                    VALUES (?, ?, ?, 'user', ?, '[]', 0.0, ?)
                    """,
                    (session_id, space_id, next_idx, clean_msg, now),
                )
                conn.execute(
                    """
                    INSERT INTO chat_turns
                    (session_id, space_id, turn_index, role, content, cited_incidents_json, latency_ms, created_at)
                    VALUES (?, ?, ?, 'assistant', ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        space_id,
                        next_idx + 1,
                        reply_text,
                        json.dumps(verification["verified_incidents"]),
                        bedrock_ms,
                        now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

        return {
            "space_id": space_id,
            "session_id": session_id,
            "turn_number": (len(prior_turns) // 2) + 1,
            "user_message": clean_msg,
            "assistant_reply": reply_text,
            "llm_engine": engine,
            "retrieval_ms": ctx["retrieval_ms"],
            "bedrock_latency_ms": bedrock_ms,
            "total_tokens": tokens,
            "cost_usd": cost_usd,
            "retrieved_incidents": [
                {
                    "incident_id": r["incident_id"],
                    "title": r["title"],
                    "service": r["service"],
                    "severity": r["severity"],
                    "bm25_score": r["bm25_score"],
                    "graph_boost": r["graph_boost"],
                    "combined_score": r["combined_score"],
                    "retrieval_source": r["retrieval_source"],
                    "graph_relation": r["graph_relation"],
                }
                for r in retrieved
            ],
            "graph_neighbors_activated": ctx["graph_neighbors_activated"],
            "citation_verification": verification,
            "history": self.get_session_turns(space_id=space_id, session_id=session_id),
        }
