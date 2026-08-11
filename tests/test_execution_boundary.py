from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from sovereign_claw import worker_manifest
from sovereign_claw.execution_boundary import (
    DEFAULT_MAX_JSON_DEPTH,
    MANDATORY_BASELINE_PROPERTIES,
    SUBPROCESS_WORKER_BUILD_IDENTITY,
    SUBPROCESS_WORKER_HANDLER_REGISTRY_IDENTITY,
    WORKER_SCHEMA_VERSION,
    IsolationCapabilityMatrix,
    WorkerProtocolError,
    WorkerRequestV1,
    WorkerResponseV1,
    _kill_process_tree,
    canonical_json_digest_bounded,
    decode_framed_json,
    encode_framed_json,
    probe_hardened_container_seccomp_v1_capabilities,
    probe_subprocess_bounded_v1_capabilities,
    run_subprocess_bounded_v1,
    validate_worker_response_authority,
)
from sovereign_claw.orchestrator import Orchestrator
from sovereign_claw.proof_vault import ProofVault
from sovereign_claw.thermodynamics import TaskManifold
from sovereign_claw.tool_authority import ToolRegistry, canonical_json, make_registry_entry
from sovereign_claw.tools_basic import TOOL_SPEC_V1_ECHO


class _OnceToolBackend:
    def __init__(self, tool: str, kwargs: dict[str, object]) -> None:
        self._tool = tool
        self._kwargs = kwargs
        self._calls = 0

    def decide_next_action(self, objective, history, forbidden_actions, drift):
        self._calls += 1
        if self._calls == 1:
            return {"tool": self._tool, "kwargs": self._kwargs, "comment": "run"}
        return {"tool": "HALT", "kwargs": {}, "comment": "done"}


class _PreviewThenRunBackend:
    def __init__(self, tool: str, kwargs: dict[str, object]) -> None:
        self._tool = tool
        self._kwargs = kwargs
        self._calls = 0

    def decide_next_action(self, objective, history, forbidden_actions, drift):
        self._calls += 1
        if self._calls <= 2:
            return {"tool": self._tool, "kwargs": self._kwargs, "comment": "run"}
        return {"tool": "HALT", "kwargs": {}, "comment": "done"}


def _request(**overrides: object) -> WorkerRequestV1:
    base = WorkerRequestV1(
        schema_version=WORKER_SCHEMA_VERSION,
        request_id="req-1",
        trace_id="trace-1",
        correlation_id="corr-1",
        tool_id="builtin.echo_text",
        tool_contract_hash="h" * 64,
        registry_snapshot_hash="r" * 64,
        worker_handler_id="builtin.echo_text.in_process",
        worker_build_identity=SUBPROCESS_WORKER_BUILD_IDENTITY,
        isolation_profile="subprocess_bounded_v1",
        action_digest="a" * 64,
        policy_identity="p" * 64,
        principal_identity="i" * 64,
        principal_scopes=(),
        capabilities=(),
        args={"text": "hi"},
        deadline_ms=200,
        cpu_budget_ms=None,
        memory_bytes=None,
        max_processes=None,
        max_request_bytes=256 * 1024,
        max_response_bytes=256 * 1024,
        max_stdout_bytes=64 * 1024,
        max_stderr_bytes=64 * 1024,
        max_output_bytes=64 * 1024,
        postcondition_validator_id="__none__",
        postcondition_validator_version="__none__",
        evidence_policy="digest_only",
        redaction_policy="default",
    )
    if not overrides:
        return base
    return replace(base, **overrides)


def _success_identity_evidence() -> dict[str, str]:
    return {
        "worker_reported_build_identity": SUBPROCESS_WORKER_BUILD_IDENTITY,
        "worker_reported_registry_identity": SUBPROCESS_WORKER_HANDLER_REGISTRY_IDENTITY,
    }


class _FakePipe:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def read(self, n: int = -1) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeProc:
    def __init__(
        self,
        *,
        stdout_chunks: list[bytes],
        stderr_chunks: list[bytes] | None = None,
        returncode: int | None = 0,
    ) -> None:
        self.pid = 4242
        self.stdin = io.BytesIO()
        self.stdout = _FakePipe(stdout_chunks)
        self.stderr = _FakePipe(stderr_chunks or [b""])
        self.returncode = returncode
        self.terminate_called = False
        self.kill_called = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)  # type: ignore[name-defined]
        return int(self.returncode)

    def terminate(self) -> None:
        self.terminate_called = True
        if self.returncode is None:
            self.returncode = 143

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = 137


def test_worker_request_canonical_serialization_is_deterministic() -> None:
    req_a = _request(args={"z": [2, 1], "a": {"k": "v"}})
    req_b = _request(args={"a": {"k": "v"}, "z": [2, 1]})
    assert req_a.canonical_bytes() == req_b.canonical_bytes()


def test_worker_request_rejects_non_finite_json_value() -> None:
    bad = _request(args={"score": float("nan")})
    with pytest.raises(ValueError):
        _ = bad.canonical_bytes()


def test_worker_request_rejects_duplicate_json_keys() -> None:
    req = _request()
    payload = req.canonical_bytes().decode("utf-8")
    payload = payload.replace('"request_id":"req-1"', '"request_id":"req-1","request_id":"evil"', 1)
    with pytest.raises(WorkerProtocolError, match="Duplicate JSON key"):
        WorkerRequestV1.from_json_bytes(payload.encode("utf-8"))


def test_decode_framed_json_rejects_trailing_bytes() -> None:
    stream = io.BytesIO(encode_framed_json(b'{"ok":true}') + b"junk")
    with pytest.raises(WorkerProtocolError, match="Trailing bytes"):
        decode_framed_json(stream, max_bytes=1024, require_eof=True)


def test_subprocess_worker_unknown_handler_fails_closed() -> None:
    req = _request(worker_handler_id="unknown.handler")
    resp = run_subprocess_bounded_v1(req)
    assert resp.status == "PROTOCOL_ERROR"
    assert resp.request_id == req.request_id


def test_subprocess_worker_rejects_worker_build_mismatch() -> None:
    req = _request(worker_build_identity="OTHER_BUILD")
    resp = run_subprocess_bounded_v1(req)
    assert resp.status == "UNSUPPORTED_ISOLATION"


def test_worker_response_identity_mismatch_rejected() -> None:
    spec = replace(TOOL_SPEC_V1_ECHO, worker_build_identity=SUBPROCESS_WORKER_BUILD_IDENTITY)
    entry = make_registry_entry(spec)
    req = _request(
        tool_id=spec.tool_id,
        tool_contract_hash=entry.tool_contract_hash,
        worker_handler_id=entry.worker_handler_id,
    )
    resp = WorkerResponseV1.from_request(
        req,
        status="SUCCEEDED",
        result="ok",
        side_effect_evidence=_success_identity_evidence(),
    )
    tampered = replace(resp, request_id="other")
    with pytest.raises(WorkerProtocolError):
        validate_worker_response_authority(req, tampered, entry)


def test_worker_response_forged_result_size_is_rejected() -> None:
    spec = replace(TOOL_SPEC_V1_ECHO, worker_build_identity=SUBPROCESS_WORKER_BUILD_IDENTITY)
    entry = make_registry_entry(spec)
    req = _request(
        tool_id=spec.tool_id,
        tool_contract_hash=entry.tool_contract_hash,
        worker_handler_id=entry.worker_handler_id,
    )
    resp = WorkerResponseV1.from_request(
        req,
        status="SUCCEEDED",
        result="ok",
        side_effect_evidence=_success_identity_evidence(),
    )
    forged = replace(resp, result_size_bytes=1)
    with pytest.raises(WorkerProtocolError, match="result_size_bytes"):
        validate_worker_response_authority(req, forged, entry)


def test_worker_response_forged_result_hash_is_rejected() -> None:
    spec = replace(TOOL_SPEC_V1_ECHO, worker_build_identity=SUBPROCESS_WORKER_BUILD_IDENTITY)
    entry = make_registry_entry(spec)
    req = _request(
        tool_id=spec.tool_id,
        tool_contract_hash=entry.tool_contract_hash,
        worker_handler_id=entry.worker_handler_id,
    )
    resp = WorkerResponseV1.from_request(
        req,
        status="SUCCEEDED",
        result="ok",
        side_effect_evidence=_success_identity_evidence(),
    )
    forged = replace(resp, result_sha256="0" * 64)
    with pytest.raises(WorkerProtocolError, match="result_sha256"):
        validate_worker_response_authority(req, forged, entry)


def test_worker_response_failure_payload_skips_success_output_schema_validation() -> None:
    spec = replace(
        TOOL_SPEC_V1_ECHO,
        output_schema={"type": "string"},
        worker_build_identity=SUBPROCESS_WORKER_BUILD_IDENTITY,
    )
    entry = make_registry_entry(spec)
    req = _request(
        tool_id=spec.tool_id,
        tool_contract_hash=entry.tool_contract_hash,
        worker_handler_id=entry.worker_handler_id,
    )
    resp = WorkerResponseV1.from_request(req, status="TIMEOUT", result={})
    validate_worker_response_authority(req, resp, entry)


def test_worker_response_registry_identity_mismatch_rejected() -> None:
    spec = replace(TOOL_SPEC_V1_ECHO, worker_build_identity=SUBPROCESS_WORKER_BUILD_IDENTITY)
    entry = make_registry_entry(spec)
    req = _request(
        tool_id=spec.tool_id,
        tool_contract_hash=entry.tool_contract_hash,
        worker_handler_id=entry.worker_handler_id,
    )
    resp = WorkerResponseV1.from_request(
        req,
        status="SUCCEEDED",
        result="ok",
        side_effect_evidence={
            "worker_reported_build_identity": SUBPROCESS_WORKER_BUILD_IDENTITY,
            "worker_reported_registry_identity": "0" * 64,
        },
    )
    with pytest.raises(WorkerProtocolError, match="registry identity mismatch"):
        validate_worker_response_authority(req, resp, entry)


def test_worker_response_build_identity_mismatch_rejected() -> None:
    spec = replace(TOOL_SPEC_V1_ECHO, worker_build_identity=SUBPROCESS_WORKER_BUILD_IDENTITY)
    entry = make_registry_entry(spec)
    req = _request(
        tool_id=spec.tool_id,
        tool_contract_hash=entry.tool_contract_hash,
        worker_handler_id=entry.worker_handler_id,
    )
    resp = WorkerResponseV1.from_request(
        req,
        status="SUCCEEDED",
        result="ok",
        side_effect_evidence={
            "worker_reported_build_identity": "0" * 64,
            "worker_reported_registry_identity": SUBPROCESS_WORKER_HANDLER_REGISTRY_IDENTITY,
        },
    )
    with pytest.raises(WorkerProtocolError, match="build identity mismatch"):
        validate_worker_response_authority(req, resp, entry)


def test_worker_response_from_request_enforces_bounded_result_serialization() -> None:
    req = _request(max_output_bytes=128)
    huge = "x" * 4096
    with pytest.raises(WorkerProtocolError) as exc:
        WorkerResponseV1.from_request(req, status="SUCCEEDED", result={"text": huge})
    assert exc.value.code == "OUTPUT_LIMIT"


def test_worker_handler_registry_identity_changes_when_handler_module_bytes_change(
    monkeypatch,
) -> None:
    real_read = worker_manifest._read_module_artifact_bytes

    def _tampered_read(module_name: str) -> bytes:
        raw = real_read(module_name)
        if module_name == "sovereign_claw.tools_basic":
            return raw + b"\n# tampered\n"
        return raw

    monkeypatch.setattr(worker_manifest, "_read_module_artifact_bytes", _tampered_read)
    tampered = worker_manifest.compute_subprocess_worker_handler_registry_identity()
    assert tampered != SUBPROCESS_WORKER_HANDLER_REGISTRY_IDENTITY


def test_worker_build_identity_changes_when_worker_entrypoint_bytes_change(monkeypatch) -> None:
    real_read = worker_manifest._read_module_artifact_bytes

    def _tampered_read(module_name: str) -> bytes:
        raw = real_read(module_name)
        if module_name == "sovereign_claw.worker_entrypoint":
            return raw + b"\n# tampered\n"
        return raw

    monkeypatch.setattr(worker_manifest, "_read_module_artifact_bytes", _tampered_read)
    tampered_registry = worker_manifest.compute_subprocess_worker_handler_registry_identity()
    tampered_build = worker_manifest.compute_subprocess_worker_build_identity(tampered_registry)
    assert tampered_build != SUBPROCESS_WORKER_BUILD_IDENTITY


def test_canonical_json_digest_bounded_rejects_huge_list() -> None:
    huge_list = list(range(5000))
    with pytest.raises(WorkerProtocolError) as exc:
        canonical_json_digest_bounded(huge_list, max_bytes=256)
    assert exc.value.code in {"OUTPUT_LIMIT", "PROTOCOL_ERROR"}


def test_canonical_json_digest_bounded_rejects_huge_dict() -> None:
    huge_dict = {f"k{i}": i for i in range(5000)}
    with pytest.raises(WorkerProtocolError) as exc:
        canonical_json_digest_bounded(huge_dict, max_bytes=256)
    assert exc.value.code in {"OUTPUT_LIMIT", "PROTOCOL_ERROR"}


def test_canonical_json_digest_bounded_rejects_deep_structure_before_recursion_limit() -> None:
    deep: list[object] = []
    cursor: list[object] = deep
    for _ in range(DEFAULT_MAX_JSON_DEPTH + 4):
        nxt: list[object] = []
        cursor.append(nxt)
        cursor = nxt
    with pytest.raises(WorkerProtocolError, match="JSON depth exceeds"):
        canonical_json_digest_bounded(deep, max_bytes=32 * 1024)


def test_canonical_json_digest_bounded_rejects_cyclic_structure() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(WorkerProtocolError, match="Cyclic JSON container"):
        canonical_json_digest_bounded(cyclic, max_bytes=32 * 1024)


def test_worker_response_json_rejects_non_integer_numeric_fields() -> None:
    req = _request()
    resp = WorkerResponseV1.from_request(
        req,
        status="SUCCEEDED",
        result="ok",
        side_effect_evidence={
            "worker_reported_build_identity": SUBPROCESS_WORKER_BUILD_IDENTITY,
            "worker_reported_registry_identity": SUBPROCESS_WORKER_HANDLER_REGISTRY_IDENTITY,
        },
    )
    payload = resp.to_dict()
    payload["duration_ms"] = "12"
    with pytest.raises(WorkerProtocolError, match="duration_ms"):
        WorkerResponseV1.from_json_bytes(canonical_json(payload))
    payload = resp.to_dict()
    payload["result_size_bytes"] = True
    with pytest.raises(WorkerProtocolError, match="result_size_bytes"):
        WorkerResponseV1.from_json_bytes(canonical_json(payload))


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only process-group integration test")
def test_kill_process_tree_terminates_real_child_grandchild_group() -> None:
    script = (
        "import signal,subprocess,sys,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)']);"
        "time.sleep(120)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    time.sleep(0.2)
    _kill_process_tree(proc)
    proc.wait(timeout=2)
    assert proc.poll() is not None
    ps = subprocess.run(
        ["ps", "-o", "pid=", "-g", str(proc.pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert ps.stdout.strip() == ""


def test_subprocess_capability_matrix_invariant() -> None:
    matrix = probe_subprocess_bounded_v1_capabilities()
    if matrix.available:
        for prop in MANDATORY_BASELINE_PROPERTIES:
            assert getattr(matrix, prop) is True


def test_hardened_probe_unavailable_even_with_env_override(monkeypatch) -> None:
    monkeypatch.setenv("SOVEREIGN_CLAW_HARDENED_SELFTEST", "ok")
    matrix = probe_hardened_container_seccomp_v1_capabilities()
    assert matrix.available is False
    assert matrix.filesystem_isolation is False
    assert matrix.network_isolation is False


def test_env_minimization_drops_pythonpath_pythonhome_and_secret(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    req = _request()
    response = WorkerResponseV1.from_request(req, status="SUCCEEDED", result={"text": "ok"})
    framed = encode_framed_json(response.canonical_bytes())

    class _Proc:
        def __init__(self) -> None:
            self.pid = 1111
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(framed)
            self.stderr = io.BytesIO(b"")
            self.returncode = 0

        def poll(self) -> int | None:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            return

        def kill(self) -> None:
            return

    def _fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setenv("PYTHONPATH", "/tmp/evil")
    monkeypatch.setenv("PYTHONHOME", "/tmp/evilhome")
    monkeypatch.setenv("SECRET_SENTINEL", "super-secret")
    monkeypatch.setattr("sovereign_claw.execution_boundary.subprocess.Popen", _fake_popen)

    resp = run_subprocess_bounded_v1(req)
    assert resp.status == "SUCCEEDED"
    assert captured["close_fds"] is True
    assert "PYTHONPATH" not in captured["env"]
    assert "PYTHONHOME" not in captured["env"]
    assert "SECRET_SENTINEL" not in captured["env"]
    assert Path(captured["cwd"]).name.startswith("sovereign-claw-worker-")


def _write_descendant_tree_script(path: Path) -> None:
    path.write_text(
        "import os,signal,subprocess,sys,time\n"
        "pid_file=sys.argv[1]\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "with open(pid_file,'w',encoding='utf-8') as fh:\n"
        "    fh.write(str(os.getpid()))\n"
        "subprocess.Popen([\n"
        "    sys.executable,\n"
        "    '-c',\n"
        "    'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)'\n"
        "])\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )


def _assert_process_group_reaped(group_pid: int) -> None:
    ps = subprocess.run(
        ["ps", "-o", "pid=", "-g", str(group_pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert ps.stdout.strip() == ""


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only process-tree timeout integration test")
def test_timeout_run_subprocess_path_kills_and_reaps_real_descendant_tree(monkeypatch, tmp_path) -> None:
    pid_file = tmp_path / "timeout-tree.pid"
    script = tmp_path / "timeout_tree_worker.py"
    _write_descendant_tree_script(script)
    monkeypatch.setattr(
        "sovereign_claw.execution_boundary.WORKER_ENTRYPOINT_COMMAND",
        (sys.executable, str(script), str(pid_file)),
    )
    req = _request(deadline_ms=30)
    resp = run_subprocess_bounded_v1(req)
    assert resp.status == "TIMEOUT"
    group_pid = int(pid_file.read_text(encoding="utf-8"))
    time.sleep(0.1)
    _assert_process_group_reaped(group_pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only process-tree cancel integration test")
def test_cancel_run_subprocess_path_kills_and_reaps_real_descendant_tree(monkeypatch, tmp_path) -> None:
    pid_file = tmp_path / "cancel-tree.pid"
    script = tmp_path / "cancel_tree_worker.py"
    _write_descendant_tree_script(script)
    monkeypatch.setattr(
        "sovereign_claw.execution_boundary.WORKER_ENTRYPOINT_COMMAND",
        (sys.executable, str(script), str(pid_file)),
    )

    started = time.monotonic()
    req = _request(deadline_ms=1000)
    resp = run_subprocess_bounded_v1(req, cancel_requested=lambda: time.monotonic() - started > 0.05)
    assert resp.status == "CANCELLED"
    group_pid = int(pid_file.read_text(encoding="utf-8"))
    time.sleep(0.1)
    _assert_process_group_reaped(group_pid)


def test_output_flood_on_stdout_fails_closed_and_kills_tree(monkeypatch) -> None:
    req = _request(max_response_bytes=16)
    proc = _FakeProc(stdout_chunks=[b"x" * 2048], returncode=None)
    killed = {"called": False}

    def _fake_kill(_proc) -> None:
        killed["called"] = True
        _proc.returncode = 137

    monkeypatch.setattr("sovereign_claw.execution_boundary._kill_process_tree", _fake_kill)
    monkeypatch.setattr("sovereign_claw.execution_boundary.subprocess.Popen", lambda *a, **k: proc)

    resp = run_subprocess_bounded_v1(req)
    assert resp.status == "OUTPUT_LIMIT"
    assert killed["called"] is True


def test_timeout_fails_closed_and_kills_tree(monkeypatch) -> None:
    req = _request(deadline_ms=1)
    proc = _FakeProc(stdout_chunks=[b""], returncode=None)
    killed = {"called": False}

    def _fake_kill(_proc) -> None:
        killed["called"] = True
        _proc.returncode = 137

    monkeypatch.setattr("sovereign_claw.execution_boundary._kill_process_tree", _fake_kill)
    monkeypatch.setattr("sovereign_claw.execution_boundary.subprocess.Popen", lambda *a, **k: proc)

    resp = run_subprocess_bounded_v1(req)
    assert resp.status == "TIMEOUT"
    assert killed["called"] is True


def test_cancelled_execution_suppresses_late_success(monkeypatch) -> None:
    req = _request()
    response = WorkerResponseV1.from_request(req, status="SUCCEEDED", result={"text": "ok"})
    proc = _FakeProc(
        stdout_chunks=[encode_framed_json(response.canonical_bytes())], returncode=None
    )
    killed = {"called": False}

    def _fake_kill(_proc) -> None:
        killed["called"] = True
        _proc.returncode = 137

    monkeypatch.setattr("sovereign_claw.execution_boundary._kill_process_tree", _fake_kill)
    monkeypatch.setattr("sovereign_claw.execution_boundary.subprocess.Popen", lambda *a, **k: proc)

    resp = run_subprocess_bounded_v1(req, cancel_requested=lambda: True)
    assert resp.status == "CANCELLED"
    assert killed["called"] is True


def test_unenforceable_resource_limit_fails_closed_without_launch(monkeypatch) -> None:
    req = _request(cpu_budget_ms=100)
    matrix = IsolationCapabilityMatrix(
        profile_id="subprocess_bounded_v1",
        wall_deadline=True,
        process_tree_kill=True,
        cpu_limit=False,
        memory_limit=True,
        process_count_limit=False,
        filesystem_isolation=False,
        network_isolation=False,
        available=True,
    )
    monkeypatch.setattr(
        "sovereign_claw.execution_boundary.probe_subprocess_bounded_v1_capabilities",
        lambda: matrix,
    )
    monkeypatch.setattr(
        "sovereign_claw.execution_boundary.subprocess.Popen",
        lambda *a, **k: pytest.fail("Popen should not be called"),
    )
    resp = run_subprocess_bounded_v1(req)
    assert resp.status == "UNSUPPORTED_ISOLATION"


def test_governed_nontrusted_profile_does_not_fallback_to_in_process() -> None:
    spec = replace(
        TOOL_SPEC_V1_ECHO,
        isolation_profile="sandbox",
        worker_handler_id="builtin.echo_text.sandbox",
        worker_build_identity=SUBPROCESS_WORKER_BUILD_IDENTITY,
    )
    entry = make_registry_entry(spec, trusted_execution_class=None)
    registry = ToolRegistry()
    registry.register(entry)
    call_count = {"n": 0}

    def _handler(text: str) -> str:
        call_count["n"] += 1
        return text

    orch = Orchestrator(
        llm_backend=_OnceToolBackend(tool=spec.tool_id, kwargs={"text": "hello"}),
        tool_registry=registry,
    )
    orch.register_governed_handler(entry.worker_handler_id, _handler)

    preview = orch.preview(TaskManifold(objective="demo", t_max_steps=2))
    receipt = orch.execute(
        TaskManifold(
            objective="demo",
            t_max_steps=2,
            metadata={"approved_action_digest": preview["action_digest"]},
        )
    )
    assert call_count["n"] == 0
    assert orch.shield.execution_log() == []
    assert receipt.halt_reason is not None


def test_governed_orchestrator_worker_success_path(tmp_path) -> None:
    spec = replace(
        TOOL_SPEC_V1_ECHO,
        isolation_profile="subprocess_bounded_v1",
        worker_build_identity=SUBPROCESS_WORKER_BUILD_IDENTITY,
    )
    entry = make_registry_entry(spec, trusted_execution_class=None)
    registry = ToolRegistry()
    registry.register(entry)
    orch = Orchestrator(
        llm_backend=_PreviewThenRunBackend(tool=spec.tool_id, kwargs={"text": "hello"}),
        tool_registry=registry,
        vault=ProofVault(db_path=tmp_path / "proof-vault.sqlite3"),
    )
    # Required immutable server-owned handler binding; non-trusted execution still uses worker path.
    orch.register_governed_handler(entry.worker_handler_id, lambda text: f"UNUSED-{text}")

    preview = orch.preview(TaskManifold(objective="demo", t_max_steps=2))
    receipt = orch.execute(
        TaskManifold(
            objective="demo",
            t_max_steps=2,
            metadata={"approved_action_digest": preview["action_digest"]},
        )
    )
    assert receipt.halt_reason == "APPROVAL_SCOPE_EXHAUSTED"
    assert orch.shield.execution_log() == []

    steps = orch.vault.get_trace_steps(receipt.trace_id)
    assert any(
        step.action == "TOOL:builtin.echo_text" and step.payload["success"] for step in steps
    )

    events = orch.vault.get_evidence_records(receipt.trace_id)
    event_types = [e.evidence_type for e in events]
    assert "authority.tool.dispatch.launch" in event_types
    assert "authority.tool.dispatch.terminal" in event_types


def test_governed_orchestrator_rejects_stale_approved_worker_build_identity_before_launch(
    tmp_path,
) -> None:
    stale_identity = "0" * 64
    spec = replace(
        TOOL_SPEC_V1_ECHO,
        isolation_profile="subprocess_bounded_v1",
        worker_build_identity=stale_identity,
    )
    entry = make_registry_entry(spec, trusted_execution_class=None)
    registry = ToolRegistry()
    registry.register(entry)
    orch = Orchestrator(
        llm_backend=_PreviewThenRunBackend(tool=spec.tool_id, kwargs={"text": "hello"}),
        tool_registry=registry,
        vault=ProofVault(db_path=tmp_path / "proof-vault.sqlite3"),
    )
    orch.register_governed_handler(entry.worker_handler_id, lambda text: f"UNUSED-{text}")

    preview = orch.preview(TaskManifold(objective="demo", t_max_steps=2))
    receipt = orch.execute(
        TaskManifold(
            objective="demo",
            t_max_steps=2,
            metadata={"approved_action_digest": preview["action_digest"]},
        )
    )
    assert receipt.halt_reason == "TOOL_CONTRACT_CHANGED"
    events = orch.vault.get_evidence_records(receipt.trace_id)
    event_types = [e.evidence_type for e in events]
    assert "authority.tool.dispatch.launch" not in event_types


def test_governed_orchestrator_persists_terminal_event_on_protocol_validation_failure(
    monkeypatch, tmp_path
) -> None:
    spec = replace(
        TOOL_SPEC_V1_ECHO,
        isolation_profile="subprocess_bounded_v1",
        worker_build_identity=SUBPROCESS_WORKER_BUILD_IDENTITY,
    )
    entry = make_registry_entry(spec, trusted_execution_class=None)
    registry = ToolRegistry()
    registry.register(entry)
    orch = Orchestrator(
        llm_backend=_PreviewThenRunBackend(tool=spec.tool_id, kwargs={"text": "hello"}),
        tool_registry=registry,
        vault=ProofVault(db_path=tmp_path / "proof-vault.sqlite3"),
    )
    orch.register_governed_handler(entry.worker_handler_id, lambda text: f"UNUSED-{text}")

    real_runner = run_subprocess_bounded_v1

    def _tampering_runner(request: WorkerRequestV1):
        response = real_runner(request)
        return replace(response, request_id="tampered")

    monkeypatch.setattr("sovereign_claw.orchestrator.run_subprocess_bounded_v1", _tampering_runner)

    preview = orch.preview(TaskManifold(objective="demo", t_max_steps=2))
    receipt = orch.execute(
        TaskManifold(
            objective="demo",
            t_max_steps=2,
            metadata={"approved_action_digest": preview["action_digest"]},
        )
    )
    events = orch.vault.get_evidence_records(receipt.trace_id)
    terminal = [e for e in events if e.evidence_type == "authority.tool.dispatch.terminal"]
    assert terminal
