"""Dual Local HTTP Server & AWS Lambda Function URL `/api/*` Handler for DevOpsChatEval."""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import platform
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, urlparse

from .bedrock_client import NOVA_LITE_MODEL, NOVA_MICRO_MODEL
from .chat_memory import DevOpsChatMemoryStore
from .coe_workflow import generate_verified_coe
from .deploy_risk import PRESET_DIFF_SCENARIOS, analyze_deploy_diff
from .judge_arena import GOLDEN_EVAL_CASES, PROMPT_TEMPLATES, run_prompt_arena

ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"
DOCS_DIR = ROOT_DIR / "docs"
BENCHMARK_JSON_PATH = ROOT_DIR / "benchmarks" / "latest_results.json"

STORE = DevOpsChatMemoryStore()

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}


def _load_benchmarks_json() -> Dict[str, Any]:
    if BENCHMARK_JSON_PATH.is_file():
        try:
            return json.loads(BENCHMARK_JSON_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def route_api_request(
    method: str,
    path: str,
    query: Dict[str, str],
    body: Dict[str, Any],
    request_id: str = "local",
) -> Tuple[int, Dict[str, Any]]:
    """Pure HTTP router shared between local dev server and AWS Lambda behind CloudFront."""
    try:
        if method == "GET" and path == "/api/health":
            spaces = STORE.list_spaces()
            iso = STORE.verify_cross_space_isolation()
            return 200, {
                "status": "ok",
                "service": "devops-chat-eval",
                "delivery": os.environ.get("DEVOPSCHATEVAL_DELIVERY", "cloudfront-s3-oac + lambda-api-origin"),
                "aws_region": os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2")),
                "bedrock_models": [NOVA_LITE_MODEL, NOVA_MICRO_MODEL],
                "python_version": platform.python_version(),
                "spaces_count": len(spaces),
                "incidents_count": sum(s["incident_count"] for s in spaces),
                "tenant_isolation_verified": iso["isolated"],
                "request_id": request_id,
            }

        if method == "GET" and path == "/api/benchmarks":
            data = _load_benchmarks_json()
            return 200, {"status": "ok", "benchmarks": data, "request_id": request_id}

        if method == "GET" and path == "/api/state":
            space_filter = query.get("space_id")
            spaces = STORE.list_spaces()
            incidents = STORE.list_incidents(space_id=space_filter if space_filter else None)
            return 200, {
                "status": "ok",
                "spaces": spaces,
                "incidents": [
                    {
                        "incident_id": i["incident_id"],
                        "space_id": i["space_id"],
                        "title": i["title"],
                        "severity": i["severity"],
                        "service": i["service"],
                        "failure_mode": i["failure_mode"],
                        "duration_minutes": i["duration_minutes"],
                        "started_at_utc": i["started_at_utc"],
                    }
                    for i in incidents
                ],
                "diff_presets": PRESET_DIFF_SCENARIOS,
                "prompt_templates": list(PROMPT_TEMPLATES.values()),
                "eval_cases_count": len(GOLDEN_EVAL_CASES),
                "benchmarks": _load_benchmarks_json(),
                "request_id": request_id,
            }

        if method == "POST" and path == "/api/chat":
            space_id = str(body.get("space_id") or "space-prod-payments").strip()
            session_id = str(body.get("session_id") or "live-web-session").strip()
            message = str(body.get("message") or "").strip()
            model_id = str(body.get("model_id") or NOVA_LITE_MODEL).strip()
            if not message:
                return 400, {"error": "message is required"}
            out = STORE.chat_turn(
                space_id=space_id,
                message=message,
                session_id=session_id,
                model_id=model_id,
                use_bedrock=True,
            )
            out["request_id"] = request_id
            return 200, out

        if method == "POST" and path == "/api/deploy-risk":
            space_id = str(body.get("space_id") or "space-prod-payments").strip()
            service = str(body.get("service") or "checkout-api").strip()
            diff_text = str(body.get("diff_text") or "").strip()
            model_id = str(body.get("model_id") or NOVA_LITE_MODEL).strip()
            if not diff_text:
                return 400, {"error": "diff_text is required"}
            out = analyze_deploy_diff(
                store=STORE,
                space_id=space_id,
                service=service,
                diff_text=diff_text,
                model_id=model_id,
                use_bedrock=True,
            )
            out["request_id"] = request_id
            return 200, out

        if method == "POST" and path == "/api/coe/generate":
            space_id = str(body.get("space_id") or "space-prod-payments").strip()
            incident_id = str(body.get("incident_id") or "INC-201").strip().upper()
            inject_probe = bool(body.get("inject_hallucination_probe", False))
            model_id = str(body.get("model_id") or NOVA_LITE_MODEL).strip()
            out = generate_verified_coe(
                store=STORE,
                space_id=space_id,
                incident_id=incident_id,
                inject_hallucination_probe=inject_probe,
                model_id=model_id,
                use_bedrock=True,
            )
            out["request_id"] = request_id
            return 200, out

        if method == "POST" and path == "/api/eval/judge":
            cand_id = str(body.get("candidate_prompt_id") or "v3_citation_constrained").strip()
            base_id = str(body.get("baseline_prompt_id") or "v1_baseline").strip()
            custom_prompt = body.get("custom_candidate_prompt")
            case_limit = int(body.get("case_limit") or 4)
            case_limit = max(1, min(16, case_limit))
            gen_model = str(body.get("gen_model_id") or NOVA_MICRO_MODEL).strip()
            judge_model = str(body.get("judge_model_id") or NOVA_MICRO_MODEL).strip()
            out = run_prompt_arena(
                store=STORE,
                candidate_prompt_id=cand_id,
                baseline_prompt_id=base_id,
                custom_candidate_prompt=str(custom_prompt) if custom_prompt else None,
                case_limit=case_limit,
                gen_model_id=gen_model,
                judge_model_id=judge_model,
                use_bedrock=True,
            )
            out["request_id"] = request_id
            return 200, out

        if method == "POST" and path == "/api/reset":
            STORE.reset_sessions()
            return 200, {
                "status": "reset_complete",
                "service": "devops-chat-eval",
                "request_id": request_id,
            }

        return 404, {"error": f"Unknown API route: {method} {path}"}
    except KeyError as exc:
        return 404, {"error": str(exc)}
    except ValueError as exc:
        return 400, {"error": str(exc)}
    except Exception as exc:
        return 500, {"error": str(exc)}


def lambda_handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """AWS Lambda Function URL handler mounted behind Amazon CloudFront `/api/*`."""
    req_ctx = event.get("requestContext") or {}
    method = ((req_ctx.get("http") or {}).get("method") or event.get("httpMethod") or "GET").upper()
    raw_path = event.get("rawPath") or event.get("path") or "/"
    query_params = {k: str(v) for k, v in (event.get("queryStringParameters") or {}).items() if v is not None}
    req_id = getattr(context, "aws_request_id", "cloudfront-api")

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

    if raw_path.startswith("/api/"):
        body: Dict[str, Any] = {}
        raw_body = event.get("body")
        if raw_body:
            if event.get("isBase64Encoded") and isinstance(raw_body, str):
                raw_body = base64.b64decode(raw_body).decode("utf-8")
            body = json.loads(raw_body) if isinstance(raw_body, str) else dict(raw_body)
        status, payload = route_api_request(method, raw_path, query_params, body, request_id=req_id)
        return {
            "statusCode": status,
            "headers": {
                **CORS_HEADERS,
                "Content-Type": "application/json; charset=utf-8",
                "Cache-Control": "no-store",
            },
            "body": json.dumps(payload, default=str),
        }

    target = WEB_DIR / ("index.html" if raw_path in ("/", "") else raw_path.lstrip("/"))
    if not target.is_file() and raw_path.endswith(".svg"):
        target = DOCS_DIR / Path(raw_path).name
    if target.is_file():
        ctype = mimetypes.guess_type(str(target))[0] or "text/html; charset=utf-8"
        return {
            "statusCode": 200,
            "headers": {**CORS_HEADERS, "Content-Type": ctype},
            "body": target.read_text(encoding="utf-8"),
        }
    return {
        "statusCode": 404,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps({"error": "Not found", "path": raw_path}),
    }


class LocalDevOpsChatEvalHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}

        if path.startswith("/api/"):
            status, payload = route_api_request("GET", path, query, {}, request_id="local-dev")
            raw = json.dumps(payload, indent=2, default=str).encode("utf-8")
            self.send_response(status)
            for k, v in CORS_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        candidate = (WEB_DIR / rel).resolve()
        if not candidate.is_file() and path.endswith(".svg"):
            candidate = (DOCS_DIR / Path(path).name).resolve()
        if not candidate.is_file():
            candidate = WEB_DIR / "index.html"

        data = candidate.read_bytes()
        ctype = mimetypes.guess_type(str(candidate))[0] or "text/html"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or "0")
        raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        body = json.loads(raw_body) if raw_body.strip() else {}
        status, payload = route_api_request("POST", parsed.path, {}, body, request_id="local-dev")
        raw = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def run_local_server(port: int = 3099) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), LocalDevOpsChatEvalHandler)
    print(f"DevOpsChatEval running at http://localhost:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    port_num = int(os.environ.get("PORT", "3099"))
    run_local_server(port=port_num)
