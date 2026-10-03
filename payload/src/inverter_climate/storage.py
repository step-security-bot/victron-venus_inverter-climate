"""Atomic local state and a process lock; a corrupt journal fails closed."""

import fcntl
import hashlib
import json
import math
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, fields
from pathlib import Path

from .controller import State


def identity(base_url: str, entity_id: str) -> str:
    return hashlib.sha256(f"{base_url.rstrip('/')}\n{entity_id}".encode()).hexdigest()


def atomic_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = output.name
            os.chmod(temporary, 0o600)
            json.dump(payload, output, allow_nan=False, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            os.unlink(temporary)


def load_state(path: Path, binding: str) -> State:
    if not path.exists():
        return State()
    if path.stat().st_size > 16384:
        raise ValueError("state journal is oversized")
    try:
        data = json.loads(path.read_text())
        if (
            type(data.get("version")) is not int
            or data["version"] != 1
            or data.get("identity") != binding
        ):
            raise ValueError("state journal belongs to another thermostat or version")
        raw = data["state"]
        if set(raw) != {f.name for f in fields(State)}:
            raise ValueError("invalid state journal fields")
        state = State(**raw)
        if state.phase not in ("idle", "pending_boost", "boosted", "pending_restore"):
            raise ValueError("invalid state journal phase")
        strings = {"phase", "observed_mode", "observed_preset"}
        nullable = {"baseline_c", "boosted_c", "surplus_since", "observed_target_c"}
        for field in fields(State):
            value = getattr(state, field.name)
            if field.name in strings:
                if value is not None and not isinstance(value, str):
                    raise ValueError("invalid journal string")
                continue
            if value is None and field.name in nullable:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("invalid journal number")
        if state.phase != "idle":
            if (
                state.baseline_c is None
                or state.boosted_c is None
                or not 5 <= state.baseline_c < state.boosted_c <= 32
                or state.boosted_c - state.baseline_c > 2.01
            ):
                raise ValueError("invalid journal ownership")
        return state
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid state journal; inspect before resetting") from exc


def save_state(path: Path, binding: str, state: State):
    atomic_json(path, {"version": 1, "identity": binding, "state": asdict(state)})


@contextmanager
def process_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lockfile", "a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another climate process owns this journal") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
