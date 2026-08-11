from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from .execution_boundary import (
    DEFAULT_MAX_REQUEST_BYTES,
    DEFAULT_MAX_RESPONSE_BYTES,
    WORKER_SUCCESS_STATUS,
    WorkerProtocolError,
    WorkerRequestV1,
    WorkerResponseV1,
    decode_framed_json,
    encode_framed_json,
)
from .tools_basic import GOVERNED_TOOL_REGISTRY


def _server_owned_worker_handlers() -> dict[str, Any]:
    # Trusted, build-time-owned mapping only.
    return {spec.worker_handler_id: fn for spec, fn in GOVERNED_TOOL_REGISTRY.values()}


def _safe_diag(message: str, max_bytes: int = 1024) -> str:
    encoded = message.encode("utf-8", errors="replace")
    return encoded[:max_bytes].decode("utf-8", errors="replace")


def _response_from_exception(request: WorkerRequestV1, exc: Exception) -> WorkerResponseV1:
    return WorkerResponseV1.from_request(
        request,
        status="TOOL_ERROR",
        result={},
        diagnostic_class=type(exc).__name__,
        diagnostic_message=_safe_diag(str(exc)),
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
            diagnostic_class=type(exc).__name__,
            diagnostic_message=_safe_diag(str(exc)),
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
        out_buf = io.StringIO()
        err_buf = io.StringIO()
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
