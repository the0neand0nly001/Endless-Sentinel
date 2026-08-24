"""Docker Engine health and resource collector.

The Docker SDK can connect to the local Unix socket or a remote ``DOCKER_HOST``.
It verifies daemon reachability, records container state/health, samples CPU and
memory, and compares restart counters between polls to detect new restarts.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import docker
from docker.errors import DockerException
from docker.tls import TLSConfig

LOGGER = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


class DockerClient:
    """Inspect Docker container health, resource use, and restart changes."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: int = 10,
        tls: TLSConfig | None = None,
        collect_stats: bool = True,
        restart_warning: int = 1,
        allowed_stopped: set[str] | None = None,
    ) -> None:
        self.client = docker.DockerClient(base_url=base_url or "unix:///var/run/docker.sock", timeout=timeout, tls=tls)
        self.collect_stats = collect_stats
        self.restart_warning = max(1, restart_warning)
        self.allowed_stopped = allowed_stopped or set()
        self._restart_counts: dict[str, int] = {}

    @classmethod
    def from_env(cls) -> "DockerClient":
        """Build a local- or remote-socket client from environment variables."""

        base_url = os.environ.get("DOCKER_HOST") or "unix:///var/run/docker.sock"
        tls: TLSConfig | None = None
        if _env_bool("DOCKER_TLS_VERIFY"):
            cert_path = os.environ.get("DOCKER_CERT_PATH")
            if not cert_path:
                raise ValueError("DOCKER_CERT_PATH is required when DOCKER_TLS_VERIFY is enabled.")
            path = Path(cert_path).expanduser()
            tls = TLSConfig(
                client_cert=(str(path / "cert.pem"), str(path / "key.pem")),
                ca_cert=str(path / "ca.pem"),
                verify=True,
            )
        allowed = {name.strip() for name in os.environ.get("DOCKER_ALLOWED_STOPPED", "").split(",") if name.strip()}
        return cls(
            base_url=base_url,
            timeout=int(os.environ.get("DOCKER_TIMEOUT_SECONDS", "10")),
            tls=tls,
            collect_stats=_env_bool("DOCKER_COLLECT_STATS", True),
            restart_warning=int(os.environ.get("DOCKER_RESTART_WARNING", "1")),
            allowed_stopped=allowed,
        )

    @staticmethod
    def _stats(container: Any) -> tuple[float, float, int, int]:
        """Normalise Docker's single-shot stats payload into CPU/RAM percentages."""

        stats = container.stats(stream=False)
        cpu_stats = stats.get("cpu_stats", {})
        previous = stats.get("precpu_stats", {})
        cpu_delta = float(cpu_stats.get("cpu_usage", {}).get("total_usage", 0)) - float(previous.get("cpu_usage", {}).get("total_usage", 0))
        system_delta = float(cpu_stats.get("system_cpu_usage", 0)) - float(previous.get("system_cpu_usage", 0))
        online_cpus = int(cpu_stats.get("online_cpus") or len(cpu_stats.get("cpu_usage", {}).get("percpu_usage") or []) or 1)
        cpu_percent = (cpu_delta / system_delta) * online_cpus * 100.0 if system_delta > 0 and cpu_delta >= 0 else 0.0

        memory = stats.get("memory_stats", {})
        memory_stats = memory.get("stats", {})
        cache = int(memory_stats.get("inactive_file", memory_stats.get("cache", 0)) or 0)
        memory_used = max(0, int(memory.get("usage", 0) or 0) - cache)
        memory_limit = int(memory.get("limit", 0) or 0)
        memory_percent = (memory_used / memory_limit) * 100.0 if memory_limit > 0 else 0.0
        return round(max(0.0, cpu_percent), 1), round(max(0.0, min(100.0, memory_percent)), 1), memory_used, memory_limit

    def poll(self) -> dict[str, Any]:
        """Return daemon/container state and alerts without propagating failures."""

        checked_at = _utc_now()
        try:
            self.client.ping()
            raw_containers = self.client.containers.list(all=True)
        except DockerException as exc:
            LOGGER.warning("Docker daemon is unreachable: %s", type(exc).__name__)
            return {
                "source": "docker", "enabled": True, "reachable": False, "status": "critical", "checked_at": checked_at,
                "containers": [], "summary": {"total_containers": 0, "running_containers": 0, "healthy_containers": 0, "total_restarts": 0, "cpu_percent": 0.0, "memory_percent": 0.0, "memory_bytes": 0},
                "alerts": [{"fingerprint": "docker:connection", "source": "Docker", "severity": "critical", "title": "Docker daemon unreachable", "message": "Endless Sentinel could not connect to the configured Docker socket.", "resource": "daemon"}],
                "error": f"Connection failed ({type(exc).__name__})",
            }

        containers: list[dict[str, Any]] = []
        alerts: list[dict[str, Any]] = []
        current_restarts: dict[str, int] = {}
        for raw in raw_containers:
            attrs = raw.attrs or {}
            state = attrs.get("State", {})
            labels = (attrs.get("Config", {}) or {}).get("Labels") or {}
            name = raw.name or raw.short_id
            identifier = raw.id
            if str(labels.get("endless-sentinel.ignore", "false")).lower() == "true":
                continue
            status = state.get("Status") or raw.status or "unknown"
            health = (state.get("Health") or {}).get("Status")
            restart_count = int(state.get("RestartCount", 0) or 0)
            current_restarts[identifier] = restart_count
            previous_restarts = self._restart_counts.get(identifier)
            restart_delta = max(0, restart_count - previous_restarts) if previous_restarts is not None else 0
            cpu_percent = memory_percent = 0.0
            memory_bytes = memory_limit = 0
            stats_error = None
            if self.collect_stats and status == "running":
                try:
                    cpu_percent, memory_percent, memory_bytes, memory_limit = self._stats(raw)
                except DockerException as exc:
                    stats_error = type(exc).__name__
                    LOGGER.debug("Docker stats unavailable for %s: %s", name, stats_error)

            healthy = status == "running" and health not in {"unhealthy"}
            containers.append({
                "id": raw.short_id, "name": name, "image": str((attrs.get("Config", {}) or {}).get("Image") or "unknown"),
                "status": status, "health": health, "healthy": healthy, "restart_count": restart_count, "restart_delta": restart_delta,
                "cpu_percent": cpu_percent, "memory_percent": memory_percent, "memory_bytes": memory_bytes, "memory_limit": memory_limit,
                "stats_error": stats_error,
            })

            allow_stopped = name in self.allowed_stopped or str(labels.get("endless-sentinel.allow-stopped", "false")).lower() == "true"
            if status != "running" and not allow_stopped:
                alerts.append({
                    "fingerprint": f"docker:{identifier}:stopped", "source": "Docker", "severity": "critical",
                    "title": f"Container {name} is not running", "message": f"Current Docker state: {status}.", "resource": name,
                })
            if health == "unhealthy":
                alerts.append({
                    "fingerprint": f"docker:{identifier}:unhealthy", "source": "Docker", "severity": "critical",
                    "title": f"Container {name} is unhealthy", "message": "Docker's health check reports an unhealthy state.", "resource": name,
                })
            if restart_delta >= self.restart_warning:
                alerts.append({
                    "fingerprint": f"docker:{identifier}:restart", "source": "Docker", "severity": "warning",
                    "title": f"Container {name} restarted unexpectedly", "message": f"Restart count increased by {restart_delta} to {restart_count}.", "resource": name,
                })

        self._restart_counts = current_restarts
        running = [item for item in containers if item["status"] == "running"]
        summary = {
            "total_containers": len(containers), "running_containers": len(running),
            "healthy_containers": sum(1 for item in containers if item["healthy"]),
            "total_restarts": sum(item["restart_count"] for item in containers),
            "cpu_percent": round(sum(item["cpu_percent"] for item in running), 1),
            "memory_percent": round(sum(item["memory_percent"] for item in running) / len(running), 1) if running else 0.0,
            "memory_bytes": sum(item["memory_bytes"] for item in running),
        }
        status = "critical" if any(a["severity"] == "critical" for a in alerts) else "warning" if alerts else "healthy"
        return {"source": "docker", "enabled": True, "reachable": True, "status": status, "checked_at": checked_at, "containers": containers, "summary": summary, "alerts": alerts, "error": None}
