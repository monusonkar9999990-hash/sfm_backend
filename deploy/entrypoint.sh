#!/bin/sh
# Container start-up: wait for the database, migrate, collect static, then run.
#
# `set -e` matters here. Without it a failed migration is followed by gunicorn
# starting anyway, and the first request fails against a half-migrated schema
# instead of the container simply refusing to come up.
set -e

echo "==> Waiting for the database..."

# The connection attempt, kept in one place so the silent retry and the loud
# final attempt cannot drift apart.
db_check() {
    python -c "
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from django.db import connection
connection.ensure_connection()
print('connected:', connection.settings_dict['ENGINE'],
      connection.settings_dict['HOST'] or '(default host)')
"
}

# Retry rather than fail on the first attempt: in Compose the app container
# regularly starts before MySQL finishes its own initialisation, and a single
# attempt turns an ordinary race into a crash loop.
attempts=0
until db_check 2>/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 30 ]; then
        echo "!! Database still unreachable after 30 attempts. The error was:"
        echo "-------------------------------------------------------------"
        # Once more with stderr showing. Suppressing it for the retries keeps
        # an ordinary start-up race quiet; suppressing it here too is how a
        # missing driver, a refused TLS mode and a wrong password all arrive
        # looking like the same "not ready yet", which is a long way from the
        # line that actually caused it.
        db_check || true
        echo "-------------------------------------------------------------"
        echo "   Check DATABASE_URL (managed Postgres) or DB_HOST/DB_USER/"
        echo "   DB_PASSWORD (MySQL), and DB_SSL_MODE if the server refuses TLS."
        exit 1
    fi
    echo "    not ready yet (attempt $attempts/30)..."
    sleep 2
done

echo "==> Applying migrations"
# Never --fake, never --run-syncdb: both let the schema and the migration
# history disagree, which is discovered much later and much worse.
python manage.py migrate --noinput

echo "==> Ensuring the default roles"
# Idempotent, and needed before anybody can be given a role — a fresh database
# has no permission rows at all, so without this the first administrator can
# sign in and do nothing.
python manage.py ensure_roles

# The first account on a fresh deployment. `seed_demo` deliberately refuses to
# run outside DEBUG — its passwords are published in the repository — so a
# managed deployment needs a different way in, and this is it: one account,
# from environment variables, created once.
if [ -n "$DJANGO_SUPERUSER_EMPLOYEE_CODE" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
    echo "==> Ensuring the bootstrap administrator"
    # Fails harmlessly when the account already exists, which is every deploy
    # after the first.
    python manage.py createsuperuser --noinput 2>&1 | grep -v "already exists" || true
fi

echo "==> Collecting static files"
python manage.py collectstatic --noinput --clear

echo "==> Checking deployment configuration"
# Fails the start-up if DEBUG is on or a security setting is missing, so a
# misconfigured container never reaches the load balancer.
#
# `--tag security` is what keeps that promise narrow enough to keep. Without
# it, `--fail-level WARNING` fails on *every* warning any app registers — and
# the first deployment to a managed platform was refused by four notes from
# the OpenAPI generator about enum naming collisions in the documentation.
# Refusing to serve the field team over a schema component's name is not the
# trade this guard was written to make.
python manage.py check --deploy --fail-level WARNING --tag security || {
    echo "!! Security checks reported problems. Refusing to start."
    exit 1
}

# Everything else is reported and does not block. Warnings still belong in the
# log — they are simply not grounds for keeping the service down.
python manage.py check --deploy || true

echo "==> Starting: $*"
exec "$@"
