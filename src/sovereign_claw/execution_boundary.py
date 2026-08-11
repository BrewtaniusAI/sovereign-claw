from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

from .tool_authority import ToolRegistryEntry, canonical_json, validate_output

WORKER_SCHEMA_VERSION = "1"
DEFAULT_MAX_JSON_DEPTH = 16
DEFAULT_MAX_REQUEST_BYTES = 256 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024
DEFAULT_MAX_STDOUT_BYTES = 64 * 1024
DEFAULT_MAX_STDERR_BYTES = 64 * 1024
TERMINATE_GRACE_SECONDS = 0.5

WORKER_SUCCESS_STATUS = "SUCCEEDED"
WORKER_FAILURE_STATUSES = frozenset(
    {
        "TOOL_ERROR",
        "TIMEOUT",
        "CANCELLED",
        "WORKER_CRASH",
        "RESOURCE_LIMIT",
        "CAPABILITY_DENIED",
        "OUTPUT_LIMIT",
        "PROTOCOL_ERROR",
        "POSTCONDITION_FAILED",
        "ISOLATION_UNAVAILABLE",
        "UNSUPPORTED_ISOLATION",
    }
)
WORKER_ALL_STATUSES = WORKER_FAILURE_STATUSES | {WORKER_SUCCESS_STATUS}


class WorkerProtocolError(Exception):
    def __init__(self, message: str, code: str = "PROTOCOL_ERROR") -> None:
        super().__init__(message)
        self.code = code


def _validate_depth(value: Any, depth: int = 0, max_depth: int = DEFAULT_MAX_JSON_DEPTH) -> None:
    if depth > max_depth:
        raise WorkerProtocolError(f"JSON depth exceeds max depth {max_depth}")
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise WorkerProtocolError("JSON object keys must be strings")
            _validate_depth(v, depth + 1, max_depth)
        return
    if isinstance(value, list):
        for item in value:
            _validate_depth(item, depth + 1, max_depth)
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise WorkerProtocolError("Non-finite numeric value in JSON envelope")


def _load_json_strict(payload: bytes, *, max_depth: int, max_bytes: int) -> Any:
    if len(payload) > max_bytes:
        raise WorkerProtocolError(f"JSON payload exceeds {max_bytes} bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkerProtocolError("JSON envelope must be valid UTF-8") from exc
    try:
        data = json.loads(
            text,
            parse_constant=lambda value: (_ for _ in ()).throw(
                WorkerProtocolError(f"Non-finite number literal: {value}")
            ),
        )
    except WorkerProtocolError:
        raise
    except Exception as exc:
        raise WorkerProtocolError(f"Malformed JSON envelope: {exc}") from exc
    _validate_depth(data, max_depth=max_depth)
    return data


def _assert_exact_keys(data: dict[str, Any], allowed: set[str]) -> None:
    incoming = set(data.keys())
    if incoming != allowed:
        extra = sorted(incoming - allowed)
        missing = sorted(allowed - incoming)
        raise WorkerProtocolError(f"Envelope keys mismatch; missing={missing} extra={extra}")


def _as_pos_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise WorkerProtocolError(f"{name} must be a positive integer")
    return value


def _as_opt_pos_int(name: str, value: Any) -> int | None:
    if value is None:
        return None
    return _as_pos_int(name, value)


@dataclass(frozen=True)
class WorkerRequestV1:
    schema_version: str
    request_id: str
    trace_id: str
    correlation_id: str
    tool_id: str
    tool_contract_hash: str
    registry_snapshot_hash: str
    worker_handler_id: str
    worker_build_identity: str
    isolation_profile: str
    action_digest: str
    policy_identity: str
    principal_identity: str
    principal_scopes: tuple[str, ...]
    capabilities: tuple[str, ...]
    args: dict[str, Any]
    deadline_ms: int
    cpu_budget_ms: int | None
    memory_bytes: int | None
    max_processes: int | None
    max_request_bytes: int
    max_response_bytes: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    max_output_bytes: int
    postcondition_validator_id: str
    postcondition_validator_version: str
    evidence_policy: str
    redaction_policy: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "correlation_id": self.correlation_id,
            "tool_id": self.tool_id,
            "tool_contract_hash": self.tool_contract_hash,
            "registry_snapshot_hash": self.registry_snapshot_hash,
            "worker_handler_id": self.worker_handler_id,
            "worker_build_identity": self.worker_build_identity,
            "isolation_profile": self.isolation_profile,
            "action_digest": self.action_digest,
            "policy_identity": self.policy_identity,
            "principal_identity": self.principal_identity,
            "principal_scopes": list(self.principal_scopes),
            "capabilities": list(self.capabilities),
            "args": self.args,
            "deadline_ms": self.deadline_ms,
            "cpu_budget_ms": self.cpu_budget_ms,
            "memory_bytes": self.memory_bytes,
            "max_processes": self.max_processes,
            "max_request_bytes": self.max_request_bytes,
            "max_response_bytes": self.max_response_bytes,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "max_output_bytes": self.max_output_bytes,
            "postcondition_validator_id": self.postcondition_validator_id,
            "postcondition_validator_version": self.postcondition_validator_version,
            "evidence_policy": self.evidence_policy,
            "redaction_policy": self.redaction_policy,
        }

    def canonical_bytes(self) -> bytes:
        payload = canonical_json(self.to_dict())
        if len(payload) > self.max_request_bytes:
            raise WorkerProtocolError(
                f"WorkerRequestV1 exceeds max_request_bytes ({self.max_request_bytes})"
            )
        return payload

    @classmethod
    def from_json_bytes(
        cls,
        payload: bytes,
        *,
        max_depth: int = DEFAULT_MAX_JSON_DEPTH,
        max_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
    ) -> "WorkerRequestV1":
        data = _load_json_strict(payload, max_depth=max_depth, max_bytes=max_bytes)
        if not isinstance(data, dict):
            raise WorkerProtocolError("WorkerRequestV1 must be a JSON object")
        _assert_exact_keys(data, set(cls.__dataclass_fields__.keys()))
        for key in (
            "schema_version",
            "request_id",
            "trace_id",
            "correlation_id",
            "tool_id",
            "tool_contract_hash",
            "registry_snapshot_hash",
            "worker_handler_id",
            "worker_build_identity",
            "isolation_profile",
            "action_digest",
            "policy_identity",
            "principal_identity",
            "postcondition_validator_id",
            "postcondition_validator_version",
            "evidence_policy",
            "redaction_policy",
        ):
            if not isinstance(data[key], str) or not data[key]:
                raise WorkerProtocolError(f"{key} must be a non-empty string")
        if data["schema_version"] != WORKER_SCHEMA_VERSION:
            raise WorkerProtocolError(
                f"Unsupported WorkerRequestV1 schema_version {data['schema_version']!r}"
            )
        for key in (
            "principal_scopes",
            "capabilities",
        ):
            if not isinstance(data[key], list) or any(not isinstance(v, str) for v in data[key]):
                raise WorkerProtocolError(f"{key} must be a list[str]")
        if not isinstance(data["args"], dict):
            raise WorkerProtocolError("args must be a JSON object")
        _validate_depth(data["args"], max_depth=max_depth)

        request = cls(
            schema_version=data["schema_version"],
            request_id=data["request_id"],
            trace_id=data["trace_id"],
            correlation_id=data["correlation_id"],
            tool_id=data["tool_id"],
            tool_contract_hash=data["tool_contract_hash"],
            registry_snapshot_hash=data["registry_snapshot_hash"],
            worker_handler_id=data["worker_handler_id"],
            worker_build_identity=data["worker_build_identity"],
            isolation_profile=data["isolation_profile"],
            action_digest=data["action_digest"],
            policy_identity=data["policy_identity"],
            principal_identity=data["principal_identity"],
            principal_scopes=tuple(data["principal_scopes"]),
            capabilities=tuple(data["capabilities"]),
            args=data["args"],
            deadline_ms=_as_pos_int("deadline_ms", data["deadline_ms"]),
            cpu_budget_ms=_as_opt_pos_int("cpu_budget_ms", data["cpu_budget_ms"]),
            memory_bytes=_as_opt_pos_int("memory_bytes", data["memory_bytes"]),
            max_processes=_as_opt_pos_int("max_processes", data["max_processes"]),
            max_request_bytes=_as_pos_int("max_request_bytes", data["max_request_bytes"]),
            max_response_bytes=_as_pos_int("max_response_bytes", data["max_response_bytes"]),
            max_stdout_bytes=_as_pos_int("max_stdout_bytes", data["max_stdout_bytes"]),
            max_stderr_bytes=_as_pos_int("max_stderr_bytes", data["max_stderr_bytes"]),
            max_output_bytes=_as_pos_int("max_output_bytes", data["max_output_bytes"]),
            postcondition_validator_id=data["postcondition_validator_id"],
            postcondition_validator_version=data["postcondition_validator_version"],
            evidence_policy=data["evidence_policy"],
            redaction_policy=data["redaction_policy"],
        )
        if len(request.canonical_bytes()) > request.max_request_bytes:
            raise WorkerProtocolError(
                f"WorkerRequestV1 exceeds max_request_bytes ({request.max_request_bytes})"
            )
        return request


@dataclass(frozen=True)
class WorkerResponseV1:
    schema_version: str
    request_id: str
    tool_id: str
    tool_contract_hash: str
    registry_snapshot_hash: str
    worker_handler_id: str
    worker_build_identity: str
    isolation_profile: str
    action_digest: str
    policy_identity: str
    principal_identity: str
    status: str
    result: Any
    result_sha256: str
    result_size_bytes: int
    diagnostic_class: str
    diagnostic_message: str
    duration_ms: int
    side_effect_evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "tool_id": self.tool_id,
            "tool_contract_hash": self.tool_contract_hash,
            "registry_snapshot_hash": self.registry_snapshot_hash,
            "worker_handler_id": self.worker_handler_id,
            "worker_build_identity": self.worker_build_identity,
            "isolation_profile": self.isolation_profile,
            "action_digest": self.action_digest,
            "policy_identity": self.policy_identity,
            "principal_identity": self.principal_identity,
            "status": self.status,
            "result": self.result,
            "result_sha256": self.result_sha256,
            "result_size_bytes": self.result_size_bytes,
            "diagnostic_class": self.diagnostic_class,
            "diagnostic_message": self.diagnostic_message,
            "duration_ms": self.duration_ms,
            "side_effect_evidence": self.side_effect_evidence,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict())

    @classmethod
    def from_json_bytes(
        cls,
        payload: bytes,
        *,
        max_depth: int = DEFAULT_MAX_JSON_DEPTH,
        max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> "WorkerResponseV1":
        data = _load_json_strict(payload, max_depth=max_depth, max_bytes=max_bytes)
        if not isinstance(data, dict):
            raise WorkerProtocolError("WorkerResponseV1 must be a JSON object")
        _assert_exact_keys(data, set(cls.__dataclass_fields__.keys()))
        if data.get("schema_version") != WORKER_SCHEMA_VERSION:
            raise WorkerProtocolError(
                f"Unsupported WorkerResponseV1 schema_version {data.get('schema_version')!r}"
            )
        if data.get("status") not in WORKER_ALL_STATUSES:
            raise WorkerProtocolError(f"Unknown worker status {data.get('status')!r}")
        if not isinstance(data.get("side_effect_evidence"), dict):
            raise WorkerProtocolError("side_effect_evidence must be an object")
        for key in (
            "request_id",
            "tool_id",
            "tool_contract_hash",
            "registry_snapshot_hash",
            "worker_handler_id",
            "worker_build_identity",
            "isolation_profile",
            "action_digest",
            "policy_identity",
            "principal_identity",
            "result_sha256",
            "diagnostic_class",
            "diagnostic_message",
        ):
            if not isinstance(data[key], str):
                raise WorkerProtocolError(f"{key} must be a string")
        _as_pos_int("duration_ms", max(1, int(data["duration_ms"])))
        if not isinstance(data.get("result_size_bytes"), int) or data["result_size_bytes"] < 0:
            raise WorkerProtocolError("result_size_bytes must be a non-negative integer")
        return cls(
            schema_version=data["schema_version"],
            request_id=data["request_id"],
            tool_id=data["tool_id"],
            tool_contract_hash=data["tool_contract_hash"],
            registry_snapshot_hash=data["registry_snapshot_hash"],
            worker_handler_id=data["worker_handler_id"],
            worker_build_identity=data["worker_build_identity"],
            isolation_profile=data["isolation_profile"],
            action_digest=data["action_digest"],
            policy_identity=data["policy_identity"],
            principal_identity=data["principal_identity"],
            status=data["status"],
            result=data["result"],
            result_sha256=data["result_sha256"],
            result_size_bytes=data["result_size_bytes"],
            diagnostic_class=data["diagnostic_class"],
            diagnostic_message=data["diagnostic_message"],
            duration_ms=int(data["duration_ms"]),
            side_effect_evidence=data["side_effect_evidence"],
        )

    @classmethod
    def from_request(
        cls,
        request: WorkerRequestV1,
        *,
        status: str,
        result: Any,
        diagnostic_class: str = "",
        diagnostic_message: str = "",
        duration_ms: int = 0,
        side_effect_evidence: dict[str, Any] | None = None,
    ) -> "WorkerResponseV1":
        encoded = canonical_json(result)
        return cls(
            schema_version=WORKER_SCHEMA_VERSION,
            request_id=request.request_id,
            tool_id=request.tool_id,
            tool_contract_hash=request.tool_contract_hash,
            registry_snapshot_hash=request.registry_snapshot_hash,
            worker_handler_id=request.worker_handler_id,
            worker_build_identity=request.worker_build_identity,
            isolation_profile=request.isolation_profile,
            action_digest=request.action_digest,
            policy_identity=request.policy_identity,
            principal_identity=request.principal_identity,
            status=status,
            result=result,
            result_sha256=hashlib.sha256(encoded).hexdigest(),
            result_size_bytes=len(encoded),
            diagnostic_class=diagnostic_class,
            diagnostic_message=diagnostic_message,
            duration_ms=max(0, int(duration_ms)),
            side_effect_evidence=side_effect_evidence or {},
        )


def encode_framed_json(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


def decode_framed_json(stream: Any, *, max_bytes: int) -> bytes:
    header = stream.read(4)
    if len(header) != 4:
        raise WorkerProtocolError("Missing framed JSON header")
    frame_len = int.from_bytes(header, "big")
    if frame_len <= 0 or frame_len > max_bytes:
        raise WorkerProtocolError(f"Frame length {frame_len} exceeds max {max_bytes}")
    payload = stream.read(frame_len)
    if len(payload) != frame_len:
        raise WorkerProtocolError("Incomplete framed JSON payload")
    return payload


@dataclass(frozen=True)
class IsolationCapabilityMatrix:
    profile_id: str
    wall_deadline: bool
    process_tree_kill: bool
    cpu_limit: bool
    memory_limit: bool
    process_count_limit: bool
    filesystem_isolation: bool
    network_isolation: bool
    available: bool = True


def probe_subprocess_bounded_v1_capabilities() -> IsolationCapabilityMatrix:
    is_posix = os.name == "posix"
    has_resource = False
    cpu_limit = False
    memory_limit = False
    process_count_limit = False
    if is_posix:
        try:
            import resource  # type: ignore

            has_resource = True
            cpu_limit = hasattr(resource, "RLIMIT_CPU")
            memory_limit = hasattr(resource, "RLIMIT_AS")
            process_count_limit = hasattr(resource, "RLIMIT_NPROC")
        except Exception:
            has_resource = False
    _ = has_resource
    return IsolationCapabilityMatrix(
        profile_id="subprocess_bounded_v1",
        wall_deadline=True,
        process_tree_kill=is_posix,
        cpu_limit=cpu_limit,
        memory_limit=memory_limit,
        process_count_limit=process_count_limit,
        filesystem_isolation=False,
        network_isolation=False,
        available=True,
    )


def probe_hardened_container_seccomp_v1_capabilities() -> IsolationCapabilityMatrix:
    # This profile is intentionally unavailable until a self-test proves that the
    # exact confinement primitives are active.
    selftest_ok = os.environ.get("SOVEREIGN_CLAW_HARDENED_SELFTEST", "") == "ok"
    return IsolationCapabilityMatrix(
        profile_id="hardened_container_seccomp_v1",
        wall_deadline=selftest_ok,
        process_tree_kill=selftest_ok,
        cpu_limit=selftest_ok,
        memory_limit=selftest_ok,
        process_count_limit=selftest_ok,
        filesystem_isolation=selftest_ok,
        network_isolation=selftest_ok,
        available=selftest_ok,
    )


def _make_fail_closed_response(
    request: WorkerRequestV1,
    *,
    status: str,
    diagnostic_class: str,
    diagnostic_message: str,
) -> WorkerResponseV1:
    return WorkerResponseV1.from_request(
        request,
        status=status,
        result={},
        diagnostic_class=diagnostic_class,
        diagnostic_message=diagnostic_message,
        duration_ms=0,
    )


def _request_limits_are_enforceable(
    request: WorkerRequestV1,
    matrix: IsolationCapabilityMatrix,
) -> tuple[bool, str, str]:
    if request.cpu_budget_ms is not None and not matrix.cpu_limit:
        return False, "UNSUPPORTED_ISOLATION", "CPU limits are not enforceable on this platform"
    if request.memory_bytes is not None and not matrix.memory_limit:
        return (
            False,
            "UNSUPPORTED_ISOLATION",
            "Memory limits are not enforceable on this platform",
        )
    if request.max_processes is not None and not matrix.process_count_limit:
        return (
            False,
            "UNSUPPORTED_ISOLATION",
            "Process-count limits are not enforceable on this platform",
        )
    if "filesystem.isolated" in request.capabilities and not matrix.filesystem_isolation:
        return (
            False,
            "ISOLATION_UNAVAILABLE",
            "Filesystem isolation primitive unavailable for requested profile",
        )
    if "network.isolated" in request.capabilities and not matrix.network_isolation:
        return (
            False,
            "ISOLATION_UNAVAILABLE",
            "Network isolation primitive unavailable for requested profile",
        )
    return True, "", ""


def _minimal_worker_env() -> dict[str, str]:
    allow = (
        "PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TMP",
        "TEMP",
    )
    return {k: v for k, v in os.environ.items() if k in allow}


def _kill_process_tree(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=TERMINATE_GRACE_SECONDS)
            return
        except Exception:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            proc.kill()
    else:
        proc.terminate()
    try:
        proc.wait(timeout=TERMINATE_GRACE_SECONDS)
    except Exception:
        proc.kill()
        proc.wait(timeout=TERMINATE_GRACE_SECONDS)


def _preexec_for_limits(request: WorkerRequestV1) -> Callable[[], None] | None:
    if os.name != "posix":
        return None
    try:
        import resource  # type: ignore
    except Exception:
        return os.setsid

    def _inner() -> None:
        os.setsid()
        if request.cpu_budget_ms is not None and hasattr(resource, "RLIMIT_CPU"):
            cpu_seconds = max(1, int(math.ceil(request.cpu_budget_ms / 1000)))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        if request.memory_bytes is not None and hasattr(resource, "RLIMIT_AS"):
            resource.setrlimit(resource.RLIMIT_AS, (request.memory_bytes, request.memory_bytes))
        if request.max_processes is not None and hasattr(resource, "RLIMIT_NPROC"):
            resource.setrlimit(
                resource.RLIMIT_NPROC, (request.max_processes, request.max_processes)
            )

    return _inner


def run_subprocess_bounded_v1(request: WorkerRequestV1) -> WorkerResponseV1:
    matrix = probe_subprocess_bounded_v1_capabilities()
    enforceable, status, message = _request_limits_are_enforceable(request, matrix)
    if not enforceable:
        return _make_fail_closed_response(
            request,
            status=status,
            diagnostic_class=status,
            diagnostic_message=message,
        )

    started = time.monotonic()
    args = [sys.executable, "-m", "sovereign_claw.worker_entrypoint"]
    proc: subprocess.Popen[bytes] | None = None
    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            cwd=os.getcwd(),
            env=_minimal_worker_env(),
            close_fds=True,
            preexec_fn=_preexec_for_limits(request),
            creationflags=creationflags,
        )
        assert proc is not None
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        req_bytes = request.canonical_bytes()
        proc.stdin.write(encode_framed_json(req_bytes))
        proc.stdin.flush()
        proc.stdin.close()
        proc.stdin = None
        timeout_seconds = max(0.001, request.deadline_ms / 1000.0)
        try:
            stdout_data, stderr_data = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            return _make_fail_closed_response(
                request,
                status="TIMEOUT",
                diagnostic_class="TIMEOUT",
                diagnostic_message="Worker exceeded wall deadline",
            )
        if len(stderr_data) > request.max_stderr_bytes:
            stderr_data = stderr_data[: request.max_stderr_bytes]
        if len(stdout_data) > request.max_response_bytes + 4:
            return _make_fail_closed_response(
                request,
                status="OUTPUT_LIMIT",
                diagnostic_class="OUTPUT_LIMIT",
                diagnostic_message="Worker stdout exceeded max_response_bytes",
            )
        if proc.returncode not in (0, None):
            return _make_fail_closed_response(
                request,
                status="WORKER_CRASH",
                diagnostic_class="WORKER_CRASH",
                diagnostic_message=f"Worker exited with code {proc.returncode}",
            )
        payload = decode_framed_json(
            stream=_BytesReader(stdout_data),
            max_bytes=request.max_response_bytes,
        )
        response = WorkerResponseV1.from_json_bytes(
            payload,
            max_depth=DEFAULT_MAX_JSON_DEPTH,
            max_bytes=request.max_response_bytes,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return WorkerResponseV1(
            schema_version=response.schema_version,
            request_id=response.request_id,
            tool_id=response.tool_id,
            tool_contract_hash=response.tool_contract_hash,
            registry_snapshot_hash=response.registry_snapshot_hash,
            worker_handler_id=response.worker_handler_id,
            worker_build_identity=response.worker_build_identity,
            isolation_profile=response.isolation_profile,
            action_digest=response.action_digest,
            policy_identity=response.policy_identity,
            principal_identity=response.principal_identity,
            status=response.status,
            result=response.result,
            result_sha256=response.result_sha256,
            result_size_bytes=response.result_size_bytes,
            diagnostic_class=response.diagnostic_class,
            diagnostic_message=response.diagnostic_message,
            duration_ms=max(response.duration_ms, elapsed_ms),
            side_effect_evidence=response.side_effect_evidence,
        )
    except WorkerProtocolError as exc:
        return _make_fail_closed_response(
            request,
            status="PROTOCOL_ERROR",
            diagnostic_class=exc.code,
            diagnostic_message=str(exc),
        )
    except Exception as exc:
        return _make_fail_closed_response(
            request,
            status="WORKER_CRASH",
            diagnostic_class="WORKER_CRASH",
            diagnostic_message=str(exc),
        )
    finally:
        if proc is not None:
            try:
                _kill_process_tree(proc)
            except Exception:
                pass


class _BytesReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def read(self, n: int) -> bytes:
        end = min(len(self._data), self._offset + n)
        chunk = self._data[self._offset : end]
        self._offset = end
        return chunk


def validate_worker_response_authority(
    request: WorkerRequestV1,
    response: WorkerResponseV1,
    entry: ToolRegistryEntry,
) -> None:
    for field in (
        "request_id",
        "tool_id",
        "tool_contract_hash",
        "registry_snapshot_hash",
        "worker_handler_id",
        "worker_build_identity",
        "isolation_profile",
        "action_digest",
        "policy_identity",
        "principal_identity",
    ):
        if getattr(request, field) != getattr(response, field):
            raise WorkerProtocolError(f"Worker response identity mismatch on {field}")
    if response.tool_contract_hash != entry.tool_contract_hash:
        raise WorkerProtocolError("Worker response tool contract hash mismatch")
    if response.result_size_bytes > entry.spec.max_output_bytes:
        raise WorkerProtocolError("Worker result exceeds max_output_bytes", code="OUTPUT_LIMIT")
    validate_output(response.result, entry.spec.output_schema)
