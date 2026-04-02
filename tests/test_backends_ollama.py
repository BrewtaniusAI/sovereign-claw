import json

from sovereign_claw.backends_ollama import (
    CypherOllama,
    RabbitOllama,
    _OllamaBase,
    _parse_action_json,
)


class DummyResponse:
    def __init__(self, payload, should_raise=False):
        self._payload = payload
        self._should_raise = should_raise

    def raise_for_status(self):
        if self._should_raise:
            raise RuntimeError("boom")

    def json(self):
        return self._payload


def test_parse_action_json_extracts_embedded_json():
    text = 'prefix {"tool": "scan", "kwargs": {"x": 1}, "comment": "ok"} suffix'
    result = _parse_action_json(text)
    assert result["tool"] == "scan"
    assert result["kwargs"] == {"x": 1}
    assert result["comment"] == "ok"


def test_parse_action_json_halts_when_tool_missing():
    text = '{"kwargs": {}, "comment": "missing tool"}'
    result = _parse_action_json(text)
    assert result == {
        "tool": "HALT",
        "kwargs": {},
        "comment": "Parse failure; halting.",
    }


def test_parse_action_json_halts_on_invalid_json():
    result = _parse_action_json("not json at all")
    assert result == {
        "tool": "HALT",
        "kwargs": {},
        "comment": "Parse failure; halting.",
    }


def test_ollama_chat_success(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return DummyResponse(
            {"message": {"content": '{"tool":"draft","kwargs":{},"comment":"ok"}'}}
        )

    monkeypatch.setattr("sovereign_claw.backends_ollama.httpx.post", fake_post)

    backend = RabbitOllama(model="llama-test", host="http://ollama.local/", timeout=12.5)
    result = backend._chat("hello world")

    assert json.loads(result)["tool"] == "draft"
    assert captured["url"] == "http://ollama.local/api/chat"
    assert captured["timeout"] == 12.5
    assert captured["json"]["model"] == "llama-test"
    assert captured["json"]["stream"] is False
    assert captured["json"]["messages"][0]["role"] == "system"
    assert captured["json"]["messages"][1] == {"role": "user", "content": "hello world"}


def test_ollama_chat_returns_halt_on_http_exception(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr("sovereign_claw.backends_ollama.httpx.post", fake_post)

    backend = RabbitOllama()
    result = json.loads(backend._chat("hello"))

    assert result["tool"] == "HALT"
    assert result["kwargs"] == {}
    assert "Ollama error: TimeoutError" in result["comment"]


def test_ollama_chat_returns_halt_when_raise_for_status_fails(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        return DummyResponse({}, should_raise=True)

    monkeypatch.setattr("sovereign_claw.backends_ollama.httpx.post", fake_post)

    backend = RabbitOllama()
    result = json.loads(backend._chat("hello"))

    assert result["tool"] == "HALT"
    assert "Ollama error: RuntimeError" in result["comment"]


def test_decide_next_action_adds_rabbit_agent_id(monkeypatch):
    def fake_chat(self, user_content):
        assert "OBJECTIVE: build safely" in user_content
        assert "CURRENT DRIFT: 0.2500" in user_content
        assert "FORBIDDEN: ['rm -rf']" in user_content
        assert '"step": 2' not in user_content  # only last 3 history entries
        assert '"step": 3' in user_content
        assert '"step": 4' in user_content
        assert '"step": 5' in user_content
        return '{"tool":"propose","kwargs":{"a":1},"comment":"draft"}'

    monkeypatch.setattr(_OllamaBase, "_chat", fake_chat)

    backend = RabbitOllama()
    result = backend.decide_next_action(
        objective="build safely",
        history=[{"step": 1}, {"step": 2}, {"step": 3}, {"step": 4}, {"step": 5}],
        forbidden_actions=["rm -rf"],
        drift=0.25,
    )

    assert result["tool"] == "propose"
    assert result["kwargs"] == {"a": 1}
    assert result["comment"] == "draft"
    assert result["agent_id"] == "rabbitollama"


def test_decide_next_action_halts_on_parse_failure(monkeypatch):
    monkeypatch.setattr(_OllamaBase, "_chat", lambda self, user_content: "totally invalid response")

    backend = RabbitOllama()
    result = backend.decide_next_action(
        objective="build safely",
        history=[],
        forbidden_actions=[],
        drift=0.0,
    )

    assert result["tool"] == "HALT"
    assert result["kwargs"] == {}
    assert result["comment"] == "Parse failure; halting."
    assert result["agent_id"] == "rabbitollama"


def test_cypher_has_expected_defaults():
    backend = CypherOllama()
    assert backend.model == "llama3"
    assert backend.host == "http://localhost:11434"
    assert backend.timeout == 30.0
    assert "Cypher" in backend.system_prompt
    assert "ok" in backend.system_prompt


def test_rabbit_has_expected_defaults():
    backend = RabbitOllama()
    assert backend.model == "llama3"
    assert backend.host == "http://localhost:11434"
    assert backend.timeout == 30.0
    assert "Rabbit" in backend.system_prompt
    assert "valid JSON" in backend.system_prompt
