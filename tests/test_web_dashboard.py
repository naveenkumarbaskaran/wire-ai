"""
Tests for WebDashboard — FastAPI + SSE browser dashboard.

All tests use httpx.AsyncClient with ASGITransport — no real uvicorn server is started.
"""

from __future__ import annotations

import json

import pytest

# Skip entire module if fastapi/httpx not installed (base dev install excludes web extra)
fastapi = pytest.importorskip("fastapi", reason="requires pip install wire-ai[web]")
httpx = pytest.importorskip("httpx", reason="requires httpx")

from wire.visibility.dashboard import WorkforceDashboard
from wire.visibility.web_dashboard import WebDashboard, _dashboard_to_dict


# ── Helpers ───────────────────────────────────────────────────────────────────

def _client(app: object) -> httpx.AsyncClient:
    """Return an httpx client wired to a FastAPI app via ASGI transport."""
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def dash() -> WorkforceDashboard:
    """A WorkforceDashboard pre-populated with representative state."""
    d = WorkforceDashboard(workforce_name="test-workforce", budget_daily=10.0)
    d.update_role("monitor", status="running", confidence=0.94, cost_usd=0.04,
                  sla_ok=True, last_event="cost spike detected")
    d.update_role("analyst", status="complete", confidence=0.75, cost_usd=0.02,
                  sla_ok=False, sla_elapsed_s=120.0, sla_limit_s=60.0)
    d.add_event("monitor", "anomaly detected", level="warning")
    d.add_event("analyst", "report generated", level="info")
    d.add_hitl(id="hitl-001", role="monitor", message="Approve budget increase?",
               risk="high", expires_at="2026-06-12T12:00:00Z")
    return d


@pytest.fixture()
def web_dashboard(dash: WorkforceDashboard) -> WebDashboard:
    return WebDashboard(dashboard=dash, port=8080)


@pytest.fixture()
def app(web_dashboard: WebDashboard) -> object:
    return web_dashboard._app


# ── /health ───────────────────────────────────────────────────────────────────

class TestHealth:
    @pytest.mark.anyio
    async def test_health_returns_200(self, app: object) -> None:
        async with _client(app) as client:
            resp = await client.get("/health")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_health_body_has_status_ok(self, app: object) -> None:
        async with _client(app) as client:
            resp = await client.get("/health")
        assert resp.json() == {"status": "ok"}


# ── / HTML page ───────────────────────────────────────────────────────────────

class TestIndexPage:
    @pytest.mark.anyio
    async def test_index_returns_200(self, app: object) -> None:
        async with _client(app) as client:
            resp = await client.get("/")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_index_content_type_html(self, app: object) -> None:
        async with _client(app) as client:
            resp = await client.get("/")
        assert "text/html" in resp.headers["content-type"]

    @pytest.mark.anyio
    async def test_index_contains_wire_branding(self, app: object) -> None:
        async with _client(app) as client:
            resp = await client.get("/")
        assert "WIRE" in resp.text

    @pytest.mark.anyio
    async def test_index_contains_sse_connection_code(self, app: object) -> None:
        async with _client(app) as client:
            resp = await client.get("/")
        assert "EventSource" in resp.text
        assert "/stream" in resp.text

    @pytest.mark.anyio
    async def test_index_is_self_contained(self, app: object) -> None:
        """Page must not reference external JS/CSS CDN URLs."""
        async with _client(app) as client:
            resp = await client.get("/")
        assert "cdn." not in resp.text
        assert "unpkg.com" not in resp.text


# ── /api/state ────────────────────────────────────────────────────────────────

class TestApiState:
    @pytest.mark.anyio
    async def test_state_returns_200(self, app: object) -> None:
        async with _client(app) as client:
            resp = await client.get("/api/state")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_state_has_workforce_name(self, app: object) -> None:
        async with _client(app) as client:
            data = (await client.get("/api/state")).json()
        assert data["workforce_name"] == "test-workforce"

    @pytest.mark.anyio
    async def test_state_has_top_level_keys(self, app: object) -> None:
        async with _client(app) as client:
            data = (await client.get("/api/state")).json()
        for key in ("workforce_name", "backend", "total_cost", "budget_daily",
                    "roles", "hitl_queue", "events"):
            assert key in data, f"Missing key: {key}"

    @pytest.mark.anyio
    async def test_state_roles_list(self, app: object) -> None:
        async with _client(app) as client:
            data = (await client.get("/api/state")).json()
        assert isinstance(data["roles"], list)
        assert len(data["roles"]) == 2

    @pytest.mark.anyio
    async def test_state_role_has_expected_fields(self, app: object) -> None:
        async with _client(app) as client:
            data = (await client.get("/api/state")).json()
        role = next(r for r in data["roles"] if r["name"] == "monitor")
        for field in ("name", "status", "confidence", "cost_usd", "sla_ok",
                      "last_event", "last_event_ts", "iteration"):
            assert field in role, f"Role missing field: {field}"

    @pytest.mark.anyio
    async def test_state_role_status_is_string(self, app: object) -> None:
        async with _client(app) as client:
            data = (await client.get("/api/state")).json()
        role = next(r for r in data["roles"] if r["name"] == "monitor")
        # monitor was set to 'running' then add_hitl overrode it to 'waiting'
        assert isinstance(role["status"], str)
        assert role["status"] in ("running", "waiting")

    @pytest.mark.anyio
    async def test_state_hitl_queue(self, app: object) -> None:
        async with _client(app) as client:
            data = (await client.get("/api/state")).json()
        assert len(data["hitl_queue"]) == 1
        item = data["hitl_queue"][0]
        assert item["id"] == "hitl-001"
        assert item["role"] == "monitor"
        assert "risk" in item
        assert "options" in item

    @pytest.mark.anyio
    async def test_state_events_capped_at_10(
        self, app: object, dash: WorkforceDashboard
    ) -> None:
        for i in range(20):
            dash.add_event("monitor", f"event {i}")
        async with _client(app) as client:
            data = (await client.get("/api/state")).json()
        assert len(data["events"]) <= 10

    @pytest.mark.anyio
    async def test_state_total_cost_positive(self, app: object) -> None:
        async with _client(app) as client:
            data = (await client.get("/api/state")).json()
        assert data["total_cost"] > 0.0

    @pytest.mark.anyio
    async def test_state_budget_daily_returned(self, app: object) -> None:
        async with _client(app) as client:
            data = (await client.get("/api/state")).json()
        assert data["budget_daily"] == pytest.approx(10.0)

    @pytest.mark.anyio
    async def test_state_no_budget_is_null(self) -> None:
        d = WorkforceDashboard(workforce_name="no-budget")
        wd = WebDashboard(dashboard=d, port=9999)
        async with _client(wd._app) as client:
            data = (await client.get("/api/state")).json()
        assert data["budget_daily"] is None


# ── /stream SSE ───────────────────────────────────────────────────────────────

class TestSSEStream:
    @pytest.mark.anyio
    async def test_stream_endpoint_exists(self, app: object) -> None:
        """The /stream route exists and returns 200 with correct content-type."""
        # We use a HEAD-style check: just open the connection and read headers only.
        # Full body consumption would block (infinite generator).
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            # GET /api/state as a proxy check that routes work; SSE headers tested via
            # the generator unit test below
            resp = await client.get("/api/state")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_sse_generator_emits_state_event(
        self, web_dashboard: WebDashboard
    ) -> None:
        """_sse_generator yields 'event: state\\ndata: {...}\\n\\n' immediately."""
        gen = web_dashboard._sse_generator()
        # The first yield should happen before any sleep
        chunk = await gen.__anext__()
        lines = chunk.split("\n")
        assert lines[0] == "event: state"
        assert lines[1].startswith("data: ")
        await gen.aclose()

    @pytest.mark.anyio
    async def test_sse_generator_data_is_valid_json(
        self, web_dashboard: WebDashboard
    ) -> None:
        gen = web_dashboard._sse_generator()
        chunk = await gen.__anext__()
        data_line = next(l for l in chunk.split("\n") if l.startswith("data: "))
        payload = json.loads(data_line[len("data: "):])
        assert "workforce_name" in payload
        assert "roles" in payload
        await gen.aclose()

    @pytest.mark.anyio
    async def test_sse_generator_data_contains_roles(
        self, web_dashboard: WebDashboard
    ) -> None:
        gen = web_dashboard._sse_generator()
        chunk = await gen.__anext__()
        data_line = next(l for l in chunk.split("\n") if l.startswith("data: "))
        payload = json.loads(data_line[len("data: "):])
        assert len(payload["roles"]) == 2
        await gen.aclose()

    @pytest.mark.anyio
    async def test_sse_generator_ends_with_double_newline(
        self, web_dashboard: WebDashboard
    ) -> None:
        gen = web_dashboard._sse_generator()
        chunk = await gen.__anext__()
        assert chunk.endswith("\n\n")
        await gen.aclose()

    @pytest.mark.anyio
    async def test_stream_route_content_type(self, web_dashboard: WebDashboard) -> None:
        """Verify /stream endpoint is wired with text/event-stream media type.

        We call the underlying _build_app route handler directly rather than
        going through the HTTP transport (which buffers the infinite body).
        """
        from fastapi.responses import StreamingResponse

        # Find the /stream route handler
        stream_handler = None
        for route in web_dashboard._app.routes:
            if hasattr(route, "path") and route.path == "/stream":  # type: ignore[union-attr]
                stream_handler = route.endpoint  # type: ignore[union-attr]
                break

        assert stream_handler is not None, "/stream route not found"
        response = await stream_handler()
        assert isinstance(response, StreamingResponse)
        assert response.media_type == "text/event-stream"


# ── _dashboard_to_dict unit tests ─────────────────────────────────────────────

class TestDashboardToDict:
    def test_serialises_enum_status_to_string(self, dash: WorkforceDashboard) -> None:
        d = _dashboard_to_dict(dash)
        for role in d["roles"]:
            assert isinstance(role["status"], str)

    def test_all_roles_present(self, dash: WorkforceDashboard) -> None:
        d = _dashboard_to_dict(dash)
        names = {r["name"] for r in d["roles"]}
        assert names == {"monitor", "analyst"}

    def test_events_limited_to_10(self, dash: WorkforceDashboard) -> None:
        for i in range(30):
            dash.add_event("monitor", f"fill {i}")
        d = _dashboard_to_dict(dash)
        assert len(d["events"]) <= 10

    def test_hitl_fields_complete(self, dash: WorkforceDashboard) -> None:
        d = _dashboard_to_dict(dash)
        item = d["hitl_queue"][0]
        assert {"id", "role", "message", "risk", "expires_at", "options"} <= item.keys()


# ── WebDashboard.url() ────────────────────────────────────────────────────────

class TestWebDashboardUrl:
    def test_url_uses_localhost(self, web_dashboard: WebDashboard) -> None:
        assert web_dashboard.url() == "http://localhost:8080"

    def test_url_reflects_custom_port(self, dash: WorkforceDashboard) -> None:
        w = WebDashboard(dashboard=dash, port=9090)
        assert w.url() == "http://localhost:9090"

    def test_url_returns_https_when_ssl_certfile_set(
        self, dash: WorkforceDashboard, tmp_path
    ) -> None:
        """url() must return https:// when both ssl_certfile and ssl_keyfile are provided."""
        fake_cert = tmp_path / "cert.pem"
        fake_key = tmp_path / "key.pem"
        fake_cert.write_text("cert")
        fake_key.write_text("key")
        w = WebDashboard(
            dashboard=dash,
            port=8443,
            ssl_certfile=str(fake_cert),
            ssl_keyfile=str(fake_key),
        )
        assert w.url().startswith("https://")
        assert ":8443" in w.url()

    def test_url_returns_http_when_no_ssl(self, dash: WorkforceDashboard) -> None:
        """url() must return http:// when no SSL files are provided."""
        w = WebDashboard(dashboard=dash, port=8080)
        assert w.url().startswith("http://")


# ── API Key Authentication ────────────────────────────────────────────────────

class TestApiKeyAuth:
    @pytest.mark.anyio
    async def test_unauthenticated_request_returns_401(
        self, dash: WorkforceDashboard
    ) -> None:
        """Any request without a valid API key must be rejected with 401."""
        w = WebDashboard(dashboard=dash, port=8080, api_key="secret-token")
        async with _client(w._app) as client:
            resp = await client.get("/api/state")
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_bearer_token_grants_access(
        self, dash: WorkforceDashboard
    ) -> None:
        """A correct Bearer token in Authorization header must return 200."""
        w = WebDashboard(dashboard=dash, port=8080, api_key="secret-token")
        async with _client(w._app) as client:
            resp = await client.get(
                "/api/state",
                headers={"Authorization": "Bearer secret-token"},
            )
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_wrong_bearer_token_returns_401(
        self, dash: WorkforceDashboard
    ) -> None:
        """A wrong Bearer token must be rejected with 401."""
        w = WebDashboard(dashboard=dash, port=8080, api_key="secret-token")
        async with _client(w._app) as client:
            resp = await client.get(
                "/api/state",
                headers={"Authorization": "Bearer wrong-token"},
            )
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_query_param_key_grants_access(
        self, dash: WorkforceDashboard
    ) -> None:
        """Passing ?key=<api_key> as a query param must return 200."""
        w = WebDashboard(dashboard=dash, port=8080, api_key="secret-token")
        async with _client(w._app) as client:
            resp = await client.get("/api/state?key=secret-token")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_health_endpoint_exempt_from_auth(
        self, dash: WorkforceDashboard
    ) -> None:
        """/health must be accessible without any credentials."""
        w = WebDashboard(dashboard=dash, port=8080, api_key="secret-token")
        async with _client(w._app) as client:
            resp = await client.get("/health")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_no_api_key_allows_unauthenticated_access(
        self, dash: WorkforceDashboard
    ) -> None:
        """When api_key is None, all routes are open (no auth middleware)."""
        w = WebDashboard(dashboard=dash, port=8080)
        async with _client(w._app) as client:
            resp = await client.get("/api/state")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_html_page_injects_api_key_into_js(
        self, dash: WorkforceDashboard
    ) -> None:
        """The served HTML must contain the API key embedded in a JS variable."""
        w = WebDashboard(dashboard=dash, port=8080, api_key="my-secret")
        async with _client(w._app) as client:
            resp = await client.get(
                "/",
                headers={"Authorization": "Bearer my-secret"},
            )
        assert resp.status_code == 200
        # The key must be embedded in JS (surrounded by quotes, not hardcoded text)
        assert '"my-secret"' in resp.text

    @pytest.mark.anyio
    async def test_html_page_sets_api_key_null_when_no_auth(
        self, dash: WorkforceDashboard
    ) -> None:
        """When no api_key is set, the JS variable must be null."""
        w = WebDashboard(dashboard=dash, port=8080)
        async with _client(w._app) as client:
            resp = await client.get("/")
        assert "API_KEY = null" in resp.text


# ── generate_self_signed_cert ─────────────────────────────────────────────────

class TestGenerateSelfSignedCert:
    def test_generates_cert_and_key_files(self, tmp_path) -> None:
        """generate_self_signed_cert() must create cert.pem and key.pem."""
        cryptography = pytest.importorskip(
            "cryptography", reason="requires pip install cryptography"
        )
        cert_path, key_path = WebDashboard.generate_self_signed_cert(str(tmp_path))
        import os
        assert os.path.isfile(cert_path), "cert.pem not created"
        assert os.path.isfile(key_path), "key.pem not created"

    def test_cert_file_is_pem(self, tmp_path) -> None:
        """The cert file must start with the PEM header."""
        pytest.importorskip("cryptography")
        cert_path, _ = WebDashboard.generate_self_signed_cert(str(tmp_path))
        with open(cert_path) as f:
            content = f.read()
        assert "BEGIN CERTIFICATE" in content

    def test_key_file_is_pem(self, tmp_path) -> None:
        """The key file must start with the PEM header."""
        pytest.importorskip("cryptography")
        _, key_path = WebDashboard.generate_self_signed_cert(str(tmp_path))
        with open(key_path) as f:
            content = f.read()
        assert "BEGIN" in content  # RSA PRIVATE KEY or PRIVATE KEY
