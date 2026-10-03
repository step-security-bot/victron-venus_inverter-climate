"""The UI queue is bounded and cannot claim that an accepted write succeeded."""

import json
import threading
from dataclasses import replace

import pytest

from inverter_climate.control import ControlBroker, ManualIntent, load_intent, save_intent
from inverter_climate.models import Climate


def climate():
    return Climate("heat", "idle", 17, 18, 10, 30, 0.5, "none", True, "°C", ("heat", "off"))


def ready(broker, observation=None):
    broker.update(observation or climate(), available=True, outcome="idle", reason="ready")


def test_disabled_and_not_yet_observed_controls_reject():
    disabled = ControlBroker()
    ready(disabled)
    assert not disabled.submit_temperature(20)
    assert not disabled.submit_mode("off")
    assert not ControlBroker(True).submit_mode("off")


def test_rapid_slider_changes_coalesce_without_io_or_optimistic_confirmation():
    broker = ControlBroker(True)
    ready(broker)
    for target in (18.5, 19, 19.5, 20, 20.5):
        assert broker.submit_temperature(target)
    assert broker.status()["outcome"] == "queued"
    assert broker.status()["available"]
    assert broker.take().value == 20.5
    assert broker.take() is None


def test_off_supersedes_queued_temperature_and_blocks_another_target():
    broker = ControlBroker(True)
    ready(broker)
    assert broker.submit_temperature(20)
    assert broker.submit_mode("off")
    assert not broker.submit_temperature(21)
    assert broker.take().value == "off"
    assert broker.take() is None


def test_queue_retains_at_most_latest_request_per_kind():
    broker = ControlBroker(True)
    ready(broker)
    for _ in range(100):
        assert broker.submit_mode("heat")
        assert broker.submit_temperature(19)
    assert broker.take().kind == "mode"
    assert broker.take().kind == "temperature"
    assert broker.take() is None


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "20", 9, 31, 20.1])
def test_invalid_temperature_never_queues(value):
    broker = ControlBroker(True)
    ready(broker)
    assert not broker.submit_temperature(value)
    assert broker.take() is None


def test_stale_observation_and_clock_regression_disable_submission():
    now = [100.0]
    broker = ControlBroker(True, clock=lambda: now[0])
    ready(broker)
    now[0] = 160
    assert not broker.submit_mode("off")
    assert not broker.status()["available"]
    now[0] = 99
    assert not broker.submit_mode("off")


def test_pending_intent_suspends_broker_and_wakes_consumer():
    broker = ControlBroker(True)
    ready(broker)
    assert broker.submit_temperature(20)
    assert broker.suspend()
    assert not broker.submit_mode("off")
    done = threading.Event()
    waiter = threading.Thread(target=lambda: (broker.wait(10), done.set()))
    waiter.start()
    assert done.wait(0.5)
    waiter.join()


def test_claiming_request_atomically_stops_accepting_new_off_requests():
    broker = ControlBroker(True)
    ready(broker)
    assert broker.submit_temperature(20)
    assert broker.take().value == 20
    # No separate suspend call is needed: an Off cannot be accepted against
    # the old target once the consumer has claimed its temperature request.
    assert not broker.submit_mode("off")
    assert broker.take() is None


def test_off_without_target_still_advertises_heat_control():
    broker = ControlBroker(True)
    ready(broker, replace(climate(), mode="off", target_c=None))
    assert not broker.status()["can_set_temperature"]
    assert broker.status()["can_set_mode"]
    assert broker.submit_mode("heat")


def test_loaded_pending_intent_is_uncertain_never_a_new_request(tmp_path):
    path = tmp_path / "state.manual"
    save_intent(path, "binding", ManualIntent("temperature", 20, 100, "heat", 18, "none"))
    loaded = load_intent(path, "binding")
    assert loaded.outcome == "unconfirmed"
    assert loaded.outstanding
    assert loaded.confirmed(replace(climate(), target_c=20))
    assert not loaded.confirmed(climate())


@pytest.mark.parametrize(
    "mutation",
    [
        {"identity": "another"},
        {"version": True},
        {"version": 2},
        {"intent": {}},
        {
            "intent": {
                "kind": "mode",
                "value": "cool",
                "sent_at": 100,
                "baseline_mode": "heat",
                "baseline_target_c": 18,
                "baseline_preset": "none",
            }
        },
    ],
)
def test_invalid_or_unbound_journal_fails_closed(tmp_path, mutation):
    path = tmp_path / "state.manual"
    save_intent(path, "binding", ManualIntent("mode", "off", 100, "heat", 18, "none"))
    data = json.loads(path.read_text())
    data.update(mutation)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_intent(path, "binding")
