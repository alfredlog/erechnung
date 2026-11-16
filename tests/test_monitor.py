"""The VPS monitor: alert after repeated failures, reminders, recovery mail."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("monitor", ROOT / "scripts" / "monitor.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, "STATE", tmp_path / "state.json")
    mails = []
    monkeypatch.setattr(m, "send_mail", lambda subject, body: mails.append((subject, body)))
    status = {"ok": True}
    monkeypatch.setattr(m, "CHECKS", [("Website", lambda: None if status["ok"] else "nicht erreichbar"),
                                      ("Server", lambda: None)])
    return m, mails, status


def test_alert_only_after_two_failures_then_recovery(monitor, monkeypatch):
    m, mails, status = monitor
    m.run()
    assert mails == []
    status["ok"] = False
    m.run()
    assert mails == []                                   # a single blip: no alarm
    m.run()
    assert len(mails) == 1 and "Problem – Website" in mails[0][0] and "nicht erreichbar" in mails[0][1]
    m.run()
    assert len(mails) == 1                               # no repeat right away
    state = json.loads(m.STATE.read_text())
    state["Website"]["alerted"] -= 7 * 3600
    m.STATE.write_text(json.dumps(state))
    m.run()
    assert len(mails) == 2 and "seit" in mails[1][1]     # reminder after 6 hours
    status["ok"] = True
    m.run()
    assert len(mails) == 3 and "wieder in Ordnung" in mails[2][0]
    m.run()
    assert len(mails) == 3


def test_a_crashing_check_is_reported(monitor, monkeypatch):
    m, mails, _ = monitor

    def boom():
        raise RuntimeError("kaputt")
    monkeypatch.setattr(m, "CHECKS", [("Sicherung", boom)])
    m.run()
    m.run()
    assert "Prüfung abgestürzt" in mails[0][1]


def test_detailed_health_needs_the_token(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.main import app
    from app.core.config import settings
    monkeypatch.setattr(settings, "monitor_token", "geheim")
    c = TestClient(app)
    assert "db" not in c.get("/health").json()
    assert "db" not in c.get("/health", headers={"X-Monitor-Token": "falsch"}).json()
    deep = c.get("/health", headers={"X-Monitor-Token": "geheim"}).json()
    assert deep["db"] == "ok" and "oldest_pending_job_minutes" in deep
