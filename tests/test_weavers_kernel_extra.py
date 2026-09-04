from __future__ import annotations

from pathlib import Path

from sovereign_claw.weavers_kernel import WeaversKernel


class DummyGlyph:
    def __init__(self, glyph_id: str = "glyph-1"):
        self.glyph_id = glyph_id
        self.xr_vector = (0.0, 0.0, 1.0)
        self.morph_weight = 0.5


class DummySessionRecord:
    def __init__(self, session_id: str = "session-1"):
        self.session_id = session_id


class DummyGardeners:
    def __init__(self):
        self.plant_calls = []
        self.record_calls = []
        self.progress_calls = []
        self.maintenance_calls = 0

    def plant_skill(self, **kwargs):
        self.plant_calls.append(kwargs)
        return "scroll-1"

    def record_session(self, **kwargs):
        self.record_calls.append(kwargs)
        return DummySessionRecord()

    def get_learner_progress(self, learner_id):
        self.progress_calls.append(learner_id)
        return [{"learner_id": learner_id, "skill": "testing"}]

    def run_wilt_check(self):
        self.maintenance_calls += 1
        return ["scroll-1"]


class DummyVault:
    def __init__(self):
        self.trace_calls = []
        self.appended_steps = []
        self.reputation_updates = []
        self.weight_calls = []

    def create_trace(self, objective, meta):
        self.trace_calls.append({"objective": objective, "meta": meta})
        return "trace-1"

    def append_step(self, step):
        self.appended_steps.append(step)

    def update_agent_reputation(self, agent_id, step_drift):
        self.reputation_updates.append({"agent_id": agent_id, "step_drift": step_drift})

    def get_agent_reputation_weight(self, agent_id, k=1.0):
        self.weight_calls.append({"agent_id": agent_id, "k": k})
        return 0.77


class DummyNeuro:
    def __init__(self):
        self.dongba_calls = []
        self.elfe_calls = []
        self.quipu_router = self

    def dongba_morph(self, skill_state, skill_name="skill"):
        self.dongba_calls.append({"skill_state": skill_state, "skill_name": skill_name})
        return DummyGlyph(glyph_id=f"glyph-{len(self.dongba_calls)}")

    def elfe_step(self, skill_state, session_quality=0.0):
        self.elfe_calls.append({"skill_state": skill_state, "session_quality": session_quality})
        return 0.8, 0.12

    def route(self, skill_state, skill_name="skill"):
        return {
            "target_node": 0.75,
            "target_name": "Virtuoso",
            "intervention": "DELIBERATE_PRACTICE",
        }


class DummyGardenersProtocol:
    created_instances = []

    def __init__(self, db_path=Path("gardeners.sqlite3")):
        self.db_path = db_path
        self.impl = DummyGardeners()
        DummyGardenersProtocol.created_instances.append(self)

    def plant_skill(self, **kwargs):
        return self.impl.plant_skill(**kwargs)

    def record_session(self, **kwargs):
        return self.impl.record_session(**kwargs)

    def get_learner_progress(self, learner_id):
        return self.impl.get_learner_progress(learner_id)

    def run_wilt_check(self):
        return self.impl.run_wilt_check()


def make_kernel(monkeypatch):
    DummyGardenersProtocol.created_instances.clear()

    vault = DummyVault()
    neuro = DummyNeuro()

    monkeypatch.setattr(
        "sovereign_claw.weavers_kernel.GardenersProtocol",
        DummyGardenersProtocol,
    )
    monkeypatch.setattr(
        "sovereign_claw.weavers_kernel.MythicNeuroKernel",
        lambda **kwargs: neuro,
    )
    monkeypatch.setattr(
        "sovereign_claw.weavers_kernel.seal_with_build_fingerprint",
        lambda meta: {"sealed": True, **meta},
    )

    kernel = WeaversKernel(vault=vault)
    gardeners_protocol = DummyGardenersProtocol.created_instances[-1]
    gardeners = gardeners_protocol.impl
    return kernel, gardeners, gardeners_protocol, vault, neuro


def test_init_uses_default_path_when_no_gardeners_db(monkeypatch):
    class RecordingGardenersProtocol:
        created_instances = []

        def __init__(self, db_path=Path("gardeners.sqlite3")):
            self.db_path = db_path
            RecordingGardenersProtocol.created_instances.append(self)

        def plant_skill(self, **kwargs):
            return "scroll-1"

        def record_session(self, **kwargs):
            return DummySessionRecord()

        def get_learner_progress(self, learner_id):
            return []

        def run_wilt_check(self):
            return []

    monkeypatch.setattr(
        "sovereign_claw.weavers_kernel.GardenersProtocol",
        RecordingGardenersProtocol,
    )
    monkeypatch.setattr(
        "sovereign_claw.weavers_kernel.MythicNeuroKernel",
        lambda **kwargs: DummyNeuro(),
    )

    kernel = WeaversKernel(vault=DummyVault())

    assert isinstance(kernel, WeaversKernel)
    assert RecordingGardenersProtocol.created_instances[-1].db_path == Path("gardeners.sqlite3")


def test_init_uses_explicit_gardeners_db(monkeypatch):
    explicit_path = Path("custom-gardeners.sqlite3")

    class RecordingGardenersProtocol:
        created_instances = []

        def __init__(self, db_path=Path("gardeners.sqlite3")):
            self.db_path = db_path
            RecordingGardenersProtocol.created_instances.append(self)

        def plant_skill(self, **kwargs):
            return "scroll-1"

        def record_session(self, **kwargs):
            return DummySessionRecord()

        def get_learner_progress(self, learner_id):
            return []

        def run_wilt_check(self):
            return []

    monkeypatch.setattr(
        "sovereign_claw.weavers_kernel.GardenersProtocol",
        RecordingGardenersProtocol,
    )
    monkeypatch.setattr(
        "sovereign_claw.weavers_kernel.MythicNeuroKernel",
        lambda **kwargs: DummyNeuro(),
    )

    kernel = WeaversKernel(vault=DummyVault(), gardeners_db=explicit_path)

    assert isinstance(kernel, WeaversKernel)
    assert RecordingGardenersProtocol.created_instances[-1].db_path == explicit_path


def test_accelerate_skill_plants_scroll_and_records_session(monkeypatch):
    kernel, gardeners, gardeners_protocol, vault, neuro = make_kernel(monkeypatch)

    receipt = kernel.accelerate_skill(
        skill_state=0.4,
        coach_quality=0.8,
        learner_quality=0.6,
        skill_name="python",
        learner_id="learner-1",
        notes="test run",
    )

    assert receipt.scroll_id == "scroll-1"
    assert receipt.vault_trace_id == "trace-1"
    assert receipt.session_id == "session-1"
    assert receipt.target_node == 0.75
    assert receipt.target_name == "Virtuoso"
    assert receipt.intervention_next == "DELIBERATE_PRACTICE"
    assert receipt.glyph_id == "glyph-2"
    assert receipt.bloomed is True
    assert receipt.lane_status == "UNVERIFIED_CONVERGENCE"

    assert len(gardeners.plant_calls) == 1
    assert len(gardeners.record_calls) == 1
    assert len(vault.trace_calls) == 1
    assert len(vault.appended_steps) == 1

    trace_meta = vault.trace_calls[0]["meta"]
    assert trace_meta["sealed"] is True
    assert trace_meta["skill_name"] == "python"
    assert trace_meta["learner_id"] == "learner-1"

    step = vault.appended_steps[0]
    assert step.status == "UNVERIFIED_CONVERGENCE"
    payload = step.payload
    assert payload["scroll_id"] == "scroll-1"
    assert payload["bloomed"] is True
    assert payload["closure_authority"] is False


def test_accelerate_skill_uses_existing_scroll_id_without_planting(monkeypatch):
    kernel, gardeners, gardeners_protocol, vault, neuro = make_kernel(monkeypatch)

    receipt = kernel.accelerate_skill(
        skill_state=0.25,
        coach_quality=0.7,
        learner_quality=0.7,
        scroll_id="existing-scroll",
    )

    assert receipt.scroll_id == "existing-scroll"
    assert gardeners.plant_calls == []
    assert len(gardeners.record_calls) == 1
    assert gardeners.record_calls[0]["scroll_id"] == "existing-scroll"


def test_accelerate_skill_uses_peer_synthesis_on_high_disagreement(monkeypatch):
    kernel, gardeners, gardeners_protocol, vault, neuro = make_kernel(monkeypatch)

    receipt = kernel.accelerate_skill(
        skill_state=0.3,
        coach_quality=1.0,
        learner_quality=0.2,
    )

    assert receipt.intervention_next == "PEER_SYNTHESIS"
    assert gardeners.record_calls[0]["intervention_type"] == "PEER_SYNTHESIS"
    assert vault.trace_calls[0]["meta"]["disagreement"] == 0.8


def test_accelerate_skill_updates_coach_reputation_when_coach_id_present(monkeypatch):
    kernel, gardeners, gardeners_protocol, vault, neuro = make_kernel(monkeypatch)

    kernel.accelerate_skill(
        skill_state=0.5,
        coach_quality=0.9,
        learner_quality=0.8,
        coach_id="coach-1",
    )

    assert len(vault.reputation_updates) == 1
    assert vault.reputation_updates[0]["agent_id"] == "coach:coach-1"


def test_get_learner_progress_delegates_to_gardeners(monkeypatch):
    kernel, gardeners, gardeners_protocol, vault, neuro = make_kernel(monkeypatch)

    result = kernel.get_learner_progress("learner-42")

    assert result == [{"learner_id": "learner-42", "skill": "testing"}]
    assert gardeners.progress_calls == ["learner-42"]


def test_get_coach_weight_delegates_to_vault(monkeypatch):
    kernel, gardeners, gardeners_protocol, vault, neuro = make_kernel(monkeypatch)

    result = kernel.get_coach_weight("coach-9", k=2.5)

    assert result == 0.77
    assert vault.weight_calls == [{"agent_id": "coach:coach-9", "k": 2.5}]


def test_run_garden_maintenance_delegates_to_gardeners(monkeypatch):
    kernel, gardeners, gardeners_protocol, vault, neuro = make_kernel(monkeypatch)

    result = kernel.run_garden_maintenance()

    assert result == ["scroll-1"]
    assert gardeners.maintenance_calls == 1
