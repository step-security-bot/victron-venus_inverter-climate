"""Bounded, single-zone preheat policy. The thermostat owns burner cycling."""

import math
from dataclasses import dataclass

from .config import Policy
from .models import Climate, Energy


@dataclass
class State:
    phase: str = "idle"
    baseline_c: float | None = None
    boosted_c: float | None = None
    since: float = 0
    last_command: float = 0
    hold_until: float = 0
    surplus_since: float | None = None
    observed_target_c: float | None = None
    observed_mode: str | None = None
    observed_preset: str | None = None
    last_tick: float = 0


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    target_c: float | None = None


def same(left: float | None, right: float | None) -> bool:
    return left is not None and right is not None and abs(left - right) < 0.02


def relinquish(state: State, now: float, policy: Policy):
    state.phase = "idle"
    state.baseline_c = state.boosted_c = None
    state.surplus_since = None
    state.hold_until = now + policy.manual_hold_seconds


def evaluate(
    state: State,
    climate: Climate | None,
    energy: Energy | None,
    policy: Policy,
    now: float,
    *,
    active: bool = False,
) -> Decision:
    """Mutate bookkeeping and persist command intent BEFORE any service POST.

    Repeated calls never resend an unconfirmed command. Observe mode never
    creates a command intent, including when recovering an active-mode state.
    """
    prior_tick = state.last_tick
    state.last_tick = now
    clock_changed = prior_tick > 0 and now < prior_tick
    if prior_tick and (clock_changed or now - prior_tick > policy.max_energy_age_seconds):
        state.surplus_since = None
    if clock_changed:
        state.hold_until = now + policy.manual_hold_seconds
        if state.phase != "idle":
            # Retain the release obligation even if a pending command only
            # confirms on a later tick after the wall-clock regression.
            state.since = now - policy.maximum_boost_seconds
    if climate is None:
        state.surplus_since = None
        return Decision("wait", "thermostat_unavailable")
    if climate.mode == "heat" and climate.target_c is None:
        state.surplus_since = None
        return Decision("wait", "thermostat_target_unavailable")

    prior_target = state.observed_target_c
    prior_mode = state.observed_mode
    prior_preset = state.observed_preset
    state.observed_target_c = climate.target_c
    state.observed_mode = climate.mode
    state.observed_preset = climate.preset

    if state.phase != "idle":
        if (
            climate.mode != "heat"
            or climate.preset != "none"
            or (
                not same(climate.target_c, state.boosted_c)
                and not same(climate.target_c, state.baseline_c)
            )
        ):
            relinquish(state, now, policy)
            return Decision("wait", "external_change_respected")
        if state.phase == "pending_restore":
            if same(climate.target_c, state.baseline_c):
                state.phase = "idle"
                state.baseline_c = state.boosted_c = None
                state.surplus_since = None
                state.hold_until = max(state.hold_until, now + policy.command_interval_seconds)
                return Decision("wait", "baseline_restored")
            return Decision("wait", "restore_unconfirmed_no_retry")
        if state.phase == "pending_boost":
            if same(climate.target_c, state.boosted_c):
                state.phase = "boosted"
            else:
                # A timed-out HTTP call may still complete downstream. Retain
                # ownership intent so a late acknowledgement can be unwound.
                reason = (
                    "boost_unconfirmed_no_retry"
                    if now - state.last_command >= policy.confirmation_seconds
                    else "awaiting_boost_confirmation"
                )
                return Decision("wait", reason)
        elif same(climate.target_c, state.baseline_c):
            relinquish(state, now, policy)
            return Decision("wait", "external_change_respected")

        elapsed = now - state.since
        reason = None
        if clock_changed:
            reason = "clock_changed"
        elif energy is None:
            reason = "energy_unavailable"
        elif energy.soc < policy.stop_soc:
            reason = "battery_reserve"
        elif energy.battery_w < -policy.max_battery_discharge_w:
            reason = "battery_discharging"
        elif energy.grid_w > policy.max_import_w:
            reason = "grid_import"
        elif elapsed >= policy.maximum_boost_seconds:
            reason = "boost_expired"
        elif climate.current_c >= policy.comfort_max_c:
            reason = "comfort_ceiling"
        elif elapsed >= policy.minimum_boost_seconds:
            # When heating, its measured/estimated 500 W is already in current
            # site consumption. Add it back ONLY to maintain an existing boost.
            headroom = max(0, -energy.grid_w)
            if climate.action == "heating":
                headroom += policy.heating_power_w
            if energy.solar_w < policy.heating_power_w or headroom < policy.heating_power_w:
                reason = "surplus_ended"
        if reason:
            baseline = state.baseline_c
            if baseline is None or not climate.min_c <= baseline <= climate.max_c:
                return Decision("wait", "baseline_outside_device_range")
            if active:
                state.phase = "pending_restore"
                state.last_command = now
                return Decision("restore", reason, baseline)
            return Decision("would_restore", reason, baseline)
        return Decision("wait", "boost_running")

    targets_match = (prior_target is None and climate.target_c is None) or same(
        prior_target, climate.target_c
    )
    if prior_mode is not None and (
        not targets_match or prior_mode != climate.mode or prior_preset != climate.preset
    ):
        relinquish(state, now, policy)
        return Decision("wait", "external_change_respected")
    if now < state.hold_until:
        state.surplus_since = None
        return Decision("wait", "manual_or_cooldown_hold")
    if climate.mode != "heat" or climate.action not in ("idle", "heating"):
        state.surplus_since = None
        return Decision("wait", "heat_mode_required")
    if climate.preset != "none" or not climate.supports_target:
        state.surplus_since = None
        return Decision("wait", "preset_or_capability_restriction")
    if not policy.comfort_min_c <= climate.target_c < policy.comfort_max_c:
        state.surplus_since = None
        return Decision("wait", "baseline_outside_comfort_band")
    upper = min(policy.comfort_max_c, climate.max_c, climate.target_c + policy.boost_delta_c)
    steps = math.floor((upper - climate.target_c + 1e-8) / climate.step_c)
    target = round(climate.target_c + steps * climate.step_c, 6)
    if target <= climate.target_c or climate.current_c >= target:
        state.surplus_since = None
        return Decision("wait", "no_preheat_needed")
    if energy is None:
        state.surplus_since = None
        return Decision("wait", "energy_unavailable")
    eligible = (
        energy.soc >= policy.start_soc
        and energy.solar_w >= policy.heating_power_w
        and energy.battery_w >= -policy.max_battery_discharge_w
        and -energy.grid_w >= policy.heating_power_w + policy.start_margin_w
    )
    if not eligible:
        state.surplus_since = None
        return Decision("wait", "insufficient_solar_export_or_reserve")
    if state.surplus_since is None:
        state.surplus_since = now
    if now - state.surplus_since < policy.surplus_hold_seconds:
        return Decision("wait", "stabilizing_surplus")
    if state.last_command and now - state.last_command < policy.command_interval_seconds:
        return Decision("wait", "command_cooldown")
    if not active:
        return Decision("would_boost", "sustained_solar_export", target)
    state.phase = "pending_boost"
    state.baseline_c = climate.target_c
    state.boosted_c = target
    state.since = state.last_command = now
    state.surplus_since = None
    return Decision("boost", "sustained_solar_export", target)
