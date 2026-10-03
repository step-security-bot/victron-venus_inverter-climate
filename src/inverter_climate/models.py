"""Strict normalization; missing measurements never become zero."""

import math
from dataclasses import dataclass
from typing import Any


class InvalidObservation(ValueError):
    """An upstream observation cannot support a control decision."""


def number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
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


@dataclass(frozen=True)
class Climate:
    mode: str
    action: str
    current_c: float
    target_c: float
    min_c: float
    max_c: float
    step_c: float
    preset: str
    supports_target: bool
    unit: str

    @classmethod
    def parse(cls, data: dict, entity_id: str, unit: str) -> "Climate":
        if data.get("entity_id") != entity_id:
            raise InvalidObservation("thermostat identity mismatch")
        attrs = data.get("attributes")
        if not isinstance(attrs, dict):
            raise InvalidObservation("missing climate attributes")
        mode = data.get("state")
        if mode in (None, "unknown", "unavailable"):
            raise InvalidObservation("thermostat unavailable")
        values = [
            celsius(number(attrs.get(key)), unit)
            for key in ("current_temperature", "temperature", "min_temp", "max_temp")
        ]
        current, target, minimum, maximum = values
        if not (-100 <= minimum < maximum <= 100) or not minimum <= target <= maximum:
            raise InvalidObservation("invalid thermostat range")
        if not -100 <= current <= 100:
            raise InvalidObservation("invalid current temperature")
        # HA permits integrations to omit precision. Use a conservative native
        # half-degree (C) or whole-degree (F) step until one is reported.
        step = number(attrs.get("target_temp_step", 0.5 if unit == "°C" else 1))
        step_c = step if unit == "°C" else step * 5 / 9
        if not 0 < step_c <= 5:
            raise InvalidObservation("invalid thermostat step")
        features = attrs.get("supported_features", 0)
        if isinstance(features, bool) or not isinstance(features, int) or features < 0:
            raise InvalidObservation("invalid thermostat features")
        return cls(
            str(mode),
            str(attrs.get("hvac_action", "unknown")),
            current,
            target,
            minimum,
            maximum,
            step_c,
            str(attrs.get("preset_mode") or "none"),
            bool(features & 1),
            unit,
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
