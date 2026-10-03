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
from .controller import Decision, evaluate
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
    ):
        self.config = config
        self.ha = ha
        self.gateway = gateway
        self.binding = binding
        self.clock = clock
        self.state = load_state(config.state_path, binding)
        self.state.surplus_since = None
        self._saved_signature = (
            journal_signature(self.state) if config.state_path.exists() else None
        )
        self._last_saved_at = clock()

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
        if not release:
            try:
                raw = self.gateway.get_energy()
                energy = Energy.parse(raw, self.clock(), self.config.policy.max_energy_age_seconds)
            except (IntegrationError, InvalidObservation):
                errors.append("energy_read_failed")
        before = copy.deepcopy(self.state)
        decision = evaluate(
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
                self.state = before
                decision = evaluate(
                    self.state, climate, energy, self.config.policy, self.clock(), active=True
                )
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
            save_state(self.config.state_path, self.binding, self.state)
            self._saved_signature = signature
            self._last_saved_at = now
        if decision.action in ("boost", "restore"):
            try:
                self.ha.set_temperature(
                    self.config.entity_id, native_temperature(decision.target_c, climate.unit)
                )
            except IntegrationError:
                errors.append("command_outcome_unconfirmed_no_retry")
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
    ha = gateway = None
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
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        with process_lock(config.state_path):
            service = Service(config, ha, gateway, identity(ha_url, config.entity_id))
            while not stop.is_set():
                result = service.tick(release=args.release)
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
                    return 1 if result["errors"] else 0
                stop.wait(config.poll_seconds)
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
        if gateway is not None:
            gateway.close()
        if ha is not None:
            ha.close()


if __name__ == "__main__":
    raise SystemExit(main())
