# Web portal

The same Flutter application the field staff carry, compiled for the browser and
served by Django at `/portal/`.

    http://192.168.1.31:8000/portal/

## What it carries

Every module the phone has: dashboard, attendance, beat planning, customers,
site visits, products, orders, reports, sync and administration. It used to
carry two of them — beat planning and reports — on the reasoning that a desk
has no camera and no GPS. A browser has both: `geolocator_web` forwards to the
browser's Geolocation API and `image_picker` opens the webcam or a file
chooser, so the split only meant a supervisor picking up a phone to approve a
site or suspend a user.

One thing still differs, and it is not a screen. A photo attached to a record
made *offline* cannot be kept — a browser tab has no filesystem, and a blob URL
does not survive a reload — so the app refuses that write and says so rather
than queueing a punch the server would reject on every replay. Online, which is
the normal case at a desk, photos upload as they do on the phone.

## Why it lives here

Serving it from this project rather than its own host puts the portal and the
API on one origin. `/portal/` and `/api/v1/` are then same-site, so the browser
asks no CORS question and none has to be answered — `CORS_ALLOW_ALL_ORIGINS` is
on in development only because `DEBUG` is, and the portal does not depend on it.

## Layout

    portal/
    ├── __init__.py   a plain package, not a Django app: no models, no migrations
    ├── urls.py       mounted by config/urls.py under /portal/
    ├── views.py      the entry point, and the assets under it
    └── web/          the build output — git-ignored, see below

## Rebuilding

`web/` is ~43MB of compiled output and is **not committed**. It has to be built
from the Flutter project after any Dart change:

    cd "<flutter project>"
    .\scripts\build-portal.ps1

That runs `flutter build web --base-href /portal/` with
`config/web-portal.json` and copies the result here. To aim it somewhere else:

    .\scripts\build-portal.ps1 -ApiBaseUrl http://192.168.1.50:8000

Two things the build depends on, both easy to trip over:

**`--base-href /portal/` is not optional.** Django serves the portal under a
prefix, and the build writes that prefix into `index.html`. Built without it,
every asset is requested from `/` and the page loads to a blank screen. In Git
Bash the value is rewritten to a Windows path by MSYS and silently ignored —
run the script from PowerShell, which is why it is a `.ps1`.

**`ALLOW_INSECURE_CONFIG=true` is in the config file on purpose.** `flutter
build web` produces a *release* build, and this project refuses to start a
release that points at a private address over plain HTTP — the app would render
its "not fit to ship" screen instead. The flag is the documented escape hatch
for a LAN target. It does **not** relax the mock rule: `USE_MOCKS` stays false,
so the portal always talks to the real API.

When the portal is finally served over HTTPS from a real hostname, drop the flag
rather than carrying it forward — it is what stops an insecure build shipping.

## A release row is needed for `web`

The portal asks `/app-version/?platform=web` on start-up, and the endpoint
answers 404 until a release has been published for that platform. Nothing
breaks — the client falls back to its cache and carries on — but the version
check does nothing and every page load writes a warning to the log.

Publish one from the Django admin under **App releases**, or:

    AppRelease.objects.create(
        platform=Platform.WEB, version='1.4.0',
        minimum_supported_version='1.2.0', is_current=True,
    )

Leave `download_url` blank. A browser reloads itself, and a link there would
offer an APK to somebody already running the app.

## Caching

`index.html` is served `no-store`; everything beside it has a hashed name. After
a rebuild a browser may still run the previous build from its service worker, so
hard-reload (Ctrl+Shift+R) if a change does not appear.
