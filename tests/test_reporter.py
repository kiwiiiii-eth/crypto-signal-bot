import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reporter import (Event, load_routes, match_channels, render_discord,
                      render_telegram, report)


def ev(**kw):
    base = dict(type="health.server-a", severity="warn", title="t", lines=["l1"])
    base.update(kw)
    return Event(**base)


def test_routes_file_loads():
    routes = load_routes()
    assert routes and all("type" in r and "channels" in r for r in routes)


def test_health_warn_routes_to_discord_and_telegram():
    assert match_channels(ev(), load_routes()) == ["discord", "telegram"]


def test_health_info_below_floor_is_dropped():
    assert match_channels(ev(severity="info"), load_routes()) == []


def test_recovery_bypasses_severity_floor():
    assert match_channels(ev(severity="recovery"), load_routes()) == ["discord", "telegram"]


def test_first_matching_rule_wins_no_fallthrough():
    routes = [
        {"type": "health.*", "min_severity": "crit", "channels": ["discord"]},
        {"type": "*", "min_severity": "info", "channels": ["telegram"]},
    ]
    assert match_channels(ev(severity="warn"), routes) == []


def test_unmatched_type_falls_to_catchall_only_on_crit():
    routes = load_routes()
    assert match_channels(ev(type="tradfi.rent", severity="warn"), routes) == []
    assert match_channels(ev(type="tradfi.rent", severity="crit"), routes) == ["telegram"]


def test_unknown_severity_rejected():
    try:
        ev(severity="fatal")
        assert False
    except ValueError:
        pass


def test_discord_render_shape():
    body = render_discord(ev(severity="crit", lines=["a", "b"]))
    embed = body["embeds"][0]
    assert "🚨" in embed["title"]
    assert embed["description"] == "a\nb"
    assert embed["footer"]["text"].endswith("health.server-a")


def test_telegram_render_plain_text():
    text = render_telegram(ev())
    assert text.startswith("⚠️ [health.server-a] t")
    assert "l1" in text


def test_report_skips_unconfigured_channels(monkeypatch):
    for var in ("DISCORD_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    assert report(ev()) == []


def test_report_survives_channel_exception(monkeypatch):
    import reporter as r

    def boom(event):
        raise RuntimeError("webhook down")

    monkeypatch.setitem(r.CHANNELS, "discord", boom)
    monkeypatch.setitem(r.CHANNELS, "telegram", lambda e: True)
    assert report(ev()) == ["telegram"]
