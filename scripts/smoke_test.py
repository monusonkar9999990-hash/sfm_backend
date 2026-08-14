"""Production smoke test - run against a deployed API, from outside the server.

    python scripts/smoke_test.py https://api.example.com
    python scripts/smoke_test.py https://api.example.com --user SFM-0002

Exercises what Phase 3 of the release plan asks for: HTTPS, the health check,
JWT login and refresh, role-based refusals, and that media URLs come back over
HTTPS. Read-only - it creates nothing and changes nothing, so it is safe
against a live system.

Run it from a laptop rather than on the server. From the server, DNS resolves
locally and the certificate is never validated, so two of the things most
likely to be wrong are the two things you would not test.

Needs no dependencies beyond the standard library.
"""

import argparse
import getpass
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 20

PASS = 'PASS'
FAIL = 'FAIL'
WARN = 'WARN'

results = []


def record(status, name, detail=''):
    results.append((status, name, detail))
    colour = {PASS: '\033[92m', FAIL: '\033[91m', WARN: '\033[93m'}.get(status, '')
    reset = '\033[0m' if colour else ''
    line = f'  {colour}{status}{reset}  {name}'
    if detail:
        line += f'\n        {detail}'
    print(line)


def request(url, method='GET', body=None, token=None, timeout=TIMEOUT):
    """Returns (status, parsed_body_or_text, headers). Never raises on HTTP status."""
    data = None
    headers = {'Accept': 'application/json'}

    if body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = f'Bearer {token}'

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode('utf-8', 'replace')
            return response.status, _parse(raw), dict(response.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        return e.code, _parse(raw), dict(e.headers)


def _parse(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('base_url', help='e.g. https://api.example.com')
    parser.add_argument('--user', help='employee code to log in with')
    parser.add_argument(
        '--skip-auth',
        action='store_true',
        help='public checks only; no credentials needed',
    )
    args = parser.parse_args()

    base = args.base_url.rstrip('/')
    api = f'{base}/api/v1'

    print(f'\nSmoke testing {base}\n')

    # --- transport --------------------------------------------------------

    print('Transport')

    if base.startswith('https://'):
        record(PASS, 'URL is HTTPS')
    else:
        record(FAIL, 'URL is HTTPS', f'got {base.split("://")[0]} - the app will refuse this')

    host = urllib.parse.urlparse(base).hostname or ''
    if host in ('localhost', '127.0.0.1', '0.0.0.0', '10.0.2.2') or host.startswith(
        ('192.168.', '10.', '172.16.', '172.17.')
    ):
        record(FAIL, 'Host is publicly routable', f'{host} is local or private')
    else:
        record(PASS, 'Host is publicly routable', host)

    if base.startswith('https://'):
        try:
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(
                __import__('socket').create_connection((host, 443), timeout=TIMEOUT),
                server_hostname=host,
            ) as sock:
                cert = sock.getpeercert()
            subject = dict(x[0] for x in cert.get('subject', ()))
            record(
                PASS,
                'TLS certificate valid for this host',
                f'CN={subject.get("commonName", "?")} until {cert.get("notAfter", "?")}',
            )
        except Exception as e:
            record(FAIL, 'TLS certificate valid for this host', str(e))

    # HTTP must redirect to HTTPS rather than serve.
    if base.startswith('https://'):
        try:
            plain = 'http://' + base.split('://', 1)[1]
            status, _, headers = request(plain)
            location = headers.get('Location', '')
            if location.startswith('https://'):
                record(PASS, 'HTTP redirects to HTTPS')
            else:
                record(WARN, 'HTTP redirects to HTTPS', f'status {status}, Location: {location or "none"}')
        except Exception as e:
            record(WARN, 'HTTP redirects to HTTPS', f'could not check: {e}')

    # --- public endpoints -------------------------------------------------

    print('\nPublic endpoints')

    status, body, headers = request(f'{api}/health/')
    if status == 200 and isinstance(body, dict) and body.get('database') == 'ok':
        record(PASS, 'GET /health/', f'status={body.get("status")} database=ok')
    else:
        record(FAIL, 'GET /health/', f'HTTP {status}: {body}')

    hsts = headers.get('Strict-Transport-Security', '')
    if hsts:
        record(PASS, 'HSTS header present', hsts)
    else:
        record(WARN, 'HSTS header present', 'absent - expected once DEBUG=False')

    if 'nosniff' in headers.get('X-Content-Type-Options', ''):
        record(PASS, 'X-Content-Type-Options: nosniff')
    else:
        record(WARN, 'X-Content-Type-Options: nosniff', 'absent')

    status, body, _ = request(f'{api}/app-version/?platform=android&current_version=1.4.0')
    if status == 200 and isinstance(body, dict):
        verdict = body.get('update_status')
        detail = (
            f'latest={body.get("latest_version")} '
            f'min={body.get("minimum_supported_version")} '
            f'verdict={verdict}'
        )
        if verdict == 'up_to_date':
            record(PASS, 'AppRelease accepts 1.4.0', detail)
        elif verdict == 'update_available':
            record(WARN, 'AppRelease accepts 1.4.0', detail + ' - users see an update prompt')
        else:
            record(FAIL, 'AppRelease accepts 1.4.0', detail + ' - the app will be BLOCKED')
    else:
        record(FAIL, 'AppRelease accepts 1.4.0', f'HTTP {status}: {body}')

    for path in ('privacy/', 'terms/', 'app-config/'):
        status, _, _ = request(f'{api}/{path}')
        if status == 200:
            record(PASS, f'GET /{path}')
        else:
            record(FAIL, f'GET /{path}', f'HTTP {status}')

    # --- unauthenticated access -------------------------------------------

    print('\nAuthentication is enforced')

    for path in ('customers/', 'orders/', 'dashboard/', 'admin/employees/'):
        status, _, _ = request(f'{api}/{path}')
        if status == 401:
            record(PASS, f'GET /{path} without a token -> 401')
        else:
            record(FAIL, f'GET /{path} without a token -> 401', f'got {status}')

    status, _, _ = request(f'{api}/auth/login/', 'POST', {'identifier': 'nobody', 'password': 'wrong'})
    if status in (400, 401):
        record(PASS, 'Bad credentials refused', f'HTTP {status}')
    else:
        record(FAIL, 'Bad credentials refused', f'got {status}')

    if args.skip_auth:
        return summarise()

    # --- authenticated ----------------------------------------------------

    print('\nSigned-in journey')

    identifier = args.user or input('  Employee code: ').strip()

    # The env var is for CI, where there is no terminal to prompt at. Prefer
    # the prompt when a person is running this: a password in an environment
    # variable is readable by every process the shell starts afterwards.
    password = os.environ.get('SFM_SMOKE_PASSWORD')
    if password:
        print('  Password: (from SFM_SMOKE_PASSWORD)')
    else:
        password = getpass.getpass('  Password (not echoed, not stored): ')

    status, body, _ = request(
        f'{api}/auth/login/', 'POST', {'identifier': identifier, 'password': password}
    )
    if status != 200 or not isinstance(body, dict) or 'access' not in body:
        record(FAIL, 'JWT login', f'HTTP {status}: {body}')
        return summarise()

    access = body['access']
    refresh = body.get('refresh', '')
    record(PASS, 'JWT login')

    status, refreshed, _ = request(f'{api}/auth/refresh/', 'POST', {'refresh': refresh})
    if status == 200 and isinstance(refreshed, dict) and 'access' in refreshed:
        record(PASS, 'JWT refresh')
        access = refreshed['access']
    else:
        record(FAIL, 'JWT refresh', f'HTTP {status}')

    status, me, _ = request(f'{api}/auth/me/', token=access)
    if status == 200 and isinstance(me, dict):
        record(PASS, 'Session restore (/auth/me/)', f'{me.get("employee_code")} | {me.get("role")}')
    else:
        record(FAIL, 'Session restore (/auth/me/)', f'HTTP {status}')

    modules = {
        'customers/': 'Customers',
        'products/': 'Products',
        'orders/': 'Orders',
        'attendance/today/': 'Attendance',
        'site-visits/': 'Site visits',
        'beats/plans/': 'Beats',
        'dashboard/': 'Dashboard',
        'reports/sales/': 'Reports',
        'sync/status/': 'Offline sync',
    }
    for path, label in modules.items():
        status, _, _ = request(f'{api}/{path}', token=access)
        if status == 200:
            record(PASS, f'{label} ({path})')
        elif status == 403:
            record(WARN, f'{label} ({path})', '403 - this role may not be permitted, which can be correct')
        else:
            record(FAIL, f'{label} ({path})', f'HTTP {status}')

    status, body, _ = request(f'{api}/customers/?search=a', token=access)
    if status == 200 and isinstance(body, dict):
        record(PASS, 'Customer search', f'{body.get("count", "?")} matches')
    else:
        record(FAIL, 'Customer search', f'HTTP {status}')

    # Media must be served over HTTPS. An http:// URL in the payload means
    # Android blocks the image and every selfie silently fails to load.
    status, body, _ = request(f'{api}/attendance/', token=access)
    if status == 200:
        blob = json.dumps(body)
        if 'http://' in blob:
            record(FAIL, 'Media URLs are HTTPS', 'an http:// URL appears in the response')
        else:
            record(PASS, 'Media URLs are HTTPS')

    return summarise()


def summarise():
    print('\n' + '=' * 60)
    failed = [r for r in results if r[0] == FAIL]
    warned = [r for r in results if r[0] == WARN]
    passed = [r for r in results if r[0] == PASS]

    print(f'  {len(passed)} passed | {len(warned)} warnings | {len(failed)} failed')

    if failed:
        print('\n  Failures:')
        for _, name, detail in failed:
            print(f'    - {name}' + (f' ({detail})' if detail else ''))
        print('\n  NOT ready for the production app build.')
        return 1

    if warned:
        print('\n  Warnings:')
        for _, name, detail in warned:
            print(f'    - {name}' + (f' ({detail})' if detail else ''))

    print('\n  All checks passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
