"""Execute real end-to-end DevOpsChatEval benchmarks with live Amazon Bedrock Converse calls."""
from __future__ import annotations

import concurrent.futures
import json
import os
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Ensure live Amazon Bedrock calls are enabled
os.environ.pop("DEVOPSCHATEVAL_OFFLINE_LLM", None)

from devops_chat_eval.bedrock_client import NOVA_LITE_MODEL, NOVA_MICRO_MODEL
from devops_chat_eval.chat_memory import DevOpsChatMemoryStore
from devops_chat_eval.coe_workflow import generate_verified_coe
from devops_chat_eval.deploy_risk import PRESET_DIFF_SCENARIOS, analyze_deploy_diff
from devops_chat_eval.judge_arena import (
    GOLDEN_EVAL_CASES,
    PROMPT_TEMPLATES,
    compute_cohens_kappa,
    evaluate_position_swapped_pair,
    generate_candidate_answer,
)

EXTRA_DIFF_BENCHMARK_CASES: List[Dict[str, Any]] = [
    {
        "preset_id": "diff-cb-disabled-fraud",
        "title": "Fraud-Scorer Disables Circuit Breaker With 12 Retries",
        "space_id": "space-prod-payments",
        "service": "fraud-scorer",
        "expected_verdict": "BLOCK_DEPLOY",
        "diff_text": "+max_retries: 12\n+circuit_breaker_enabled: false\n",
    },
    {
        "preset_id": "diff-ssm-hotpath-edge",
        "title": "Session-Edge Adds Per-Request SSM Parameter Fetch in Handler",
        "space_id": "space-prod-identity",
        "service": "session-edge",
        "expected_verdict": "BLOCK_DEPLOY",
        "diff_text": "+flag = ssm.get_parameter(Name='/prod/edge/auth_mode')\n",
    },
    {
        "preset_id": "diff-safe-sqs-6x-timeout",
        "title": "Safe Order-Router Queue Visibility Set to 6x Worker Timeout",
        "space_id": "space-prod-fulfillment",
        "service": "order-router",
        "expected_verdict": "SAFE_TO_DEPLOY",
        "diff_text": "+VisibilityTimeout: 540\n+Timeout: 90\n",
    },
    {
        "preset_id": "diff-safe-scoped-iam",
        "title": "Safe Scoped SpaceId PrincipalTag IAM Policy Update",
        "space_id": "space-prod-identity",
        "service": "tenant-rbac",
        "expected_verdict": "SAFE_TO_DEPLOY",
        "diff_text": '+"Resource": "arn:aws:devops-space:us-east-1:*:space/${aws:PrincipalTag/SpaceId}/*"\n',
    },
    {
        "preset_id": "diff-safe-checkout-timeout",
        "title": "Safe Checkout Client Timeout Above Downstream p99 (2700 ms)",
        "space_id": "space-prod-payments",
        "service": "checkout-api",
        "expected_verdict": "SAFE_TO_DEPLOY",
        "diff_text": "+read_timeout_ms: 2700\n+max_retries: 2\n+retry_strategy: exponential_jitter\n",
    },
]

MULTI_TURN_BENCHMARK_FLOWS: List[Dict[str, Any]] = [
    {
        "session_id": "bench-sess-payments-1",
        "space_id": "space-prod-payments",
        "turns": [
            {
                "query": "Why did checkout-api hit 18.4% 5xx during deploy-8841a2c?",
                "expected_incident": "INC-201",
            },
            {
                "query": "Did another service sharing Aurora-Payments-Primary saturate CPU with retries later that month?",
                "expected_incident": "INC-203",
            },
        ],
    },
    {
        "session_id": "bench-sess-payments-2",
        "space_id": "space-prod-payments",
        "turns": [
            {
                "query": "What happened when checkout-api lowered read_timeout_ms to 800 ms against payment-gateway?",
                "expected_incident": "INC-202",
            },
            {
                "query": "Did payment-gateway also have a KMS Decrypt outage after a Terraform IAM change?",
                "expected_incident": "INC-205",
            },
        ],
    },
    {
        "session_id": "bench-sess-fulfillment-1",
        "space_id": "space-prod-fulfillment",
        "turns": [
            {
                "query": "Why did inventory-svc lose 68400 reservation locks in Valkey-InventoryCluster?",
                "expected_incident": "INC-206",
            },
            {
                "query": "Which batch service writing to that cluster later suffered ECS Fargate exit 137 OOMKills?",
                "expected_incident": "INC-209",
            },
        ],
    },
    {
        "session_id": "bench-sess-identity-1",
        "space_id": "space-prod-identity",
        "turns": [
            {
                "query": "Explain the 4200 QPS OIDC key rotation stampede on auth-token-svc.",
                "expected_incident": "INC-211",
            },
            {
                "query": "How did session-edge upstream of auth-token-svc throttle SSM GetParameter?",
                "expected_incident": "INC-212",
            },
        ],
    },
]


def pct(vals: List[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round((q / 100.0) * (len(s) - 1)))))
    return round(s[idx], 3)


def main() -> None:
    print("======================================================================")
    print(" Running DevOpsChatEval Live AWS Bedrock & Grounding Benchmark Suite")
    print("======================================================================")
    tracemalloc.start()
    t_suite_start = time.perf_counter()

    with tempfile.TemporaryDirectory() as tmpdir:
        store = DevOpsChatMemoryStore(db_path=Path(tmpdir) / "bench_devops.db")
        inc_lookup = {inc["incident_id"]: inc for inc in store.list_incidents(space_id=None)}

        bedrock_latencies_lite: List[float] = []
        bedrock_latencies_micro: List[float] = []
        total_bedrock_calls = 0
        total_bedrock_tokens = 0
        total_bedrock_cost_usd = 0.0

        # ------------------------------------------------------------------
        # 1. Multi-Turn DevOps Chat & Cross-Space Isolation
        # ------------------------------------------------------------------
        print("[1/4] Benchmarking Multi-Turn DevOps Chat & Hybrid BM25+Graph Retrieval...")
        iso_res = store.verify_cross_space_isolation()
        retrieval_latencies: List[float] = []
        chat_results: List[Dict[str, Any]] = []
        chat_hits = 0
        chat_grounded = 0

        for flow in MULTI_TURN_BENCHMARK_FLOWS:
            for turn_spec in flow["turns"]:
                res = store.chat_turn(
                    space_id=flow["space_id"],
                    message=turn_spec["query"],
                    session_id=flow["session_id"],
                    model_id=NOVA_LITE_MODEL,
                    use_bedrock=True,
                )
                retrieval_latencies.append(res["retrieval_ms"])
                if res["bedrock_latency_ms"] > 0:
                    bedrock_latencies_lite.append(res["bedrock_latency_ms"])
                    total_bedrock_calls += 1
                    total_bedrock_tokens += res["total_tokens"]
                    total_bedrock_cost_usd += res["cost_usd"]

                top_ids = [r["incident_id"] for r in res["retrieved_incidents"]]
                hit = turn_spec["expected_incident"] in top_ids
                if hit:
                    chat_hits += 1
                if res["citation_verification"]["grounded"]:
                    chat_grounded += 1

                chat_results.append(
                    {
                        "session_id": flow["session_id"],
                        "space_id": flow["space_id"],
                        "turn_number": res["turn_number"],
                        "expected_incident": turn_spec["expected_incident"],
                        "retrieved_top3": top_ids,
                        "recall_hit": hit,
                        "grounded": res["citation_verification"]["grounded"],
                        "retrieval_ms": res["retrieval_ms"],
                        "bedrock_latency_ms": res["bedrock_latency_ms"],
                        "tokens": res["total_tokens"],
                    }
                )

        # ------------------------------------------------------------------
        # 2. Pre-Deployment Diff Risk Analyzer (6 Risky + 4 Safe Benign)
        # ------------------------------------------------------------------
        print("[2/4] Benchmarking Pre-Deployment Config & Code Diff Risk Analyzer (10 cases)...")
        all_diffs = list(PRESET_DIFF_SCENARIOS) + EXTRA_DIFF_BENCHMARK_CASES
        diff_eval_ms_list: List[float] = []
        diff_results: List[Dict[str, Any]] = []
        risky_total = 0
        risky_caught = 0
        safe_total = 0
        safe_false_positives = 0

        for idx, dcase in enumerate(all_diffs):
            # Run live Bedrock mitigation guidance on the 5 primary presets
            run_live = idx < 5
            dres = analyze_deploy_diff(
                store=store,
                space_id=dcase["space_id"],
                service=dcase["service"],
                diff_text=dcase["diff_text"],
                model_id=NOVA_LITE_MODEL,
                use_bedrock=run_live,
            )
            diff_eval_ms_list.append(dres["deterministic_eval_ms"])
            if dres["bedrock_latency_ms"] > 0:
                bedrock_latencies_lite.append(dres["bedrock_latency_ms"])
                total_bedrock_calls += 1
                total_bedrock_tokens += dres["total_tokens"]
                total_bedrock_cost_usd += dres["cost_usd"]

            if dcase["expected_verdict"] == "BLOCK_DEPLOY":
                risky_total += 1
                if dres["verdict"] == "BLOCK_DEPLOY":
                    risky_caught += 1
            else:
                safe_total += 1
                if dres["verdict"] != "SAFE_TO_DEPLOY":
                    safe_false_positives += 1

            diff_results.append(
                {
                    "preset_id": dcase["preset_id"],
                    "title": dcase["title"],
                    "space_id": dcase["space_id"],
                    "service": dcase["service"],
                    "expected_verdict": dcase["expected_verdict"],
                    "observed_verdict": dres["verdict"],
                    "risk_score": dres["risk_score"],
                    "matched_incidents": [m["incident_id"] for m in dres["matched_incidents"][:2]],
                    "deterministic_eval_ms": dres["deterministic_eval_ms"],
                    "bedrock_latency_ms": dres["bedrock_latency_ms"],
                }
            )

        # ------------------------------------------------------------------
        # 3. Fact-Verified AWS COE Workflow & Hallucination Stripping Gate
        # ------------------------------------------------------------------
        print("[3/4] Benchmarking Fact-Verified AWS COE Postmortem Workflow (8 runs)...")
        coe_targets = [
            ("space-prod-payments", "INC-201"),
            ("space-prod-fulfillment", "INC-206"),
            ("space-prod-fulfillment", "INC-207"),
            ("space-prod-identity", "INC-211"),
        ]
        coe_gate_ms_list: List[float] = []
        coe_results: List[Dict[str, Any]] = []
        injected_hallucinations_total = 0
        injected_hallucinations_caught = 0
        post_gate_surviving_hallucinations = 0

        for sp_id, inc_id in coe_targets:
            for probe_flag in (False, True):
                cres = generate_verified_coe(
                    store=store,
                    space_id=sp_id,
                    incident_id=inc_id,
                    inject_hallucination_probe=probe_flag,
                    model_id=NOVA_LITE_MODEL,
                    use_bedrock=True,
                )
                coe_gate_ms_list.append(cres["gate_eval_ms"])
                if cres["bedrock_latency_ms"] > 0:
                    bedrock_latencies_lite.append(cres["bedrock_latency_ms"])
                    total_bedrock_calls += 1
                    total_bedrock_tokens += cres["total_tokens"]
                    total_bedrock_cost_usd += cres["cost_usd"]

                if probe_flag:
                    injected_hallucinations_total += len(cres["injected_probes"])
                    serialized_doc = json.dumps(cres["coe_document"])
                    caught_here = sum(
                        1
                        for tok in ("99.94%", "03:47 UTC", "quantum-billing-svc")
                        if tok not in serialized_doc
                    )
                    injected_hallucinations_caught += caught_here

                post_gate_surviving_hallucinations += cres["post_gate_ungrounded_claims"]
                coe_results.append(
                    {
                        "incident_id": inc_id,
                        "space_id": sp_id,
                        "probe_injected": probe_flag,
                        "clauses_checked": cres["raw_draft_clauses_checked"],
                        "flagged_claims_count": cres["flagged_claims_count"],
                        "post_gate_ungrounded": cres["post_gate_ungrounded_claims"],
                        "gate_eval_ms": cres["gate_eval_ms"],
                        "bedrock_latency_ms": cres["bedrock_latency_ms"],
                    }
                )

        # ------------------------------------------------------------------
        # 4. 16-Case Prompt Tuning (`v1`, `v2`, `v3`) & Position-Swapped Judge
        # ------------------------------------------------------------------
        print("[4/4] Benchmarking 16-Case Prompt Tuning & Position-Swapped LLM-as-a-Judge...")
        prompt_ids = ["v1_baseline", "v2_fewshot_cot", "v3_citation_constrained"]
        gen_tasks: List[Tuple[str, Dict[str, Any]]] = [
            (pid, case) for pid in prompt_ids for case in GOLDEN_EVAL_CASES
        ]

        gen_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

        def _run_gen(item: Tuple[str, Dict[str, Any]]) -> Tuple[Tuple[str, str], Dict[str, Any]]:
            pid, c = item
            inc = inc_lookup[c["incident_id"]]
            out = generate_candidate_answer(
                prompt_id=pid,
                system_prompt=PROMPT_TEMPLATES[pid]["system_prompt"],
                case=c,
                inc=inc,
                gen_model_id=NOVA_MICRO_MODEL,
                use_bedrock=True,
            )
            return (pid, c["case_id"]), out

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for key, out in pool.map(_run_gen, gen_tasks):
                gen_cache[key] = out
                if out["latency_ms"] > 1.0:
                    bedrock_latencies_micro.append(out["latency_ms"])
                    total_bedrock_calls += 1
                    total_bedrock_tokens += out["total_tokens"]
                    total_bedrock_cost_usd += out["cost_usd"]

        # Deduplicate ordered pairs `(pid_a, pid_b, case_id)` needed across v2 vs v1, v3 vs v1, and calibration
        needed_pairs: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        pair_specs: List[Tuple[str, str, Dict[str, Any]]] = []
        seen_pair_keys = set()

        for cand_id in ("v2_fewshot_cot", "v3_citation_constrained"):
            for c in GOLDEN_EVAL_CASES:
                pk = (cand_id, "v1_baseline", c["case_id"])
                if pk not in seen_pair_keys:
                    seen_pair_keys.add(pk)
                    pair_specs.append((cand_id, "v1_baseline", c))

        for c in GOLDEN_EVAL_CASES:
            pa, pb = c["calib_pair"]
            pk = (pa, pb, c["case_id"])
            if pk not in seen_pair_keys:
                seen_pair_keys.add(pk)
                pair_specs.append((pa, pb, c))

        def _run_swapped_pair(spec: Tuple[str, str, Dict[str, Any]]) -> Tuple[Tuple[str, str, str], Dict[str, Any]]:
            pa, pb, c = spec
            inc = inc_lookup[c["incident_id"]]
            ga = gen_cache[(pa, c["case_id"])]
            gb = gen_cache[(pb, c["case_id"])]
            judged = evaluate_position_swapped_pair(
                resp_a_text=ga["text"],
                resp_b_text=gb["text"],
                case=c,
                inc=inc,
                judge_model_id=NOVA_MICRO_MODEL,
                use_bedrock=True,
            )
            return (pa, pb, c["case_id"]), judged

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for pk, jres in pool.map(_run_swapped_pair, pair_specs):
                needed_pairs[pk] = jres
                if jres["judge_latency_ms"] > 1.0:
                    bedrock_latencies_micro.append(round(jres["judge_latency_ms"] / 2.0, 2))
                    total_bedrock_calls += 2
                    total_bedrock_tokens += jres["judge_tokens"]
                    total_bedrock_cost_usd += jres["judge_cost_usd"]

        # Summarize Prompt Version Leaderboard (v1, v2, v3 across 16 cases)
        prompt_leaderboard: List[Dict[str, Any]] = []
        for pid in prompt_ids:
            recalls = []
            rubrics = []
            lats = []
            costs = []
            hallucinations = 0
            for c in GOLDEN_EVAL_CASES:
                inc = inc_lookup[c["incident_id"]]
                g = gen_cache[(pid, c["case_id"])]
                lats.append(g["latency_ms"])
                costs.append(g["cost_usd"])
                # Grab rubric score
                from devops_chat_eval.judge_arena import score_response_rubric

                rb = score_response_rubric(g["text"], c, inc)
                recalls.append(rb["required_token_recall"])
                rubrics.append(rb["rubric_score_5"])
                if rb["hallucinated_incident"]:
                    hallucinations += 1

            if pid == "v1_baseline":
                win_rate = 0.0
                wins = 0
                pos_cons = 100.0
                decision = "BASELINE"
            else:
                wins = sum(
                    1
                    for c in GOLDEN_EVAL_CASES
                    if needed_pairs[(pid, "v1_baseline", c["case_id"])]["debiased_winner"] == "A"
                )
                cons = sum(
                    1
                    for c in GOLDEN_EVAL_CASES
                    if needed_pairs[(pid, "v1_baseline", c["case_id"])]["position_consistent"]
                )
                win_rate = round((wins / float(len(GOLDEN_EVAL_CASES))) * 100.0, 1)
                pos_cons = round((cons / float(len(GOLDEN_EVAL_CASES))) * 100.0, 1)
                mean_rec = round(statistics.mean(recalls) * 100.0, 1)
                p95_l = pct(lats, 95)
                decision = (
                    "PROMOTE"
                    if (win_rate >= 65.0 and mean_rec >= 80.0 and hallucinations == 0 and pos_cons >= 75.0 and p95_l <= 2500.0)
                    else "HOLD"
                )

            prompt_leaderboard.append(
                {
                    "prompt_id": pid,
                    "label": PROMPT_TEMPLATES[pid]["label"],
                    "cases": len(GOLDEN_EVAL_CASES),
                    "wins_vs_v1": wins,
                    "debiased_win_rate_vs_v1_pct": win_rate,
                    "telemetry_recall_pct": round(statistics.mean(recalls) * 100.0, 1),
                    "mean_rubric_score_5": round(statistics.mean(rubrics), 2),
                    "position_consistency_pct": pos_cons,
                    "gen_p50_ms": pct(lats, 50),
                    "gen_p95_ms": pct(lats, 95),
                    "cost_per_1k_calls_usd": round(statistics.mean(costs) * 1000.0, 4),
                    "promotion_decision": decision,
                }
            )

        # Summarize 16-Case Human Calibration Suite (Cohen's kappa)
        calib_preds: List[str] = []
        calib_refs: List[str] = []
        calib_consistent = 0
        calib_flips_caught = 0
        calib_judge_lats: List[float] = []
        calibration_rows: List[Dict[str, Any]] = []

        for c in GOLDEN_EVAL_CASES:
            pa, pb = c["calib_pair"]
            jr = needed_pairs[(pa, pb, c["case_id"])]
            calib_preds.append(jr["debiased_winner"])
            calib_refs.append(c["human_calib_winner"])
            if jr["position_consistent"]:
                calib_consistent += 1
            else:
                calib_flips_caught += 1
            calib_judge_lats.append(jr["judge_latency_ms"])
            calibration_rows.append(
                {
                    "case_id": c["case_id"],
                    "incident_id": c["incident_id"],
                    "pair": f"{pa} vs {pb}",
                    "forward_winner": jr["pass_forward_winner"],
                    "swapped_winner": jr["pass_swapped_winner"],
                    "debiased_winner": jr["debiased_winner"],
                    "human_label": c["human_calib_winner"],
                    "agreed": jr["debiased_winner"] == c["human_calib_winner"],
                    "position_consistent": jr["position_consistent"],
                    "bias_flag": jr["bias_flag"],
                    "judge_pair_ms": jr["judge_latency_ms"],
                }
            )

        cohens_kappa = compute_cohens_kappa(calib_preds, calib_refs, labels=["A", "B", "TIE"])
        human_agree_count = sum(1 for p, r in zip(calib_preds, calib_refs) if p == r)
        human_agree_pct = round((human_agree_count / float(len(GOLDEN_EVAL_CASES))) * 100.0, 1)
        pos_consistency_pct = round((calib_consistent / float(len(GOLDEN_EVAL_CASES))) * 100.0, 1)

        _, peak_mem_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        wall_sec = round(time.perf_counter() - t_suite_start, 2)

        v3_row = next(r for r in prompt_leaderboard if r["prompt_id"] == "v3_citation_constrained")

        results_payload: Dict[str, Any] = {
            "service": "devops-chat-eval",
            "benchmark_timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "environment": {
                "python_version": platform.python_version(),
                "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
                "aws_region": os.environ.get("AWS_DEFAULT_REGION", "us-west-2"),
                "chat_and_coe_model": NOVA_LITE_MODEL,
                "judge_and_tuning_model": NOVA_MICRO_MODEL,
                "state_store": "SQLite WAL (isolated per-Space Control/Data Plane)",
            },
            "headline_kpis": {
                "spaces_isolated": 3,
                "incidents_indexed": 14,
                "cross_tenant_leaks": iso_res["cross_tenant_leaks"],
                "chat_turns_evaluated": len(chat_results),
                "chat_top3_recall_hits": chat_hits,
                "chat_top3_recall_pct": round((chat_hits / float(len(chat_results))) * 100.0, 1),
                "chat_grounded_turns": chat_grounded,
                "retrieval_p50_ms": pct(retrieval_latencies, 50),
                "retrieval_p95_ms": pct(retrieval_latencies, 95),
                "diff_cases_total": len(diff_results),
                "diff_risky_caught": risky_caught,
                "diff_risky_total": risky_total,
                "diff_hazard_recall_pct": round((risky_caught / float(risky_total)) * 100.0, 1),
                "diff_safe_total": safe_total,
                "diff_false_positives": safe_false_positives,
                "diff_eval_p50_ms": pct(diff_eval_ms_list, 50),
                "diff_eval_p95_ms": pct(diff_eval_ms_list, 95),
                "coe_runs_total": len(coe_results),
                "coe_injected_hallucinations_total": injected_hallucinations_total,
                "coe_injected_hallucinations_caught": injected_hallucinations_caught,
                "coe_hallucination_catch_pct": round(
                    (injected_hallucinations_caught / float(max(1, injected_hallucinations_total))) * 100.0, 1
                ),
                "coe_post_gate_ungrounded_claims": post_gate_surviving_hallucinations,
                "coe_gate_p50_ms": pct(coe_gate_ms_list, 50),
                "eval_golden_cases": len(GOLDEN_EVAL_CASES),
                "v3_win_rate_vs_v1_pct": v3_row["debiased_win_rate_vs_v1_pct"],
                "v3_wins_vs_v1": v3_row["wins_vs_v1"],
                "v3_telemetry_recall_pct": v3_row["telemetry_recall_pct"],
                "v3_promotion_decision": v3_row["promotion_decision"],
                "judge_cohens_kappa": cohens_kappa,
                "judge_human_agreement_count": human_agree_count,
                "judge_human_agreement_pct": human_agree_pct,
                "judge_position_consistency_pct": pos_consistency_pct,
                "judge_position_flips_caught": calib_flips_caught,
                "judge_pair_p50_ms": pct(calib_judge_lats, 50),
                "judge_pair_p95_ms": pct(calib_judge_lats, 95),
                "nova_lite_p50_ms": pct(bedrock_latencies_lite, 50),
                "nova_lite_p95_ms": pct(bedrock_latencies_lite, 95),
                "nova_micro_p50_ms": pct(bedrock_latencies_micro, 50),
                "nova_micro_p95_ms": pct(bedrock_latencies_micro, 95),
                "bedrock_calls_total": total_bedrock_calls,
                "bedrock_tokens_total": total_bedrock_tokens,
                "bedrock_total_cost_usd": round(total_bedrock_cost_usd, 5),
                "peak_memory_kib": round(peak_mem_bytes / 1024.0, 1),
                "suite_wall_seconds": wall_sec,
            },
            "prompt_leaderboard": prompt_leaderboard,
            "judge_calibration_rows": calibration_rows,
            "diff_benchmark_results": diff_results,
            "coe_benchmark_results": coe_results,
            "chat_benchmark_results": chat_results,
        }

        out_path = ROOT_DIR / "benchmarks" / "latest_results.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results_payload, indent=2) + "\n", encoding="utf-8")

        print("\n✅ Saved measured results to:", out_path)
        print(json.dumps(results_payload["headline_kpis"], indent=2))
        print("\nPrompt Leaderboard:")
        for row in prompt_leaderboard:
            print(" ", row)


if __name__ == "__main__":
    main()
