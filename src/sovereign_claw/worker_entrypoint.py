from __future__ import annotations

import hashlib
import io
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from .execution_boundary import (
    DEFAULT_MAX_REQUEST_BYTES,
    DEFAULT_MAX_RESPONSE_BYTES,
    WORKER_SUCCESS_STATUS,
    WorkerRequestV1,
    WorkerResponseV1,
    decode_framed_json,
    encode_framed_json,
)
from .tools_basic import GOVERNED_TOOL_REGISTRY


def _server_owned_worker_handlers() -> dict[str, Any]:
    # Trusted, build-time-owned mapping only.
    return {spec.worker_handler_id: fn for spec, fn in GOVERNED_TOOL_REGISTRY.values()}


class _BoundedOutputError(RuntimeError):
    pass


class _BoundedTextWriter(io.TextIOBase):
    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._used = 0

    def write(self, s: str) -> int:
        if not s:
            return 0
        encoded = s.encode("utf-8", errors="replace")
        self._used += len(encoded)
        if self._used > self._max_bytes:
            raise _BoundedOutputError("handler stream exceeded configured cap")
        return len(s)

    def flush(self) -> None:
        return


def _safe_diag_fields(exc: Exception) -> tuple[str, str, dict[str, Any]]:
    raw = str(exc).encode("utf-8", errors="replace")
    digest = hashlib.sha256(raw).hexdigest()
    return (
        type(exc).__name__,
        "Worker exception redacted by policy",
        {
            "diagnostic_digest": digest,
            "diagnostic_size_bytes": len(raw),
        },
    )


def _response_from_exception(request: WorkerRequestV1, exc: Exception) -> WorkerResponseV1:
    diag_class, diag_message, diag_evidence = _safe_diag_fields(exc)
    return WorkerResponseV1.from_request(
        request,
        status="TOOL_ERROR",
        result={},
        diagnostic_class=diag_class,
        diagnostic_message=diag_message,
        side_effect_evidence=diag_evidence,
    )


def main() -> int:
    try:
        req_payload = decode_framed_json(
            stream=io.BufferedReader(io.FileIO(0, "rb")),
            max_bytes=DEFAULT_MAX_REQUEST_BYTES,
        )
        request = WorkerRequestV1.from_json_bytes(
            req_payload,
            max_bytes=DEFAULT_MAX_REQUEST_BYTES,
        )
    except Exception as exc:
        diag_class, diag_message, diag_evidence = _safe_diag_fields(exc)
        fallback_request = WorkerRequestV1(
            schema_version="1",
            request_id="invalid",
            trace_id="invalid",
            correlation_id="invalid",
            tool_id="invalid",
            tool_contract_hash="invalid",
            registry_snapshot_hash="invalid",
            worker_handler_id="invalid",
            worker_build_identity="invalid",
            isolation_profile="subprocess_bounded_v1",
            action_digest="invalid",
            policy_identity="invalid",
            principal_identity="invalid",
            principal_scopes=(),
            capabilities=(),
            args={},
            deadline_ms=1,
            cpu_budget_ms=None,
            memory_bytes=None,
            max_processes=None,
            max_request_bytes=DEFAULT_MAX_REQUEST_BYTES,
            max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
            max_stdout_bytes=1,
            max_stderr_bytes=1,
            max_output_bytes=1,
            postcondition_validator_id="none",
            postcondition_validator_version="none",
            evidence_policy="digest_only",
            redaction_policy="default",
        )
        response = WorkerResponseV1.from_request(
            fallback_request,
            status="PROTOCOL_ERROR",
            result={},
            diagnostic_class=diag_class,
            diagnostic_message=diag_message,
            side_effect_evidence=diag_evidence,
        )
        encoded = response.canonical_bytes()
        io.FileIO(1, "wb").write(encode_framed_json(encoded))
        return 0

    handlers = _server_owned_worker_handlers()
    handler = handlers.get(request.worker_handler_id)
    if handler is None:
        response = WorkerResponseV1.from_request(
            request,
            status="PROTOCOL_ERROR",
            result={},
            diagnostic_class="UNKNOWN_HANDLER",
            diagnostic_message=f"Unknown worker_handler_id: {request.worker_handler_id}",
        )
    else:
        out_buf = _BoundedTextWriter(request.max_stdout_bytes)
        err_buf = _BoundedTextWriter(request.max_stderr_bytes)
        try:
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                result = handler(**request.args)
            response = WorkerResponseV1.from_request(
                request,
                status=WORKER_SUCCESS_STATUS,
                result=result,
                diagnostic_class="",
                diagnostic_message="",
                side_effect_evidence={},
            )
        except _BoundedOutputError as exc:
            response = WorkerResponseV1.from_request(
                request,
                status="OUTPUT_LIMIT",
                result={},
                diagnostic_class="OUTPUT_LIMIT",
                diagnostic_message="Worker handler stdout/stderr exceeded configured limits",
                side_effect_evidence=_safe_diag_fields(exc)[2],
            )
        except Exception as exc:
            response = _response_from_exception(request, exc)

    encoded = response.canonical_bytes()
    if len(encoded) > request.max_response_bytes:
        response = WorkerResponseV1.from_request(
            request,
            status="OUTPUT_LIMIT",
            result={},
            diagnostic_class="OUTPUT_LIMIT",
            diagnostic_message="WorkerResponse exceeds max_response_bytes",
        )
        encoded = response.canonical_bytes()
    io.FileIO(1, "wb").write(encode_framed_json(encoded))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
