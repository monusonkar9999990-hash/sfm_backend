"""Serves the web portal — the Flutter app compiled for the browser.

Not a Django app: no models, no templates, nothing to migrate. It is a plain
package holding two small views and the built front-end under `web/`, so it
stays out of INSTALLED_APPS.
"""
