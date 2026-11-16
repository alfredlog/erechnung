#!/usr/bin/env python3
"""Watches E-Rechnung Agent on the VPS and e-mails you when something is wrong.

Runs on the host (not in Docker), every 5 minutes from cron. Only the Python standard
library is used. Settings come from the app's .env (SMTP, BASE_URL, ALERT_EMAIL, …).

Checks
  containers   all services of docker compose are running (and healthy)
  website      https://<domain>/health answers through Caddy (also tests HTTPS)
  inside       database answers, worker is alive, no jobs stuck in the queue
  real test    a real XRechnung goes through the check (parser + rules + KoSIT)
  mail in      the receiving mail server answers on port 25
  certificate  HTTPS certificate is valid for more than 14 days
  disk/memory  enough space and memory left
  backup       a database backup from the last 26 hours exists

Mails: one when a check fails twice in a row (no alarm for a single blip), a reminder
every 6 hours while it stays broken, and one when it works again.

  python3 scripts/monitor.py              # run all checks (cron)
  python3 scripts/monitor.py --report     # show the result of every check, send nothing
  python3 scripts/monitor.py --test-mail  # send a test alert
"""
from __future__ import annotations

import json
import os
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # ~/apps/erechnung
STATE = Path.home() / ".erechnung-monitor.json"
SAMPLE = ROOT / "tests" / "fixtures" / "xrechnung" / "01.01a-INVOICE_ubl.xml"
FAIL_AFTER = 2           # consecutive failures before an alert
REMIND_HOURS = 6


def load_env() -> dict:
    env = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith(("SMTP_", "MAIL_", "ALERT_", "MONITOR_"))})
    return env


ENV = load_env()
BASE = ENV.get("BASE_URL", "http://127.0.0.1:8100").rstrip("/")
DOMAIN = BASE.split("://", 1)[-1].split("/", 1)[0]


def http(path: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 60):
    req = urllib.request.Request(BASE + path, data=data, headers=headers or {}, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (our own server)
        return r.status, r.read()


# ------------------------------------------------------------------ checks: return None if OK, else a German text
def check_containers() -> str | None:
    out = subprocess.run(["docker", "compose", "ps", "--all", "--format", "json"], cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        return f"docker compose ps fehlgeschlagen: {out.stderr.strip()[:300]}"
    rows = [json.loads(x) for x in out.stdout.splitlines() if x.strip().startswith("{")]
    if not rows and out.stdout.strip().startswith("["):
        rows = json.loads(out.stdout)
    expected = {"api", "worker", "mailin", "kosit", "db"}
    seen = {r.get("Service"): r for r in rows}
    bad = [f"{name}: fehlt" for name in sorted(expected - set(seen))]
    for name, r in sorted(seen.items()):
        state, health = r.get("State", ""), r.get("Health", "")
        if state != "running" or health in ("unhealthy",):
            bad.append(f"{name}: {state}{' / ' + health if health else ''}")
    return ("Container nicht in Ordnung: " + ", ".join(bad)) if bad else None


_deep: dict = {}


def check_website() -> str | None:
    headers = {"X-Monitor-Token": ENV.get("MONITOR_TOKEN", "")} if ENV.get("MONITOR_TOKEN") else {}
    try:
        status, body = http("/health", headers=headers, timeout=20)
    except (urllib.error.URLError, OSError) as exc:
        return f"{BASE}/health nicht erreichbar: {exc}"
    if status != 200:
        return f"{BASE}/health antwortet mit HTTP {status}"
    _deep.update(json.loads(body))
    return None


def check_inside() -> str | None:
    if not ENV.get("MONITOR_TOKEN"):
        return None  # detailed check not configured
    if not _deep:
        return "Keine Detaildaten (Website nicht erreichbar)."
    problems = []
    if _deep.get("db") != "ok":
        problems.append(f"Datenbank: {_deep.get('db', 'keine Antwort')} (stimmt MONITOR_TOKEN?)")
    beat = _deep.get("worker_seconds_ago")
    if beat is None or beat > 300:
        problems.append("Worker lebt nicht (kein Lebenszeichen seit "
                        f"{'unbekannt' if beat is None else str(beat // 60) + ' Minuten'})")
    stuck = _deep.get("oldest_pending_job_minutes") or 0
    if stuck > 15:
        problems.append(f"Aufgaben warten seit {stuck:.0f} Minuten (Rechnungen werden nicht verarbeitet)")
    return "; ".join(problems) or None


def check_real_invoice() -> str | None:
    if not SAMPLE.exists():
        return None
    boundary = uuid.uuid4().hex
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"test.xml\"\r\n"
            f"Content-Type: application/xml\r\n\r\n").encode()
    body = head + SAMPLE.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    try:
        status, raw = http("/api/v1/check", data=body,
                           headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=90)
    except (urllib.error.URLError, OSError) as exc:
        return f"Testrechnung konnte nicht geprüft werden: {exc}"
    result = json.loads(raw)
    if status != 200 or result.get("error"):
        return f"Testrechnung: Fehler {result.get('error') or status}"
    if not result.get("invoice"):
        return "Testrechnung wurde nicht gelesen."
    if _deep.get("kosit_configured") and result.get("kosit") is None:
        return f"KoSIT-Prüfung läuft nicht: {result.get('kosit_skipped_reason') or 'keine Antwort'}"
    return None


def check_mail_in() -> str | None:
    if not ENV.get("INBOUND_DOMAIN"):
        return None
    try:
        with socket.create_connection(("127.0.0.1", 25), timeout=10) as s:
            banner = s.recv(200).decode(errors="replace")
            s.sendall(b"QUIT\r\n")
    except OSError as exc:
        return f"Mail-Empfang (Port 25) antwortet nicht: {exc}"
    return None if banner.startswith("220") else f"Mail-Empfang antwortet unerwartet: {banner[:80]}"


def check_certificate() -> str | None:
    if not BASE.startswith("https://"):
        return None
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((DOMAIN, 443), timeout=10) as raw:
            with ctx.wrap_socket(raw, server_hostname=DOMAIN) as s:
                not_after = s.getpeercert()["notAfter"]
    except (OSError, ssl.SSLError) as exc:
        return f"HTTPS-Zertifikat nicht in Ordnung: {exc}"
    days = (ssl.cert_time_to_seconds(not_after) - time.time()) / 86400
    if days > 14:
        return None
    return f"HTTPS-Zertifikat läuft in {days:.0f} Tagen ab (Caddy erneuert normalerweise selbst)."


def check_resources() -> str | None:
    problems = []
    disk = shutil.disk_usage("/")
    used = disk.used / disk.total * 100
    if used > 85:
        problems.append(f"Festplatte zu {used:.0f} % voll ({disk.free // 2**30} GB frei)")
    try:
        mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        avail = int(mem["MemAvailable"].split()[0]) // 1024
        if avail < 200:
            problems.append(f"nur noch {avail} MB Arbeitsspeicher frei")
    except (OSError, KeyError, ValueError):
        pass
    return "; ".join(problems) or None


def check_backup() -> str | None:
    folder = Path(ENV.get("BACKUP_DIR", str(Path.home() / "backups" / "erechnung")))
    files = sorted(folder.glob("db-*.sql.gz"), key=lambda p: p.stat().st_mtime) if folder.exists() else []
    if not files:
        return f"Keine Datenbank-Sicherung in {folder} gefunden."
    hours = (time.time() - files[-1].stat().st_mtime) / 3600
    if hours > 26:
        return f"Letzte Datenbank-Sicherung ist {hours:.0f} Stunden alt ({files[-1].name})."
    if files[-1].stat().st_size < 1000:
        return f"Letzte Sicherung ist verdächtig klein ({files[-1].stat().st_size} Bytes)."
    return None


CHECKS = [("Container", check_containers), ("Website", check_website), ("Innenleben", check_inside),
          ("Testrechnung", check_real_invoice), ("Mail-Empfang", check_mail_in),
          ("Zertifikat", check_certificate), ("Server", check_resources), ("Sicherung", check_backup)]


# ------------------------------------------------------------------ alerts
def send_mail(subject: str, body: str) -> None:
    to = ENV.get("ALERT_EMAIL")
    host = ENV.get("SMTP_HOST")
    if not (to and host and ENV.get("MAIL_FROM")):
        print("ALERT (keine E-Mail, ALERT_EMAIL/SMTP fehlt):", subject, "\n", body, flush=True)
        return
    msg = EmailMessage()
    msg["From"] = formataddr(("E-Rechnung Agent Monitor", ENV["MAIL_FROM"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=ENV["MAIL_FROM"].rpartition("@")[2])
    msg.set_content(body)
    port = int(ENV.get("SMTP_OUT_PORT", "587"))
    security = ENV.get("SMTP_SECURITY", "starttls")
    if security == "ssl":
        conn = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
    else:
        conn = smtplib.SMTP(host, port, timeout=30)
        if security == "starttls":
            conn.starttls(context=ssl.create_default_context())
    with conn:
        if ENV.get("SMTP_USER"):
            conn.login(ENV["SMTP_USER"], ENV.get("SMTP_PASSWORD", ""))
        conn.send_message(msg)


def run(report: bool = False) -> int:
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = time.time()
    newly_bad, still_bad, recovered = [], [], []
    for name, fn in CHECKS:
        try:
            problem = fn()
        except Exception as exc:  # noqa: BLE001  a broken check is itself worth knowing
            problem = f"Prüfung abgestürzt: {type(exc).__name__}: {exc}"
        s = state.setdefault(name, {"fails": 0, "alerted": 0, "since": None})
        if report:
            print(f"{'OK    ' if not problem else 'FEHLER'}  {name:<13} {problem or ''}")
        if problem:
            s["fails"] += 1
            s["since"] = s["since"] or now
            s["problem"] = problem
            if s["fails"] >= FAIL_AFTER:
                if not s["alerted"]:
                    newly_bad.append((name, problem))
                    s["alerted"] = now
                elif now - s["alerted"] > REMIND_HOURS * 3600:
                    still_bad.append((name, problem, s["since"]))
                    s["alerted"] = now
        else:
            if s.get("alerted"):
                recovered.append((name, s.get("since")))
            state[name] = {"fails": 0, "alerted": 0, "since": None}
    STATE.write_text(json.dumps(state))
    if report:
        return 0
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    if newly_bad or still_bad:
        lines = [f"Stand {stamp} auf {DOMAIN}:", ""]
        lines += [f"✗ {n}: {p}" for n, p in newly_bad]
        lines += [f"✗ {n} (seit {datetime.fromtimestamp(t).strftime('%d.%m. %H:%M')}): {p}" for n, p, t in still_bad]
        lines += ["", "Nachsehen auf dem Server:", f"  cd {ROOT} && docker compose ps",
                  "  docker compose logs --tail 50 api worker mailin kosit", "",
                  f"Alle Prüfungen anzeigen: python3 {ROOT}/scripts/monitor.py --report"]
        names = ", ".join(n for n, *_ in newly_bad + still_bad)
        send_mail(f"⚠ E-Rechnung Agent: Problem – {names}", "\n".join(lines))
    if recovered:
        lines = [f"Stand {stamp}: wieder in Ordnung", ""]
        lines += [f"✓ {n}" + (f" (gestört seit {datetime.fromtimestamp(t).strftime('%d.%m. %H:%M')})" if t else "")
                  for n, t in recovered]
        send_mail("✓ E-Rechnung Agent: wieder in Ordnung – " + ", ".join(n for n, _ in recovered), "\n".join(lines))
    print(f"{datetime.now(UTC).isoformat(timespec='seconds')} checks done: "
          f"{len(newly_bad)} new, {len(still_bad)} reminders, {len(recovered)} recovered", flush=True)
    return 0


if __name__ == "__main__":
    if "--test-mail" in sys.argv:
        send_mail("Test: E-Rechnung Agent Monitor", f"Wenn du diese E-Mail liest, kommen Warnungen von {DOMAIN} an.")
        print("Test-E-Mail verschickt an", ENV.get("ALERT_EMAIL"))
        sys.exit(0)
    sys.exit(run(report="--report" in sys.argv))
