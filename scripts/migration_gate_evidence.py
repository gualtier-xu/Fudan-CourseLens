"""Small, public-only evidence envelopes for offline migration gates."""
from __future__ import annotations
import hashlib, json, time
from typing import Any

EVIDENCE_SCHEMA = "courselens.migration-gate-evidence.v1"
_SAFE = {"required_go_sha256", "rejection_count", "false_positive_count", "checks", "zero_count", "file_count", "payload_git_tree", "public_tree", "public_commit", "source_commit", "worker_commit", "attempt", "timestamp"}
_FORBIDDEN = ("task", "issue", "log", "secret", "fixture", "repository", "run_id")

def build_evidence(gate: str, status: str, **values: Any) -> dict[str, Any]:
    if not isinstance(gate, str) or not isinstance(status, str) or set(values) - _SAFE or any(part in key for key in values for part in _FORBIDDEN):
        raise ValueError("unsafe migration gate evidence field")
    payload = {"schema": EVIDENCE_SCHEMA, "gate": gate, "status": status, "timestamp": int(values.pop("timestamp", time.time())), **values}
    payload["evidence_sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return payload
