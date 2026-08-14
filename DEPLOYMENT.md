# Production deployment — SFM backend

Sort String Solution · API for `com.sortstringsolution.salesforcemanagement` 1.4.0

Two routes are prepared. Pick one; they are alternatives, not steps.

- **A — Linux VPS**: gunicorn + nginx + systemd. Most control, most steps.
- **B — Docker Compose**: the same stack in containers. Fewer steps, needs Docker on the host.

Whichever you pick, the app cannot ship until the API answers on **HTTPS at a
real hostname**. The Flutter release build refuses to start against `http://`
or a private address — that guard is deliberate, and it is the last thing
standing between a demo build and the Play Store.

## What you must supply

Nothing below is invented, and none of it can be guessed:

| | |
|---|---|
| API hostname | e.g. `api.sortstringsolution.com` — a DNS A record pointing at the server |
| Server | VPS IP + SSH access, or a Docker host |
| MySQL password | For the `sfm_user` account (route A: you create it; route B: Compose creates it) |
| `DJANGO_SECRET_KEY` | Generate it — command below. Never reuse the development one |
| TLS certificate | Let's Encrypt via certbot is free and scripted below |

---

## Route A — Linux VPS

### The short way

`deploy/provision.sh` does every step below on a fresh Ubuntu machine:

```bash
scp -r . user@<your-vps-ip>:/tmp/sfm
ssh user@<your-vps-ip>
sudo bash /tmp/sfm/deploy/provision.sh
```

It asks for a hostname, an email for the certificate, and a database password.
It checks DNS before touching certbot, refuses to expose anything that fails
`check --deploy`, and never overwrites an existing `.env`, database or
certificate — so it is safe to run again after fixing something.

**You need a hostname, not an IP.** Let's Encrypt will not certify a bare IP,
and the app refuses to run over plain HTTP. A free subdomain from
[duckdns.org](https://www.duckdns.org) pointed at the VPS works and takes two
minutes; your own domain is the tidier answer.

The steps below are what the script does, for when something needs doing by
hand.

### 1. System packages

```bash
sudo apt update
sudo apt install -y python3-venv python3-dev build-essential \
    default-libmysqlclient-dev pkg-config libjpeg-dev zlib1g-dev \
    mysql-server nginx certbot python3-certbot-nginx
```

### 2. Database

```bash
sudo mysql
```

```sql
CREATE DATABASE sfm_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'sfm_user'@'localhost' IDENTIFIED BY '<a-strong-password>';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX,
      REFERENCES ON sfm_db.* TO 'sfm_user'@'localhost';
FLUSH PRIVILEGES;
```

The application must not connect as root. Grant no more than the list above —
it is what migrations need and nothing else.

### 3. Code and virtualenv

```bash
sudo useradd --system --create-home --home /srv/sfm --shell /usr/sbin/nologin sfm
sudo -u sfm git clone <your-repo> /srv/sfm/app
cd /srv/sfm/app
sudo -u sfm python3 -m venv venv
sudo -u sfm venv/bin/pip install -r requirements.txt
```

### 4. Environment

```bash
sudo -u sfm cp .env.example .env
sudo -u sfm nano .env
```

Generate the secret key on the server, not on a laptop, and not into your
shell history:

```bash
sudo -u sfm venv/bin/python -c \
  "from django.core.management.utils import get_random_secret_key as k; print(k())"
```

The values that must change from the template:

```properties
DJANGO_SECRET_KEY=<the generated key>
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=<your-api-domain>
DJANGO_CSRF_TRUSTED_ORIGINS=https://<your-api-domain>
CORS_ALLOWED_ORIGINS=
DB_PASSWORD=<the MySQL password from step 2>
DJANGO_MEDIA_ROOT=/srv/sfm/media
```

Leave `CORS_ALLOWED_ORIGINS` empty unless a browser front-end will call this
API. The mobile app is not a browser and needs no CORS entry.

**Check the throttles.** The development `.env` raises them so a test run does
not throttle itself. Production must be:

```properties
THROTTLE_LOGIN=10/min
THROTTLE_ANON=30/min
THROTTLE_INVITE=5/hour
THROTTLE_USER=2000/hour
```

10/min on login is what makes password guessing impractical. Shipping the
development value of 200/min removes that protection entirely.

Then lock the file down — it holds the database password and the signing key
for every JWT:

```bash
sudo chown root:sfm .env && sudo chmod 640 .env
```

### 5. Migrate and collect static

```bash
sudo -u sfm venv/bin/python manage.py migrate --noinput
sudo -u sfm venv/bin/python manage.py collectstatic --noinput
sudo -u sfm mkdir -p /srv/sfm/media
```

`migrate` only. Never `--fake`, never `flush`, never `migrate zero` on a
database with real data in it.

### 6. Verify before exposing anything

```bash
sudo -u sfm venv/bin/python manage.py check --deploy
```

Expect no `security.W*` warnings. If `W018` (DEBUG is True) appears, `.env`
was not read — check the path and the file permissions.

### 7. gunicorn under systemd

```bash
sudo cp deploy/sfm-api.service.template /etc/systemd/system/sfm-api.service
sudo nano /etc/systemd/system/sfm-api.service     # replace <placeholders>
sudo systemctl daemon-reload
sudo systemctl enable --now sfm-api
sudo systemctl status sfm-api
```

### 8. nginx and TLS

```bash
sudo cp deploy/nginx.conf.template /etc/nginx/sites-available/sfm-api
sudo nano /etc/nginx/sites-available/sfm-api      # replace <placeholders>
sudo ln -s /etc/nginx/sites-available/sfm-api /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d <your-api-domain>
```

certbot installs a renewal timer. Confirm it:

```bash
sudo certbot renew --dry-run
```

---

## Route B — Docker Compose

### 1. Environment

```bash
cp .env.example .env
nano .env
```

Same values as route A step 4, plus one Compose needs:

```properties
MYSQL_ROOT_PASSWORD=<a strong password, different from DB_PASSWORD>
```

Leave `DB_HOST` alone — Compose overrides it to `db`.

### 2. nginx config

```bash
cp deploy/nginx.conf.template deploy/nginx.conf
nano deploy/nginx.conf      # replace <placeholders>
```

`deploy/nginx.conf` is gitignored, so your real hostname stays out of the repo.

For the first certificate, comment out the `listen 443` block, bring the stack
up on port 80 only, then:

```bash
docker compose run --rm --entrypoint "" nginx \
  certbot certonly --webroot -w /var/www/certbot -d <your-api-domain>
```

Restore the 443 block and `docker compose restart nginx`.

### 3. Up

```bash
docker compose up -d --build
docker compose logs -f api
```

The entrypoint waits for MySQL, migrates, collects static, and runs
`check --deploy` before gunicorn starts. A container that fails those checks
never accepts traffic.

---

## After either route

### 1. Create the first administrator

```bash
python manage.py createsuperuser        # route A
docker compose exec api python manage.py createsuperuser   # route B
```

Do **not** run `seed_demo` on production. It creates demo users with a known
password.

### 2. Set the AppRelease row — the app is blocked without it

The Flutter build reports version `1.4.0`. The server decides whether that is
still supported, and with no row at all the version check has nothing to
answer with.

```bash
python manage.py shell
```

```python
from appinfo.models import AppRelease
AppRelease.objects.update_or_create(
    platform='android',
    is_current=True,
    defaults={
        'version': '1.4.0',
        'minimum_supported_version': '1.4.0',
        'force_update': False,
        'release_notes': 'First production release.',
    },
)
```

Confirm it accepts the shipped build:

```python
AppRelease.current().verdict_for('1.4.0')   # must be 'up_to_date'
```

Anything other than `up_to_date` means every user is shown the update wall on
first launch.

### 3. Smoke test

```bash
python scripts/smoke_test.py https://<your-api-domain>
```

It checks HTTPS, the health endpoint, JWT login and refresh, RBAC (401/403),
and that media URLs come back as HTTPS. Run it from a machine outside the
server, so DNS and the certificate are exercised too.

### 4. Then, and only then, build the app

```powershell
flutter build appbundle --release `
  --dart-define=USE_MOCKS=false `
  --dart-define=API_BASE_URL=https://<your-api-domain> `
  --dart-define=APP_VERSION=1.4.0
```

---

## Backups

Set this up before the app has users, not after.

```bash
# Database, daily
mysqldump --single-transaction --routines sfm_db | gzip > sfm-$(date +%F).sql.gz

# Uploaded photos — attendance selfies and site evidence, which exist nowhere else
tar czf sfm-media-$(date +%F).tar.gz /srv/sfm/media
```

`--single-transaction` takes a consistent snapshot without locking the tables,
so the dump does not block field staff mid-shift.

Keep them off the same machine. A backup on the server that dies with the
server is not a backup.

## Things that go wrong

| Symptom | Cause |
|---|---|
| Every request redirects forever | nginx is not sending `X-Forwarded-Proto`; Django thinks it is on HTTP and redirects to HTTPS on a loop |
| `DisallowedHost` | The hostname is missing from `DJANGO_ALLOWED_HOSTS` |
| 502 from nginx | gunicorn is not running, or is bound to a different port — `journalctl -u sfm-api -n 50` |
| 413 on a photo upload | `client_max_body_size` in nginx is below the file size |
| App shows "Update required" | The AppRelease row rejects 1.4.0 — see step 2 |
| App refuses to start | The release guard: mocks on, or the URL is HTTP or private. The message names each fault |
| `ImproperlyConfigured: DB_PASSWORD` | `.env` was not read, or `DJANGO_DEBUG` is not `False` |
