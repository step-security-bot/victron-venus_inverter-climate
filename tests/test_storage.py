"""Recovery journals are durable, private, and bound to one thermostat."""

import json
import os
import stat
from dataclasses import asdict
from pathlib import Path

import pytest

from inverter_climate.controller import State
from inverter_climate.storage import atomic_json, identity, load_state, process_lock, save_state


def owned_state():
    return State(
        phase="pending_boost",
        baseline_c=18,
        boosted_c=18.5,
        since=10000,
        last_command=10000,
        last_tick=10000,
        observed_target_c=18,
        observed_mode="heat",
        observed_preset="none",
    )


def test_identity_binds_to_endpoint_and_exact_entity_without_exposing_either():
    binding = identity("https://ha.example.test/", "climate.furnace")
    assert binding == identity("https://ha.example.test", "climate.furnace")
    assert len(binding) == 64
    assert "furnace" not in binding
    assert binding != identity("https://other.example.test", "climate.furnace")
    assert binding != identity("https://ha.example.test", "climate.other")


def test_first_launch_starts_with_no_claim_to_thermostat(tmp_path):
    assert load_state(tmp_path / "state.json", "binding") == State()


@pytest.mark.parametrize("phase", ["pending_boost", "boosted", "pending_restore"])
def test_each_ownership_phase_survives_restart(tmp_path, phase):
    path = tmp_path / "nested" / "state.json"
    state = owned_state()
    state.phase = phase
    save_state(path, "binding", state)
    assert load_state(path, "binding") == state
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_different_thermostat_cannot_recover_an_owned_target(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, "first", owned_state())
    with pytest.raises(ValueError, match="another thermostat"):
        load_state(path, "second")


@pytest.mark.parametrize("contents", ["", "{", "null", "[]", '"text"', "NaN"])
def test_corrupt_state_never_silently_resets_to_idle(tmp_path, contents):
    path = tmp_path / "state.json"
    path.write_text(contents)
    with pytest.raises(ValueError):
        load_state(path, "binding")


@pytest.mark.parametrize(
    "changes",
    [
        {"phase": "unknown"},
        {"phase": None},
        {"baseline_c": None},
        {"boosted_c": None},
        {"boosted_c": 17.5},
        {"boosted_c": 25},
        {"baseline_c": True},
        {"last_command": float("nan")},
        {"last_tick": "10000"},
        {"observed_mode": 1},
    ],
)
def test_invalid_ownership_or_field_types_fail_closed(tmp_path, changes):
    path = tmp_path / "state.json"
    state = asdict(owned_state())
    state.update(changes)
    path.write_text(json.dumps({"version": 1, "identity": "binding", "state": state}))
    with pytest.raises(ValueError):
        load_state(path, "binding")


@pytest.mark.parametrize("version", [True, 1.0, 2, "1", None])
def test_journal_schema_version_is_exact(tmp_path, version):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"version": version, "identity": "binding", "state": asdict(owned_state())})
    )
    with pytest.raises(ValueError):
        load_state(path, "binding")


@pytest.mark.parametrize("operation", ["missing", "extra"])
def test_journal_schema_requires_exact_state_fields(tmp_path, operation):
    path = tmp_path / "state.json"
    state = asdict(owned_state())
    if operation == "missing":
        del state["last_command"]
    else:
        state["unexpected"] = True
    path.write_text(json.dumps({"version": 1, "identity": "binding", "state": state}))
    with pytest.raises(ValueError):
        load_state(path, "binding")


def test_oversized_state_is_not_read(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(" " * 16385)
    with pytest.raises(ValueError, match="oversized"):
        load_state(path, "binding")


def test_serialization_failure_keeps_old_journal_and_removes_temporary(tmp_path):
    path = tmp_path / "state.json"
    atomic_json(path, {"old": True})
    with pytest.raises(ValueError):
        atomic_json(path, {"not_json": float("nan")})
    assert json.loads(path.read_text()) == {"old": True}
    assert list(tmp_path.iterdir()) == [path]


def test_rename_failure_keeps_old_journal_and_removes_temporary(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    atomic_json(path, {"old": True})

    def fail_replace(*_):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        atomic_json(path, {"new": True})
    assert json.loads(path.read_text()) == {"old": True}
    assert list(tmp_path.iterdir()) == [path]


def test_lock_blocks_second_instance_and_releases_on_exception(tmp_path):
    path = tmp_path / "state.json"
    with pytest.raises(RuntimeError):
        with process_lock(path):
            with pytest.raises(ValueError, match="another climate process"):
                with process_lock(path):
                    pytest.fail("second process obtained an owned journal")
            raise RuntimeError("simulate process failure")
    with process_lock(path):
        assert stat.S_IMODE(Path(str(path) + ".lockfile").stat().st_mode) == 0o600
