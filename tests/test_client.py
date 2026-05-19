"""Tests for the client module — probe_health and CortexClient construction."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Generator

import pytest

from hangarx_memory.client import CortexClient, probe_health


# ---------------------------------------------------------------------------
# Mini HTTP server for end-to-end probe tests (no monkeypatching required).
# ---------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    """In-process /health responder configured per test."""

    server_version = "StubCortex/0"
    payload: dict = {}
    status: int = 200

    def log_message(self, format, *args):  # silence test noise
        pass

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler API
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        body = json.dumps(self.payload).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def stub_server() -> Generator[tuple[str, type], None, None]:
    """Spin up a localhost stub server. Yields (base_url, handler_class)."""
    handler = type(
        "_HandlerCopy",
        (_StubHandler,),
        {"payload": {}, "status": 200},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", handler
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# probe_health
# ---------------------------------------------------------------------------


class TestProbeHealth:
    def test_empty_url_returns_none(self) -> None:
        assert probe_health("", timeout=0.1) is None

    def test_unreachable_host_returns_none(self) -> None:
        # Port 1 is reserved + unbound in practice — connection refused.
        assert probe_health("http://127.0.0.1:1", timeout=0.2) is None

    def test_healthy_response_returns_data(self, stub_server) -> None:
        base_url, handler = stub_server
        handler.payload = {
            "success": True,
            "data": {"status": "healthy", "ready": True, "services": {}},
        }
        result = probe_health(base_url, timeout=1.0)
        assert isinstance(result, dict)
        assert result["status"] == "healthy"
        assert result["ready"] is True

    def test_unwrapped_payload_returned_as_is(self, stub_server) -> None:
        # If Cortex ever stops wrapping in {success, data}, we still parse.
        base_url, handler = stub_server
        handler.payload = {"status": "healthy", "ready": True}
        result = probe_health(base_url, timeout=1.0)
        assert result == {"status": "healthy", "ready": True}

    def test_explicit_failure_returns_none(self, stub_server) -> None:
        base_url, handler = stub_server
        handler.payload = {"success": False, "error": "FalkorDB down"}
        assert probe_health(base_url, timeout=1.0) is None

    def test_non_2xx_returns_none(self, stub_server) -> None:
        base_url, handler = stub_server
        handler.status = 503
        handler.payload = {"status": "unhealthy"}
        assert probe_health(base_url, timeout=1.0) is None

    def test_non_object_payload_returns_none(self, stub_server) -> None:
        base_url, handler = stub_server
        handler.payload = ["array", "not", "object"]
        assert probe_health(base_url, timeout=1.0) is None


# ---------------------------------------------------------------------------
# CortexClient — just the construction shape; full API tests need a stack.
# ---------------------------------------------------------------------------


class TestCortexClient:
    def test_construct_with_minimal_args(self) -> None:
        client = CortexClient(base_url="http://localhost:3400", api_key="")
        assert client.base_url == "http://localhost:3400"
        assert client.api_key == ""
        assert client.auth_mode == "bearer"

    def test_trailing_slash_stripped(self) -> None:
        client = CortexClient(
            base_url="http://localhost:3400/", api_key="key"
        )
        assert client.base_url == "http://localhost:3400"

    def test_auth_mode_normalized_to_lowercase(self) -> None:
        client = CortexClient(
            base_url="http://x", api_key="k", auth_mode="X-API-KEY"
        )
        assert client.auth_mode == "x-api-key"

    def test_x_api_key_header(self) -> None:
        client = CortexClient(
            base_url="http://x", api_key="secret", auth_mode="x-api-key"
        )
        headers = client._headers()
        assert headers["x-api-key"] == "secret"
        assert "Authorization" not in headers

    def test_bearer_header(self) -> None:
        client = CortexClient(
            base_url="http://x", api_key="secret", auth_mode="bearer"
        )
        headers = client._headers()
        assert headers["Authorization"] == "Bearer secret"
        assert "x-api-key" not in headers

    def test_workspace_header_included_when_set(self) -> None:
        client = CortexClient(
            base_url="http://x",
            api_key="k",
            workspace_id="ws-123",
        )
        headers = client._headers()
        assert headers["x-workspace-id"] == "ws-123"

    def test_workspace_header_omitted_when_empty(self) -> None:
        client = CortexClient(base_url="http://x", api_key="k")
        headers = client._headers()
        assert "x-workspace-id" not in headers
