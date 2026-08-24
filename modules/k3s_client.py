"""Kubernetes/k3s health collector.

The official Kubernetes Python client is used to inspect node Ready conditions,
pod phases, container waiting reasons, readiness, and restart counts.  The
collector can use a mounted kubeconfig or in-cluster service-account identity.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

LOGGER = logging.getLogger(__name__)

CRITICAL_WAITING_REASONS = {"CrashLoopBackOff", "CreateContainerError", "CreateContainerConfigError", "RunContainerError"}
WARNING_WAITING_REASONS = {"ImagePullBackOff", "ErrImagePull", "ContainerCreating", "PodInitializing"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class K3sClient:
    """Collect node readiness and workload health from a k3s cluster."""

    def __init__(self, *, kubeconfig: str | None, context: str | None = None, in_cluster: bool = False, timeout: int = 10, restart_warning: int = 5) -> None:
        if not in_cluster and not kubeconfig:
            raise ValueError("Set K3S_KUBECONFIG or enable K3S_IN_CLUSTER when k3s monitoring is enabled.")
        self.kubeconfig = kubeconfig
        self.context = context
        self.in_cluster = in_cluster
        self.timeout = timeout
        self.restart_warning = restart_warning
        self._api: client.CoreV1Api | None = None

    @classmethod
    def from_env(cls) -> "K3sClient":
        """Create a k3s collector from environment variables."""

        in_cluster = os.environ.get("K3S_IN_CLUSTER", "false").lower() in {"1", "true", "yes", "on"}
        kubeconfig = os.environ.get("K3S_KUBECONFIG") or os.environ.get("KUBECONFIG")
        if kubeconfig:
            kubeconfig = str(Path(kubeconfig).expanduser())
        return cls(
            kubeconfig=kubeconfig,
            context=os.environ.get("K3S_CONTEXT"),
            in_cluster=in_cluster,
            timeout=int(os.environ.get("K3S_TIMEOUT_SECONDS", "10")),
            restart_warning=int(os.environ.get("K3S_RESTART_WARNING", "5")),
        )

    def _ensure_api(self) -> client.CoreV1Api:
        if self._api is not None:
            return self._api
        if self.in_cluster:
            config.load_incluster_config()
        else:
            config.load_kube_config(config_file=self.kubeconfig, context=self.context)
        self._api = client.CoreV1Api()
        return self._api

    @staticmethod
    def _node_ready(node: Any) -> tuple[bool, str]:
        conditions = node.status.conditions or []
        ready = next((condition for condition in conditions if condition.type == "Ready"), None)
        if ready is None:
            return False, "Ready condition missing"
        return ready.status == "True", ready.message or ready.reason or "Ready"

    @staticmethod
    def _pod_health(pod: Any) -> tuple[str, str | None, int, bool]:
        """Return severity, reason, total restarts, and readiness for one pod."""

        phase = pod.status.phase or "Unknown"
        statuses = list(pod.status.init_container_statuses or []) + list(pod.status.container_statuses or [])
        restart_count = sum(int(status.restart_count or 0) for status in statuses)
        ready = bool(statuses) and all(bool(status.ready) for status in statuses if status.state and not status.state.terminated)

        waiting_reasons = [
            status.state.waiting.reason
            for status in statuses
            if status.state and status.state.waiting and status.state.waiting.reason
        ]
        if phase in {"Failed", "Unknown"}:
            return "critical", phase, restart_count, False
        critical_reason = next((reason for reason in waiting_reasons if reason in CRITICAL_WAITING_REASONS), None)
        if critical_reason:
            return "critical", critical_reason, restart_count, False
        warning_reason = next((reason for reason in waiting_reasons if reason in WARNING_WAITING_REASONS), None)
        if warning_reason or phase == "Pending":
            return "warning", warning_reason or phase, restart_count, False
        if phase == "Running" and not ready:
            return "warning", "NotReady", restart_count, False
        return "healthy", None, restart_count, phase in {"Running", "Succeeded"}

    def poll(self) -> dict[str, Any]:
        """Return cluster readiness, pod health, and actionable alerts."""

        checked_at = _utc_now()
        try:
            api = self._ensure_api()
            raw_nodes = api.list_node(_request_timeout=self.timeout).items
            raw_pods = api.list_pod_for_all_namespaces(watch=False, _request_timeout=self.timeout).items
        except (ApiException, OSError, ValueError, config.ConfigException) as exc:
            self._api = None
            LOGGER.warning("k3s API is unreachable: %s", type(exc).__name__)
            return {
                "source": "k3s", "enabled": True, "reachable": False, "status": "critical", "checked_at": checked_at,
                "nodes": [], "pods": [], "summary": {"total_nodes": 0, "ready_nodes": 0, "total_pods": 0, "healthy_pods": 0, "total_restarts": 0},
                "alerts": [{"fingerprint": "k3s:connection", "source": "k3s", "severity": "critical", "title": "k3s API unreachable", "message": "Endless Sentinel could not query the Kubernetes API.", "resource": "cluster"}],
                "error": f"Connection failed ({type(exc).__name__})",
            }
        except Exception as exc:
            self._api = None
            LOGGER.exception("Unexpected k3s collection error")
            return {
                "source": "k3s", "enabled": True, "reachable": False, "status": "critical", "checked_at": checked_at,
                "nodes": [], "pods": [], "summary": {"total_nodes": 0, "ready_nodes": 0, "total_pods": 0, "healthy_pods": 0, "total_restarts": 0},
                "alerts": [{"fingerprint": "k3s:connection", "source": "k3s", "severity": "critical", "title": "k3s collection failed", "message": "An unexpected Kubernetes client error occurred.", "resource": "cluster"}],
                "error": f"Collection failed ({type(exc).__name__})",
            }

        nodes: list[dict[str, Any]] = []
        pods: list[dict[str, Any]] = []
        alerts: list[dict[str, Any]] = []

        for raw_node in raw_nodes:
            name = raw_node.metadata.name
            ready, message = self._node_ready(raw_node)
            nodes.append({"name": name, "ready": ready, "message": message, "roles": sorted((raw_node.metadata.labels or {}).keys())})
            if not ready:
                alerts.append({
                    "fingerprint": f"k3s:node:{name}:not-ready", "source": "k3s", "severity": "critical",
                    "title": f"k3s node {name} is not ready", "message": message, "resource": name,
                })

        for raw_pod in raw_pods:
            namespace = raw_pod.metadata.namespace or "default"
            name = raw_pod.metadata.name
            severity, reason, restarts, ready = self._pod_health(raw_pod)
            pod = {"namespace": namespace, "name": name, "phase": raw_pod.status.phase or "Unknown", "ready": ready, "severity": severity, "reason": reason, "restarts": restarts}
            pods.append(pod)
            if severity != "healthy":
                alerts.append({
                    "fingerprint": f"k3s:pod:{namespace}:{name}:health", "source": "k3s", "severity": severity,
                    "title": f"Pod {namespace}/{name} is unhealthy", "message": f"State: {reason or pod['phase']}; restarts: {restarts}.", "resource": f"{namespace}/{name}",
                })
            elif restarts >= self.restart_warning:
                alerts.append({
                    "fingerprint": f"k3s:pod:{namespace}:{name}:restarts", "source": "k3s", "severity": "warning",
                    "title": f"Pod {namespace}/{name} has restarted repeatedly", "message": f"Observed {restarts} container restarts (threshold {self.restart_warning}).", "resource": f"{namespace}/{name}",
                })

        healthy_pods = sum(1 for pod in pods if pod["severity"] == "healthy")
        summary = {
            "total_nodes": len(nodes), "ready_nodes": sum(1 for node in nodes if node["ready"]),
            "total_pods": len(pods), "healthy_pods": healthy_pods, "total_restarts": sum(pod["restarts"] for pod in pods),
        }
        status = "critical" if any(a["severity"] == "critical" for a in alerts) else "warning" if alerts else "healthy"
        return {"source": "k3s", "enabled": True, "reachable": True, "status": status, "checked_at": checked_at, "nodes": nodes, "pods": pods, "summary": summary, "alerts": alerts, "error": None}
