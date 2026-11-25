"""Pre-Deployment Config & Code Diff Risk Analyzer correlated against historical Space incidents."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .bedrock_client import NOVA_LITE_MODEL, invoke_single_turn
from .chat_memory import DevOpsChatMemoryStore

PRESET_DIFF_SCENARIOS: List[Dict[str, Any]] = [
    {
        "preset_id": "diff-timeout-cascade",
        "title": "Checkout HTTP Client Timeout Lowered Below Payment-Gateway p99",
        "space_id": "space-prod-payments",
        "service": "checkout-api",
        "expected_verdict": "BLOCK_DEPLOY",
        "diff_text": (
            "diff --git a/services/checkout/http_client.yaml b/services/checkout/http_client.yaml\n"
            "--- a/services/checkout/http_client.yaml\n"
            "+++ b/services/checkout/http_client.yaml\n"
            "@@ -6,7 +6,7 @@ upstream_clients:\n"
            "   payment_gateway:\n"
            "     target: https://payment-gateway.internal\n"
            "-    read_timeout_ms: 2600\n"
            "-    max_retries: 2\n"
            "+    read_timeout_ms: 750\n"
            "+    max_retries: 8\n"
            "     retry_backoff: fixed_50ms\n"
        ),
    },
    {
        "preset_id": "diff-rds-pool-overcommit",
        "title": "ECS Task Pool Size x Replica Count Exceeds Aurora max_connections",
        "space_id": "space-prod-payments",
        "service": "checkout-api",
        "expected_verdict": "BLOCK_DEPLOY",
        "diff_text": (
            "diff --git a/deploy/ecs_checkout_task.json b/deploy/ecs_checkout_task.json\n"
            "--- a/deploy/ecs_checkout_task.json\n"
            "+++ b/deploy/ecs_checkout_task.json\n"
            "@@ -11,6 +11,6 @@\n"
            "-  \"ecs_task_count\": 10,\n"
            "-  \"max_pool_size\": 20,\n"
            "+  \"ecs_task_count\": 14,\n"
            "+  \"max_pool_size\": 40,\n"
            "   \"db_cluster\": \"Aurora-Payments-Primary\"\n"
        ),
    },
    {
        "preset_id": "diff-sqs-visibility-mismatch",
        "title": "Order-Router Lambda Timeout Raised Above SQS VisibilityTimeout",
        "space_id": "space-prod-fulfillment",
        "service": "order-router",
        "expected_verdict": "BLOCK_DEPLOY",
        "diff_text": (
            "diff --git a/infra/order_router_template.yaml b/infra/order_router_template.yaml\n"
            "--- a/infra/order_router_template.yaml\n"
            "+++ b/infra/order_router_template.yaml\n"
            "@@ -18,6 +18,6 @@ Resources:\n"
            "   OrderDispatchQueue:\n"
            "     VisibilityTimeout: 30\n"
            "   OrderRouterFunction:\n"
            "-    Timeout: 25\n"
            "+    Timeout: 90\n"
        ),
    },
    {
        "preset_id": "diff-iam-wildcard-regression",
        "title": "Tenant-RBAC Policy Bundle Introduces Unscoped Wildcard Resource",
        "space_id": "space-prod-identity",
        "service": "tenant-rbac",
        "expected_verdict": "BLOCK_DEPLOY",
        "diff_text": (
            "diff --git a/policies/space_authorizer.json b/policies/space_authorizer.json\n"
            "--- a/policies/space_authorizer.json\n"
            "+++ b/policies/space_authorizer.json\n"
            "@@ -5,6 +5,6 @@\n"
            "   \"Effect\": \"Allow\",\n"
            "   \"Action\": [\"devops-space:ReadIncident\", \"kms:Decrypt\"],\n"
            "-  \"Resource\": \"arn:aws:devops-space:*:*:space/${aws:PrincipalTag/SpaceId}/*\"\n"
            "+  \"Resource\": \"*\"\n"
        ),
    },
    {
        "preset_id": "diff-safe-hardened-rollout",
        "title": "Safe Hardened Rollout: Bounded Pool + Jittered Retry + Circuit Breaker",
        "space_id": "space-prod-payments",
        "service": "fraud-scorer",
        "expected_verdict": "SAFE_TO_DEPLOY",
        "diff_text": (
            "diff --git a/services/fraud_scorer/client.py b/services/fraud_scorer/client.py\n"
            "--- a/services/fraud_scorer/client.py\n"
            "+++ b/services/fraud_scorer/client.py\n"
            "@@ -14,5 +14,6 @@\n"
            "-MAX_RETRIES = 3\n"
            "+MAX_RETRIES = 2\n"
            "+BACKOFF_STRATEGY = 'exponential_full_jitter'\n"
            "+CIRCUIT_BREAKER_ENABLED = True\n"
            " READ_TIMEOUT_MS = 2800\n"
            " MAX_POOL_SIZE = 16\n"
            " ECS_TASK_COUNT = 10\n"
        ),
    },
]

DIFF_RISK_SYSTEM_PROMPT = (
    "You are the AWS DevOps Agent Pre-Deployment Triage Gate.\n"
    "Given deterministic static diff findings and matched historical incidents from this customer Space, "
    "write a concise 3-bullet pre-deployment recommendation:\n"
    "1) Blast-radius summary referencing exact matched [INC-xxx] IDs and thresholds,\n"
    "2) Exact config/code parameter values to change before merging,\n"
    "3) Canary rollback alarm check. Keep total length under 120 words."
)


def _split_diff_lines(diff_text: str) -> Tuple[List[str], List[str], List[str]]:
    added: List[str] = []
    removed: List[str] = []
    context: List[str] = []
    has_hunk_markers = False

    for raw in diff_text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("diff --git", "index ", "@@")):
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            has_hunk_markers = True
            added.append(raw[1:].strip())
        elif raw.startswith("-"):
            has_hunk_markers = True
            removed.append(raw[1:].strip())
        else:
            context.append(line)

    # If user pasted a plain YAML/JSON/Python snippet without +/- markers, treat lines as proposed state
    if not has_hunk_markers:
        added = list(context)
    return added, removed, context


def _extract_number(lines: List[str], patterns: List[str]) -> Optional[float]:
    joined = "\n".join(lines)
    for pat in patterns:
        m = re.search(pat, joined, flags=re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def analyze_deploy_diff(
    store: DevOpsChatMemoryStore,
    space_id: str,
    service: str,
    diff_text: str,
    model_id: str = NOVA_LITE_MODEL,
    use_bedrock: bool = True,
) -> Dict[str, Any]:
    """Deterministically score config/code diff hazards against Space topology + past incidents."""
    if not diff_text or not diff_text.strip():
        raise ValueError("diff_text is required")

    t0 = time.perf_counter()
    space = store.get_space(space_id)
    incidents = store.list_incidents(space_id=space_id)
    edges = store.get_space_edges(space_id=space_id)

    added, removed, context = _split_diff_lines(diff_text)
    active_lines = added + context
    added_blob = "\n".join(added)
    removed_blob = "\n".join(removed)
    active_blob = "\n".join(active_lines)

    # Determine downstream p99 latencies relevant to `service`
    downstream_p99_map: Dict[str, int] = space.get("downstream_p99_ms", {})
    service_targets = [e["target"] for e in edges if e["source"].lower() == service.lower()]
    relevant_p99s: List[Tuple[str, int]] = []
    for tgt in service_targets:
        if tgt in downstream_p99_map:
            relevant_p99s.append((tgt, int(downstream_p99_map[tgt])))
    # Also check if diff mentions a specific downstream service explicitly
    for dep_name, dep_p99 in downstream_p99_map.items():
        norm_dep = dep_name.lower().replace("-", "_")
        if dep_name.lower() in active_blob.lower() or norm_dep in active_blob.lower():
            if (dep_name, int(dep_p99)) not in relevant_p99s:
                relevant_p99s.append((dep_name, int(dep_p99)))

    findings: List[Dict[str, Any]] = []
    triggered_modes: List[str] = []

    # 1. Timeout below downstream p99 check
    timeout_ms = _extract_number(
        added,
        [
            r"(?:read_timeout_ms|client_timeout_ms|http_timeout_ms|timeout_ms)\s*[:=]\s*(\d+(?:\.\d+)?)",
        ],
    )
    if timeout_ms is None:
        sec_val = _extract_number(
            added,
            [r"(?:read_timeout_sec|client_timeout_sec|read_timeout)\s*[:=]\s*(\d+(?:\.\d+)?)"],
        )
        if sec_val is not None and sec_val < 30:
            timeout_ms = sec_val * 1000.0

    if timeout_ms is not None and relevant_p99s:
        max_dep, max_p99 = max(relevant_p99s, key=lambda x: x[1])
        if timeout_ms < max_p99 * 1.10:
            findings.append(
                {
                    "rule_id": "DR-TIMEOUT-01",
                    "severity": "CRITICAL",
                    "risk_points": 68,
                    "failure_mode": "timeout_below_downstream_p99",
                    "summary": (
                        f"Configured client timeout ({int(timeout_ms)} ms) is below `{max_dep}` p99 latency "
                        f"({max_p99} ms), guaranteeing premature aborts and retry amplification."
                    ),
                }
            )
            triggered_modes.append("timeout_below_downstream_p99")

    # 2. DB connection pool overcommit check
    pool_size = _extract_number(
        active_lines,
        [r"(?:max_pool_size|pool_size|db_pool_size|max_connections_per_task)\s*[\"']?\s*[:=]\s*(\d+)"],
    )
    task_count = _extract_number(
        active_lines,
        [r"(?:ecs_task_count|task_count|desired_count|replicas|max_capacity)\s*[\"']?\s*[:=]\s*(\d+)"],
    )
    if pool_size is not None:
        effective_tasks = int(task_count) if task_count is not None else 12
        total_conns = int(pool_size) * effective_tasks
        rds_limit = int(space["rds_max_connections"])
        if total_conns > int(rds_limit * 0.90):
            findings.append(
                {
                    "rule_id": "DR-DBPOOL-02",
                    "severity": "CRITICAL",
                    "risk_points": 76,
                    "failure_mode": "db_connection_pool_exhaustion",
                    "summary": (
                        f"Total DB pool demand ({int(pool_size)} conns x {effective_tasks} tasks = {total_conns}) "
                        f"exceeds safe 90% threshold of Space RDS max_connections={rds_limit}."
                    ),
                }
            )
            triggered_modes.append("db_connection_pool_exhaustion")

    # 3. Unbounded retry / missing jitter backoff check
    retries_val = _extract_number(added, [r"(?:max_retries|retry_attempts|retries)\s*[\"']?\s*[:=]\s*(\d+)"])
    has_while_true = bool(re.search(r"\bwhile\s+True\s*:", added_blob))
    has_jitter = bool(re.search(r"jitter|exponential", active_blob, re.IGNORECASE))
    fixed_backoff = bool(re.search(r"fixed_\d+ms|sleep\(\s*0\.0\d+", active_blob, re.IGNORECASE))
    if has_while_true or (retries_val is not None and (retries_val >= 5 or (retries_val >= 4 and not has_jitter) or fixed_backoff)):
        findings.append(
            {
                "rule_id": "DR-RETRY-03",
                "severity": "HIGH",
                "risk_points": 38,
                "failure_mode": "unbounded_retry_missing_circuit_breaker",
                "summary": (
                    f"High/unbounded retry configuration ({'while True' if has_while_true else f'max_retries={int(retries_val or 0)}'}) "
                    "without exponential full-jitter backoff risks synchronized thundering-herd storms."
                ),
            }
        )
        triggered_modes.append("unbounded_retry_missing_circuit_breaker")

    # 4. Disabled or removed circuit breaker / singleflight coalescing
    cb_disabled = bool(
        re.search(r"(?:circuit_breaker|circuit_breaker_enabled|singleflight)\s*[\"']?\s*[:=]\s*(?:false|0|off)", added_blob, re.IGNORECASE)
    )
    cb_removed = bool(
        re.search(r"circuit_breaker|singleflight", removed_blob, re.IGNORECASE)
        and not re.search(r"circuit_breaker|singleflight", added_blob, re.IGNORECASE)
    )
    if cb_disabled or cb_removed:
        findings.append(
            {
                "rule_id": "DR-BREAKER-04",
                "severity": "HIGH",
                "risk_points": 34,
                "failure_mode": "cache_stampede_missing_singleflight",
                "summary": "Circuit breaker or singleflight request coalescing disabled/removed on downstream dependency call.",
            }
        )
        triggered_modes.extend(["unbounded_retry_missing_circuit_breaker", "cache_stampede_missing_singleflight"])

    # 5. SQS VisibilityTimeout <= Lambda/Worker Timeout
    vis_timeout = _extract_number(active_lines, [r"(?:VisibilityTimeout|visibility_timeout(?:_sec)?)\s*[\"']?\s*[:=]\s*(\d+)"])
    fn_timeout = _extract_number(active_lines, [r"(?:FunctionTimeout|worker_timeout(?:_sec)?|\bTimeout)\s*[\"']?\s*[:=]\s*(\d+)"])
    if vis_timeout is not None and fn_timeout is not None and vis_timeout < fn_timeout * 2:
        findings.append(
            {
                "rule_id": "DR-SQSVIS-05",
                "severity": "CRITICAL",
                "risk_points": 72,
                "failure_mode": "queue_visibility_lt_timeout",
                "summary": (
                    f"Queue VisibilityTimeout ({int(vis_timeout)}s) is shorter than recommended >=2x..6x worker "
                    f"Timeout ({int(fn_timeout)}s), causing duplicate concurrent message execution."
                ),
            }
        )
        triggered_modes.append("queue_visibility_lt_timeout")

    # 6. IAM Wildcard Resource/Action or KMS Decrypt Regression
    wildcard_iam = bool(
        re.search(r"[\"']?(?:Resource|Action)[\"']?\s*:\s*[\"']\*[\"']", added_blob)
        and "PrincipalTag/SpaceId" not in added_blob
    )
    kms_dropped = bool(
        re.search(r"kms:Decrypt", removed_blob, re.IGNORECASE)
        and not re.search(r"kms:Decrypt", added_blob, re.IGNORECASE)
    )
    if wildcard_iam or kms_dropped:
        mode = "iam_wildcard_tenant_boundary" if wildcard_iam else "iam_kms_policy_regression"
        findings.append(
            {
                "rule_id": "DR-IAMKMS-06",
                "severity": "CRITICAL",
                "risk_points": 78,
                "failure_mode": mode,
                "summary": (
                    "IAM policy introduces unscoped '*' wildcard without PrincipalTag/SpaceId boundary."
                    if wildcard_iam
                    else "IAM policy removes required kms:Decrypt permission on KMS envelope key."
                ),
            }
        )
        triggered_modes.extend(["iam_wildcard_tenant_boundary", "iam_kms_policy_regression"])

    # 7. Synchronous SSM / per-request socket creation in hot path
    sync_ssm_or_unpooled = bool(
        re.search(r"ssm\.get_parameter\(|PoolManager\(\)", added_blob)
    )
    if sync_ssm_or_unpooled:
        findings.append(
            {
                "rule_id": "DR-HOTPATH-07",
                "severity": "HIGH",
                "risk_points": 66,
                "failure_mode": "sync_control_plane_call_in_data_plane",
                "summary": "Per-request control-plane SSM call or unpooled socket client detected inside hot request handler.",
            }
        )
        triggered_modes.extend(["sync_control_plane_call_in_data_plane", "nat_ephemeral_port_exhaustion"])

    # Match historical incidents in this Space by failure_mode or service/dependency overlap
    matched_incidents: List[Dict[str, Any]] = []
    for inc in incidents:
        mode_hit = inc["failure_mode"] in triggered_modes
        svc_hit = inc["service"].lower() == service.lower() and len(findings) > 0
        if mode_hit or svc_hit:
            matched_incidents.append(
                {
                    "incident_id": inc["incident_id"],
                    "title": inc["title"],
                    "service": inc["service"],
                    "severity": inc["severity"],
                    "failure_mode": inc["failure_mode"],
                    "match_reason": "exact_failure_signature" if mode_hit else "same_service_blast_radius",
                    "past_mitigation": inc["mitigation"],
                }
            )

    matched_incidents.sort(key=lambda x: (0 if x["match_reason"] == "exact_failure_signature" else 1, x["incident_id"]))

    raw_score = sum(int(f["risk_points"]) for f in findings)
    risk_score = min(100, raw_score) if findings else 8

    if risk_score >= 65:
        verdict = "BLOCK_DEPLOY"
    elif risk_score >= 30:
        verdict = "WARN_REVIEW"
    else:
        verdict = "SAFE_TO_DEPLOY"

    deterministic_ms = round((time.perf_counter() - t0) * 1000.0, 3)

    # Generate Bedrock mitigation guidance
    bedrock_prompt = json.dumps(
        {
            "space_id": space_id,
            "service": service,
            "risk_score": risk_score,
            "verdict": verdict,
            "deterministic_findings": findings,
            "matched_past_incidents": matched_incidents[:3],
            "diff_excerpt": diff_text[:900],
        },
        indent=2,
    )
    llm_res = (
        invoke_single_turn(
            system_prompt=DIFF_RISK_SYSTEM_PROMPT,
            user_prompt=bedrock_prompt,
            model_id=model_id,
            max_tokens=280,
            temperature=0.1,
        )
        if use_bedrock
        else None
    )

    if llm_res and llm_res.get("text"):
        guidance = llm_res["text"]
        bedrock_ms = llm_res["latency_ms"]
        tokens = llm_res["total_tokens"]
        cost_usd = llm_res["cost_usd"]
        engine = llm_res["engine"]
    else:
        if findings:
            inc_refs = ", ".join(f"[{m['incident_id']}]" for m in matched_incidents[:2]) or "Space topology limits"
            guidance = (
                f"- **Blast Radius ({verdict})**: Proposed change on `{service}` triggers {len(findings)} hazard rule(s) "
                f"matching historical outage signature {inc_refs}.\n"
                f"- **Required Fix**: {findings[0]['summary']} Apply past mitigation: "
                f"{matched_incidents[0]['past_mitigation'] if matched_incidents else 'Restore bounded timeouts, connection pools, and least-privilege ARNs.'}\n"
                "- **Rollout Gate**: Validate in canary shard for 15 minutes with automatic CloudWatch alarm rollback."
            )
        else:
            guidance = (
                f"- **Safe Change (`SAFE_TO_DEPLOY`)**: `{service}` diff keeps connection pools, timeouts, retry jitter, "
                "and IAM boundaries within Space limits.\n"
                "- **Rollout Check**: Proceed with standard 10% -> 50% -> 100% baked deployment."
            )
        bedrock_ms = 0.0
        tokens = 0
        cost_usd = 0.0
        engine = "deterministic-risk-advisor"

    return {
        "space_id": space_id,
        "service": service,
        "risk_score": risk_score,
        "verdict": verdict,
        "hazard_count": len(findings),
        "findings": findings,
        "matched_incidents": matched_incidents,
        "mitigation_guidance": guidance,
        "llm_engine": engine,
        "deterministic_eval_ms": deterministic_ms,
        "bedrock_latency_ms": bedrock_ms,
        "total_tokens": tokens,
        "cost_usd": cost_usd,
    }
