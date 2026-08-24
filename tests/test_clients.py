"""Collector helper tests that do not require live infrastructure."""

from types import SimpleNamespace

from modules.docker_client import DockerClient
from modules.k3s_client import K3sClient
from modules.notifier import DiscordNotifier
from modules.proxmox_client import ProxmoxClient, _percent


def test_percent_clamps_and_handles_missing_totals():
    assert _percent(50, 100) == 50.0
    assert _percent(200, 100) == 100.0
    assert _percent(10, 0) == 0.0


def test_proxmox_severity_thresholds():
    assert ProxmoxClient._severity(79.9, 80, 95) is None
    assert ProxmoxClient._severity(80, 80, 95) == "warning"
    assert ProxmoxClient._severity(95, 80, 95) == "critical"


def test_docker_stats_are_normalised():
    payload = {
        "cpu_stats": {"cpu_usage": {"total_usage": 300}, "system_cpu_usage": 1000, "online_cpus": 2},
        "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 500},
        "memory_stats": {"usage": 600, "limit": 1000, "stats": {"inactive_file": 100}},
    }
    fake_container = SimpleNamespace(stats=lambda stream: payload)
    cpu, memory, used, limit = DockerClient._stats(fake_container)
    assert cpu == 80.0
    assert memory == 50.0
    assert (used, limit) == (500, 1000)


def test_k3s_detects_crash_loop():
    waiting = SimpleNamespace(reason="CrashLoopBackOff")
    state = SimpleNamespace(waiting=waiting, terminated=None)
    container = SimpleNamespace(restart_count=6, ready=False, state=state)
    pod = SimpleNamespace(status=SimpleNamespace(phase="Running", init_container_statuses=[], container_statuses=[container]))
    severity, reason, restarts, ready = K3sClient._pod_health(pod)
    assert (severity, reason, restarts, ready) == ("critical", "CrashLoopBackOff", 6, False)


def test_notifier_without_webhook_is_safe():
    notifier = DiscordNotifier(None)
    ok, message = notifier.send_test()
    assert ok is False
    assert message == "Discord webhook is not configured."
