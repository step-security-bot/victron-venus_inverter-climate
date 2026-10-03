"""Small synchronous adapters for Home Assistant and inverter-gateway.

Google/Nest authorization remains owned by Home Assistant. These clients only
contact their configured origins and never create artificial HA entity states.
"""

from __future__ import annotations

import json
import math
import re
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_CLIMATE_ENTITY = re.compile(r"climate\.[a-z0-9_]+", flags=re.ASCII)


class IntegrationError(Exception):
    """A safe-to-log integration failure, without credentials or response data."""


def _origin(base_url: str) -> str:
    """Require an unambiguous origin, so a base path cannot alter the API target."""
    message = "Integration URL must be an HTTP(S) origin without credentials or a path."
    if (
        not isinstance(base_url, str)
        or not base_url
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in base_url
        )
        or any(character in base_url for character in ("\\", "?", "#"))
    ):
        raise IntegrationError(message)
    try:
        parts = urlsplit(base_url)
        if (
            parts.scheme.lower() not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or "%" in parts.netloc
        ):
            raise ValueError
        # Accessing port also validates malformed and out-of-range ports.
        if parts.port is not None and parts.port < 1:
            raise ValueError
        url = httpx.URL(base_url)
        if not url.is_absolute_url or not url.host:
            raise ValueError
        return str(url.copy_with(path="/"))
    except (ValueError, httpx.InvalidURL):
        raise IntegrationError(message) from None


def _credential(value: str, label: str, *, optional: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not optional and not value)
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise IntegrationError(f"{label} is missing or invalid.")
    return value


def _entity_id(value: str) -> str:
    if not isinstance(value, str) or _CLIMATE_ENTITY.fullmatch(value) is None:
        raise IntegrationError("A valid climate entity ID is required.")
    return value


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError("Non-finite JSON number")


class _JsonClient:
    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        *,
        label: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None,
    ) -> None:
        origin = _origin(base_url)
        try:
            valid_timeout = (
                not isinstance(timeout_seconds, bool)
                and isinstance(timeout_seconds, (int, float))
                and math.isfinite(timeout_seconds)
                and timeout_seconds > 0
            )
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise IntegrationError("Integration timeout must be a finite positive number.")
        self._label = label
        self._origin = origin
        self._client = httpx.Client(
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "inverter-climate/0.1",
                **headers,
            },
            timeout=timeout_seconds,
            transport=transport,
            verify=True,
            follow_redirects=False,
            trust_env=False,
        )

    def _request(self, method: str, path: str, *, body: dict | None = None) -> object:
        try:
            with self._client.stream(method, self._origin + path, json=body) as response:
                if not 200 <= response.status_code < 300:
                    raise IntegrationError(
                        f"{self._label} request failed (HTTP {response.status_code})."
                    )
                # Request identity transfer encoding and reject compressed replies
                # to bound memory even for a malicious compressed response body.
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise IntegrationError(
                        f"{self._label} returned an unsupported content encoding."
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        length = int(content_length)
                    except ValueError:
                        raise IntegrationError(
                            f"{self._label} returned an invalid response length."
                        ) from None
                    if length < 0 or length > MAX_RESPONSE_BYTES:
                        raise IntegrationError(f"{self._label} response exceeds the size limit.")
                chunks = bytearray()
                stream = (
                    (response.content,)
                    if response.is_stream_consumed
                    else response.iter_raw(chunk_size=65536)
                )
                for chunk in stream:
                    if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise IntegrationError(f"{self._label} response exceeds the size limit.")
                    chunks.extend(chunk)
                try:
                    return json.loads(chunks, parse_constant=_reject_nonfinite_json)
                except (ValueError, UnicodeError, RecursionError):
                    raise IntegrationError(f"{self._label} returned invalid JSON.") from None
        except httpx.HTTPError:
            raise IntegrationError(f"{self._label} request failed or timed out.") from None

    def _object(self, method: str, path: str) -> dict:
        result = self._request(method, path)
        if not isinstance(result, dict):
            raise IntegrationError(f"{self._label} returned an invalid object response.")
        return result

    def close(self) -> None:
        self._client.close()


class HomeAssistantClient(_JsonClient):
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 10,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(
            base_url,
            {"Authorization": f"Bearer {_credential(token, 'Home Assistant token')}"},
            label="Home Assistant",
            timeout_seconds=timeout_seconds,
            transport=transport,
        )

    def get_config(self) -> dict:
        return self._object("GET", "api/config")

    def get_climate(self, entity_id: str) -> dict:
        entity_id = _entity_id(entity_id)
        result = self._object("GET", f"api/states/{entity_id}")
        if result.get("entity_id") != entity_id:
            raise IntegrationError("Home Assistant returned a different climate entity.")
        return result

    def discover_climates(self) -> list[dict]:
        result = self._request("GET", "api/states")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise IntegrationError("Home Assistant returned an invalid states response.")
        return [
            item
            for item in result
            if isinstance(item.get("entity_id"), str)
            and _CLIMATE_ENTITY.fullmatch(item["entity_id"]) is not None
        ]

    def set_temperature(self, entity_id: str, temperature: float) -> None:
        entity_id = _entity_id(entity_id)
        try:
            valid_temperature = (
                not isinstance(temperature, bool)
                and isinstance(temperature, (int, float))
                and math.isfinite(temperature)
            )
        except OverflowError:
            valid_temperature = False
        if not valid_temperature:
            raise IntegrationError("Temperature must be a finite number.")
        result = self._request(
            "POST",
            "api/services/climate/set_temperature",
            body={"entity_id": entity_id, "temperature": temperature},
        )
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise IntegrationError("Home Assistant returned an invalid service response.")


class GatewayClient(_JsonClient):
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        cf_client_id: str = "",
        cf_client_secret: str = "",
        timeout_seconds: float = 10,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {_credential(token, 'Gateway token')}"}
        cf_client_id = _credential(cf_client_id, "Cloudflare client ID", optional=True)
        cf_client_secret = _credential(cf_client_secret, "Cloudflare client secret", optional=True)
        if bool(cf_client_id) != bool(cf_client_secret):
            raise IntegrationError("Cloudflare client ID and secret must be provided together.")
        if cf_client_id:
            headers["CF-Access-Client-Id"] = cf_client_id
            headers["CF-Access-Client-Secret"] = cf_client_secret
        super().__init__(
            base_url,
            headers,
            label="Gateway",
            timeout_seconds=timeout_seconds,
            transport=transport,
        )

    def get_energy(self) -> dict:
        return self._object("GET", "v1/energy")
