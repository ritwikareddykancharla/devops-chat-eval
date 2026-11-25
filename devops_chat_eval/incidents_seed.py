"""Canonical seed dataset: 14 AWS microservice incidents across 3 isolated customer Spaces + topology graph."""
from __future__ import annotations

from typing import Any, Dict, List

CUSTOMER_SPACES: List[Dict[str, Any]] = [
    {
        "space_id": "space-prod-payments",
        "account_id": "111122223333",
        "title": "Payments & Checkout Core",
        "region": "us-west-2",
        "rds_max_connections": 450,
        "services": [
            "checkout-api",
            "payment-gateway",
            "fraud-scorer",
            "ledger-writer",
            "Aurora-Payments-Primary",
            "DynamoDB-LedgerTable",
            "KMS-PaymentsKey",
        ],
        "downstream_p99_ms": {
            "payment-gateway": 1850,
            "fraud-scorer": 620,
            "ledger-writer": 145,
            "Aurora-Payments-Primary": 48,
        },
    },
    {
        "space_id": "space-prod-fulfillment",
        "account_id": "444455556666",
        "title": "Order Fulfillment & Logistics",
        "region": "us-west-2",
        "rds_max_connections": 320,
        "services": [
            "order-router",
            "inventory-svc",
            "carrier-webhook",
            "warehouse-sync",
            "shipping-calc",
            "Valkey-InventoryCluster",
            "SQS-OrderDispatchQueue",
        ],
        "downstream_p99_ms": {
            "inventory-svc": 310,
            "carrier-webhook": 2400,
            "shipping-calc": 490,
            "Valkey-InventoryCluster": 14,
        },
    },
    {
        "space_id": "space-prod-identity",
        "account_id": "777788889999",
        "title": "Identity, Auth & Audit Edge",
        "region": "us-east-1",
        "rds_max_connections": 600,
        "services": [
            "auth-token-svc",
            "session-edge",
            "tenant-rbac",
            "audit-ingest",
            "OIDC-JWKS-Origin",
            "SSM-ConfigStore",
            "Kinesis-AuditStream",
        ],
        "downstream_p99_ms": {
            "auth-token-svc": 280,
            "OIDC-JWKS-Origin": 940,
            "tenant-rbac": 95,
            "Kinesis-AuditStream": 65,
        },
    },
]

# Explicit directed service topology edges within each customer Space
SERVICE_EDGES: List[Dict[str, str]] = [
    # Payments Space
    {"space_id": "space-prod-payments", "source": "checkout-api", "target": "Aurora-Payments-Primary", "relation": "queries_sql_pool"},
    {"space_id": "space-prod-payments", "source": "checkout-api", "target": "payment-gateway", "relation": "calls_sync_http"},
    {"space_id": "space-prod-payments", "source": "checkout-api", "target": "fraud-scorer", "relation": "calls_sync_grpc"},
    {"space_id": "space-prod-payments", "source": "payment-gateway", "target": "ledger-writer", "relation": "emits_settlement"},
    {"space_id": "space-prod-payments", "source": "payment-gateway", "target": "KMS-PaymentsKey", "relation": "decrypts_envelope"},
    {"space_id": "space-prod-payments", "source": "fraud-scorer", "target": "Aurora-Payments-Primary", "relation": "reads_velocity_features"},
    {"space_id": "space-prod-payments", "source": "ledger-writer", "target": "DynamoDB-LedgerTable", "relation": "writes_journal"},
    # Fulfillment Space
    {"space_id": "space-prod-fulfillment", "source": "order-router", "target": "SQS-OrderDispatchQueue", "relation": "consumes_messages"},
    {"space_id": "space-prod-fulfillment", "source": "order-router", "target": "inventory-svc", "relation": "reserves_stock"},
    {"space_id": "space-prod-fulfillment", "source": "order-router", "target": "shipping-calc", "relation": "quotes_rates_grpc"},
    {"space_id": "space-prod-fulfillment", "source": "inventory-svc", "target": "Valkey-InventoryCluster", "relation": "holds_ttl_locks"},
    {"space_id": "space-prod-fulfillment", "source": "warehouse-sync", "target": "Valkey-InventoryCluster", "relation": "writes_bulk_snapshots"},
    {"space_id": "space-prod-fulfillment", "source": "order-router", "target": "carrier-webhook", "relation": "posts_labels_egress"},
    # Identity Space
    {"space_id": "space-prod-identity", "source": "session-edge", "target": "auth-token-svc", "relation": "verifies_jwt"},
    {"space_id": "space-prod-identity", "source": "session-edge", "target": "SSM-ConfigStore", "relation": "fetches_edge_flags"},
    {"space_id": "space-prod-identity", "source": "auth-token-svc", "target": "OIDC-JWKS-Origin", "relation": "refreshes_jwks_keys"},
    {"space_id": "space-prod-identity", "source": "auth-token-svc", "target": "tenant-rbac", "relation": "evaluates_policy"},
    {"space_id": "space-prod-identity", "source": "tenant-rbac", "source_role": "policy_authorizer", "target": "audit-ingest", "relation": "streams_decisions"},
    {"space_id": "space-prod-identity", "source": "audit-ingest", "target": "Kinesis-AuditStream", "relation": "writes_audit_shards"},
]

INCIDENTS_SEED: List[Dict[str, Any]] = [
    # ===================== SPACE 1: space-prod-payments (5 incidents) =====================
    {
        "incident_id": "INC-201",
        "space_id": "space-prod-payments",
        "title": "Checkout API RDS Aurora max_connections Exhaustion After ECS Scale-Out",
        "severity": "SEV-1",
        "service": "checkout-api",
        "upstream_services": ["CloudFront-CheckoutEdge"],
        "downstream_services": ["Aurora-Payments-Primary", "payment-gateway"],
        "failure_mode": "db_connection_pool_exhaustion",
        "started_at_utc": "2026-10-14T14:12:00Z",
        "resolved_at_utc": "2026-10-14T14:49:00Z",
        "duration_minutes": 37,
        "deploy_sha": "deploy-8841a2c",
        "alarms": ["CW-Aurora-DatabaseConnections-High", "CW-CheckoutApi-5xx-Critical"],
        "metrics": {
            "ecs_task_count": 12,
            "pool_size_per_task": 45,
            "attempted_db_connections": 540,
            "rds_max_connections": 450,
            "checkout_5xx_pct": 18.4,
            "p99_latency_ms": 2420,
            "failed_checkouts": 3180,
        },
        "root_cause": (
            "Deploy deploy-8841a2c raised SQLAlchemy max_pool_size from 25 to 45 per ECS task without "
            "routing through RDS Proxy. When checkout-api scaled to 12 tasks during flash traffic, total "
            "connections hit 540 against Aurora db.r6g.large max_connections=450, rejecting new sessions with FATAL too many clients."
        ),
        "mitigation": (
            "Rolled back max_pool_size to 20 per task (240 peak connections across 12 tasks) and routed "
            "checkout-api + fraud-scorer SQL pools through RDS Proxy with connection multiplexing."
        ),
        "timeline": [
            {"ts": "14:12 UTC", "event": "Deploy deploy-8841a2c promoted max_pool_size=45 across 8 checkout-api ECS tasks."},
            {"ts": "14:18 UTC", "event": "Target tracking scaled checkout-api from 8 to 12 tasks; attempted connections reached 540 vs limit 450."},
            {"ts": "14:20 UTC", "event": "CW-Aurora-DatabaseConnections-High and CW-CheckoutApi-5xx-Critical fired at 18.4% 5xx rate."},
            {"ts": "14:36 UTC", "event": "On-call reverted task definition pool_size_per_task to 20 and drained excess idle backends."},
            {"ts": "14:49 UTC", "event": "Checkout p99 latency returned from 2420 ms to 190 ms and 5xx rate dropped to 0.02%."},
        ],
    },
    {
        "incident_id": "INC-202",
        "space_id": "space-prod-payments",
        "title": "Checkout-to-Payment-Gateway Timeout Reduction Below Downstream p99 Triggered Retry Cascade",
        "severity": "SEV-1",
        "service": "payment-gateway",
        "upstream_services": ["checkout-api"],
        "downstream_services": ["ledger-writer", "KMS-PaymentsKey"],
        "failure_mode": "timeout_below_downstream_p99",
        "started_at_utc": "2026-10-22T18:05:00Z",
        "resolved_at_utc": "2026-10-22T18:39:00Z",
        "duration_minutes": 34,
        "deploy_sha": "deploy-9104f7b",
        "alarms": ["CW-CheckoutApi-UpstreamTimeout-Spike", "CW-PaymentGateway-DuplicateAuth-Warn"],
        "metrics": {
            "configured_client_timeout_ms": 800,
            "downstream_p99_ms": 1850,
            "retry_amplification_factor": 3.4,
            "checkout_5xx_pct": 14.2,
            "p99_latency_ms": 3190,
            "inflight_threads_pct": 97.5,
        },
        "root_cause": (
            "Config change deploy-9104f7b lowered checkout-api HTTP client read_timeout_ms from 2500 ms to 800 ms "
            "even though payment-gateway card-network settlement p99 latency is 1850 ms. Every p95+ authorization "
            "timed out at 800 ms and triggered 3 immediate retries, multiplying load by 3.4x."
        ),
        "mitigation": (
            "Restored checkout-api client timeout to 2600 ms (above 1850 ms downstream p99 plus 40% headroom) "
            "and enforced idempotency keys with a token-bucket retry budget capped at 10%."
        ),
        "timeline": [
            {"ts": "18:05 UTC", "event": "Deploy deploy-9104f7b updated checkout-api HTTP client timeout to 800 ms."},
            {"ts": "18:09 UTC", "event": "CW-CheckoutApi-UpstreamTimeout-Spike triggered as card authorizations exceeding 800 ms aborted."},
            {"ts": "18:14 UTC", "event": "Retry amplification hit 3.4x, pushing payment-gateway worker pool utilization to 97.5%."},
            {"ts": "18:28 UTC", "event": "Config rollback restored client timeout to 2600 ms and enabled 10% retry token bucket."},
            {"ts": "18:39 UTC", "event": "Checkout 5xx dropped from 14.2% to 0.01% and p99 latency stabilized at 1780 ms."},
        ],
    },
    {
        "incident_id": "INC-203",
        "space_id": "space-prod-payments",
        "title": "Fraud-Scorer Unbounded Retry Loop Without Jitter Saturated Shared Aurora Reader & Primary",
        "severity": "SEV-2",
        "service": "fraud-scorer",
        "upstream_services": ["checkout-api"],
        "downstream_services": ["Aurora-Payments-Primary"],
        "failure_mode": "unbounded_retry_missing_circuit_breaker",
        "started_at_utc": "2026-10-29T09:15:00Z",
        "resolved_at_utc": "2026-10-29T09:58:00Z",
        "duration_minutes": 43,
        "deploy_sha": "deploy-9420d1e",
        "alarms": ["CW-FraudScorer-CPU-95", "CW-Aurora-ReadIOPS-BurstDepleted"],
        "metrics": {
            "max_retries_configured": 20,
            "aurora_cpu_pct": 98.6,
            "rds_active_connections": 438,
            "fraud_rpc_error_pct": 22.8,
            "p99_latency_ms": 1960,
        },
        "root_cause": (
            "A feature-fetch wrapper in deploy-9420d1e set max_retries=20 with fixed 50 ms sleep and disabled "
            "the circuit breaker around velocity SQL lookups on Aurora-Payments-Primary. A transient 2-second "
            "failover synchronized all fraud-scorer pods into a thundering herd."
        ),
        "mitigation": (
            "Capped retries at 2 with exponential full-jitter backoff (100 ms base) and re-enabled a half-open "
            "circuit breaker tripping at 25% error rate over 15 seconds."
        ),
        "timeline": [
            {"ts": "09:15 UTC", "event": "Deploy deploy-9420d1e shipped fraud-scorer client with max_retries=20 and circuit_breaker=false."},
            {"ts": "09:21 UTC", "event": "Brief storage latency spike triggered synchronized 20x retries; Aurora CPU hit 98.6%."},
            {"ts": "09:24 UTC", "event": "CW-FraudScorer-CPU-95 fired; fraud RPC error rate reached 22.8% and spilled into checkout-api."},
            {"ts": "09:44 UTC", "event": "Emergency patch capped retries=2 with full jitter and tripped fail-open fallback scoring."},
            {"ts": "09:58 UTC", "event": "Aurora active connections cooled from 438 to 142 and RPC errors cleared."},
        ],
    },
    {
        "incident_id": "INC-204",
        "space_id": "space-prod-payments",
        "title": "Ledger-Writer DynamoDB Hot Partition Throttling on Unsharded Merchant Settlement Key",
        "severity": "SEV-2",
        "service": "ledger-writer",
        "upstream_services": ["payment-gateway"],
        "downstream_services": ["DynamoDB-LedgerTable"],
        "failure_mode": "hot_partition_throttling",
        "started_at_utc": "2026-11-03T16:40:00Z",
        "resolved_at_utc": "2026-11-03T17:19:00Z",
        "duration_minutes": 39,
        "deploy_sha": "deploy-9612b09",
        "alarms": ["CW-DynamoDB-WriteThrottleEvents-LedgerTable", "CW-LedgerWriter-DLQ-Depth"],
        "metrics": {
            "partition_wcu_demand": 2850,
            "single_partition_wcu_limit": 1000,
            "throttled_writes_pct": 31.5,
            "dlq_messages": 1420,
            "p99_latency_ms": 1140,
        },
        "root_cause": (
            "Batch settlement job wrote all top-merchant entries under static partition key MERCHANT#GLOBAL_PROMO, "
            "driving 2850 WCU/s onto a single DynamoDB physical partition capped at 1000 WCU/s."
        ),
        "mitigation": (
            "Added 8-way deterministic write-sharding suffix (MERCHANT#ID#0..7) and replayed 1420 DLQ settlement records cleanly."
        ),
        "timeline": [
            {"ts": "16:40 UTC", "event": "Promotional settlement wave started writing to partition key MERCHANT#GLOBAL_PROMO."},
            {"ts": "16:43 UTC", "event": "CW-DynamoDB-WriteThrottleEvents-LedgerTable fired at 31.5% throttled PutItem calls."},
            {"ts": "17:02 UTC", "event": "Deployed 8-shard partition suffix writer and scatter-gather reader."},
            {"ts": "17:19 UTC", "event": "Write throttling fell to 0% and all 1420 DLQ items reprocessed."},
        ],
    },
    {
        "incident_id": "INC-205",
        "space_id": "space-prod-payments",
        "title": "Payment-Gateway KMS Decrypt AccessDeniedException After IAM Role Policy Refactor",
        "severity": "SEV-1",
        "service": "payment-gateway",
        "upstream_services": ["checkout-api"],
        "downstream_services": ["KMS-PaymentsKey"],
        "failure_mode": "iam_kms_policy_regression",
        "started_at_utc": "2026-11-09T11:02:00Z",
        "resolved_at_utc": "2026-11-09T11:24:00Z",
        "duration_minutes": 22,
        "deploy_sha": "tf-plan-7731c",
        "alarms": ["CW-KMS-AccessDenied-PaymentsKey", "CW-PaymentGateway-5xx-Critical"],
        "metrics": {
            "kms_access_denied_count": 4890,
            "payment_5xx_pct": 41.0,
            "p99_latency_ms": 340,
            "affected_minutes": 22,
        },
        "root_cause": (
            "Terraform refactor tf-plan-7731c replaced the payment-gateway task IAM policy and dropped "
            "kms:Decrypt on KMS-PaymentsKey while also leaving kms:EncryptionContext condition mismatched."
        ),
        "mitigation": (
            "Reverted IAM policy statement restoring least-privilege kms:Decrypt and kms:GenerateDataKey "
            "scoped to KMS-PaymentsKey ARN and added pre-deploy IAM Access Analyzer policy simulation."
        ),
        "timeline": [
            {"ts": "11:02 UTC", "event": "Terraform apply tf-plan-7731c updated PaymentGatewayTaskRole inline IAM policy."},
            {"ts": "11:04 UTC", "event": "CW-KMS-AccessDenied-PaymentsKey alerted as 41.0% of token decrypts returned AccessDeniedException."},
            {"ts": "11:16 UTC", "event": "Rolled back IAM policy version v4 -> v3 via AWS IAM SetDefaultPolicyVersion."},
            {"ts": "11:24 UTC", "event": "IAM propagation completed; KMS errors dropped to 0 and payment 5xx recovered."},
        ],
    },
    # ===================== SPACE 2: space-prod-fulfillment (5 incidents) =====================
    {
        "incident_id": "INC-206",
        "space_id": "space-prod-fulfillment",
        "title": "Inventory-Svc Valkey Eviction Storm Dropped Active Stock Reservation Locks",
        "severity": "SEV-1",
        "service": "inventory-svc",
        "upstream_services": ["order-router", "warehouse-sync"],
        "downstream_services": ["Valkey-InventoryCluster"],
        "failure_mode": "cache_eviction_lock_loss",
        "started_at_utc": "2026-10-17T20:10:00Z",
        "resolved_at_utc": "2026-10-17T20:54:00Z",
        "duration_minutes": 44,
        "deploy_sha": "deploy-6129e4a",
        "alarms": ["CW-ElastiCache-Evictions-High", "CW-InventorySvc-LockConflict-Error"],
        "metrics": {
            "cache_memory_pct": 99.4,
            "evicted_keys_count": 68400,
            "oversold_sku_count": 47,
            "p99_latency_ms": 820,
        },
        "root_cause": (
            "warehouse-sync bulk snapshot writer wrote 4.2 GB of un-TTL'd catalog blobs into the shared "
            "Valkey-InventoryCluster configured with allkeys-lru, evicting 68400 active reservation lock keys."
        ),
        "mitigation": (
            "Isolated reservation locks onto a dedicated volatile-ttl Valkey replication group and enforced "
            "900-second TTL bounds on warehouse-sync catalog cache entries."
        ),
        "timeline": [
            {"ts": "20:10 UTC", "event": "warehouse-sync deploy-6129e4a started full-catalog hydration into Valkey-InventoryCluster."},
            {"ts": "20:19 UTC", "event": "Cluster memory hit 99.4%; allkeys-lru evicted 68400 keys including active SKU locks."},
            {"ts": "20:38 UTC", "event": "Paused bulk hydration, flushed un-TTL'd catalog prefix, and pointed locks to volatile-ttl cluster."},
            {"ts": "20:54 UTC", "event": "Evictions returned to 0 and inventory reconciliation repaired 47 oversold SKU counts."},
        ],
    },
    {
        "incident_id": "INC-207",
        "space_id": "space-prod-fulfillment",
        "title": "Order-Router SQS Visibility Timeout Shorter Than Worker Runtime Caused Duplicate Shipments",
        "severity": "SEV-1",
        "service": "order-router",
        "upstream_services": ["SQS-OrderDispatchQueue"],
        "downstream_services": ["inventory-svc", "carrier-webhook"],
        "failure_mode": "queue_visibility_lt_timeout",
        "started_at_utc": "2026-10-25T13:30:00Z",
        "resolved_at_utc": "2026-10-25T14:06:00Z",
        "duration_minutes": 36,
        "deploy_sha": "cfn-stack-5190a",
        "alarms": ["CW-OrderRouter-DuplicateDispatch-Critical", "CW-SQS-ApproximateAgeOfOldestMessage"],
        "metrics": {
            "sqs_visibility_timeout_sec": 30,
            "worker_timeout_sec": 90,
            "p99_processing_sec": 44.5,
            "duplicate_dispatch_count": 312,
            "p99_latency_ms": 44500,
        },
        "root_cause": (
            "CloudFormation update cfn-stack-5190a raised order-router Lambda timeout to 90s for batch label "
            "generation while leaving SQS-OrderDispatchQueue VisibilityTimeout at 30s. Batches taking 44.5s "
            "re-appeared on the queue before deletion, dispatching 312 duplicate shipping labels."
        ),
        "mitigation": (
            "Set SQS VisibilityTimeout to 540s (6x the 90s Lambda timeout per AWS best practice) and added "
            "conditional DynamoDB idempotency guard on order_id."
        ),
        "timeline": [
            {"ts": "13:30 UTC", "event": "CloudFormation cfn-stack-5190a updated order-router Lambda timeout=90s with SQS visibility=30s."},
            {"ts": "13:37 UTC", "event": "Carrier label batches hit 44.5s p99; messages became visible again at 30s and re-invoked."},
            {"ts": "13:52 UTC", "event": "Updated SQS VisibilityTimeout to 540s and enabled conditional PutItem idempotency lock."},
            {"ts": "14:06 UTC", "event": "Duplicate dispatches halted at 312 and voided duplicate carrier tracking numbers."},
        ],
    },
    {
        "incident_id": "INC-208",
        "space_id": "space-prod-fulfillment",
        "title": "Carrier-Webhook NAT Gateway Port Allocation Errors From Unpooled Per-Request TLS Sockets",
        "severity": "SEV-2",
        "service": "carrier-webhook",
        "upstream_services": ["order-router"],
        "downstream_services": ["External-Carrier-API"],
        "failure_mode": "nat_ephemeral_port_exhaustion",
        "started_at_utc": "2026-11-01T15:00:00Z",
        "resolved_at_utc": "2026-11-01T15:35:00Z",
        "duration_minutes": 35,
        "deploy_sha": "deploy-6508c11",
        "alarms": ["CW-NATGateway-ErrorPortAllocation", "CW-CarrierWebhook-ConnectTimeout"],
        "metrics": {
            "nat_port_allocation_errors": 9420,
            "active_ephemeral_ports": 55000,
            "webhook_failure_pct": 19.6,
            "p99_latency_ms": 2400,
        },
        "root_cause": (
            "Refactor deploy-6508c11 instantiated a fresh urllib3 PoolManager inside each request handler "
            "instead of sharing a module-level keep-alive session, exhausting 55000 NAT Gateway ephemeral ports in TIME_WAIT."
        ),
        "mitigation": (
            "Moved HTTP Session to singleton connection pool (maxsize=64, keep-alive=60s), dropping outbound SYN rate by 94%."
        ),
        "timeline": [
            {"ts": "15:00 UTC", "event": "Deploy deploy-6508c11 rolled out carrier-webhook handler creating per-call TLS clients."},
            {"ts": "15:08 UTC", "event": "CW-NATGateway-ErrorPortAllocation spiked to 9420 errors at 55000 ports."},
            {"ts": "15:24 UTC", "event": "Rolled out shared keep-alive PoolManager singleton across carrier-webhook containers."},
            {"ts": "15:35 UTC", "event": "NAT port allocation errors dropped to 0 and webhook failure rate fell from 19.6% to 0%."},
        ],
    },
    {
        "incident_id": "INC-209",
        "space_id": "space-prod-fulfillment",
        "title": "Warehouse-Sync ECS Fargate OOMKill (Exit 137) on Unbounded XML Manifest Deserialization",
        "severity": "SEV-2",
        "service": "warehouse-sync",
        "upstream_services": ["S3-ManifestIngest"],
        "downstream_services": ["Valkey-InventoryCluster"],
        "failure_mode": "container_oom_unbounded_batch",
        "started_at_utc": "2026-11-06T07:45:00Z",
        "resolved_at_utc": "2026-11-06T08:23:00Z",
        "duration_minutes": 38,
        "deploy_sha": "deploy-6810f32",
        "alarms": ["CW-ECS-MemoryUtilization-100", "CW-WarehouseSync-TaskStopped-137"],
        "metrics": {
            "task_memory_limit_mib": 1024,
            "peak_rss_mib": 1021,
            "oom_kill_count": 29,
            "manifest_lag_minutes": 38,
        },
        "root_cause": (
            "Deploy deploy-6810f32 increased manifest batch concurrency from 2 to 10 while loading entire DOM "
            "trees in memory inside 1024 MiB Fargate tasks, triggering Linux kernel OOMKills (exit code 137)."
        ),
        "mitigation": (
            "Switched parser to streaming iterparse() clearing processed elements and capped batch concurrency at 3 (peak RSS 310 MiB)."
        ),
        "timeline": [
            {"ts": "07:45 UTC", "event": "Deploy deploy-6810f32 raised batch concurrency to 10 in warehouse-sync."},
            {"ts": "07:51 UTC", "event": "29 ECS Fargate tasks terminated with exit code 137 at 1021 MiB RSS."},
            {"ts": "08:11 UTC", "event": "Deployed streaming XML iterparse() patch with concurrency=3."},
            {"ts": "08:23 UTC", "event": "Task memory stabilized at 310 MiB and 38-minute manifest backlog cleared."},
        ],
    },
    {
        "incident_id": "INC-210",
        "space_id": "space-prod-fulfillment",
        "title": "Shipping-Calc gRPC Silent Connection Drops Across NLB 350s Idle Timeout",
        "severity": "SEV-3",
        "service": "shipping-calc",
        "upstream_services": ["order-router"],
        "downstream_services": [],
        "failure_mode": "nlb_idle_keepalive_mismatch",
        "started_at_utc": "2026-11-11T04:15:00Z",
        "resolved_at_utc": "2026-11-11T04:48:00Z",
        "duration_minutes": 33,
        "deploy_sha": "deploy-6994a08",
        "alarms": ["CW-ShippingCalc-GrpcUnavailable-Burst"],
        "metrics": {
            "nlb_idle_timeout_sec": 350,
            "grpc_keepalive_time_sec": 600,
            "rpc_unavailable_pct": 6.8,
            "p99_latency_ms": 490,
        },
        "root_cause": (
            "gRPC HTTP/2 channels configured keepalive_time_ms=600000 (600s) over an AWS Network Load Balancer "
            "whose fixed TCP idle timeout is 350s. Off-peak idle channels were silently reset by NLB."
        ),
        "mitigation": (
            "Configured gRPC client keepalive_time_ms=120000 (120s < 350s NLB idle timeout) with keepalive_timeout_ms=20000."
        ),
        "timeline": [
            {"ts": "04:15 UTC", "event": "Off-peak trough left gRPC subchannels idle >350s behind internal NLB."},
            {"ts": "04:20 UTC", "event": "CW-ShippingCalc-GrpcUnavailable-Burst alerted at 6.8% UNAVAILABLE errors on first request."},
            {"ts": "04:39 UTC", "event": "Rolled out gRPC keepalive_time=120s across order-router clients."},
            {"ts": "04:48 UTC", "event": "NLB RST packets eliminated and gRPC UNAVAILABLE rate returned to 0%."},
        ],
    },
    # ===================== SPACE 3: space-prod-identity (4 incidents) =====================
    {
        "incident_id": "INC-211",
        "space_id": "space-prod-identity",
        "title": "Auth-Token-Svc OIDC JWKS Cache Stampede on Scheduled Signing Key Rotation",
        "severity": "SEV-1",
        "service": "auth-token-svc",
        "upstream_services": ["session-edge"],
        "downstream_services": ["OIDC-JWKS-Origin", "tenant-rbac"],
        "failure_mode": "cache_stampede_missing_singleflight",
        "started_at_utc": "2026-10-19T12:00:00Z",
        "resolved_at_utc": "2026-10-19T12:29:00Z",
        "duration_minutes": 29,
        "deploy_sha": "deploy-3102b8f",
        "alarms": ["CW-AuthTokenSvc-JWKSUpstream429", "CW-SessionEdge-Auth5xx-Critical"],
        "metrics": {
            "jwks_refresh_qps": 4200,
            "upstream_rate_limit_qps": 100,
            "auth_5xx_pct": 24.6,
            "p99_latency_ms": 1680,
        },
        "root_cause": (
            "When OIDC signing kid rotated at 12:00 UTC, every auth-token-svc worker invalidated its local JWKS "
            "cache simultaneously without request coalescing or circuit breaker, sending 4200 QPS to OIDC-JWKS-Origin (limit 100 QPS)."
        ),
        "mitigation": (
            "Implemented singleflight request coalescing per pod plus stale-while-revalidate caching (300s grace) "
            "and a 5-second negative-kid cooldown."
        ),
        "timeline": [
            {"ts": "12:00 UTC", "event": "Scheduled OIDC kid rotation introduced new signing key id kid-2026-10b."},
            {"ts": "12:01 UTC", "event": "4200 QPS cache-miss storm hit OIDC-JWKS-Origin; HTTP 429 pushed auth 5xx to 24.6%."},
            {"ts": "12:18 UTC", "event": "Deployed singleflight coalescing and seeded JWKS payload in shared edge cache."},
            {"ts": "12:29 UTC", "event": "Origin QPS dropped from 4200 to 2 QPS and auth 5xx returned to 0%."},
        ],
    },
    {
        "incident_id": "INC-212",
        "space_id": "space-prod-identity",
        "title": "Session-Edge Lambda@Edge Synchronous SSM Parameter Fetch Throttled Viewer Requests",
        "severity": "SEV-1",
        "service": "session-edge",
        "upstream_services": ["CloudFront-GlobalViewer"],
        "downstream_services": ["SSM-ConfigStore", "auth-token-svc"],
        "failure_mode": "sync_control_plane_call_in_data_plane",
        "started_at_utc": "2026-10-31T17:10:00Z",
        "resolved_at_utc": "2026-10-31T17:38:00Z",
        "duration_minutes": 28,
        "deploy_sha": "deploy-3389d41",
        "alarms": ["CW-LambdaEdge-5xx-ViewerRequest", "CW-SSM-GetParameter-Throttled"],
        "metrics": {
            "ssm_get_parameter_qps": 3900,
            "ssm_tps_limit": 1000,
            "edge_5xx_pct": 16.9,
            "p99_latency_ms": 1410,
        },
        "root_cause": (
            "Deploy deploy-3389d41 placed a per-request ssm:GetParameter call inside the hot data-plane "
            "viewer-request handler without warm-container TTL caching, violating AWS control-plane/data-plane separation."
        ),
        "mitigation": (
            "Removed synchronous SSM call from request path, bundled immutable config snapshot at build time, "
            "and added 300-second background refresh from S3/CloudFront config origin."
        ),
        "timeline": [
            {"ts": "17:10 UTC", "event": "Published Lambda@Edge version deploy-3389d41 calling ssm:GetParameter per request."},
            {"ts": "17:13 UTC", "event": "SSM GetParameter hit 3900 QPS against 1000 TPS regional limit; edge 5xx hit 16.9%."},
            {"ts": "17:27 UTC", "event": "Re-pointed CloudFront behavior to previous Lambda@Edge version with in-memory cached config."},
            {"ts": "17:38 UTC", "event": "SSM throttling ceased and global viewer 5xx returned to 0.00%."},
        ],
    },
    {
        "incident_id": "INC-213",
        "space_id": "space-prod-identity",
        "title": "Tenant-RBAC Wildcard Resource Policy Regression Blocked by Canary Isolation Gate",
        "severity": "SEV-2",
        "service": "tenant-rbac",
        "upstream_services": ["auth-token-svc"],
        "downstream_services": ["audit-ingest"],
        "failure_mode": "iam_wildcard_tenant_boundary",
        "started_at_utc": "2026-11-07T10:05:00Z",
        "resolved_at_utc": "2026-11-07T10:26:00Z",
        "duration_minutes": 21,
        "deploy_sha": "policy-pr-418",
        "alarms": ["CW-TenantRBAC-CanaryScopeViolation", "CW-TenantRBAC-FailClosed-403"],
        "metrics": {
            "canary_scope_violations": 14,
            "cross_tenant_reads_allowed": 0,
            "fail_closed_403_pct": 8.3,
            "p99_latency_ms": 95,
        },
        "root_cause": (
            "Policy bundle policy-pr-418 accidentally replaced arn:aws:devops-space:*:*:space/${aws:PrincipalTag/SpaceId}/* "
            "with Resource: '*' in the canary shard. The data-plane fail-closed scope guard rejected unscoped tokens with HTTP 403."
        ),
        "mitigation": (
            "Rolled back policy-pr-418 in 11 minutes, restored mandatory aws:PrincipalTag/SpaceId condition, "
            "and added deterministic wildcard diff blocking in CI."
        ),
        "timeline": [
            {"ts": "10:05 UTC", "event": "Canary shard loaded policy-pr-418 containing unscoped Resource '*' statement."},
            {"ts": "10:06 UTC", "event": "Data-plane invariant detector triggered fail-closed 403s (0 cross-tenant reads leaked)."},
            {"ts": "10:17 UTC", "event": "Reverted canary policy bundle to pinned PrincipalTag/SpaceId template."},
            {"ts": "10:26 UTC", "event": "Canary 403s cleared to 0% with 0 cross-tenant data exposure."},
        ],
    },
    {
        "incident_id": "INC-214",
        "space_id": "space-prod-identity",
        "title": "Audit-Ingest Kinesis IteratorAge Lag From Synchronous Per-Record Checkpoint Commits",
        "severity": "SEV-3",
        "service": "audit-ingest",
        "upstream_services": ["tenant-rbac"],
        "downstream_services": ["Kinesis-AuditStream"],
        "failure_mode": "checkpoint_sync_io_lag",
        "started_at_utc": "2026-11-14T21:20:00Z",
        "resolved_at_utc": "2026-11-14T21:55:00Z",
        "duration_minutes": 35,
        "deploy_sha": "deploy-3590e12",
        "alarms": ["CW-Kinesis-GetRecords-IteratorAgeMilliseconds"],
        "metrics": {
            "iterator_age_ms": 284000,
            "checkpoint_writes_per_sec": 1650,
            "shard_count": 8,
            "p99_latency_ms": 610,
        },
        "root_cause": (
            "Consumer patch deploy-3590e12 checkpointed DynamoDB lease state after every single Kinesis record "
            "(1650 writes/sec) instead of once per record batch, stalling shard consumers to 284000 ms iterator age."
        ),
        "mitigation": (
            "Restored end-of-batch checkpointing (every 250 records or 5 seconds), draining 284s iterator lag in 9 minutes."
        ),
        "timeline": [
            {"ts": "21:20 UTC", "event": "Deploy deploy-3590e12 enabled per-record lease checkpointing in audit-ingest."},
            {"ts": "21:31 UTC", "event": "CW-Kinesis-GetRecords-IteratorAgeMilliseconds crossed 284000 ms threshold."},
            {"ts": "21:46 UTC", "event": "Rolled out batched checkpointing (250 records / 5s interval)."},
            {"ts": "21:55 UTC", "event": "Kinesis IteratorAge dropped below 800 ms across all 8 shards."},
        ],
    },
]
