"""Bounded, nonblocking handoff of explicit local thermostat commands."""

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import Climate, number
from .storage import atomic_json


@dataclass(frozen=True)
class Request:
    kind: str
    value: float | str
    queued_at: float
    baseline: Climate


class ControlBroker:
    """The D-Bus producer never performs network or filesystem operations.

    One latest request per kind is retained. An Off request supersedes queued
    changes. The main thread exclusively owns durable intent and HA requests.
    """

    def __init__(self, enabled: bool = False, *, clock=time.monotonic, max_age_seconds=60):
        self.enabled = enabled
        self._clock = clock
        self._max_age = max_age_seconds
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._queue: dict[str, Request] = {}
        self._climate = None
        self._updated_at = None
        self._available = False
        self._outcome = "idle"
        self._reason = "controls_disabled" if not enabled else "awaiting_observation"

    def update(
        self,
        climate: Climate | None,
        *,
        available: bool,
        outcome: str,
        reason: str,
        refresh: bool = True,
    ):
        with self._lock:
            self._climate = climate
            if refresh or climate is None:
                self._updated_at = self._clock() if climate is not None else None
            self._available = self.enabled and available and climate is not None
            self._outcome, self._reason = outcome, reason

    def _fresh(self):
        return (
            self._updated_at is not None and 0 <= (self._clock() - self._updated_at) < self._max_age
        )

    def submit_temperature(self, target_c) -> bool:
        return self._submit("temperature", target_c)

    def submit_mode(self, mode) -> bool:
        return self._submit("mode", mode)

    def _submit(self, kind, value):
        with self._lock:
            if not self._available or not self._fresh():
                return False
            try:
                if kind == "temperature":
                    self._climate.temperature_command(value)
                    if "mode" in self._queue and self._queue["mode"].value == "off":
                        return False
                    value = number(value)
                else:
                    value = self._climate.hvac_mode_command(value)
            except (TypeError, ValueError):
                return False
            if kind == "mode" and value == "off":
                self._queue.clear()
            self._queue.pop(kind, None)
            self._queue[kind] = Request(kind, value, self._clock(), self._climate)
            self._wake.set()
            return True

    def take(self) -> Request | None:
        with self._lock:
            if not self._queue:
                return None
            # Claiming a request and suspending producer acceptance must be one
            # operation: otherwise an accepted Off could follow an already
            # claimed temperature write but retain the old observation basis.
            self._available = False
            kind = next(iter(self._queue))
            return self._queue.pop(kind)

    def fresh_request(self, request: Request) -> bool:
        return 0 <= self._clock() - request.queued_at < self._max_age

    def suspend(self) -> bool:
        """Atomically stop accepting commands and report already queued work."""
        with self._lock:
            self._available = False
            return bool(self._queue)

    def discard(self):
        with self._lock:
            self._queue.clear()

    def wake(self):
        self._wake.set()

    def wait(self, timeout):
        self._wake.wait(timeout)
        self._wake.clear()

    def status(self) -> dict:
        with self._lock:
            climate = self._climate
            available = self._available and self._fresh()
            modes = tuple(m for m in ("heat", "off") if climate and m in climate.hvac_modes)
            return {
                "enabled": self.enabled,
                "available": available,
                "pending": bool(self._queue) or self._outcome in ("pending", "unconfirmed"),
                "outcome": "queued" if self._queue else self._outcome,
                "reason": self._reason,
                "can_set_temperature": bool(available and climate.can_set_temperature),
                "can_set_mode": bool(available and modes),
                "min_c": climate.min_c if climate else None,
                "max_c": climate.max_c if climate else None,
                "step_c": climate.step_c if climate else None,
                "supported_modes": modes,
            }


@dataclass
class ManualIntent:
    kind: str
    value: float | str
    sent_at: float
    baseline_mode: str
    baseline_target_c: float | None
    baseline_preset: str
    outcome: str = "pending"

    @property
    def outstanding(self):
        return self.outcome in ("pending", "unconfirmed")

    def confirmed(self, climate: Climate) -> bool:
        if self.kind == "mode":
            return climate.mode == self.value
        return (
            climate.mode == self.baseline_mode
            and climate.preset == self.baseline_preset
            and climate.target_c is not None
            and abs(climate.target_c - self.value) < 0.02
        )

    def externally_changed(self, climate: Climate) -> bool:
        target_changed = climate.target_c != self.baseline_target_c
        if climate.target_c is not None and self.baseline_target_c is not None:
            target_changed = abs(climate.target_c - self.baseline_target_c) >= 0.02
        return (
            climate.mode != self.baseline_mode
            or climate.preset != self.baseline_preset
            or target_changed
        )


def manual_path(state_path: Path) -> Path:
    return state_path.with_name(state_path.name + ".manual")


def save_intent(path: Path, binding: str, intent: ManualIntent):
    atomic_json(path, {"version": 1, "identity": binding, "intent": asdict(intent)})


def _validate_intent(intent: ManualIntent):
    if intent.kind not in ("temperature", "mode") or intent.outcome not in (
        "pending",
        "unconfirmed",
        "confirmed",
        "rejected",
    ):
        raise ValueError("invalid manual command journal state")
    if intent.kind == "mode":
        if intent.value not in ("heat", "off"):
            raise ValueError("invalid manual command journal mode")
    elif not -100 <= number(intent.value) <= 100:
        raise ValueError("invalid manual command journal temperature")
    if number(intent.sent_at) < 0:
        raise ValueError("invalid manual command journal time")
    if intent.baseline_target_c is not None:
        number(intent.baseline_target_c)
    if not all(
        isinstance(value, str) and len(value) <= 64
        for value in (intent.baseline_mode, intent.baseline_preset)
    ):
        raise ValueError("invalid manual command journal observation")


def load_intent(path: Path, binding: str) -> ManualIntent | None:
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 4096:
            raise ValueError("manual command journal is oversized")
        data = json.loads(path.read_text())
        if type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("invalid manual command journal version")
        if data.get("identity") != binding:
            raise ValueError("manual command journal belongs to another thermostat")
        intent = ManualIntent(**data["intent"])
        _validate_intent(intent)
        if intent.outstanding:
            # A crash may occur either side of the POST. Observation can resolve
            # the intent, but restarting can never turn it into a resend.
            intent.outcome = "unconfirmed"
        return intent
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError, OverflowError) as exc:
        raise ValueError("invalid manual command journal; inspect before resetting") from exc
