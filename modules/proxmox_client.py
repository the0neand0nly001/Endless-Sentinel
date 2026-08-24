"""Proxmox VE API collector.

The collector authenticates with :mod:`proxmoxer`, walks every node returned by
the cluster API, and normalises CPU and memory data into percentages consumed by
the dashboard and alert engine.  API failures are returned as structured health
results so a temporary Proxmox outage never terminates the polling thread.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from proxmoxer import ProxmoxAPI

LOGGER = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _percent(used: Any, total: Any) -> float:
    total_value = _as_float(total)
    if total_value <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (_as_float(used) / total_value) * 100.0)), 1)


class ProxmoxClient:
    """Collect CPU, memory, and availability from a Proxmox VE cluster."""

    def __init__(
        self,
        *,
        host: str,
        user: str,
        token_name: str | None = None,
        token_value: str | None = None,
        password: str | None = None,
        verify_ssl: bool = True,
        timeout: int = 10,
        cpu_warning: float = 80.0,
        cpu_critical: float = 95.0,
        memory_warning: float = 80.0,
        memory_critical: float = 95.0,
    ) -> None:
        if not host or not user:
            raise ValueError("PROXMOX_HOST and PROXMOX_USER are required when Proxmox monitoring is enabled.")
        if bool(token_name) != bool(token_value):
            raise ValueError("PROXMOX_TOKEN_NAME and PROXMOX_TOKEN_VALUE must be configured together.")
        if not token_value and not password:
            raise ValueError("Configure a Proxmox API token or PROXMOX_PASSWORD.")

        normalised_host, port = self._normalise_host(host)
        auth: dict[str, Any] = {"user": user, "verify_ssl": verify_ssl, "timeout": timeout, "port": port}
        if token_value:
            auth.update(token_name=token_name, token_value=token_value)
        else:
            auth.update(password=password)

        self.api = ProxmoxAPI(normalised_host, **auth)
        self.cpu_warning = cpu_warning
        self.cpu_critical = cpu_critical
        self.memory_warning = memory_warning
        self.memory_critical = memory_critical

    @staticmethod
    def _normalise_host(host: str) -> tuple[str, int]:
        """Accept either a hostname or an HTTPS URL without leaking it to logs."""

        value = host.strip().rstrip("/")
        parsed = urlparse(value if "://" in value else f"https://{value}")
        if not parsed.hostname:
            raise ValueError("PROXMOX_HOST is not a valid hostname or URL.")
        return parsed.hostname, parsed.port or 8006

    @classmethod
    def from_env(cls) -> "ProxmoxClient":
        """Build a collector exclusively from environment configuration."""

        return cls(
            host=os.environ.get("PROXMOX_HOST", ""),
            user=os.environ.get("PROXMOX_USER", ""),
            token_name=os.environ.get("PROXMOX_TOKEN_NAME"),
            token_value=os.environ.get("PROXMOX_TOKEN_VALUE"),
            password=os.environ.get("PROXMOX_PASSWORD"),
            verify_ssl=os.environ.get("PROXMOX_VERIFY_SSL", "true").lower() in {"1", "true", "yes", "on"},
            timeout=int(os.environ.get("PROXMOX_TIMEOUT_SECONDS", "10")),
            cpu_warning=float(os.environ.get("PROXMOX_CPU_WARNING", "80")),
            cpu_critical=float(os.environ.get("PROXMOX_CPU_CRITICAL", "95")),
            memory_warning=float(os.environ.get("PROXMOX_MEMORY_WARNING", "80")),
            memory_critical=float(os.environ.get("PROXMOX_MEMORY_CRITICAL", "95")),
        )

    @staticmethod
    def _severity(value: float, warning: float, critical: float) -> str | None:
        if value >= critical:
            return "critical"
        if value >= warning:
            return "warning"
        return None

    def _resource_alert(self, node: str, resource: str, value: float) -> dict[str, Any] | None:
        warning, critical = (
            (self.cpu_warning, self.cpu_critical)
            if resource == "CPU"
            else (self.memory_warning, self.memory_critical)
        )
        severity = self._severity(value, warning, critical)
        if not severity:
            return None
        threshold = critical if severity == "critical" else warning
        return {
            "fingerprint": f"proxmox:{node}:{resource.lower()}",
            "source": "Proxmox",
            "severity": severity,
            "title": f"{resource} threshold crossed on {node}",
            "message": f"{resource} is {value:.1f}% (threshold {threshold:.1f}%).",
            "resource": node,
            "value": value,
            "threshold": threshold,
        }

    def poll(self) -> dict[str, Any]:
        """Return a complete cluster snapshot and any threshold alerts."""

        checked_at = _utc_now()
        try:
            raw_nodes = self.api.nodes.get()
        except Exception as exc:  # proxmoxer wraps several transport libraries
            LOGGER.warning("Proxmox API is unreachable: %s", type(exc).__name__)
            return {
                "source": "proxmox", "enabled": True, "reachable": False, "status": "critical",
                "checked_at": checked_at, "nodes": [],
                "summary": {"total_nodes": 0, "online_nodes": 0, "avg_cpu": 0.0, "avg_memory": 0.0},
                "alerts": [{
                    "fingerprint": "proxmox:connection", "source": "Proxmox", "severity": "critical",
                    "title": "Proxmox API unreachable", "message": "Endless Sentinel could not complete the Proxmox API request.",
                    "resource": "cluster",
                }],
                "error": f"Connection failed ({type(exc).__name__})",
            }

        nodes: list[dict[str, Any]] = []
        alerts: list[dict[str, Any]] = []
        for raw_node in raw_nodes:
            name = str(raw_node.get("node", "unknown-node"))
            if raw_node.get("status") == "offline":
                nodes.append({"name": name, "online": False, "cpu_percent": 0.0, "memory_percent": 0.0})
                alerts.append({
                    "fingerprint": f"proxmox:{name}:offline", "source": "Proxmox", "severity": "critical",
                    "title": f"Proxmox node {name} is offline", "message": "The cluster reports this node as offline.", "resource": name,
                })
                continue
            try:
                status = self.api.nodes(name).status.get()
                memory = status.get("memory") or {}
                cpu_percent = round(_as_float(status.get("cpu")) * 100.0, 1)
                memory_percent = _percent(memory.get("used", raw_node.get("mem")), memory.get("total", raw_node.get("maxmem")))
                uptime = int(_as_float(status.get("uptime", raw_node.get("uptime"))))
                node = {"name": name, "online": True, "cpu_percent": cpu_percent, "memory_percent": memory_percent, "uptime_seconds": uptime}
                nodes.append(node)
                for resource, value in (("CPU", cpu_percent), ("Memory", memory_percent)):
                    alert = self._resource_alert(name, resource, value)
                    if alert:
                        alerts.append(alert)
            except Exception as exc:
                LOGGER.warning("Could not collect Proxmox node %s: %s", name, type(exc).__name__)
                nodes.append({"name": name, "online": False, "cpu_percent": 0.0, "memory_percent": 0.0, "error": type(exc).__name__})
                alerts.append({
                    "fingerprint": f"proxmox:{name}:collection", "source": "Proxmox", "severity": "critical",
                    "title": f"Proxmox node {name} is unreachable", "message": "The node status endpoint did not respond.", "resource": name,
                })

        online = [node for node in nodes if node["online"]]
        summary = {
            "total_nodes": len(nodes),
            "online_nodes": len(online),
            "avg_cpu": round(sum(node["cpu_percent"] for node in online) / len(online), 1) if online else 0.0,
            "avg_memory": round(sum(node["memory_percent"] for node in online) / len(online), 1) if online else 0.0,
        }
        status = "critical" if any(a["severity"] == "critical" for a in alerts) else "warning" if alerts else "healthy"
        return {"source": "proxmox", "enabled": True, "reachable": True, "status": status, "checked_at": checked_at, "nodes": nodes, "summary": summary, "alerts": alerts, "error": None}
