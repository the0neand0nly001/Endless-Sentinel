"""Flask route and feedback-loop tests."""

import os

os.environ.update({
    "ENABLE_BACKGROUND_POLLER": "false",
    "PROXMOX_ENABLED": "false",
    "K3S_ENABLED": "false",
    "DOCKER_ENABLED": "false",
    "DISCORD_WEBHOOK_URL": "",
    "FLASK_SECRET_KEY": "test-only-secret",
})

from app import create_app  # noqa: E402


def build_client():
    application = create_app(start_poller=False)
    application.config.update(TESTING=True)
    return application.test_client()


def test_dashboard_has_fixed_metadata_and_working_navigation():
    response = build_client().get("/")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "<title>Endless Sentinel | Homelab Resource Monitor</title>" in html
    assert 'name="description"' in html
    assert 'href="#"' not in html
    assert 'data-menu-toggle' in html
    assert 'id="dynamic-favicon"' in html


def test_status_api_has_all_platform_layers_and_no_secrets():
    response = build_client().get("/api/status")
    payload = response.get_json()
    assert response.status_code == 200
    assert set(payload["services"]) == {"proxmox", "k3s", "docker"}
    assert "DISCORD_WEBHOOK_URL" not in response.get_data(as_text=True)
    assert "token_value" not in response.get_data(as_text=True).lower()


def test_manual_poll_returns_feedback_and_snapshot():
    response = build_client().post(
        "/actions/poll",
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
    )
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["message"] == "Collector cycle completed successfully."
    assert payload["state"]["cycle"] == 1


def test_missing_route_uses_custom_404_page():
    response = build_client().get("/not-a-real-route")
    html = response.get_data(as_text=True)
    assert response.status_code == 404
    assert "<title>Page Not Found | Endless Sentinel</title>" in html
    assert "Signal lost." in html
    assert 'href="/"' in html


def test_health_endpoint_reports_web_process_liveness():
    response = build_client().get("/health")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["monitor_status"] in {"unknown", "initializing", "healthy", "warning", "critical"}


def test_unconfigured_discord_action_returns_clear_error():
    response = build_client().post(
        "/actions/test-alert",
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
    )
    payload = response.get_json()
    assert response.status_code == 503
    assert payload == {"ok": False, "message": "Discord webhook is not configured."}
