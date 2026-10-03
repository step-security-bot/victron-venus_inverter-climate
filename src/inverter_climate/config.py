"""Public policy configuration; credentials are loaded only from the environment."""

import math
import re
import tomllib
from dataclasses import dataclass, fields
from dataclasses import field as dataclass_field
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
class EnergyConfig:
    backend: str = "gateway"
    solar_paths: tuple[str, ...] = ()
    grid_phases: tuple[str, ...] = ()
    timeout_seconds: float = 5

    def __post_init__(self):
        if self.backend not in ("gateway", "venus"):
            raise ValueError("energy backend must be gateway or venus")
        for value in (self.solar_paths, self.grid_phases):
            if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
                raise ValueError("energy source selections must be arrays of strings")
        if self.backend == "venus":
            if not self.solar_paths or not self.grid_phases:
                raise ValueError("Venus requires explicit solar_paths and grid_phases")
        elif self.solar_paths or self.grid_phases:
            raise ValueError("local source selections require the Venus backend")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0 < self.timeout_seconds <= 10
        ):
            raise ValueError("energy timeout must be within (0, 10] seconds")


@dataclass(frozen=True)
class DeviceConfig:
    enabled: bool = True
    device_instance: int = 80
    custom_name: str = "Inverter Climate"
    stale_seconds: float = 120

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("device enabled must be a boolean")
        if type(self.device_instance) is not int or not 0 <= self.device_instance <= 255:
            raise ValueError("device_instance must be an integer within 0..255")
        if (
            not isinstance(self.custom_name, str)
            or not self.custom_name.strip()
            or len(self.custom_name) > 64
            or not self.custom_name.isprintable()
        ):
            raise ValueError("custom_name must be a nonempty name of at most 64 characters")
        if (
            isinstance(self.stale_seconds, bool)
            or not isinstance(self.stale_seconds, (int, float))
            or not 10 <= self.stale_seconds <= 900
        ):
            raise ValueError("device stale_seconds must be within 10..900 seconds")


@dataclass(frozen=True)
class Config:
    entity_id: str
    mode: str
    poll_seconds: float
    state_path: Path
    status_path: Path
    policy: Policy
    energy: EnergyConfig = dataclass_field(default_factory=EnergyConfig)
    device: DeviceConfig = dataclass_field(default_factory=DeviceConfig)

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "rb") as stream:
            data = tomllib.load(stream)
        if set(data) - {"service", "policy", "energy", "device"}:
            raise ValueError("unknown configuration section")
        service = data.get("service", {})
        if any(
            not isinstance(data.get(key, {}), dict)
            for key in ("service", "policy", "energy", "device")
        ):
            raise ValueError("service, policy, energy and device must be configuration tables")
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
        energy_values = dict(data.get("energy", {}))
        for key in ("solar_paths", "grid_phases"):
            if key in energy_values:
                if not isinstance(energy_values[key], list):
                    raise ValueError(f"{key} must be an array")
                energy_values[key] = tuple(energy_values[key])
        try:
            energy = EnergyConfig(**energy_values)
        except TypeError as exc:
            raise ValueError("unknown energy setting") from exc
        try:
            device = DeviceConfig(**data.get("device", {}))
        except TypeError as exc:
            raise ValueError("unknown device setting") from exc
        if energy.backend == "venus" and device.enabled and device.stale_seconds < 2 * poll:
            raise ValueError("device stale_seconds must allow at least two poll intervals")
        return cls(entity, mode, poll, state, status, policy, energy, device)
