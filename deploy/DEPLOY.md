# Deploying the Signalor backend to a Hetzner VPS

Single-box Docker Compose deployment that mirrors the Render topology:
**Caddy → Django/Gunicorn web → Celery worker → Postgres → Redis**, with the
two scheduled management commands and nightly DB backups run from the host
crontab.

- **Server:** Ubuntu, `178.105.176.220` (8 GB)
- **Volume:** mounted at `/mnt/HC_Volume_105845076` (durable state: Postgres,
  backups, Caddy certs)
- **App image:** built from `be/Dockerfile` (Chromium baked in for screenshots)

All paths below assume the repo lives at `/opt/signalor` and you run commands
from `/opt/signalor/be/deploy`.

---

## 0. Prerequisites (one-time)

**DNS** — point your API hostname at the server *before* starting Caddy, or
Let's Encrypt can't validate:

```
api.signalor.ai.   A   178.105.176.220
```

**Firewall** — open SSH + HTTP + HTTPS. In the **Hetzner Cloud Console**
firewall (if attached) allow inbound `22, 80, 443`. And on the host:

```bash
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw enable
```

> Postgres (5432) and Redis (6379) are **not** published to the host — they're
> only reachable on the internal Docker network. Keep it that way.

---

## 1. Install Docker + Compose plugin

```bash
curl -fsSL https://get.docker.com | sh
docker compose version   # confirm the v2 plugin is present
```

---

## 2. Create the durable directories on the volume

```bash
mkdir -p /mnt/HC_Volume_105845076/{postgres,backups,caddy/data,caddy/config}
```

---

## 3. Get the code

```bash
mkdir -p /opt && cd /opt
git clone <your-repo-url> signalor
cd /opt/signalor/be/deploy
```

---

## 4. Configure secrets

```bash
cp stack.env.example stack.env
```

Generate the three crypto values and paste them into `stack.env`:

```bash
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(64))"
python3 -c "import secrets; print('DB_PASSWORD=' + secrets.token_urlsafe(32))"
# Needs the cryptography pkg; if missing: pip install cryptography  (or generate later inside the web container)
python3 -c "from cryptography.fernet import Fernet; print('ENCRYPTION_KEY=' + Fernet.generate_key().decode())"
```

Then fill in `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, API keys (Gemini,
DataForSEO), SMTP, OAuth, payments, and the OTEL header. **Leave `DATABASE_URL`
unset** — the stack uses the `DB_*` vars so the local Postgres (no TLS) connects
cleanly.

Also set your hostname in **`Caddyfile`** (replace `api.signalor.ai`) so it
matches `ALLOWED_HOSTS`.

---

## 5. Build and start

```bash
docker compose build          # first build pulls Chromium + deps — several minutes
docker compose up -d
docker compose ps             # all services should be "running"/"healthy"
docker compose logs -f web    # watch migrations run, then gunicorn boot
```

The `web` container runs `reconcile_migrations.py` + `migrate` automatically on
every start before gunicorn takes over.

---

## 6. Create an admin user

```bash
docker compose exec web python manage.py createsuperuser
```

---

## 7. Verify

```bash
# From the server — should return JSON (not a 400/502):
curl -sS https://api.signalor.ai/api/auth/get-session

# TLS cert issued?
docker compose logs caddy | grep -i certificate
```

Then hit `https://api.signalor.ai/<ADMIN_URL>` in a browser and log in.

---

## 8. Scheduled jobs + backups (host crontab)

These replace Render's two cron services and add DB backups. `exec` reuses the
running `web` container (cheap; Chromium image already loaded).

```bash
chmod +x /opt/signalor/be/deploy/backup.sh
crontab -e
```

Add:

```cron
# Scheduled analyses — every 30 min (matches Render's */30 schedule)
*/30 * * * * cd /opt/signalor/be/deploy && /usr/bin/docker compose exec -T web python manage.py run_scheduled_analyses >> /var/log/signalor-cron.log 2>&1

# Cleanup stale rewards — daily 04:00
0 4 * * * cd /opt/signalor/be/deploy && /usr/bin/docker compose exec -T web python manage.py cleanup_stale_rewards >> /var/log/signalor-cron.log 2>&1

# Postgres backup — daily 03:00 → /mnt/HC_Volume_105845076/backups (7-day retention)
0 3 * * * /opt/signalor/be/deploy/backup.sh >> /var/log/signalor-backup.log 2>&1
```

> The Celery **worker** (sitemap audits) already runs as a long-lived container —
> it is *not* a cron job.

---

## 9. Updating to a new release

```bash
cd /opt/signalor && git pull
cd be/deploy
docker compose build web worker
docker compose up -d            # recreates changed services; migrations run on web start
docker compose logs -f web
```

Roll back by checking out the previous commit and repeating.

---

## Restoring a backup

```bash
gunzip -c /mnt/HC_Volume_105845076/backups/signalor_<STAMP>.sql.gz \
  | docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

---

## Operational notes

- **Volume is only 10 GB.** Watch it: `df -h /mnt/HC_Volume_105845076`. Resize in
  the Hetzner console (then `resize2fs /dev/sdb`) before it passes ~70%.
- **Memory (8 GB):** web (gunicorn + Chromium), worker, Postgres, Redis all share
  it. Chromium screenshots are the heaviest. If you see OOM kills
  (`dmesg | grep -i oom`), drop gunicorn `--workers`/`--threads` or worker
  `--concurrency`, or add swap.
- **Logs** stream to stdout → `docker compose logs`, and (if OTEL is configured)
  to Better Stack. Nothing is written to the volume.
- **Webhook/OAuth callback URLs** (Shopify, Dodo, Stripe, Google) must point at
  this server's hostname — update them in each provider's dashboard when you cut
  over from Render.
