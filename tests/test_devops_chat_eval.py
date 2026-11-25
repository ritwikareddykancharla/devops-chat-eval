"""Unit test suite for DevOpsChatEval (Chat Memory, Diff Risk, COE Grounding, Judge Arena, and API)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

# Run unit tests deterministically without network dependency; live Bedrock runs in benchmarks/run_benchmarks.py
os.environ["DEVOPSCHATEVAL_OFFLINE_LLM"] = "1"

from devops_chat_eval.chat_memory import DevOpsChatMemoryStore
from devops_chat_eval.coe_workflow import generate_verified_coe
from devops_chat_eval.deploy_risk import PRESET_DIFF_SCENARIOS, analyze_deploy_diff
from devops_chat_eval.judge_arena import (
    GOLDEN_EVAL_CASES,
    compute_cohens_kappa,
    evaluate_position_swapped_pair,
    run_prompt_arena,
)
from devops_chat_eval.server import lambda_handler, route_api_request


class DevOpsChatEvalTestSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_devops_space.db"
        self.store = DevOpsChatMemoryStore(db_path=self.db_path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_01_seed_14_incidents_and_3_spaces(self) -> None:
        spaces = self.store.list_spaces()
        self.assertEqual(len(spaces), 3)
        all_incidents = self.store.list_incidents()
        self.assertEqual(len(all_incidents), 14)
        counts = {s["space_id"]: s["incident_count"] for s in spaces}
        self.assertEqual(counts["space-prod-payments"], 5)
        self.assertEqual(counts["space-prod-fulfillment"], 5)
        self.assertEqual(counts["space-prod-identity"], 4)

    def test_02_strict_cross_space_tenant_isolation(self) -> None:
        iso = self.store.verify_cross_space_isolation()
        self.assertTrue(iso["isolated"])
        self.assertEqual(iso["cross_tenant_leaks"], 0)
        with self.assertRaises(KeyError):
            # INC-206 belongs to space-prod-fulfillment, must fail in space-prod-payments
            self.store.get_incident("space-prod-payments", "INC-206")

    def test_03_hybrid_bm25_and_graph_neighbor_expansion(self) -> None:
        ctx = self.store.retrieve_context(
            space_id="space-prod-payments",
            query="checkout-api database saturation on Aurora-Payments-Primary",
            session_id="s-graph",
            top_k=3,
        )
        returned_ids = [r["incident_id"] for r in ctx["retrieved_incidents"]]
        self.assertIn("INC-201", returned_ids)
        # INC-203 (fraud-scorer) shares Aurora-Payments-Primary with checkout-api
        self.assertIn("INC-203", returned_ids)
        self.assertGreaterEqual(len(ctx["graph_neighbors_activated"]), 1)

    def test_04_multi_turn_session_memory_and_citation_verification(self) -> None:
        t1 = self.store.chat_turn(
            space_id="space-prod-payments",
            message="What happened during INC-201 on checkout-api?",
            session_id="multi-turn-test",
            use_bedrock=False,
        )
        self.assertEqual(t1["turn_number"], 1)
        self.assertTrue(t1["citation_verification"]["grounded"])
        self.assertIn("INC-201", t1["citation_verification"]["verified_incidents"])

        t2 = self.store.chat_turn(
            space_id="space-prod-payments",
            message="Did any related service sharing that database hit retry storms, and what fixed it?",
            session_id="multi-turn-test",
            use_bedrock=False,
        )
        self.assertEqual(t2["turn_number"], 2)
        self.assertEqual(len(t2["history"]), 4)

        # Verify foreign/hallucinated incident IDs are flagged if injected into a reply
        bad_ver = self.store.verify_chat_citations(
            "space-prod-payments",
            "See [INC-201], foreign [INC-206], and fake [INC-999].",
            t1["retrieved_incidents"],
        )
        self.assertFalse(bad_ver["grounded"])
        self.assertIn("INC-206", bad_ver["cross_tenant_incidents"])
        self.assertIn("INC-999", bad_ver["hallucinated_incidents"])

    def test_05_deploy_diff_blocks_timeout_and_pool_overcommit(self) -> None:
        p_timeout = PRESET_DIFF_SCENARIOS[0]
        res1 = analyze_deploy_diff(
            self.store, p_timeout["space_id"], p_timeout["service"], p_timeout["diff_text"], use_bedrock=False
        )
        self.assertEqual(res1["verdict"], "BLOCK_DEPLOY")
        rules1 = {f["rule_id"] for f in res1["findings"]}
        self.assertIn("DR-TIMEOUT-01", rules1)
        self.assertIn("DR-RETRY-03", rules1)
        matched_ids1 = {m["incident_id"] for m in res1["matched_incidents"]}
        self.assertIn("INC-202", matched_ids1)

        p_pool = PRESET_DIFF_SCENARIOS[1]
        res2 = analyze_deploy_diff(
            self.store, p_pool["space_id"], p_pool["service"], p_pool["diff_text"], use_bedrock=False
        )
        self.assertEqual(res2["verdict"], "BLOCK_DEPLOY")
        self.assertIn("INC-201", {m["incident_id"] for m in res2["matched_incidents"]})

    def test_06_deploy_diff_blocks_sqs_visibility_and_iam_wildcard(self) -> None:
        p_sqs = PRESET_DIFF_SCENARIOS[2]
        res_sqs = analyze_deploy_diff(
            self.store, p_sqs["space_id"], p_sqs["service"], p_sqs["diff_text"], use_bedrock=False
        )
        self.assertEqual(res_sqs["verdict"], "BLOCK_DEPLOY")
        self.assertIn("INC-207", {m["incident_id"] for m in res_sqs["matched_incidents"]})

        p_iam = PRESET_DIFF_SCENARIOS[3]
        res_iam = analyze_deploy_diff(
            self.store, p_iam["space_id"], p_iam["service"], p_iam["diff_text"], use_bedrock=False
        )
        self.assertEqual(res_iam["verdict"], "BLOCK_DEPLOY")
        self.assertIn("INC-213", {m["incident_id"] for m in res_iam["matched_incidents"]})

    def test_07_deploy_diff_safe_rollout_zero_false_positives(self) -> None:
        p_safe = PRESET_DIFF_SCENARIOS[4]
        res = analyze_deploy_diff(
            self.store, p_safe["space_id"], p_safe["service"], p_safe["diff_text"], use_bedrock=False
        )
        self.assertEqual(res["verdict"], "SAFE_TO_DEPLOY")
        self.assertEqual(res["hazard_count"], 0)
        self.assertLess(res["risk_score"], 25)

    def test_08_coe_workflow_grounds_valid_telemetry(self) -> None:
        coe = generate_verified_coe(
            self.store,
            space_id="space-prod-payments",
            incident_id="INC-201",
            inject_hallucination_probe=False,
            use_bedrock=False,
        )
        self.assertTrue(coe["grounding_verified"])
        self.assertEqual(coe["post_gate_ungrounded_claims"], 0)
        self.assertEqual(coe["flagged_claims_count"], 0)
        doc = coe["coe_document"]
        for key in ("executive_summary", "impact_metrics", "incident_timeline", "five_whys", "action_items"):
            self.assertTrue(doc[key])

    def test_09_coe_grounding_gate_catches_and_strips_hallucinations(self) -> None:
        coe = generate_verified_coe(
            self.store,
            space_id="space-prod-payments",
            incident_id="INC-201",
            inject_hallucination_probe=True,
            use_bedrock=False,
        )
        self.assertGreaterEqual(coe["flagged_claims_count"], 3)
        reasons = {c["reason"] for c in coe["flagged_claims"]}
        self.assertIn("UNGROUNDED_METRIC", reasons)
        self.assertIn("UNGROUNDED_TIMESTAMP", reasons)
        self.assertIn("UNGROUNDED_SERVICE", reasons)
        self.assertEqual(coe["post_gate_ungrounded_claims"], 0)
        serialized = json.dumps(coe["coe_document"])
        self.assertNotIn("99.94%", serialized)
        self.assertNotIn("03:47 UTC", serialized)
        self.assertNotIn("quantum-billing-svc", serialized)

    def test_10_cohens_kappa_exact_math(self) -> None:
        perfect = compute_cohens_kappa(["A", "B", "TIE", "A"], ["A", "B", "TIE", "A"], labels=["A", "B", "TIE"])
        self.assertEqual(perfect, 1.0)
        partial = compute_cohens_kappa(
            ["A", "A", "B", "B", "TIE", "A"],
            ["A", "B", "B", "B", "TIE", "A"],
            labels=["A", "B", "TIE"],
        )
        self.assertGreater(partial, 0.6)
        self.assertLess(partial, 1.0)

    def test_11_position_swapped_judge_and_promotion_gate(self) -> None:
        arena_v3 = run_prompt_arena(
            self.store,
            candidate_prompt_id="v3_citation_constrained",
            baseline_prompt_id="v1_baseline",
            case_limit=6,
            use_bedrock=False,
        )
        self.assertEqual(arena_v3["promotion_decision"], "PROMOTE")
        self.assertGreaterEqual(arena_v3["debiased_win_rate_pct"], 80.0)

        arena_v1 = run_prompt_arena(
            self.store,
            candidate_prompt_id="v1_baseline",
            baseline_prompt_id="v3_citation_constrained",
            case_limit=4,
            use_bedrock=False,
        )
        self.assertEqual(arena_v1["promotion_decision"], "HOLD")

        # Verify synthetic position-bias detection when two nearly equal answers swap slots
        case = GOLDEN_EVAL_CASES[0]
        inc = self.store.get_incident(case["space_id"], case["incident_id"])
        pair = evaluate_position_swapped_pair("Short generic note.", "Short generic note.", case, inc, use_bedrock=False)
        self.assertTrue(pair["position_consistent"])
        self.assertEqual(pair["debiased_winner"], "TIE")

    def test_12_lambda_handler_and_api_routes(self) -> None:
        st_health, payload_health = route_api_request("GET", "/api/health", {}, {})
        self.assertEqual(st_health, 200)
        self.assertEqual(payload_health["spaces_count"], 3)
        self.assertEqual(payload_health["incidents_count"], 14)

        lam_resp = lambda_handler(
            {
                "rawPath": "/api/deploy-risk",
                "requestContext": {"http": {"method": "POST"}},
                "body": json.dumps(
                    {
                        "space_id": "space-prod-payments",
                        "service": "checkout-api",
                        "diff_text": PRESET_DIFF_SCENARIOS[0]["diff_text"],
                    }
                ),
            }
        )
        self.assertEqual(lam_resp["statusCode"], 200)
        parsed = json.loads(lam_resp["body"])
        self.assertEqual(parsed["verdict"], "BLOCK_DEPLOY")


if __name__ == "__main__":
    unittest.main()
