"""Endless Sentinel Flask application and background polling coordinator.

The web process owns one :class:`MonitorService`.  A daemon thread polls the
configured Proxmox, k3s, and Docker clients while Flask serves thread-safe
snapshots to the dashboard and JSON API.  Collector and notification failures
are isolated to each cycle and logged without terminating the application.
"""

from __future__ import annotations

import copy
import logging
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

from modules import DiscordNotifier, DockerClient, K3sClient, ProxmoxClient

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER = logging.getLogger("endless-sentinel")


def utc_now() -> str:
    """Return an RFC 3339 timestamp in UTC."""

    return datetime.now(timezone.utc).isoformat()


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a conventional boolean environment variable."""

    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def human_bytes(value: Any) -> str:
    """Render byte counts for dashboard summaries."""

    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return "0 B"


def initial_summary(source: str) -> dict[str, Any]:
    """Return source-specific zero values before the first successful poll."""

    if source == "proxmox":
        return {"total_nodes": 0, "online_nodes": 0, "avg_cpu": 0.0, "avg_memory": 0.0}
    if source == "k3s":
        return {"total_nodes": 0, "ready_nodes": 0, "total_pods": 0, "healthy_pods": 0, "total_restarts": 0}
    return {"total_containers": 0, "running_containers": 0, "healthy_containers": 0, "total_restarts": 0, "cpu_percent": 0.0, "memory_percent": 0.0, "memory_bytes": 0}


def source_result(source: str, status: str, *, enabled: bool, error: str | None = None) -> dict[str, Any]:
    """Create a stable empty collector payload for disabled/config-error states."""

    result: dict[str, Any] = {
        "source": source,
        "enabled": enabled,
        "reachable": None if not enabled or status == "initializing" else False,
        "status": status,
        "checked_at": None,
        "summary": initial_summary(source),
        "alerts": [],
        "error": error,
    }
    result["containers" if source == "docker" else "pods" if source == "k3s" else "nodes"] = []
    if source == "k3s":
        result["nodes"] = []
    return result


@dataclass
class CollectorSlot:
    """A configured collector or its safe configuration error state."""

    name: str
    enabled: bool
    client: Any | None = None
    configuration_error: str | None = None


class MonitorService:
    """Coordinate collector cycles, state history, and alert transitions."""

    def __init__(self) -> None:
        self.poll_interval = max(10, int(os.environ.get("POLL_INTERVAL_SECONDS", "30")))
        self.history_limit = max(12, min(1440, int(os.environ.get("HISTORY_LENGTH", "120"))))
        self.send_recoveries = env_bool("SEND_RECOVERY_ALERTS", True)
        self.notifier = DiscordNotifier.from_env()
        self.collectors = self._build_collectors()
        self._state_lock = threading.RLock()
        self._poll_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_alerts: dict[str, dict[str, Any]] = {}
        self._notified_severity: dict[str, str] = {}
        services = {
            name: source_result(name, "initializing" if slot.enabled else "disabled", enabled=slot.enabled, error=slot.configuration_error)
            for name, slot in self.collectors.items()
        }
        self._state: dict[str, Any] = {
            "app": "Endless Sentinel",
            "version": "1.0.0",
            "overall_status": "initializing" if any(slot.enabled for slot in self.collectors.values()) else "unknown",
            "updated_at": None,
            "polling": False,
            "cycle": 0,
            "services": services,
            "alerts": [],
            "history": [],
            "summary": {"nodes_ready": 0, "nodes_total": 0, "workloads_healthy": 0, "workloads_total": 0, "active_alerts": 0},
            "notifier": {"configured": self.notifier.configured, "status": "ready" if self.notifier.configured else "disabled", "last_delivery_at": None, "last_error": None},
        }

    @staticmethod
    def _auto_enabled(name: str) -> bool:
        explicit = os.environ.get(f"{name.upper()}_ENABLED")
        if explicit is not None:
            return explicit.strip().lower() in {"1", "true", "yes", "on"}
        if name == "proxmox":
            return bool(os.environ.get("PROXMOX_HOST"))
        if name == "k3s":
            return bool(os.environ.get("K3S_KUBECONFIG") or os.environ.get("KUBECONFIG") or env_bool("K3S_IN_CLUSTER"))
        return bool(os.environ.get("DOCKER_HOST") or Path("/var/run/docker.sock").exists())

    def _build_collectors(self) -> dict[str, CollectorSlot]:
        factories: dict[str, Callable[[], Any]] = {
            "proxmox": ProxmoxClient.from_env,
            "k3s": K3sClient.from_env,
            "docker": DockerClient.from_env,
        }
        slots: dict[str, CollectorSlot] = {}
        for name, factory in factories.items():
            enabled = self._auto_enabled(name)
            if not enabled:
                slots[name] = CollectorSlot(name=name, enabled=False)
                continue
            try:
                slots[name] = CollectorSlot(name=name, enabled=True, client=factory())
            except Exception as exc:
                LOGGER.error("%s collector configuration is invalid: %s", name, exc)
                slots[name] = CollectorSlot(name=name, enabled=True, configuration_error=str(exc))
        return slots

    def snapshot(self) -> dict[str, Any]:
        """Return a deep copy so Flask never races with the polling thread."""

        with self._state_lock:
            return copy.deepcopy(self._state)

    @staticmethod
    def _config_alert(name: str, message: str) -> dict[str, Any]:
        label = {"proxmox": "Proxmox", "k3s": "k3s", "docker": "Docker"}[name]
        return {
            "fingerprint": f"{name}:configuration", "source": label, "severity": "critical",
            "title": f"{label} collector is misconfigured", "message": message, "resource": "configuration",
        }

    def _collect_source(self, name: str, slot: CollectorSlot) -> dict[str, Any]:
        if not slot.enabled:
            return source_result(name, "disabled", enabled=False)
        if slot.configuration_error:
            result = source_result(name, "critical", enabled=True, error=slot.configuration_error)
            result["checked_at"] = utc_now()
            result["alerts"] = [self._config_alert(name, slot.configuration_error)]
            return result
        try:
            return slot.client.poll()
        except Exception as exc:  # final isolation boundary around third-party clients
            LOGGER.exception("Unhandled %s collector failure", name)
            result = source_result(name, "critical", enabled=True, error=f"Collection failed ({type(exc).__name__})")
            result["checked_at"] = utc_now()
            result["alerts"] = [{
                "fingerprint": f"{name}:unexpected", "source": name.title(), "severity": "critical",
                "title": f"{name.title()} collection failed", "message": "An unexpected collector error occurred; the next cycle will retry.", "resource": "collector",
            }]
            return result

    @staticmethod
    def _overall_status(services: dict[str, dict[str, Any]], alerts: list[dict[str, Any]]) -> str:
        if any(alert.get("severity") == "critical" for alert in alerts):
            return "critical"
        if alerts or any(service.get("status") == "warning" for service in services.values()):
            return "warning"
        enabled = [service for service in services.values() if service.get("enabled")]
        return "healthy" if enabled and all(service.get("status") == "healthy" for service in enabled) else "unknown"

    @staticmethod
    def _summary(services: dict[str, dict[str, Any]], alert_count: int) -> dict[str, int]:
        proxmox = services["proxmox"]["summary"]
        k3s = services["k3s"]["summary"]
        docker = services["docker"]["summary"]
        return {
            "nodes_ready": int(proxmox.get("online_nodes", 0)) + int(k3s.get("ready_nodes", 0)),
            "nodes_total": int(proxmox.get("total_nodes", 0)) + int(k3s.get("total_nodes", 0)),
            "workloads_healthy": int(k3s.get("healthy_pods", 0)) + int(docker.get("healthy_containers", 0)),
            "workloads_total": int(k3s.get("total_pods", 0)) + int(docker.get("total_containers", 0)),
            "active_alerts": alert_count,
        }

    @staticmethod
    def _history_point(services: dict[str, dict[str, Any]], timestamp: str) -> dict[str, Any]:
        proxmox = services["proxmox"]["summary"]
        docker = services["docker"]["summary"]
        return {
            "timestamp": timestamp,
            "proxmox_cpu": float(proxmox.get("avg_cpu", 0.0)),
            "proxmox_memory": float(proxmox.get("avg_memory", 0.0)),
            "docker_cpu": float(docker.get("cpu_percent", 0.0)),
            "docker_memory": float(docker.get("memory_percent", 0.0)),
        }

    def poll_once(self) -> tuple[bool, str]:
        """Run one full cycle; return immediately when another cycle is active."""

        if not self._poll_lock.acquire(blocking=False):
            return False, "A collector cycle is already running."
        started_at = utc_now()
        with self._state_lock:
            self._state["polling"] = True
        try:
            services = {name: self._collect_source(name, slot) for name, slot in self.collectors.items()}
            raw_alerts = [alert for service in services.values() for alert in service.get("alerts", [])]
            current_alerts: dict[str, dict[str, Any]] = {}
            for alert in raw_alerts:
                fingerprint = str(alert["fingerprint"])
                previous = self._active_alerts.get(fingerprint)
                current = dict(alert)
                current["first_seen"] = previous.get("first_seen") if previous else started_at
                current["last_seen"] = started_at
                current["occurrences"] = int(previous.get("occurrences", 0)) + 1 if previous else 1
                current_alerts[fingerprint] = current

            recoveries = [alert for fingerprint, alert in self._active_alerts.items() if fingerprint not in current_alerts]
            transitions = [
                alert for fingerprint, alert in current_alerts.items()
                if self._notified_severity.get(fingerprint) != alert.get("severity")
            ]
            if self.notifier.configured and (transitions or (recoveries and self.send_recoveries)):
                delivered, message = self.notifier.send_alerts(transitions, recoveries if self.send_recoveries else ())
                if delivered:
                    for alert in transitions:
                        self._notified_severity[alert["fingerprint"]] = alert["severity"]
                    for alert in recoveries:
                        self._notified_severity.pop(alert["fingerprint"], None)
                LOGGER.info("Discord alert pipeline: %s", message)
            else:
                for alert in recoveries:
                    self._notified_severity.pop(alert["fingerprint"], None)

            self._active_alerts = current_alerts
            severity_order = {"critical": 0, "warning": 1, "healthy": 2}
            alert_list = sorted(current_alerts.values(), key=lambda item: (severity_order.get(item.get("severity"), 9), item.get("source", ""), item.get("title", "")))
            with self._state_lock:
                history = self._state["history"] + [self._history_point(services, started_at)]
                self._state.update({
                    "overall_status": self._overall_status(services, alert_list),
                    "updated_at": started_at,
                    "cycle": int(self._state["cycle"]) + 1,
                    "services": services,
                    "alerts": alert_list,
                    "history": history[-self.history_limit :],
                    "summary": self._summary(services, len(alert_list)),
                    "notifier": {
                        "configured": self.notifier.configured,
                        "status": "error" if self.notifier.last_error else "ready" if self.notifier.configured else "disabled",
                        "last_delivery_at": self.notifier.last_delivery_at,
                        "last_error": self.notifier.last_error,
                    },
                })
            LOGGER.info("Collector cycle %s completed with %s active alert(s)", self._state["cycle"], len(alert_list))
            return True, "Collector cycle completed successfully."
        finally:
            with self._state_lock:
                self._state["polling"] = False
            self._poll_lock.release()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                LOGGER.exception("Background poll loop recovered from an unexpected failure")
            self._stop_event.wait(self.poll_interval)

    def start(self) -> None:
        """Start exactly one daemon poller for this Flask process."""

        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="endless-sentinel-poller", daemon=True)
        self._thread.start()
        LOGGER.info("Background collector started with a %ss interval", self.poll_interval)


def create_app(*, start_poller: bool | None = None) -> Flask:
    """Application factory used by Gunicorn, local development, and tests."""

    application = Flask(__name__, template_folder="templates", static_folder="static")
    application.config.update(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32),
        JSON_SORT_KEYS=False,
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    monitor = MonitorService()
    application.extensions["endless_sentinel_monitor"] = monitor
    application.jinja_env.filters["human_bytes"] = human_bytes

    @application.after_request
    def add_security_headers(response: Any) -> Any:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        if request.path.startswith("/api/") or request.path == "/health":
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @application.get("/")
    def dashboard() -> str:
        return render_template(
            "dashboard.html",
            page_title="Endless Sentinel | Homelab Resource Monitor",
            meta_description="Monitor Proxmox nodes, k3s workloads, Docker containers, and Discord alert delivery from one responsive homelab dashboard.",
            state=monitor.snapshot(),
            refresh_seconds=max(5, int(os.environ.get("DASHBOARD_REFRESH_SECONDS", "10"))),
        )

    @application.get("/api/status")
    def api_status() -> Any:
        return jsonify(monitor.snapshot())

    @application.post("/actions/poll")
    def poll_action() -> Any:
        ok, message = monitor.poll_once()
        wants_json = request.headers.get("Accept", "").startswith("application/json") or request.headers.get("X-Requested-With") == "fetch"
        if wants_json:
            return jsonify({"ok": ok, "message": message, "state": monitor.snapshot()}), 200 if ok else 409
        flash(message, "success" if ok else "warning")
        return redirect(url_for("dashboard"))

    @application.post("/actions/test-alert")
    def test_alert_action() -> Any:
        ok, message = monitor.notifier.send_test()
        wants_json = request.headers.get("Accept", "").startswith("application/json") or request.headers.get("X-Requested-With") == "fetch"
        if wants_json:
            return jsonify({"ok": ok, "message": message}), 200 if ok else 503
        flash(message, "success" if ok else "error")
        return redirect(url_for("dashboard", _anchor="alerts"))

    @application.get("/health")
    def health() -> Any:
        snapshot = monitor.snapshot()
        return jsonify({"status": "ok", "monitor_status": snapshot["overall_status"], "updated_at": snapshot["updated_at"], "cycle": snapshot["cycle"]})

    @application.errorhandler(404)
    def page_not_found(_error: Any) -> tuple[str, int]:
        return render_template(
            "404.html",
            page_title="Page Not Found | Endless Sentinel",
            meta_description="The requested Endless Sentinel dashboard route could not be found.",
        ), 404

    @application.errorhandler(500)
    def internal_error(error: Any) -> tuple[str, int]:
        LOGGER.error("Unhandled web request error: %s", type(error).__name__)
        return render_template(
            "500.html",
            page_title="Service Error | Endless Sentinel",
            meta_description="Endless Sentinel encountered a temporary dashboard error.",
        ), 500

    if start_poller is None:
        start_poller = env_bool("ENABLE_BACKGROUND_POLLER", True)
    if start_poller:
        monitor.start()
    return application


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_HOST", "0.0.0.0"),
        port=int(os.environ.get("FLASK_PORT", "8080")),
        debug=env_bool("FLASK_DEBUG", False),
        use_reloader=False,
    )
