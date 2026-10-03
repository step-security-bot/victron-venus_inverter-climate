"""Public policy configuration; credentials are loaded only from the environment."""

import math
import re
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Policy:
    comfort_min_c: float = 17
    comfort_max_c: float = 19
    boost_delta_c: float = 0.5
    heating_power_w: float = 500
    start_margin_w: float = 100
    max_import_w: float = 100
    max_battery_discharge_w: float = 50
    start_soc: float = 90
    stop_soc: float = 80
    surplus_hold_seconds: float = 180
    minimum_boost_seconds: float = 900
    maximum_boost_seconds: float = 1800
    command_interval_seconds: float = 900
    manual_hold_seconds: float = 7200
    confirmation_seconds: float = 120
    max_energy_age_seconds: float = 120

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{field.name} must be numeric")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite or value < 0:
                raise ValueError(f"{field.name} must be finite and nonnegative")
        if not 5 <= self.comfort_min_c < self.comfort_max_c <= 30:
            raise ValueError("comfort range must be within 5–30 °C")
        if not 0 < self.boost_delta_c <= 2:
            raise ValueError("boost_delta_c must be within (0, 2]")
        if not 0 < self.heating_power_w <= 20000:
            raise ValueError("invalid heating power")
        if not 0 <= self.stop_soc < self.start_soc <= 100:
            raise ValueError("require 0 <= stop_soc < start_soc <= 100")
        if not 300 <= self.minimum_boost_seconds <= self.maximum_boost_seconds <= 7200:
            raise ValueError("boost duration must be between 300 and 7200 seconds")
        if self.command_interval_seconds < 300 or self.manual_hold_seconds < 300:
            raise ValueError("command/manual intervals must be at least 300 seconds")
        if self.surplus_hold_seconds < 30 or not 30 <= self.confirmation_seconds <= 300:
            raise ValueError("invalid stabilization or confirmation interval")
        if not 5 <= self.max_energy_age_seconds <= 300:
            raise ValueError("energy freshness must be between 5 and 300 seconds")


@dataclass(frozen=True)
class Config:
    entity_id: str
    mode: str
    poll_seconds: float
    state_path: Path
    status_path: Path
    policy: Policy

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "rb") as stream:
            data = tomllib.load(stream)
        if set(data) - {"service", "policy"}:
            raise ValueError("unknown configuration section")
        service = data.get("service", {})
        if not isinstance(service, dict) or not isinstance(data.get("policy", {}), dict):
            raise ValueError("service and policy must be configuration tables")
        allowed = {"entity_id", "mode", "poll_seconds", "state_path", "status_path"}
        if set(service) - allowed:
            raise ValueError("unknown service setting")
        entity = service.get("entity_id", "")
        if not isinstance(entity, str) or not re.fullmatch(r"climate\.[a-z0-9_]+", entity):
            raise ValueError("configure an exact climate entity_id")
        mode = service.get("mode", "observe")
        if mode not in ("observe", "active"):
            raise ValueError("mode must be observe or active")
        poll = service.get("poll_seconds", 30)
        if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not 5 <= poll <= 60:
            raise ValueError("poll_seconds must be between 5 and 60")
        paths = [service.get("state_path", "state.json"), service.get("status_path", "status.json")]
        if any(not isinstance(value, str) or not value.strip() for value in paths):
            raise ValueError("state/status paths must be nonempty strings")
        state, status = map(Path, paths)
        if state.resolve() == status.resolve():
            raise ValueError("state_path and status_path must differ")
        try:
            policy = Policy(**data.get("policy", {}))
        except TypeError as exc:
            raise ValueError("unknown policy setting") from exc
        return cls(entity, mode, poll, state, status, policy)
