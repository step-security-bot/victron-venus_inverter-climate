"""Strict normalization; missing measurements never become zero."""

import math
from dataclasses import dataclass
from typing import Any

_HVAC_MODES = frozenset(("off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only"))


class InvalidObservation(ValueError):
    """An upstream observation cannot support a control decision."""


def number(value: Any) -> float:
    value_type = type(value)
    is_dbus_boolean = value_type.__name__ == "Boolean" and (
        value_type.__module__.split(".")[0] in ("dbus", "_dbus_bindings")
    )
    if isinstance(value, bool) or is_dbus_boolean or not isinstance(value, (int, float)):
        raise InvalidObservation("expected a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise InvalidObservation("expected a finite number") from exc
    if not math.isfinite(result):
        raise InvalidObservation("expected a finite number")
    return result


def celsius(value: float, unit: str) -> float:
    if unit == "°C":
        return value
    if unit == "°F":
        return (value - 32) * 5 / 9
    raise InvalidObservation("unsupported temperature unit")


def native_temperature(value: float, unit: str) -> float:
    if unit == "°C":
        return round(value, 4)
    if unit == "°F":
        return round(value * 9 / 5 + 32, 4)
    raise InvalidObservation("unsupported temperature unit")


def _climate_step(attrs: dict, unit: str) -> float:
    # HA permits integrations to omit precision. Use a conservative native
    # half-degree (C) or whole-degree (F) step until one is reported.
    raw_step = attrs.get("target_temp_step")
    if raw_step is None:
        raw_step = 0.5 if unit == "°C" else 1
    step = number(raw_step)
    step_c = step if unit == "°C" else step * 5 / 9
    if not 0 < step_c <= 5:
        raise InvalidObservation("invalid thermostat step")
    return step_c


def _climate_capabilities(attrs: dict) -> tuple[bool, tuple[str, ...]]:
    features = attrs.get("supported_features", 0)
    if isinstance(features, bool) or not isinstance(features, int) or features < 0:
        raise InvalidObservation("invalid thermostat features")
    modes = attrs.get("hvac_modes")
    if modes is None:
        modes = []
    if (
        not isinstance(modes, list)
        or any(not isinstance(item, str) or item not in _HVAC_MODES for item in modes)
        or len(set(modes)) != len(modes)
    ):
        raise InvalidObservation("invalid thermostat HVAC modes")
    return bool(features & 1), tuple(modes)


@dataclass(frozen=True)
class Climate:
    mode: str
    action: str
    current_c: float
    target_c: float | None
    min_c: float
    max_c: float
    step_c: float
    preset: str
    supports_target: bool
    unit: str
    hvac_modes: tuple[str, ...] = ()

    @property
    def can_set_temperature(self) -> bool:
        """A manual target is valid only in an advertised single heating mode."""
        return (
            self.supports_target is True
            and self.mode == "heat"
            and "heat" in self.hvac_modes
            and self.target_c is not None
            and self.preset == "none"
        )

    @property
    def supports_heat_off(self) -> bool:
        return "heat" in self.hvac_modes and "off" in self.hvac_modes

    def temperature_command(self, value_c: float) -> float:
        """Validate a Celsius request and return a device-aligned HA-unit value.

        The caller must freshly read this observation and check request age
        immediately before issuing a command. This immutable model has no clock
        or authorization state, and this method performs no I/O.
        """
        if not self.can_set_temperature:
            raise InvalidObservation("manual target is not supported in the current mode")
        value = number(value_c)
        minimum, maximum, step = map(number, (self.min_c, self.max_c, self.step_c))
        if not -100 <= minimum < maximum <= 100 or not 0 < step <= 5:
            raise InvalidObservation("invalid thermostat command range or step")
        # The slider starts at the advertised minimum. Celsius conversion does
        # not change this lattice; allow only floating-point/display roundoff.
        epsilon = min(0.0001, step * 0.001)
        if not minimum - epsilon <= value <= maximum + epsilon:
            raise InvalidObservation("requested temperature is outside device limits")
        steps = (value - minimum) / step
        if not math.isfinite(steps):
            raise InvalidObservation("invalid thermostat command step")
        aligned = minimum + round(steps) * step
        if abs(value - aligned) > epsilon:
            raise InvalidObservation("requested temperature does not match device step")
        if not minimum - epsilon <= aligned <= maximum + epsilon:
            raise InvalidObservation("requested temperature is outside device limits")
        aligned = min(maximum, max(minimum, aligned))
        command = native_temperature(aligned, self.unit)
        if abs(celsius(command, self.unit) - aligned) > epsilon:
            raise InvalidObservation("thermostat command precision is unsupported")
        return command

    def hvac_mode_command(self, mode: str) -> str:
        """Validate an explicit advertised Heat/Off request without changing targets."""
        if not isinstance(mode, str) or mode not in ("heat", "off") or mode not in self.hvac_modes:
            raise InvalidObservation("requested HVAC mode is not supported")
        return mode

    @classmethod
    def parse(cls, data: dict, entity_id: str, unit: str) -> "Climate":
        if data.get("entity_id") != entity_id:
            raise InvalidObservation("thermostat identity mismatch")
        attrs = data.get("attributes")
        if not isinstance(attrs, dict):
            raise InvalidObservation("missing climate attributes")
        mode = data.get("state")
        if not isinstance(mode, str) or mode not in _HVAC_MODES:
            raise InvalidObservation("thermostat unavailable")
        values = [
            celsius(number(attrs.get(key)), unit)
            for key in ("current_temperature", "min_temp", "max_temp")
        ]
        current, minimum, maximum = values
        raw_target = attrs.get("temperature")
        # Nest does not expose a single target while Off or in a range mode.
        # Keep the room observation and mode controls available in those states.
        target = (
            None if raw_target is None and mode != "heat" else celsius(number(raw_target), unit)
        )
        if not (-100 <= minimum < maximum <= 100) or (
            target is not None and not minimum <= target <= maximum
        ):
            raise InvalidObservation("invalid thermostat range")
        if not -100 <= current <= 100:
            raise InvalidObservation("invalid current temperature")
        step_c = _climate_step(attrs, unit)
        supports_target, modes = _climate_capabilities(attrs)
        return cls(
            str(mode),
            str(attrs.get("hvac_action", "unknown")),
            current,
            target,
            minimum,
            maximum,
            step_c,
            str(attrs.get("preset_mode") or "none"),
            supports_target,
            unit,
            modes,
        )


@dataclass(frozen=True)
class Energy:
    soc: float
    solar_w: float
    grid_w: float
    battery_w: float

    @classmethod
    def parse(cls, data: dict, now: float, max_age: float) -> "Energy":
        if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise InvalidObservation("unsupported energy schema")
        source_type = data.get("source_type")
        if source_type == "venus_dbus":
            if data.get("source_connected") is not True:
                raise InvalidObservation("local D-Bus source disconnected")
        elif source_type is None:
            if data.get("mqtt_connected") is not True:
                raise InvalidObservation("gateway MQTT disconnected")
        else:
            raise InvalidObservation("unsupported energy source type")
        generated = number(data.get("generated_at"))
        response_age = now - generated
        if response_age < -5 or response_age > max_age:
            raise InvalidObservation("energy response expired")
        metrics = data.get("metrics")
        if not isinstance(metrics, dict):
            raise InvalidObservation("missing energy metrics")
        values = []
        for name, unit in (
            ("battery_soc", "%"),
            ("solar_power", "W"),
            ("grid_power", "W"),
            ("battery_power", "W"),
        ):
            metric = metrics.get(name)
            if not isinstance(metric, dict) or metric.get("status") != "fresh":
                raise InvalidObservation(f"{name} is not fresh")
            age = number(metric.get("age_seconds"))
            if age < 0 or age + max(0, response_age) > max_age:
                raise InvalidObservation(f"{name} expired")
            sources = metric.get("sources")
            if metric.get("unit") != unit or not isinstance(sources, list) or not sources:
                raise InvalidObservation(f"{name} has no verified unit or sources")
            values.append(number(metric.get("value")))
        soc, solar, grid, battery = values
        if not 0 <= soc <= 100 or solar < 0:
            raise InvalidObservation("energy values outside supported range")
        return cls(soc, solar, grid, battery)
