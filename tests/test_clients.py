"""Adapter contract and credential-boundary tests; no live thermostat requests."""

import json

import httpx
import pytest

from inverter_climate import clients
from inverter_climate.clients import GatewayClient, HomeAssistantClient, IntegrationError


def test_ha_reads_config_entity_and_discovery():
    thermostat = {
        "entity_id": "climate.test_furnace",
        "state": "heat",
        "attributes": {"temperature": 20},
    }
    config = {"unit_system": {"temperature": "°C"}}
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url)))
        assert request.headers["Authorization"] == "Bearer ha-test-token"
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.headers["User-Agent"] == "inverter-climate/0.1"
        if request.url.path == "/api/config":
            return httpx.Response(200, json=config)
        if request.url.path == "/api/states/climate.test_furnace":
            return httpx.Response(200, json=thermostat)
        return httpx.Response(200, json=[thermostat, {"entity_id": "sensor.test"}])

    client = HomeAssistantClient(
        "https://ha.example.invalid/", "ha-test-token", transport=httpx.MockTransport(handler)
    )
    assert client.get_config() == config
    assert client.get_climate("climate.test_furnace") == thermostat
    assert client.discover_climates() == [thermostat]
    assert seen == [
        ("GET", "https://ha.example.invalid/api/config"),
        ("GET", "https://ha.example.invalid/api/states/climate.test_furnace"),
        ("GET", "https://ha.example.invalid/api/states"),
    ]
    client.close()


def test_ha_set_temperature_calls_service_with_explicit_target():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/services/climate/set_temperature"
        assert json.loads(request.content) == {
            "entity_id": "climate.test_furnace",
            "temperature": 20.5,
        }
        return httpx.Response(200, json=[])

    client = HomeAssistantClient(
        "https://ha.example.invalid", "test-token", transport=httpx.MockTransport(handler)
    )
    assert client.set_temperature("climate.test_furnace", 20.5) is None
    assert len(seen) == 1
    client.close()


def test_gateway_forwards_configured_auth_and_returns_raw_contract():
    payload = {
        "schema_version": 1,
        "mqtt_connected": False,
        "metrics": {"solar_power": {"value": None}},
    }

    def handler(request):
        assert request.method == "GET"
        assert str(request.url) == "https://gateway.example.invalid/v1/energy"
        assert request.headers["Authorization"] == "Bearer read-test-token"
        assert request.headers["CF-Access-Client-Id"] == "test-client-id"
        assert request.headers["CF-Access-Client-Secret"] == "test-client-secret"
        assert request.headers["User-Agent"] == "inverter-climate/0.1"
        return httpx.Response(200, json=payload)

    client = GatewayClient(
        "https://gateway.example.invalid",
        "read-test-token",
        cf_client_id="test-client-id",
        cf_client_secret="test-client-secret",
        transport=httpx.MockTransport(handler),
    )
    assert client.get_energy() == payload
    client.close()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "file:///tmp/private",
        "ftp://example.invalid",
        "https:///missing-host",
        "https://user:secret@example.invalid",
        "https://@example.invalid",
        "https://example.invalid/api",
        "https://example.invalid//",
        "https://example.invalid/..",
        "https://example.invalid/%2f",
        "https://example.invalid?token=secret",
        "https://example.invalid?",
        "https://example.invalid#",
        "https://example.invalid#secret",
        " https://example.invalid",
        "https://example.invalid\n",
        "https://example.invalid\x00",
        "https://example.invalid\\@evil.invalid",
        "https://example.invalid:0",
        "https://example.invalid:65536",
        "https://example.invalid:not-a-port",
        "https://%65xample.invalid",
        "https://[invalid",
        None,
    ],
)
def test_ambiguous_or_credential_bearing_origins_are_rejected(url):
    with pytest.raises(IntegrationError) as error:
        HomeAssistantClient(url, "test-token")
    assert "secret" not in str(error.value)
    assert "example.invalid" not in str(error.value)


@pytest.mark.parametrize(
    "url", ["http://localhost:8123", "https://ha.example.invalid/", "http://[::1]:8123"]
)
def test_valid_origins_support_local_and_https_connections(url):
    client = HomeAssistantClient(
        url, "test-token", transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
    )
    assert client.get_config() == {}
    client.close()


@pytest.mark.parametrize(
    "entity",
    [
        "",
        "climate.",
        "climate.Test",
        "climate.a/b",
        "climate.a?token=secret",
        "sensor.thermostat",
        "climate.test\n",
        "climate.é",
        "climate.test-1",
        None,
        True,
    ],
)
def test_invalid_entity_ids_never_send_requests(entity):
    seen = []
    client = HomeAssistantClient(
        "https://ha.example.invalid",
        "test-token",
        transport=httpx.MockTransport(lambda req: seen.append(req)),
    )
    with pytest.raises(IntegrationError):
        client.get_climate(entity)
    with pytest.raises(IntegrationError):
        client.set_temperature(entity, 20)
    assert seen == []
    client.close()


@pytest.mark.parametrize(
    "temperature", [True, False, float("inf"), float("-inf"), float("nan"), "20", None, 10**1000]
)
def test_invalid_temperatures_never_send_requests(temperature):
    seen = []
    client = HomeAssistantClient(
        "https://ha.example.invalid",
        "test-token",
        transport=httpx.MockTransport(lambda req: seen.append(req)),
    )
    with pytest.raises(IntegrationError):
        client.set_temperature("climate.test", temperature)
    assert seen == []
    client.close()


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 429, 500, 503])
def test_http_errors_and_redirects_never_leak_response_or_forward_credentials(status):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://evil.invalid/token"},
            text="private-response-test-token",
        )

    client = GatewayClient(
        "https://gateway.example.invalid",
        "private-test-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(IntegrationError) as error:
        client.get_energy()
    assert str(error.value) == f"Gateway request failed (HTTP {status})."
    assert len(seen) == 1
    client.close()


def test_network_error_is_sanitized_and_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectTimeout("private URL and private-test-token", request=request)

    client = GatewayClient(
        "https://gateway.example.invalid",
        "private-test-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(IntegrationError) as error:
        client.get_energy()
    assert str(error.value) == "Gateway request failed or timed out."
    assert error.value.__suppress_context__ is True
    assert len(calls) == 1
    client.close()


@pytest.mark.parametrize(
    "body", [b"private body", b'{"bad": NaN}', b'{"bad": Infinity}', b"\xff", b"[" * 2000]
)
def test_malformed_json_is_sanitized(body):
    client = HomeAssistantClient(
        "https://ha.example.invalid",
        "test-token",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
    )
    with pytest.raises(IntegrationError, match="returned invalid JSON"):
        client.get_config()
    client.close()


def test_config_and_energy_require_object_responses():
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=[]))
    for client in [
        HomeAssistantClient("https://ha.example.invalid", "test-token", transport=transport),
        GatewayClient("https://gateway.example.invalid", "test-token", transport=transport),
    ]:
        with pytest.raises(IntegrationError, match="invalid object response"):
            client.get_config() if isinstance(client, HomeAssistantClient) else client.get_energy()
        client.close()


def test_wrong_climate_response_is_rejected():
    client = HomeAssistantClient(
        "https://ha.example.invalid",
        "test-token",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"entity_id": "climate.wrong"})
        ),
    )
    with pytest.raises(IntegrationError, match="different climate entity"):
        client.get_climate("climate.test")
    client.close()


@pytest.mark.parametrize("body", [{}, [None], ["climate.test"]])
def test_discovery_and_service_reject_malformed_response_shapes(body):
    client = HomeAssistantClient(
        "https://ha.example.invalid",
        "test-token",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )
    with pytest.raises(IntegrationError, match="invalid states response"):
        client.discover_climates()
    with pytest.raises(IntegrationError, match="invalid service response"):
        client.set_temperature("climate.test", 20)
    client.close()


def test_discovery_does_not_surface_invalid_entity_ids():
    entities = [
        {"entity_id": item} for item in ["climate.test", "climate.Bad", "climate.test/path", None]
    ]
    client = HomeAssistantClient(
        "https://ha.example.invalid",
        "test-token",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=entities)),
    )
    assert client.discover_climates() == [{"entity_id": "climate.test"}]
    client.close()


@pytest.mark.parametrize("length", ["-1", "not-a-number", str(clients.MAX_RESPONSE_BYTES + 1)])
def test_response_length_is_checked_before_reading(length):
    def handler(request):
        return httpx.Response(
            200, headers={"Content-Length": length}, stream=httpx.ByteStream(b"{}")
        )

    client = HomeAssistantClient(
        "https://ha.example.invalid", "test-token", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(IntegrationError):
        client.get_config()
    client.close()


@pytest.mark.parametrize("with_length", [True, False])
def test_oversized_bodies_are_rejected_even_without_content_length(monkeypatch, with_length):
    monkeypatch.setattr(clients, "MAX_RESPONSE_BYTES", 100)

    def handler(request):
        headers = {"Content-Length": "102"} if with_length else {}
        return httpx.Response(
            200, headers=headers, stream=httpx.ByteStream(b'"' + b"a" * 100 + b'"')
        )

    client = HomeAssistantClient(
        "https://ha.example.invalid", "test-token", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(IntegrationError, match="exceeds the size limit"):
        client.get_config()
    client.close()


def test_compressed_response_is_rejected_without_decompression():
    client = HomeAssistantClient(
        "https://ha.example.invalid",
        "test-token",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip")
            )
        ),
    )
    with pytest.raises(IntegrationError, match="unsupported content encoding"):
        client.get_config()
    client.close()


@pytest.mark.parametrize(
    "token", ["", " leading", "trailing ", "with space", "line\nbreak", "nonascii-ä", None]
)
def test_invalid_credentials_fail_without_echoing_value(token):
    with pytest.raises(IntegrationError) as error:
        HomeAssistantClient("https://ha.example.invalid", token)
    assert str(error.value) == "Home Assistant token is missing or invalid."


@pytest.mark.parametrize(
    "cf_id,cf_secret", [("id", ""), ("", "secret"), (None, ""), ("id", "bad\nsecret")]
)
def test_cloudflare_credentials_are_a_valid_complete_pair(cf_id, cf_secret):
    with pytest.raises(IntegrationError):
        GatewayClient(
            "https://gateway.example.invalid",
            "test-token",
            cf_client_id=cf_id,
            cf_client_secret=cf_secret,
        )


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan"), "10", None, 10**1000])
def test_timeout_must_be_finite_positive_number(timeout):
    with pytest.raises(IntegrationError, match="timeout must be a finite positive number"):
        HomeAssistantClient("https://ha.example.invalid", "test-token", timeout_seconds=timeout)


def test_transport_preserves_tls_and_does_not_use_environment_proxy_or_redirects(monkeypatch):
    options = {}
    real_client = httpx.Client

    def capture_client(**kwargs):
        options.update(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr(clients.httpx, "Client", capture_client)
    client = HomeAssistantClient(
        "https://ha.example.invalid",
        "test-token",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
    )
    assert options["verify"] is True
    assert options["trust_env"] is False
    assert options["follow_redirects"] is False
    client.close()
