"""Amazon Bedrock Converse API client supporting Nova Lite and Nova Micro with latency & cost tracking."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

NOVA_LITE_MODEL = "us.amazon.nova-lite-v1:0"
NOVA_MICRO_MODEL = "us.amazon.nova-micro-v1:0"

# On-demand US Cross-Region Inference pricing per 1M tokens (USD)
MODEL_PRICING_PER_1M: Dict[str, Dict[str, float]] = {
    NOVA_LITE_MODEL: {"input": 0.06, "output": 0.24},
    NOVA_MICRO_MODEL: {"input": 0.035, "output": 0.14},
}

_CLIENT_LOCK = threading.Lock()
_CACHED_CLIENTS: Dict[str, Any] = {}


def estimate_cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    rates = MODEL_PRICING_PER_1M.get(model_id, MODEL_PRICING_PER_1M[NOVA_LITE_MODEL])
    cost = (input_tokens / 1_000_000.0) * rates["input"] + (output_tokens / 1_000_000.0) * rates["output"]
    return round(cost, 8)


def _get_boto3_bedrock_client(region: str) -> Optional[Any]:
    with _CLIENT_LOCK:
        if region in _CACHED_CLIENTS:
            return _CACHED_CLIENTS[region]

        try:
            import boto3  # type: ignore
            from botocore.config import Config  # type: ignore
        except ImportError:
            return None

        cfg = Config(
            region_name=region,
            connect_timeout=5,
            read_timeout=25,
            retries={"max_attempts": 2, "mode": "standard"},
        )

        # In AWS Lambda or environments with direct IAM role/env credentials
        if os.environ.get("AWS_EXECUTION_ENV") or os.environ.get("AWS_ACCESS_KEY_ID"):
            client = boto3.client("bedrock-runtime", region_name=region, config=cfg)
            _CACHED_CLIENTS[region] = client
            return client

        # Try standard session first, then fall back to `aws configure export-credentials`
        profile = os.environ.get("DEVOPSCHATEVAL_BEDROCK_PROFILE", os.environ.get("AWS_PROFILE", "portfolio"))
        try:
            sess = boto3.Session(profile_name=profile, region_name=region)
            creds = sess.get_credentials()
            if creds is not None:
                frozen = creds.get_frozen_credentials()
                if frozen and frozen.access_key:
                    client = sess.client("bedrock-runtime", config=cfg)
                    _CACHED_CLIENTS[region] = client
                    return client
        except Exception:
            pass

        aws_bin = shutil.which("aws")
        if aws_bin:
            try:
                proc = subprocess.run(
                    [aws_bin, "configure", "export-credentials", "--profile", profile, "--format", "process"],
                    capture_output=True,
                    text=True,
                    timeout=6.0,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    exported = json.loads(proc.stdout)
                    client = boto3.client(
                        "bedrock-runtime",
                        region_name=region,
                        aws_access_key_id=exported["AccessKeyId"],
                        aws_secret_access_key=exported["SecretAccessKey"],
                        aws_session_token=exported.get("SessionToken"),
                        config=cfg,
                    )
                    _CACHED_CLIENTS[region] = client
                    return client
            except Exception:
                pass

        return None


def converse_bedrock(
    system_prompt: str,
    messages: List[Dict[str, Any]],
    model_id: Optional[str] = None,
    max_tokens: int = 450,
    temperature: float = 0.1,
) -> Optional[Dict[str, Any]]:
    """Invoke Amazon Bedrock Converse API and return response text, server/client latency, tokens, and USD cost."""
    if os.environ.get("DEVOPSCHATEVAL_OFFLINE_LLM") == "1":
        return None

    resolved_model = model_id or os.environ.get("DEVOPSCHATEVAL_BEDROCK_MODEL", NOVA_LITE_MODEL)
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
    t0 = time.perf_counter()

    client = _get_boto3_bedrock_client(region)
    if client is not None:
        try:
            resp = client.converse(
                modelId=resolved_model,
                system=[{"text": system_prompt}],
                messages=messages,
                inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
            )
            round_trip_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            server_ms = float(resp.get("metrics", {}).get("latencyMs") or round_trip_ms)
            usage = resp.get("usage", {})
            in_tok = int(usage.get("inputTokens", 0))
            out_tok = int(usage.get("outputTokens", 0))
            tot_tok = int(usage.get("totalTokens", in_tok + out_tok))
            blocks = resp.get("output", {}).get("message", {}).get("content", [])
            text = "\n".join(b.get("text", "") for b in blocks if "text" in b).strip()
            return {
                "text": text,
                "model_id": resolved_model,
                "engine": f"aws-bedrock ({resolved_model})",
                "latency_ms": round(server_ms, 2),
                "round_trip_ms": round_trip_ms,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "total_tokens": tot_tok,
                "cost_usd": estimate_cost_usd(resolved_model, in_tok, out_tok),
            }
        except Exception:
            # Reset cached client once in case temporary session credentials rotated
            with _CLIENT_LOCK:
                _CACHED_CLIENTS.pop(region, None)

    # CLI fallback if boto3 is unavailable
    aws_bin = shutil.which("aws")
    if not aws_bin:
        return None

    env = dict(os.environ)
    env.pop("AWS_ACCESS_KEY_ID", None)
    env.pop("AWS_SECRET_ACCESS_KEY", None)
    env.pop("AWS_SESSION_TOKEN", None)
    env["AWS_PROFILE"] = os.environ.get("DEVOPSCHATEVAL_BEDROCK_PROFILE", os.environ.get("AWS_PROFILE", "portfolio"))
    env["AWS_DEFAULT_REGION"] = region

    cmd = [
        aws_bin,
        "bedrock-runtime",
        "converse",
        "--region",
        region,
        "--model-id",
        resolved_model,
        "--system",
        json.dumps([{"text": system_prompt}]),
        "--messages",
        json.dumps(messages),
        "--inference-config",
        json.dumps({"maxTokens": max_tokens, "temperature": temperature}),
        "--output",
        "json",
    ]
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=18.0)
        round_trip_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            server_ms = float(data.get("metrics", {}).get("latencyMs") or round_trip_ms)
            usage = data.get("usage", {})
            in_tok = int(usage.get("inputTokens", 0))
            out_tok = int(usage.get("outputTokens", 0))
            tot_tok = int(usage.get("totalTokens", in_tok + out_tok))
            blocks = data.get("output", {}).get("message", {}).get("content", [])
            text = "\n".join(b.get("text", "") for b in blocks if "text" in b).strip()
            return {
                "text": text,
                "model_id": resolved_model,
                "engine": f"aws-bedrock ({resolved_model})",
                "latency_ms": round(server_ms, 2),
                "round_trip_ms": round_trip_ms,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "total_tokens": tot_tok,
                "cost_usd": estimate_cost_usd(resolved_model, in_tok, out_tok),
            }
    except Exception:
        pass

    return None


def invoke_single_turn(
    system_prompt: str,
    user_prompt: str,
    model_id: Optional[str] = None,
    max_tokens: int = 450,
    temperature: float = 0.1,
) -> Optional[Dict[str, Any]]:
    """Convenience wrapper for single-turn Bedrock Converse calls."""
    return converse_bedrock(
        system_prompt=system_prompt,
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        model_id=model_id,
        max_tokens=max_tokens,
        temperature=temperature,
    )
