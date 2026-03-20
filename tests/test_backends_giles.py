from sovereign_claw.backends_giles import (
    GilesTiered,
    GilesTieredConfig,
    ProviderConfig,
    _call_anthropic,
    _call_gemini,
    _call_openai,
    _call_perplexity,
)


class DummyResponse:
    def __init__(self, payload, should_raise=False):
        self._payload = payload
        self._should_raise = should_raise

    def raise_for_status(self):
        if self._should_raise:
            raise RuntimeError("bad status")

    def json(self):
        return self._payload


def test_giles_tiered_config_providers_filters_none():
    config = GilesTieredConfig(
        primary=ProviderConfig(name="openai", api_key="k1", model="m1"),
        secondary=None,
        tertiary=ProviderConfig(name="gemini", api_key="k3", model="m3"),
    )

    providers = config.providers()

    assert len(providers) == 2
    assert providers[0].name == "openai"
    assert providers[1].name == "gemini"


def test_call_anthropic_success(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return DummyResponse({"content": [{"text": "anthropic says hi"}]})

    monkeypatch.setattr("sovereign_claw.backends_giles.httpx.post", fake_post)

    cfg = ProviderConfig(name="anthropic", api_key="secret", model="claude-x", timeout=11.0)
    result = _call_anthropic(cfg, "prompt text")

    assert result == "anthropic says hi"
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "secret"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["json"]["model"] == "claude-x"
    assert captured["json"]["messages"] == [{"role": "user", "content": "prompt text"}]
    assert captured["timeout"] == 11.0


def test_call_openai_success(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return DummyResponse({"choices": [{"message": {"content": "openai says hi"}}]})

    monkeypatch.setattr("sovereign_claw.backends_giles.httpx.post", fake_post)

    cfg = ProviderConfig(name="openai", api_key="secret", model="gpt-x", timeout=9.0)
    result = _call_openai(cfg, "prompt text")

    assert result == "openai says hi"
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["json"]["model"] == "gpt-x"
    assert captured["json"]["messages"] == [{"role": "user", "content": "prompt text"}]
    assert captured["timeout"] == 9.0


def test_call_gemini_success(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return DummyResponse({"candidates": [{"content": {"parts": [{"text": "gemini says hi"}]}}]})

    monkeypatch.setattr("sovereign_claw.backends_giles.httpx.post", fake_post)

    cfg = ProviderConfig(name="gemini", api_key="secret", model="gemini-x", timeout=7.5)
    result = _call_gemini(cfg, "prompt text")

    assert result == "gemini says hi"
    assert "https://generativelanguage.googleapis.com/v1beta/models/gemini-x:generateContent?key=secret" == captured["url"]
    assert captured["json"] == {"contents": [{"parts": [{"text": "prompt text"}]}]}
    assert captured["timeout"] == 7.5


def test_call_perplexity_success(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return DummyResponse({"choices": [{"message": {"content": "perplexity says hi"}}]})

    monkeypatch.setattr("sovereign_claw.backends_giles.httpx.post", fake_post)

    cfg = ProviderConfig(name="perplexity", api_key="secret", model="sonar", timeout=14.0)
    result = _call_perplexity(cfg, "prompt text")

    assert result == "perplexity says hi"
    assert captured["url"] == "https://api.perplexity.ai/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["json"]["model"] == "sonar"
    assert captured["json"]["messages"] == [{"role": "user", "content": "prompt text"}]
    assert captured["timeout"] == 14.0


def test_provider_calls_return_none_on_exception(monkeypatch):
    def fake_post(*args, **kwargs):
        raise TimeoutError("nope")

    monkeypatch.setattr("sovereign_claw.backends_giles.httpx.post", fake_post)

    cfg = ProviderConfig(name="openai", api_key="secret", model="gpt")
    assert _call_anthropic(cfg, "prompt") is None
    assert _call_openai(cfg, "prompt") is None
    assert _call_gemini(cfg, "prompt") is None
    assert _call_perplexity(cfg, "prompt") is None


def test_giles_uses_primary_provider_on_success(monkeypatch):
    config = GilesTieredConfig(
        primary=ProviderConfig(name="openai", api_key="k1", model="m1"),
        secondary=ProviderConfig(name="gemini", api_key="k2", model="m2"),
    )
    backend = GilesTiered(config)

    monkeypatch.setattr(
        "sovereign_claw.backends_giles._call_openai",
        lambda cfg, prompt: '{"tool":"approve","kwargs":{"x":1},"comment":"ok"}',
    )
    monkeypatch.setattr(
        "sovereign_claw.backends_giles._call_gemini",
        lambda cfg, prompt: '{"tool":"reject","kwargs":{},"comment":"should not be used"}',
    )
    monkeypatch.setitem(
        __import__("sovereign_claw.backends_giles", fromlist=["_PROVIDER_DISPATCH"])._PROVIDER_DISPATCH,
        "openai",
        __import__("sovereign_claw.backends_giles", fromlist=["_call_openai"])._call_openai,
    )
    monkeypatch.setitem(
        __import__("sovereign_claw.backends_giles", fromlist=["_PROVIDER_DISPATCH"])._PROVIDER_DISPATCH,
        "gemini",
        __import__("sovereign_claw.backends_giles", fromlist=["_call_gemini"])._call_gemini,
    )

    result = backend.decide_next_action(
        objective="stabilize pipeline",
        history=[{"n": 1}],
        forbidden_actions=["delete"],
        drift=0.1,
    )

    assert result["tool"] == "approve"
    assert result["kwargs"] == {"x": 1}
    assert result["comment"] == "ok"
    assert result["agent_id"] == "giles"
    assert result["provider"] == "openai"


def test_giles_falls_back_to_secondary_on_primary_failure(monkeypatch):
    config = GilesTieredConfig(
        primary=ProviderConfig(name="openai", api_key="k1", model="m1"),
        secondary=ProviderConfig(name="gemini", api_key="k2", model="m2"),
    )
    backend = GilesTiered(config)

    def fail_primary(cfg, prompt):
        return None

    def succeed_secondary(cfg, prompt):
        assert "ENVELOPE: stabilize pipeline" in prompt
        assert "DRIFT: 0.3300" in prompt
        assert "FORBIDDEN: ['delete']" in prompt
        return '{"tool":"approve","kwargs":{"provider":"secondary"},"comment":"fallback ok"}'

    monkeypatch.setattr("sovereign_claw.backends_giles._call_openai", fail_primary)
    monkeypatch.setattr("sovereign_claw.backends_giles._call_gemini", succeed_secondary)

    module = __import__("sovereign_claw.backends_giles", fromlist=["_PROVIDER_DISPATCH"])
    monkeypatch.setitem(module._PROVIDER_DISPATCH, "openai", module._call_openai)
    monkeypatch.setitem(module._PROVIDER_DISPATCH, "gemini", module._call_gemini)

    result = backend.decide_next_action(
        objective="stabilize pipeline",
        history=[{"n": 1}, {"n": 2}, {"n": 3}, {"n": 4}],
        forbidden_actions=["delete"],
        drift=0.33,
    )

    assert result["tool"] == "approve"
    assert result["kwargs"] == {"provider": "secondary"}
    assert result["comment"] == "fallback ok"
    assert result["agent_id"] == "giles"
    assert result["provider"] == "gemini"


def test_giles_skips_unknown_provider_and_uses_next(monkeypatch):
    config = GilesTieredConfig(
        primary=ProviderConfig(name="unknown", api_key="k1", model="m1"),
        secondary=ProviderConfig(name="perplexity", api_key="k2", model="m2"),
    )
    backend = GilesTiered(config)

    monkeypatch.setattr(
        "sovereign_claw.backends_giles._call_perplexity",
        lambda cfg, prompt: '{"tool":"route","kwargs":{},"comment":"used known provider"}',
    )
    module = __import__("sovereign_claw.backends_giles", fromlist=["_PROVIDER_DISPATCH"])
    monkeypatch.setitem(module._PROVIDER_DISPATCH, "perplexity", module._call_perplexity)

    result = backend.decide_next_action(
        objective="route request",
        history=[],
        forbidden_actions=[],
        drift=0.0,
    )

    assert result["tool"] == "route"
    assert result["agent_id"] == "giles"
    assert result["provider"] == "perplexity"


def test_giles_returns_halt_when_all_providers_fail(monkeypatch):
    config = GilesTieredConfig(
        primary=ProviderConfig(name="openai", api_key="k1", model="m1"),
        secondary=ProviderConfig(name="gemini", api_key="k2", model="m2"),
        tertiary=ProviderConfig(name="perplexity", api_key="k3", model="m3"),
    )
    backend = GilesTiered(config)

    monkeypatch.setattr("sovereign_claw.backends_giles._call_openai", lambda cfg, prompt: None)
    monkeypatch.setattr("sovereign_claw.backends_giles._call_gemini", lambda cfg, prompt: None)
    monkeypatch.setattr("sovereign_claw.backends_giles._call_perplexity", lambda cfg, prompt: None)

    module = __import__("sovereign_claw.backends_giles", fromlist=["_PROVIDER_DISPATCH"])
    monkeypatch.setitem(module._PROVIDER_DISPATCH, "openai", module._call_openai)
    monkeypatch.setitem(module._PROVIDER_DISPATCH, "gemini", module._call_gemini)
    monkeypatch.setitem(module._PROVIDER_DISPATCH, "perplexity", module._call_perplexity)

    result = backend.decide_next_action(
        objective="route request",
        history=[],
        forbidden_actions=[],
        drift=0.0,
    )

    assert result == {
        "tool": "HALT",
        "kwargs": {},
        "comment": "All Giles providers failed; halting under Silence Clause.",
        "agent_id": "giles",
    }


def test_giles_parses_invalid_provider_output_as_halt(monkeypatch):
    config = GilesTieredConfig(
        primary=ProviderConfig(name="openai", api_key="k1", model="m1"),
    )
    backend = GilesTiered(config)

    monkeypatch.setattr(
        "sovereign_claw.backends_giles._call_openai",
        lambda cfg, prompt: "this is not valid json",
    )
    module = __import__("sovereign_claw.backends_giles", fromlist=["_PROVIDER_DISPATCH"])
    monkeypatch.setitem(module._PROVIDER_DISPATCH, "openai", module._call_openai)

    result = backend.decide_next_action(
        objective="route request",
        history=[],
        forbidden_actions=[],
        drift=0.0,
    )

    assert result["tool"] == "HALT"
    assert result["kwargs"] == {}
    assert result["comment"] == "Parse failure; halting."
    assert result["agent_id"] == "giles"
    assert result["provider"] == "openai"