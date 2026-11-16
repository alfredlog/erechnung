# Deployment on the VPS

## 1. Configure

```bash
cd ~/apps/erechnung
git pull
cp -n .env.example .env        # only if there is no .env yet
nano .env
```

Set at least `SECRET_KEY` (`openssl rand -hex 32`), `BASE_URL`, `POSTGRES_PASSWORD` and the same
password in `DATABASE_URL`. If the database already exists, keep the password it was created with.

## 2. Start

```bash
docker compose up -d --build
docker compose ps
curl localhost:8100/health
```

Containers: `api` (website), `worker` (background processing), `kosit` (validator), `db` (PostgreSQL).
Tables are created automatically on the first start.

## 3. Domain with Caddy + HTTPS

DNS at IONOS: A records for `@` and `www` → the VPS IP. Then add to `/etc/caddy/Caddyfile`:

```caddy
erechnungagent.de, www.erechnungagent.de {
    request_body {
        max_size 20MB
    }
    reverse_proxy 127.0.0.1:8100
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy gets the HTTPS certificate automatically and sets the X-Forwarded headers.

## 4. Receiving invoice mails (port 25)

Every company gets its own address `<firma>-xxxxxx@eingang.erechnungagent.de` (page "Rechnungen per
E-Mail"). The `mailin` container receives these mails; it never sends mail and refuses unknown addresses.

1. `.env`: `INBOUND_DOMAIN=eingang.erechnungagent.de`
2. DNS at IONOS: an **MX** record for host `eingang`, pointing to `erechnungagent.de`, priority 10.
3. Firewall: `sudo ufw allow 25/tcp` (Docker publishes the port anyway; this keeps ufw honest).
4. `docker compose up -d --build`, then test from outside:
   `swaks --to <address> --server eingang.erechnungagent.de --attach @rechnung.pdf` or simply send a mail.

## 5. Sending invoices by e-mail

Mails go out through the IONOS mailbox `rechnungen@erechnungagent.de` (never directly from the VPS,
whose IP would land in spam). In `.env`:

```
SMTP_HOST=smtp.ionos.de
SMTP_OUT_PORT=587
SMTP_SECURITY=starttls
SMTP_USER=rechnungen@erechnungagent.de
SMTP_PASSWORD=<mailbox password>
MAIL_FROM=rechnungen@erechnungagent.de
```

Then `docker compose up -d api worker`. Sender shown to customers: "<Firma> via E-Rechnung Agent",
Reply-To = the company's own e-mail. Max. `MAX_MAILS_PER_DAY` mails per company and day.

## 6. Plans and payments (Stripe)

Plans and prices are defined in `app/services/billing.py` (PLANS). One command creates or updates
products, prices, the customer portal and the webhook in Stripe – for test and live mode alike:

```bash
# .env: STRIPE_SECRET_KEY=sk_test_…   (later sk_live_…)
docker compose run --rm api python -m app.services.billing setup
# copy the printed STRIPE_WEBHOOK_SECRET=whsec_… into .env, then:
docker compose up -d api worker
```

Run the command again after changing prices: new prices are created and take over the lookup keys,
existing subscribers keep their price until they change plan. `--reset-webhook` recreates the
webhook (new secret). Test cards: 4242 4242 4242 4242, any future date, any CVC.

## 7. Team, notifications, app

- Team: Admins invite colleagues (role Mitarbeiter) or the tax advisor (read + export) under
  Einstellungen → Team. Invitation mails need the SMTP settings; without them the page shows the link.
- Notifications: the worker sends the daily "invoices waiting" mail at 8:00 German time and trial
  reminders. Users switch it off under "Mein Konto".
- App: the site is installable (manifest, service worker at /sw.js). Needs HTTPS. On Android it also
  appears in the share menu. Help page for customers: /app.

## 8. Backups (daily)

```bash
mkdir -p ~/backups/erechnung
crontab -e
# 03:15 every night: database dump + original files, keep 30 days
15 3 * * * cd ~/apps/erechnung && docker compose exec -T db pg_dump -U erechnung erechnung | gzip > ~/backups/erechnung/db-$(date +\%F).sql.gz && docker run --rm -v erechnung_files:/data -v ~/backups/erechnung:/b alpine tar czf /b/files-$(date +\%F).tgz -C /data . && find ~/backups/erechnung -mtime +30 -delete
```

Copy the backups regularly to a second place (e.g. netcup storage or your laptop).

## 9. Update

```bash
cd ~/apps/erechnung && git pull && docker compose up -d --build
```

## 10. PDF Libre tools

PDF Libre's `.env`: `ERECHNUNG_API_URL=http://127.0.0.1:8100`. The tools "XRechnung anzeigen"
and "E-Rechnung prüfen" call `POST /api/v1/render`.
