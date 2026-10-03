"""Small long-running service with a read-only observation default."""

import argparse
import copy
import json
import os
import signal
import sys
import threading
import time
from dataclasses import asdict
from typing import Protocol

from . import __version__
from .clients import GatewayClient, HomeAssistantClient, IntegrationError
from .config import Config
from .control import ControlBroker, ManualIntent, load_intent, manual_path, save_intent
from .controller import Decision, evaluate, relinquish, same
from .models import Climate, Energy, InvalidObservation, native_temperature
from .storage import atomic_json, identity, load_state, process_lock, save_state


def required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is required")
    return value


class EnergyClient(Protocol):
    def get_energy(self) -> dict: ...

    def close(self) -> None: ...


def make_energy_client(config: Config) -> EnergyClient:
    if config.energy.backend == "venus":
        from .venus import VenusEnergyClient

        return VenusEnergyClient(
            solar_paths=config.energy.solar_paths,
            grid_phases=config.energy.grid_phases,
            timeout_seconds=config.energy.timeout_seconds,
        )
    return GatewayClient(
        required_env("GATEWAY_BASE_URL"),
        required_env("GATEWAY_READ_TOKEN"),
        cf_client_id=os.environ.get("CF_ACCESS_CLIENT_ID", ""),
        cf_client_secret=os.environ.get("CF_ACCESS_CLIENT_SECRET", ""),
        timeout_seconds=config.energy.timeout_seconds,
    )


def make_device_publisher(config: Config, binding: str, command_broker=None):
    if config.energy.backend != "venus" or not config.device.enabled:
        return None
    from .dbus_device import DbusDevicePublisher

    return DbusDevicePublisher(
        identity=binding,
        device_instance=config.device.device_instance,
        custom_name=config.device.custom_name,
        stale_seconds=config.device.stale_seconds,
        firmware_version=__version__,
        command_broker=command_broker,
    )


def journal_signature(state) -> dict:
    # Poll timestamps and surplus qualification are transient. Restarting always
    # requalifies surplus, so stable observation needs no recurring SD-card writes.
    return {
        key: value
        for key, value in asdict(state).items()
        if key not in ("last_tick", "surplus_since")
    }


class Service:
    def __init__(
        self,
        config: Config,
        ha: HomeAssistantClient,
        gateway: EnergyClient,
        binding: str,
        *,
        clock=time.time,
        command_broker: ControlBroker | None = None,
    ):
        self.config = config
        self.ha = ha
        self.gateway = gateway
        self.binding = binding
        self.clock = clock
        self.controls = command_broker or ControlBroker(config.device.control_enabled)
        self._manual_path = manual_path(config.state_path)
        self._intent = load_intent(self._manual_path, binding)
        self._control_reason = "awaiting_observation"
        self._control_outcome = self._intent.outcome if self._intent else "idle"
        self.state = load_state(config.state_path, binding)
        self.state.surplus_since = None
        self._saved_signature = (
            journal_signature(self.state) if config.state_path.exists() else None
        )
        self._last_saved_at = clock()

    def _save_state(self):
        save_state(self.config.state_path, self.binding, self.state)
        self._saved_signature = journal_signature(self.state)
        self._last_saved_at = self.clock()

    def _manual_hold(self, climate):
        relinquish(self.state, self.clock(), self.config.policy)
        if climate is not None:
            self.state.observed_target_c = climate.target_c
            self.state.observed_mode = climate.mode
            self.state.observed_preset = climate.preset
        self._save_state()

    @property
    def manual_outstanding(self) -> bool:
        return self._intent is not None and self._intent.outstanding

    def next_poll_seconds(self) -> float:
        controls = self.controls.status()
        if controls["available"] and controls["pending"]:
            return 0
        if self._intent is not None and self._intent.outcome == "pending":
            return min(5, self.config.poll_seconds)
        return self.config.poll_seconds

    def _update_controls(self, climate, *, refresh=True):
        outstanding = self.manual_outstanding
        auto_pending = self.state.phase in ("pending_boost", "pending_restore")
        reason = self._control_reason
        if auto_pending:
            reason = "automatic_command_unconfirmed"
        elif climate is None:
            reason = "thermostat_unavailable"
        elif not self.config.device.control_enabled:
            reason = "controls_disabled"
        elif reason == "awaiting_observation":
            reason = "ready"
        self.controls.update(
            climate,
            available=self.config.device.control_enabled and not outstanding and not auto_pending,
            outcome=self._control_outcome,
            reason=reason,
            refresh=refresh,
        )

    def _resolve_manual_intent(self, intent, climate) -> Decision:
        if climate is not None and intent.confirmed(climate):
            intent.outcome = "confirmed"
            self._control_reason = "manual_command_confirmed"
        elif climate is not None and intent.externally_changed(climate):
            intent.outcome = "rejected"
            self._control_reason = "external_change_respected"
            self.controls.discard()
        else:
            elapsed = self.clock() - intent.sent_at
            if elapsed < 0 or elapsed >= self.config.policy.confirmation_seconds:
                if intent.outcome != "unconfirmed":
                    intent.outcome = "unconfirmed"
                    save_intent(self._manual_path, self.binding, intent)
                self.controls.discard()
            self._control_reason = "manual_command_unconfirmed_no_retry"
            self._control_outcome = intent.outcome
            return Decision("wait", self._control_reason)
        self._manual_hold(climate)
        save_intent(self._manual_path, self.binding, intent)
        self._control_outcome = intent.outcome
        return Decision("wait", self._control_reason)

    def _manual_command(self, climate, errors) -> Decision | None:
        """Resolve observation first, then consume at most one explicit request."""
        intent = self._intent
        if intent is not None and intent.outstanding:
            return self._resolve_manual_intent(intent, climate)
        if self.state.phase in ("pending_boost", "pending_restore"):
            self.controls.discard()
            return None
        request = self.controls.take()
        if request is None:
            return None
        self.controls.suspend()
        self._control_outcome = "rejected"
        native, rejection = self._manual_preflight(request, climate)
        if rejection is not None:
            return rejection
        return self._dispatch_manual_request(request, climate, native, errors)

    def _manual_preflight(self, request, climate) -> tuple[float | str | None, Decision | None]:
        if not self.config.device.control_enabled:
            self.controls.discard()
            self._control_reason = "controls_disabled"
            return None, Decision("wait", self._control_reason)
        if climate is None or not self.controls.fresh_request(request):
            self.controls.discard()
            self._control_reason = "manual_request_expired_or_unavailable"
            return None, Decision("wait", self._control_reason)
        baseline = request.baseline
        if (
            climate.mode != baseline.mode
            or climate.preset != baseline.preset
            or not (
                climate.target_c is None
                and baseline.target_c is None
                or same(climate.target_c, baseline.target_c)
            )
        ):
            self.controls.discard()
            self._control_reason = "external_change_respected"
            self._manual_hold(climate)
            return None, Decision("wait", self._control_reason)
        try:
            native = (
                climate.temperature_command(request.value)
                if request.kind == "temperature"
                else climate.hvac_mode_command(request.value)
            )
        except (ValueError, TypeError):
            self.controls.discard()
            self._control_reason = "manual_capability_restriction"
            return None, Decision("wait", self._control_reason)
        return native, None

    def _dispatch_manual_request(self, request, climate, native, errors) -> Decision:
        self.controls.suspend()
        # Preserve existing ownership until the manual intent is durable. If
        # the subsequent state write fails, recovery retains both the original
        # obligation and an uncertain intent; it cannot send or retry a POST.
        previous_state = copy.deepcopy(self.state)
        intent = ManualIntent(
            request.kind,
            request.value,
            self.clock(),
            climate.mode,
            climate.target_c,
            climate.preset,
        )
        self._intent = intent
        save_intent(self._manual_path, self.binding, intent)
        already_confirmed = intent.confirmed(climate)
        if not already_confirmed:
            self.state.last_command = self.clock()
        self._manual_hold(climate)
        if not self.controls.fresh_request(request):
            # This branch is known to precede every POST. Restore the previous
            # automatic obligation durably before declaring the intent rejected.
            # If restoring fails, the pending journal still blocks unsafe work.
            self.state = previous_state
            self._save_state()
            intent.outcome = "rejected"
            self.controls.discard()
            self._control_reason = "manual_request_expired_or_unavailable"
            self._control_outcome = intent.outcome
            save_intent(self._manual_path, self.binding, intent)
            return Decision("wait", self._control_reason)
        if already_confirmed:
            intent.outcome = "confirmed"
            save_intent(self._manual_path, self.binding, intent)
        self._control_reason = (
            "manual_command_confirmed"
            if intent.outcome == "confirmed"
            else "manual_command_pending"
        )
        if intent.outstanding:
            self._control_outcome = intent.outcome
            self._update_controls(climate, refresh=False)
            try:
                if request.kind == "temperature":
                    self.ha.set_temperature(self.config.entity_id, native)
                else:
                    self.ha.set_hvac_mode(self.config.entity_id, native)
            except IntegrationError:
                intent.outcome = "unconfirmed"
                self.controls.discard()
                self._control_reason = "manual_command_unconfirmed_no_retry"
                save_intent(self._manual_path, self.binding, intent)
                errors.append(self._control_reason)
        self._control_outcome = intent.outcome
        self._update_controls(climate, refresh=False)
        return Decision("wait", self._control_reason)

    def read_climate(self) -> Climate:
        config = self.ha.get_config()
        units = config.get("unit_system", {})
        if not isinstance(units, dict):
            raise InvalidObservation("HA temperature units unavailable")
        return Climate.parse(
            self.ha.get_climate(self.config.entity_id),
            self.config.entity_id,
            units.get("temperature"),
        )

    def tick(self, *, release: bool = False) -> dict:
        climate = None
        energy = None
        errors = []
        try:
            climate = self.read_climate()
        except (IntegrationError, InvalidObservation):
            errors.append("thermostat_read_failed")
        self._update_controls(climate)
        manual_decision = self._manual_command(climate, errors)
        if not release:
            try:
                raw = self.gateway.get_energy()
                energy = Energy.parse(raw, self.clock(), self.config.policy.max_energy_age_seconds)
            except (IntegrationError, InvalidObservation):
                errors.append("energy_read_failed")
        before = copy.deepcopy(self.state)
        decision = manual_decision or evaluate(
            self.state,
            climate,
            energy,
            self.config.policy,
            self.clock(),
            active=self.config.mode == "active",
        )
        if decision.action in ("boost", "restore"):
            # Recheck the actual target/mode immediately before writing. HA/Nest
            # has no compare-and-set API: an external change after this read is
            # still a possible race, documented rather than hidden.
            try:
                if decision.action == "boost" and not release:
                    raw = self.gateway.get_energy()
                climate = self.read_climate()
                if decision.action == "boost" and not release:
                    energy = Energy.parse(
                        raw, self.clock(), self.config.policy.max_energy_age_seconds
                    )
            except (IntegrationError, InvalidObservation):
                self.state = before
                self.state.surplus_since = None
                decision = Decision("wait", "command_preflight_failed")
                errors.append("command_preflight_failed")
            else:
                self.state = copy.deepcopy(before)
                decision = evaluate(
                    self.state, climate, energy, self.config.policy, self.clock(), active=True
                )
        if decision.action in ("boost", "restore") and self.controls.suspend():
            # A request accepted during the HA preflight has manual priority.
            self.state = before
            decision = Decision("wait", "manual_request_queued")
        # A failure here stops the process before a command is sent. Preserve
        # ownership/manual changes immediately; checkpoint owned boosts every
        # minute, while unchanged observation leaves persistent flash untouched.
        signature = journal_signature(self.state)
        now = self.clock()
        checkpoint = self.state.phase != "idle" and (
            now - self._last_saved_at >= 60 or now < self._last_saved_at
        )
        if (
            signature != self._saved_signature
            or checkpoint
            or decision.action in ("boost", "restore")
        ):
            self._save_state()
        if decision.action in ("boost", "restore"):
            self._update_controls(climate, refresh=False)
            try:
                self.ha.set_temperature(
                    self.config.entity_id, native_temperature(decision.target_c, climate.unit)
                )
            except IntegrationError:
                errors.append("command_outcome_unconfirmed_no_retry")
        self._update_controls(climate, refresh=False)
        result = {
            "schema_version": 1,
            "generated_at": self.clock(),
            "mode": self.config.mode,
            "energy_backend": self.config.energy.backend,
            "phase": self.state.phase,
            "decision": asdict(decision),
            "climate": asdict(climate) if climate else None,
            "energy": asdict(energy) if energy else None,
            "estimated_heating_power_w": self.config.policy.heating_power_w,
            "errors": errors,
            "control": self.controls.status(),
        }
        atomic_json(self.config.status_path, result)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--once", action="store_true", help="evaluate once and exit")
    parser.add_argument(
        "--discover", action="store_true", help="list HA climate entities (read-only)"
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="release an owned boost; active mode required to write",
    )
    args = parser.parse_args()
    ha = gateway = publisher = None
    try:
        ha_url = required_env("HA_BASE_URL")
        ha = HomeAssistantClient(ha_url, required_env("HA_TOKEN"))
        if args.discover:
            for entity in ha.discover_climates():
                print(
                    json.dumps({"entity_id": entity.get("entity_id"), "state": entity.get("state")})
                )
            return 0
        config = Config.load(args.config)
        gateway = make_energy_client(config)
        stop = threading.Event()
        with process_lock(config.state_path):
            binding = identity(ha_url, config.entity_id)
            service = Service(config, ha, gateway, binding)

            def stop_service(*_args):
                stop.set()
                service.controls.wake()

            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, stop_service)
            # Foreground discovery/release checks must remain usable without a
            # publishing bus, and one-shot probes should not churn GUI devices.
            if not args.once and not args.release:
                publisher = make_device_publisher(config, binding, service.controls)
                if publisher is not None:
                    publisher.start()
            while not stop.is_set():
                if publisher is not None:
                    publisher.check_health()
                result = service.tick(release=args.release)
                if publisher is not None:
                    publisher.publish(result)
                # Logs omit entity identity, endpoints and credentials.
                print(
                    json.dumps(
                        {
                            key: result[key]
                            for key in ("generated_at", "mode", "phase", "decision", "errors")
                        }
                    ),
                    flush=True,
                )
                if args.once or (args.release and service.state.phase == "idle"):
                    return (
                        1
                        if result["errors"] or (args.release and service.manual_outstanding)
                        else 0
                    )
                service.controls.wait(service.next_poll_seconds())
        return 0
    except (ValueError, OSError, IntegrationError) as exc:
        # Untrusted exceptions can embed paths/URLs/response bodies. Never dump
        # them to public CI logs; detailed diagnosis uses local configuration.
        print(
            f"inverter-climate stopped: {type(exc).__name__}; check configuration/access/state",
            file=sys.stderr,
        )
        return 2
    finally:
        if publisher is not None:
            publisher.close()
        if gateway is not None:
            gateway.close()
        if ha is not None:
            ha.close()


if __name__ == "__main__":
    raise SystemExit(main())
