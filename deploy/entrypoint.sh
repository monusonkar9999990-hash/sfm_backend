#!/bin/sh
# Container start-up: wait for the database, migrate, collect static, then run.
#
# `set -e` matters here. Without it a failed migration is followed by gunicorn
# starting anyway, and the first request fails against a half-migrated schema
# instead of the container simply refusing to come up.
set -e

echo "==> Waiting for the database..."

# Retry rather than fail on the first attempt: in Compose the app container
# regularly starts before MySQL finishes its own initialisation, and a single
# attempt turns an ordinary race into a crash loop.
attempts=0
until python -c "
import django, os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
from django.db import connection
connection.ensure_connection()
" 2>/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 30 ]; then
        echo "!! Database still unreachable after 30 attempts. Giving up."
        echo "   Check DB_HOST, DB_USER, DB_PASSWORD and that MySQL is running."
        exit 1
    fi
    echo "    not ready yet (attempt $attempts/30)..."
    sleep 2
done

echo "==> Applying migrations"
# Never --fake, never --run-syncdb: both let the schema and the migration
# history disagree, which is discovered much later and much worse.
python manage.py migrate --noinput

echo "==> Collecting static files"
python manage.py collectstatic --noinput --clear

echo "==> Checking deployment configuration"
# Fails the start-up if DEBUG is on or a security setting is missing, so a
# misconfigured container never reaches the load balancer.
python manage.py check --deploy --fail-level WARNING || {
    echo "!! Deployment checks reported problems. Refusing to start."
    exit 1
}

echo "==> Starting: $*"
exec "$@"
