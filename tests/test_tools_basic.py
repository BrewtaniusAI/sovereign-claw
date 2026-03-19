from __future__ import annotations

import json

import pytest

from sovereign_claw.tools_basic import (
    TOOL_REGISTRY,
    ToolSpec,
    echo_text,
    list_directory,
    read_text_file,
    register_all,
    tool_descriptions,
    validate_kwargs,
    write_json_file,
)


def test_toolspec_defaults():
    spec = ToolSpec(name="demo", description="demo tool")

    assert spec.name == "demo"
    assert spec.description == "demo tool"
    assert spec.required_kwargs == []
    assert spec.safety_tier == "READ_ONLY"


def test_validate_kwargs_accepts_required_kwargs():
    spec = ToolSpec(
        name="demo",
        description="demo tool",
        required_kwargs=["path", "data"],
    )

    validate_kwargs(spec, {"path": "x", "data": {"ok": True}})


def test_validate_kwargs_raises_for_missing_required_kwargs():
    spec = ToolSpec(
        name="demo",
        description="demo tool",
        required_kwargs=["path", "data"],
    )

    with pytest.raises(TypeError, match="missing required kwargs"):
        validate_kwargs(spec, {"path": "x"})


def test_echo_text_returns_input():
    assert echo_text("hello world") == "hello world"


def test_read_text_file_reads_utf8_contents(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("hello file", encoding="utf-8")

    assert read_text_file(str(path)) == "hello file"


def test_read_text_file_raises_for_missing_file(tmp_path):
    path = tmp_path / "missing.txt"

    with pytest.raises(FileNotFoundError, match="No such file"):
        read_text_file(str(path))


def test_write_json_file_writes_formatted_json(tmp_path):
    path = tmp_path / "data.json"
    data = {"name": "sovereign", "count": 2}

    returned = write_json_file(str(path), data)

    assert returned == str(path)
    assert path.exists()

    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == data

    raw = path.read_text(encoding="utf-8")
    assert "\n" in raw


def test_list_directory_returns_sorted_names(tmp_path):
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "c").mkdir()

    assert list_directory(str(tmp_path)) == ["a.txt", "b.txt", "c"]


def test_list_directory_raises_for_non_directory(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("x", encoding="utf-8")

    with pytest.raises(NotADirectoryError, match="Not a directory"):
        list_directory(str(path))


def test_tool_registry_contains_expected_tools():
    assert set(TOOL_REGISTRY) == {
        "echo_text",
        "read_text_file",
        "write_json_file",
        "list_directory",
    }

    for name, (fn, spec) in TOOL_REGISTRY.items():
        assert callable(fn)
        assert spec.name == name


class DummyOrchestrator:
    def __init__(self):
        self.registered = []

    def register_tool(self, name, fn):
        self.registered.append((name, fn))


def test_register_all_registers_every_tool():
    orchestrator = DummyOrchestrator()

    register_all(orchestrator)

    assert [name for name, _fn in orchestrator.registered] == list(TOOL_REGISTRY.keys())


def test_tool_descriptions_matches_registry_specs():
    descriptions = tool_descriptions()

    assert len(descriptions) == len(TOOL_REGISTRY)

    names = {d["name"] for d in descriptions}
    assert names == set(TOOL_REGISTRY.keys())

    for desc in descriptions:
        assert set(desc) == {
            "name",
            "description",
            "required_kwargs",
            "safety_tier",
        }
