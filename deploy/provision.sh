#!/usr/bin/env bash
#
# Provisions a fresh Ubuntu VPS to serve the SFM API over HTTPS.
#
#   scp -r . user@<your-vps-ip>:/tmp/sfm
#   ssh user@<your-vps-ip>
#   sudo bash /tmp/sfm/deploy/provision.sh
#
# Idempotent: safe to run again after fixing something. It never drops a
# database, never overwrites an existing .env, and never touches a certificate
# that already exists.
#
# What it does NOT do, because these are yours to decide:
#   * choose a hostname
#   * choose passwords
#   * point DNS at this machine
#
# It asks for the first two and checks the third before going near certbot.

set -euo pipefail

APP_USER=sfm
APP_DIR=/srv/sfm/app
MEDIA_DIR=/srv/sfm/media
VENV=/srv/sfm/app/venv

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m !! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m !! %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run with sudo."

# --------------------------------------------------------------- what we need

say "Hostname"
echo "The API needs a name, not a bare IP: Let's Encrypt will not issue a"
echo "certificate for an IP address, and the app refuses to run over plain HTTP."
echo
echo "Free option: register a subdomain at https://www.duckdns.org and point it"
echo "at this machine's IP. Takes about two minutes."
echo
read -rp "API hostname (e.g. sortstring-sfm.duckdns.org): " API_HOST
[ -n "$API_HOST" ] || die "A hostname is required."

read -rp "Email for certificate expiry notices: " CERT_EMAIL
[ -n "$CERT_EMAIL" ] || die "Let's Encrypt requires an email."

# DNS first. certbot's rate limits are per-domain-per-week, and burning one on
# a hostname that does not resolve here is a bad way to spend an afternoon.
say "Checking that $API_HOST points at this machine"
PUBLIC_IP=$(curl -fsS https://api.ipify.org || echo '')
RESOLVED=$(getent hosts "$API_HOST" | awk '{print $1}' | head -1 || echo '')

echo "  this machine : ${PUBLIC_IP:-unknown}"
echo "  $API_HOST resolves to : ${RESOLVED:-nothing}"

if [ -z "$RESOLVED" ]; then
    die "$API_HOST does not resolve. Create the DNS record first, then re-run."
fi
if [ -n "$PUBLIC_IP" ] && [ "$RESOLVED" != "$PUBLIC_IP" ]; then
    warn "$API_HOST points at $RESOLVED, not at this machine."
    read -rp "Carry on anyway? [y/N] " go
    [ "$go" = "y" ] || exit 1
fi

# --------------------------------------------------------------- system

say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3-venv python3-dev build-essential pkg-config \
    default-libmysqlclient-dev libjpeg-dev zlib1g-dev \
    mysql-server nginx certbot python3-certbot-nginx curl

say "Creating the service user"
id -u "$APP_USER" >/dev/null 2>&1 || \
    useradd --system --create-home --home /srv/sfm --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR" "$MEDIA_DIR"

say "Copying the application"
# --delete would remove the venv and .env on a re-run, so it is deliberately
# absent; this only refreshes the code.
rsync -a --exclude venv --exclude .env --exclude __pycache__ \
    "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/" "$APP_DIR/"
chown -R "$APP_USER:$APP_USER" /srv/sfm

# --------------------------------------------------------------- database

say "Database"
if mysql -e "USE sfm_db" 2>/dev/null; then
    echo "  sfm_db already exists — leaving it and its data alone."
else
    read -rsp "  Choose a password for the sfm_user database account: " DB_PASSWORD
    echo
    [ -n "$DB_PASSWORD" ] || die "The database password cannot be empty."

    mysql <<SQL
CREATE DATABASE sfm_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'sfm_user'@'localhost' IDENTIFIED BY '${DB_PASSWORD}';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX,
      REFERENCES ON sfm_db.* TO 'sfm_user'@'localhost';
FLUSH PRIVILEGES;
SQL
    echo "  created."
fi

# --------------------------------------------------------------- environment

say "Environment"
if [ -f "$APP_DIR/.env" ]; then
    echo "  .env exists — leaving it alone. Check DJANGO_ALLOWED_HOSTS includes"
    echo "  $API_HOST, then re-run if you change it."
else
    [ -n "${DB_PASSWORD:-}" ] || read -rsp "  sfm_user database password: " DB_PASSWORD
    echo

    SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')

    cat > "$APP_DIR/.env" <<ENV
DJANGO_SECRET_KEY=${SECRET}
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=${API_HOST}
DJANGO_CSRF_TRUSTED_ORIGINS=https://${API_HOST}
CORS_ALLOWED_ORIGINS=
DJANGO_TIME_ZONE=Asia/Kolkata
DJANGO_LOG_LEVEL=INFO

DB_NAME=sfm_db
DB_USER=sfm_user
DB_PASSWORD=${DB_PASSWORD}
DB_HOST=127.0.0.1
DB_PORT=3306
DB_CONN_MAX_AGE=60
DB_CONNECT_TIMEOUT=10
DB_ISOLATION_LEVEL=read committed

API_PAGE_SIZE=20
# Production rates. 10/min on login is what makes password guessing
# impractical; development raises these and they must never come back.
THROTTLE_ANON=30/min
THROTTLE_USER=2000/hour
THROTTLE_LOGIN=10/min
THROTTLE_OTP=5/min
THROTTLE_INVITE=5/hour

JWT_ACCESS_MINUTES=60
JWT_REFRESH_DAYS=7

DJANGO_MEDIA_ROOT=${MEDIA_DIR}
FILE_UPLOAD_MAX_MEMORY_SIZE=5242880
DATA_UPLOAD_MAX_MEMORY_SIZE=10485760

DJANGO_SECURE_SSL_REDIRECT=True
DJANGO_HSTS_SECONDS=2592000
ENV
    echo "  written, with a freshly generated secret key."
fi

# root owns it, the service user may only read: it holds the database password
# and the key every JWT is signed with.
chown root:"$APP_USER" "$APP_DIR/.env"
chmod 640 "$APP_DIR/.env"

# --------------------------------------------------------------- application

say "Python environment"
sudo -u "$APP_USER" python3 -m venv "$VENV" 2>/dev/null || true
sudo -u "$APP_USER" "$VENV/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$VENV/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

say "Migrations and static files"
cd "$APP_DIR"
# migrate only. Never --fake, never flush: this may be re-run against a
# database with real field data in it.
sudo -u "$APP_USER" bash -c "set -a; . '$APP_DIR/.env'; set +a; '$VENV/bin/python' manage.py migrate --noinput"
sudo -u "$APP_USER" bash -c "set -a; . '$APP_DIR/.env'; set +a; '$VENV/bin/python' manage.py collectstatic --noinput"

say "Deployment checks"
sudo -u "$APP_USER" bash -c "set -a; . '$APP_DIR/.env'; set +a; '$VENV/bin/python' manage.py check --deploy" \
    || die "Deployment checks failed. Fix them before exposing this."

# --------------------------------------------------------------- gunicorn

say "gunicorn service"
sed -e "s|<your-service-user>|$APP_USER|g" \
    -e "s|<your-service-group>|$APP_USER|g" \
    -e "s|<your-project-path>|$APP_DIR|g" \
    -e "s|<your-media-root>|$MEDIA_DIR|g" \
    "$APP_DIR/deploy/sfm-api.service.template" > /etc/systemd/system/sfm-api.service

systemctl daemon-reload
systemctl enable --now sfm-api
sleep 3
systemctl is-active --quiet sfm-api || {
    journalctl -u sfm-api -n 30 --no-pager
    die "gunicorn did not start. The log above says why."
}

# --------------------------------------------------------------- nginx

say "nginx"
sed -e "s|<your-api-domain>|$API_HOST|g" \
    -e "s|<your-media-root>|$MEDIA_DIR|g" \
    -e "s|<your-project-path>|$APP_DIR|g" \
    "$APP_DIR/deploy/nginx.conf.template" > /etc/nginx/sites-available/sfm-api

# The TLS block points at certificates that do not exist yet, so serve plain
# HTTP first, let certbot install the real block, and reload.
sed -i '/listen 443 ssl;/,$d' /etc/nginx/sites-available/sfm-api
echo '}' >> /etc/nginx/sites-available/sfm-api

ln -sf /etc/nginx/sites-available/sfm-api /etc/nginx/sites-enabled/sfm-api
rm -f /etc/nginx/sites-enabled/default
mkdir -p /var/www/certbot
nginx -t && systemctl reload nginx

say "Certificate"
if [ -d "/etc/letsencrypt/live/$API_HOST" ]; then
    echo "  already issued — leaving it alone."
else
    certbot --nginx -d "$API_HOST" --non-interactive --agree-tos \
        -m "$CERT_EMAIL" --redirect \
        || die "certbot failed. Check that port 80 is open and DNS is correct."
fi

# Now the real config, with the TLS block certbot's paths satisfy.
sed -e "s|<your-api-domain>|$API_HOST|g" \
    -e "s|<your-media-root>|$MEDIA_DIR|g" \
    -e "s|<your-project-path>|$APP_DIR|g" \
    "$APP_DIR/deploy/nginx.conf.template" > /etc/nginx/sites-available/sfm-api
nginx -t && systemctl reload nginx

say "Firewall"
if command -v ufw >/dev/null; then
    ufw allow OpenSSH  >/dev/null 2>&1 || true
    ufw allow 'Nginx Full' >/dev/null 2>&1 || true
    ufw --force enable >/dev/null 2>&1 || true
    echo "  80, 443 and SSH open; everything else closed."
    echo "  MySQL is not exposed: it listens on localhost only."
fi

# --------------------------------------------------------------- done

say "Checking it answers"
sleep 2
if curl -fsS "https://$API_HOST/api/v1/health/" | grep -q '"status"'; then
    echo "  https://$API_HOST/api/v1/health/ is up."
else
    warn "The health endpoint did not answer. Try:"
    warn "  journalctl -u sfm-api -n 50"
fi

cat <<DONE

────────────────────────────────────────────────────────────
 Your production API:  https://$API_HOST
────────────────────────────────────────────────────────────

Two things left, in this order:

1. Create the first administrator
     cd $APP_DIR
     sudo -u $APP_USER bash -c "set -a; . .env; set +a; venv/bin/python manage.py createsuperuser"

   Do NOT run seed_demo here: it creates accounts with a known password.

2. Tell the version gate which build is current, or every user is shown the
   update wall on first launch:
     sudo -u $APP_USER bash -c "set -a; . .env; set +a; venv/bin/python manage.py shell"

     from appinfo.models import AppRelease
     AppRelease.objects.update_or_create(
         platform='android', is_current=True,
         defaults={'version': '1.4.0',
                   'minimum_supported_version': '1.4.0',
                   'force_update': False,
                   'release_notes': 'First production release.'})

Then, from your laptop:
     python scripts/smoke_test.py https://$API_HOST --user <employee-code>

And the app build:
     flutter build appbundle --release \\
       --dart-define=USE_MOCKS=false \\
       --dart-define=API_BASE_URL=https://$API_HOST \\
       --dart-define=APP_VERSION=1.4.0

DONE
