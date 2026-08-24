"""Discord webhook alert delivery for Endless Sentinel.

Alerts from every collector are rendered as compact Discord embeds.  Delivery
errors are logged without exposing the webhook URL and are returned to the
poller instead of raising, so monitoring continues during Discord outages.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

LOGGER = logging.getLogger(__name__)
COLORS = {"critical": 0xFF6B6B, "warning": 0xF4B860, "healthy": 0x00B37E, "recovery": 0x00B37E}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


class DiscordNotifier:
    """Format alert transitions and post them to a Discord webhook."""

    def __init__(self, webhook_url: str | None, *, timeout: int = 10, username: str = "Endless Sentinel") -> None:
        self.webhook_url = (webhook_url or "").strip()
        self.timeout = timeout
        self.username = username
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Endless Sentinel/1.0"})
        self.last_delivery_at: str | None = None
        self.last_error: str | None = None

    @classmethod
    def from_env(cls) -> "DiscordNotifier":
        """Create a notifier using only environment-provided secrets."""

        return cls(
            os.environ.get("DISCORD_WEBHOOK_URL"),
            timeout=int(os.environ.get("DISCORD_TIMEOUT_SECONDS", "10")),
            username=os.environ.get("DISCORD_USERNAME", "Endless Sentinel"),
        )

    @property
    def configured(self) -> bool:
        return self.webhook_url.startswith("https://")

    @staticmethod
    def _alert_embed(alert: dict[str, Any]) -> dict[str, Any]:
        severity = str(alert.get("severity", "warning")).lower()
        return {
            "title": _truncate(alert.get("title") or "Endless Sentinel alert", 256),
            "description": _truncate(alert.get("message") or "A monitored resource changed state.", 4096),
            "color": COLORS.get(severity, COLORS["warning"]),
            "fields": [
                {"name": "Source", "value": _truncate(alert.get("source") or "Endless Sentinel", 1024), "inline": True},
                {"name": "Resource", "value": _truncate(alert.get("resource") or "cluster", 1024), "inline": True},
                {"name": "Severity", "value": severity.upper(), "inline": True},
            ],
            "footer": {"text": "Endless Sentinel · homelab observability"},
            "timestamp": alert.get("last_seen") or alert.get("timestamp") or _utc_now(),
        }

    @staticmethod
    def _recovery_embed(alert: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": _truncate(f"Recovered · {alert.get('title', 'Resource healthy')}", 256),
            "description": _truncate(f"{alert.get('resource', 'The resource')} is no longer reporting this condition.", 4096),
            "color": COLORS["recovery"],
            "fields": [
                {"name": "Source", "value": _truncate(alert.get("source") or "Endless Sentinel", 1024), "inline": True},
                {"name": "Resource", "value": _truncate(alert.get("resource") or "cluster", 1024), "inline": True},
                {"name": "State", "value": "RECOVERED", "inline": True},
            ],
            "footer": {"text": "Endless Sentinel · homelab observability"},
            "timestamp": _utc_now(),
        }

    def _post(self, payload: dict[str, Any]) -> tuple[bool, str]:
        if not self.configured:
            return False, "Discord webhook is not configured."
        try:
            response = self.session.post(self.webhook_url, json=payload, timeout=self.timeout)
            if response.status_code == 429:
                retry_after = min(float(response.json().get("retry_after", 1.0)), 5.0)
                time.sleep(max(0.0, retry_after))
                response = self.session.post(self.webhook_url, json=payload, timeout=self.timeout)
            response.raise_for_status()
        except (requests.RequestException, ValueError) as exc:
            self.last_error = f"Delivery failed ({type(exc).__name__})"
            LOGGER.warning("Discord webhook delivery failed: %s", type(exc).__name__)
            return False, self.last_error
        self.last_delivery_at = _utc_now()
        self.last_error = None
        return True, "Discord notification delivered."

    def send_alerts(self, alerts: Iterable[dict[str, Any]], recoveries: Iterable[dict[str, Any]] = ()) -> tuple[bool, str]:
        """Deliver new alerts and optional recovery transitions in batches of ten."""

        embeds = [self._alert_embed(alert) for alert in alerts]
        embeds.extend(self._recovery_embed(alert) for alert in recoveries)
        if not embeds:
            return True, "No alert transitions to deliver."
        for offset in range(0, len(embeds), 10):
            ok, message = self._post({"username": self.username, "embeds": embeds[offset : offset + 10]})
            if not ok:
                return False, message
        return True, f"Delivered {len(embeds)} alert transition{'s' if len(embeds) != 1 else ''}."

    def send_test(self) -> tuple[bool, str]:
        """Send a safe test message from the dashboard action."""

        payload = {
            "username": self.username,
            "embeds": [{
                "title": "Endless Sentinel webhook verified",
                "description": "Discord alert delivery is configured and responding.",
                "color": COLORS["healthy"],
                "fields": [
                    {"name": "Source", "value": "Endless Sentinel", "inline": True},
                    {"name": "State", "value": "CONNECTED", "inline": True},
                ],
                "footer": {"text": "Endless Sentinel · homelab observability"},
                "timestamp": _utc_now(),
            }],
        }
        return self._post(payload)
