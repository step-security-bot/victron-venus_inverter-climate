"""Exercise ownership, recovery, and physical policy boundaries over time."""

from dataclasses import asdict, replace

import pytest

from inverter_climate.config import Policy
from inverter_climate.controller import State, evaluate
from inverter_climate.models import Climate, Energy, native_temperature


@pytest.fixture
def policy():
    return Policy(boost_delta_c=0.5)


@pytest.fixture
def climate():
    return Climate(
        mode="heat",
        action="idle",
        current_c=17.5,
        target_c=18,
        min_c=10,
        max_c=30,
        step_c=0.5,
        preset="none",
        supports_target=True,
        unit="°C",
    )


@pytest.fixture
def energy():
    return Energy(soc=95, solar_w=2000, grid_w=-700, battery_w=100)


def stabilize(state, climate, energy, policy, start=10000, active=True):
    """Observe every 30 seconds, so a long data gap never counts as surplus."""
    tick = start
    decision = evaluate(state, climate, energy, policy, tick, active=active)
    while tick < start + policy.surplus_hold_seconds:
        tick = min(tick + 30, start + policy.surplus_hold_seconds)
        decision = evaluate(state, climate, energy, policy, tick, active=active)
    return decision, tick


def start_boost(climate, energy, policy):
    state = State()
    decision, tick = stabilize(state, climate, energy, policy)
    assert decision.action == "boost"
    acknowledged = replace(climate, target_c=decision.target_c, action="heating")
    assert evaluate(state, acknowledged, energy, policy, tick + 30, active=True).reason == (
        "boost_running"
    )
    assert state.phase == "boosted"
    return state, acknowledged, tick


def test_observation_mode_never_creates_write_intent(climate, energy, policy):
    state = State()
    decision, tick = stabilize(state, climate, energy, policy, active=False)
    assert decision.action == "would_boost"
    assert decision.target_c == 18.5
    assert state.phase == "idle"
    assert state.last_command == 0
    assert state.baseline_c is None and state.boosted_c is None
    assert evaluate(state, climate, energy, policy, tick + 30).action == "would_boost"


def test_active_boost_records_recovery_intent_before_dispatch(climate, energy, policy):
    state = State()
    decision, tick = stabilize(state, climate, energy, policy)
    assert decision.action == "boost"
    assert state.phase == "pending_boost"
    assert state.baseline_c == 18
    assert state.boosted_c == 18.5
    assert state.since == state.last_command == tick


@pytest.mark.parametrize(
    "changes",
    [
        {"soc": 89},
        {"grid_w": -599},
        {"grid_w": 10, "solar_w": 10000},
        {"grid_w": -700, "solar_w": 499},
        {"battery_w": -51},
    ],
)
def test_boost_requires_measured_export_and_reserve(climate, energy, policy, changes):
    state = State()
    decision, _ = stabilize(state, climate, replace(energy, **changes), policy)
    assert decision.action == "wait"
    assert state.phase == "idle"
    assert state.surplus_since is None


def test_start_never_adds_furnace_estimate_to_measured_export(climate, energy, policy):
    state = State()
    decision, _ = stabilize(
        state, replace(climate, action="heating"), replace(energy, grid_w=-200), policy
    )
    assert decision.reason == "insufficient_solar_export_or_reserve"


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"mode": "off"}, "heat_mode_required"),
        ({"mode": "cool"}, "heat_mode_required"),
        ({"mode": "heat_cool"}, "heat_mode_required"),
        ({"action": "unknown"}, "heat_mode_required"),
        ({"action": "unavailable"}, "heat_mode_required"),
        ({"preset": "eco"}, "preset_or_capability_restriction"),
        ({"preset": "away"}, "preset_or_capability_restriction"),
        ({"supports_target": False}, "preset_or_capability_restriction"),
        ({"target_c": 16}, "baseline_outside_comfort_band"),
        ({"target_c": 19}, "baseline_outside_comfort_band"),
        ({"current_c": 18.5}, "no_preheat_needed"),
    ],
)
def test_device_user_and_comfort_restrictions(climate, energy, policy, changes, reason):
    state = State()
    decision, _ = stabilize(state, replace(climate, **changes), energy, policy)
    assert decision.action == "wait"
    assert decision.reason == reason


@pytest.mark.parametrize("limit", ["comfort", "device"])
def test_rounding_never_exceeds_upper_bound(climate, energy, policy, limit):
    policy = replace(policy, comfort_max_c=19.2, boost_delta_c=1)
    climate = replace(climate, target_c=18.9, step_c=0.5)
    if limit == "device":
        climate = replace(climate, max_c=19.1)
    decision, _ = stabilize(State(), climate, energy, policy)
    assert decision.reason == "no_preheat_needed"


def test_one_native_fahrenheit_step_is_sent_without_celsius_rounding_error(
    climate,
    energy,
    policy,
):
    climate = replace(climate, target_c=(64 - 32) * 5 / 9, current_c=17, step_c=5 / 9, unit="°F")
    policy = replace(policy, boost_delta_c=0.6)
    decision, _ = stabilize(State(), climate, energy, policy)
    assert decision.action == "boost"
    assert native_temperature(decision.target_c, "°F") == 65
    assert decision.target_c - climate.target_c <= policy.boost_delta_c


def test_smaller_than_native_step_cannot_round_up_past_allowed_delta(climate, energy, policy):
    climate = replace(climate, step_c=5 / 9, unit="°F")
    decision, _ = stabilize(State(), climate, energy, policy)
    assert decision.reason == "no_preheat_needed"


@pytest.mark.parametrize("observation", ["missing_energy", "missing_climate", "gap", "regression"])
def test_unobserved_or_discontinuous_time_does_not_count_as_surplus(
    climate,
    energy,
    policy,
    observation,
):
    state = State()
    evaluate(state, climate, energy, policy, 10000, active=True)
    evaluate(state, climate, energy, policy, 10090, active=True)
    if observation == "missing_energy":
        evaluate(state, climate, None, policy, 10100, active=True)
        next_tick = 10120
    elif observation == "missing_climate":
        evaluate(state, None, energy, policy, 10100, active=True)
        next_tick = 10120
    elif observation == "gap":
        next_tick = 10300
    else:
        next_tick = 9990
    decision = evaluate(state, climate, energy, policy, next_tick, active=True)
    assert decision.action == "wait"
    assert state.phase == "idle"
    if observation == "regression":
        assert decision.reason == "manual_or_cooldown_hold"
    else:
        assert decision.reason == "stabilizing_surplus"
        assert state.surplus_since == next_tick


def test_small_failed_export_sample_restarts_entire_stabilization(climate, energy, policy):
    state = State()
    evaluate(state, climate, energy, policy, 10000, active=True)
    evaluate(state, climate, energy, policy, 10090, active=True)
    evaluate(state, climate, replace(energy, grid_w=0), policy, 10120, active=True)
    decision = evaluate(state, climate, energy, policy, 10180, active=True)
    assert decision.reason == "stabilizing_surplus"
    assert state.surplus_since == 10180


def test_pending_boost_timeout_and_restart_never_retry(climate, energy, policy):
    state = State()
    _, tick = stabilize(state, climate, energy, policy)
    assert evaluate(state, climate, energy, policy, tick + 30, active=True).reason == (
        "awaiting_boost_confirmation"
    )
    restored = State(**asdict(state))
    decision = evaluate(restored, climate, energy, policy, tick + 180, active=True)
    assert decision.reason == "boost_unconfirmed_no_retry"
    assert restored.phase == "pending_boost"
    assert restored.last_command == tick
    assert evaluate(restored, climate, energy, policy, tick + 10000, active=True).action == "wait"


def test_late_boost_acknowledgement_after_max_duration_is_unwound(climate, energy, policy):
    state = State()
    _, tick = stabilize(state, climate, energy, policy)
    evaluate(state, climate, energy, policy, tick + 180, active=True)
    late = replace(climate, target_c=18.5)
    decision = evaluate(
        state, late, energy, policy, tick + policy.maximum_boost_seconds, active=True
    )
    assert decision.action == "restore"
    assert decision.reason == "boost_expired"
    assert decision.target_c == 18
    assert state.phase == "pending_restore"


def test_clock_regression_while_pending_boost_expires_even_a_later_acknowledgement(
    climate,
    energy,
    policy,
):
    state = State()
    _, tick = stabilize(state, climate, energy, policy)
    regressed = tick - 3600
    assert evaluate(state, climate, energy, policy, regressed, active=True).action == "wait"
    restarted = State(**asdict(state))
    late_acknowledgement = replace(climate, target_c=state.boosted_c)
    decision = evaluate(
        restarted, late_acknowledgement, energy, policy, regressed + 30, active=True
    )
    assert decision.action == "restore"
    assert decision.target_c == climate.target_c


@pytest.mark.parametrize("phase", ["pending_boost", "boosted", "pending_restore"])
@pytest.mark.parametrize("changes", [{"target_c": 18.8}, {"mode": "off"}, {"preset": "eco"}])
def test_manual_native_changes_relinquish_ownership_in_every_phase(
    climate,
    energy,
    policy,
    phase,
    changes,
):
    state, boosted, tick = start_boost(climate, energy, policy)
    state.phase = phase
    changed = replace(boosted, **changes)
    decision = evaluate(state, changed, energy, policy, tick + 60, active=True)
    assert decision.action == "wait"
    assert decision.reason == "external_change_respected"
    assert state.phase == "idle"
    assert state.baseline_c is None and state.boosted_c is None
    assert state.hold_until == tick + 60 + policy.manual_hold_seconds


def test_native_return_to_original_temperature_is_respected(climate, energy, policy):
    state, _, tick = start_boost(climate, energy, policy)
    decision = evaluate(state, climate, energy, policy, tick + 60, active=True)
    assert decision.reason == "external_change_respected"
    assert state.phase == "idle"


def test_manual_change_while_idle_blocks_immediate_preheat(climate, energy, policy):
    state = State()
    evaluate(state, climate, energy, policy, 10000, active=True)
    changed = replace(climate, target_c=18.2)
    assert evaluate(state, changed, energy, policy, 10030, active=True).reason == (
        "external_change_respected"
    )
    assert evaluate(state, changed, energy, policy, 10060, active=True).reason == (
        "manual_or_cooldown_hold"
    )


@pytest.mark.parametrize(
    "scenario, reason",
    [
        ("missing_energy", "energy_unavailable"),
        ("reserve", "battery_reserve"),
        ("discharge", "battery_discharging"),
        ("import", "grid_import"),
        ("ceiling", "comfort_ceiling"),
        ("clock", "clock_changed"),
    ],
)
def test_safety_and_comfort_stops_override_minimum_boost_time(
    climate,
    energy,
    policy,
    scenario,
    reason,
):
    state, boosted, tick = start_boost(climate, energy, policy)
    now = tick + 60
    if scenario == "missing_energy":
        energy = None
    elif scenario == "reserve":
        energy = replace(energy, soc=79)
    elif scenario == "discharge":
        energy = replace(energy, battery_w=-51)
    elif scenario == "import":
        energy = replace(energy, grid_w=101)
    elif scenario == "ceiling":
        boosted = replace(boosted, current_c=policy.comfort_max_c)
    else:
        now = tick - 1
    decision = evaluate(state, boosted, energy, policy, now, active=True)
    assert decision.action == "restore"
    assert decision.reason == reason
    assert decision.target_c == 18
    assert state.last_command == now


def test_minimum_duration_absorbs_export_dip_then_restores(climate, energy, policy):
    state, boosted, tick = start_boost(climate, energy, policy)
    idle = replace(boosted, action="idle")
    no_export = replace(energy, grid_w=0)
    assert evaluate(state, idle, no_export, policy, tick + 60, active=True).reason == (
        "boost_running"
    )
    decision = evaluate(
        state, idle, no_export, policy, tick + policy.minimum_boost_seconds, active=True
    )
    assert decision.action == "restore"
    assert decision.reason == "surplus_ended"


def test_running_furnace_power_is_added_back_only_for_maintenance(climate, energy, policy):
    state, boosted, tick = start_boost(climate, energy, policy)
    no_export = replace(energy, grid_w=0)
    decision = evaluate(
        state, boosted, no_export, policy, tick + policy.minimum_boost_seconds, active=True
    )
    assert decision.reason == "boost_running"
    decision = evaluate(
        state, boosted, no_export, policy, tick + policy.maximum_boost_seconds, active=True
    )
    assert decision.action == "restore"
    assert decision.reason == "boost_expired"


def test_solar_disappearing_ends_boost_even_with_furnace_estimate(climate, energy, policy):
    state, boosted, tick = start_boost(climate, energy, policy)
    decision = evaluate(
        state,
        boosted,
        replace(energy, solar_w=0, grid_w=0),
        policy,
        tick + policy.minimum_boost_seconds,
        active=True,
    )
    assert decision.action == "restore"
    assert decision.reason == "surplus_ended"


def test_pending_restore_is_persistent_no_retry_until_confirmation(climate, energy, policy):
    state, boosted, tick = start_boost(climate, energy, policy)
    decision = evaluate(state, boosted, None, policy, tick + 60, active=True)
    assert decision.action == "restore"
    restarted = State(**asdict(state))
    decision = evaluate(restarted, boosted, energy, policy, tick + 600, active=True)
    assert decision.reason == "restore_unconfirmed_no_retry"
    assert restarted.last_command == tick + 60
    decision = evaluate(restarted, climate, energy, policy, tick + 630, active=True)
    assert decision.reason == "baseline_restored"
    assert restarted.phase == "idle"
    assert restarted.baseline_c is None and restarted.boosted_c is None
    assert restarted.hold_until >= tick + 630 + policy.command_interval_seconds


def test_observe_recovery_can_propose_restore_but_never_record_intent(climate, energy, policy):
    state, boosted, tick = start_boost(climate, energy, policy)
    decision = evaluate(state, boosted, None, policy, tick + 60)
    assert decision.action == "would_restore"
    assert state.phase == "boosted"
    assert state.last_command == tick


def test_restore_cannot_exceed_changed_device_limits(climate, energy, policy):
    state, boosted, tick = start_boost(climate, energy, policy)
    decision = evaluate(state, replace(boosted, min_c=18.2), None, policy, tick + 60, active=True)
    assert decision.action == "wait"
    assert decision.reason == "baseline_outside_device_range"
    assert state.phase == "boosted"


def test_missing_thermostat_does_not_erase_recovery_intent(climate, energy, policy):
    state, boosted, tick = start_boost(climate, energy, policy)
    decision = evaluate(state, None, energy, policy, tick + 60, active=True)
    assert decision.reason == "thermostat_unavailable"
    assert state.phase == "boosted"
    decision = evaluate(
        state, boosted, energy, policy, tick + policy.maximum_boost_seconds, active=True
    )
    assert decision.action == "restore"


def test_recent_command_blocks_another_boost_even_after_stable_export(climate, energy, policy):
    state = State(last_command=9999)
    decision, _ = stabilize(state, climate, energy, policy)
    assert decision.reason == "command_cooldown"
    assert state.phase == "idle"
