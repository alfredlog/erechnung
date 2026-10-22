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

## 3. Domain with Nginx + HTTPS

`/etc/nginx/sites-available/erechnung`:

```nginx
server {
    server_name example.de www.example.de;
    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:8100;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/erechnung /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d example.de -d www.example.de
```

## 4. Backups (daily)

```bash
mkdir -p ~/backups/erechnung
crontab -e
# 03:15 every night: database dump + original files, keep 30 days
15 3 * * * cd ~/apps/erechnung && docker compose exec -T db pg_dump -U erechnung erechnung | gzip > ~/backups/erechnung/db-$(date +\%F).sql.gz && docker run --rm -v erechnung_files:/data -v ~/backups/erechnung:/b alpine tar czf /b/files-$(date +\%F).tgz -C /data . && find ~/backups/erechnung -mtime +30 -delete
```

Copy the backups regularly to a second place (e.g. netcup storage or your laptop).

## 5. Update

```bash
cd ~/apps/erechnung && git pull && docker compose up -d --build
```

## 6. PDF Libre tools

PDF Libre's `.env`: `ERECHNUNG_API_URL=http://127.0.0.1:8100`. The tools "XRechnung anzeigen"
and "E-Rechnung prüfen" call `POST /api/v1/render`.
