"""Structured event -> channel delivery layer.

Analysis code (health checks, anomaly rules, Hermes answers) emits Event
objects and never talks to Discord/Telegram directly. routes.json decides
which channels receive which event types at which severity; each channel
has its own renderer. Adding a channel or rerouting an event type must not
touch analysis code.
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTES_FILE = Path(__file__).resolve().parent / "routes.json"

SEVERITY_ORDER = {"info": 0, "warn": 1, "crit": 2, "recovery": 0}
DISCORD_COLORS = {"info": 0x3498DB, "warn": 0xE67E22, "crit": 0xE74C3C, "recovery": 0x2ECC71}
SEVERITY_EMOJI = {"info": "ℹ️", "warn": "⚠️", "crit": "🚨", "recovery": "✅"}


@dataclass
class Event:
    type: str                     # dotted, e.g. "health.futures_staleness"
    severity: str                 # info | warn | crit | recovery
    title: str
    lines: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)   # machine-readable payload
    source: str = "server-a"

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_ORDER:
            raise ValueError(f"unknown severity: {self.severity}")


def load_routes(path: Path = ROUTES_FILE) -> list[dict]:
    return json.loads(path.read_text())["routes"]


def match_channels(event: Event, routes: list[dict]) -> list[str]:
    """First rule whose type glob matches and min_severity is met wins."""
    for rule in routes:
        if not fnmatch.fnmatch(event.type, rule["type"]):
            continue
        floor = SEVERITY_ORDER[rule.get("min_severity", "info")]
        if event.severity == "recovery" or SEVERITY_ORDER[event.severity] >= floor:
            return rule["channels"]
        return []
    return []


# --- renderers -------------------------------------------------------------

def render_discord(event: Event) -> dict:
    return {"embeds": [{
        "title": f"{SEVERITY_EMOJI[event.severity]} {event.title}",
        "description": "\n".join(event.lines)[:4000],
        "color": DISCORD_COLORS[event.severity],
        "footer": {"text": f"{event.source} · {event.type}"},
    }]}


def render_telegram(event: Event) -> str:
    head = f"{SEVERITY_EMOJI[event.severity]} [{event.type}] {event.title}"
    return "\n".join([head, *event.lines])[:4000]


# --- channels --------------------------------------------------------------

def send_discord(event: Event) -> bool:
    url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not url:
        return False
    req = urllib.request.Request(
        url, data=json.dumps(render_discord(event)).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=15).read()
    return True


def send_telegram(event: Event) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": render_telegram(event)}).encode()
    urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=15
    ).read()
    return True


CHANNELS = {"discord": send_discord, "telegram": send_telegram}


def report(event: Event, routes: list[dict] | None = None) -> list[str]:
    """Deliver event to its routed channels; returns channels that succeeded.

    A channel with missing config or a delivery error is skipped, never
    fatal — one dead webhook must not take down the analysis job.
    """
    delivered = []
    for name in match_channels(event, routes if routes is not None else load_routes()):
        sender = CHANNELS.get(name)
        if sender is None:
            print(f"reporter: unknown channel {name}", file=sys.stderr)
            continue
        try:
            if sender(event):
                delivered.append(name)
            else:
                print(f"reporter: channel {name} not configured", file=sys.stderr)
        except Exception as exc:
            print(f"reporter: {name} delivery failed: {exc}", file=sys.stderr)
    return delivered
