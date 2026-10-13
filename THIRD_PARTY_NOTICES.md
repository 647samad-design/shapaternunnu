# Third-party components

Lumen Market is proprietary software (see `LICENSE`). It uses the open-source
components below, each under its own licence. All of them allow commercial use
in closed-source products; none requires publishing Lumen Market's own code.

Versions are those installed from `requirements.txt` at the time of writing
(October 2026); `pip install -r requirements.txt` may install newer
compatible releases under the same licences.

## Python packages (direct dependencies)

| Package | Version | Licence | Project |
|---|---|---|---|
| Django | 6.1 | BSD-3-Clause | https://www.djangoproject.com/ |
| djangorestframework | 3.18 | BSD-3-Clause | https://www.django-rest-framework.org/ |
| drf-spectacular | 0.30 | BSD-3-Clause | https://github.com/tfranzel/drf-spectacular |
| drf-spectacular-sidecar (bundles Swagger UI, Apache-2.0, and ReDoc, MIT) | 2026.10 | BSD-3-Clause | https://github.com/tfranzel/drf-spectacular-sidecar |
| whitenoise | 6.12 | MIT | https://whitenoise.readthedocs.io/ |
| python-dotenv | 1.2 | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| reportlab | 5.0 | BSD | https://www.reportlab.com/ |
| Pillow | 12.3 | MIT-CMU (HPND) | https://python-pillow.org/ |
| psycopg2-binary | 2.9 | LGPL-3.0 with exceptions | https://www.psycopg.org/ |
| dj-database-url | 3.1 | BSD-3-Clause | https://github.com/jazzband/dj-database-url |
| sentry-sdk | 2.71 | MIT | https://github.com/getsentry/sentry-python |
| stripe | 16.0 | MIT | https://github.com/stripe/stripe-python |
| django-storages | 1.14 | BSD-3-Clause | https://github.com/jschneier/django-storages |
| boto3 | 1.43 | Apache-2.0 | https://github.com/boto/boto3 |
| gunicorn | 26.2 | MIT | https://gunicorn.org/ |
| qrcode | 8.2 | BSD-3-Clause | https://github.com/lincolnloop/python-qrcode |
| PyJWT (with cryptography) | 2.14 | MIT | https://github.com/jpadilla/pyjwt |
| redis | 8.1 | MIT | https://github.com/redis/redis-py |
| openpyxl | 3.1 | MIT | https://openpyxl.readthedocs.io/ |

## Python packages (installed as dependencies of the above)

| Package | Licence |
|---|---|
| asgiref, sqlparse, pycparser, idna, python-dateutil, colorama, uritemplate | BSD (python-dateutil and uritemplate: BSD or Apache-2.0) |
| cryptography | Apache-2.0 or BSD-3-Clause |
| botocore, s3transfer, requests | Apache-2.0 |
| certifi | MPL-2.0 (unmodified certificate bundle) |
| cffi | MIT-0 |
| attrs, charset-normalizer, et_xmlfile, inflection, jmespath, jsonschema, jsonschema-specifications, PyYAML, referencing, rpds-py, six, urllib3 | MIT |
| typing_extensions | PSF-2.0 |

Notes:
* **psycopg2** (LGPL) is used as an unmodified, separately installed library,
  which the LGPL allows in proprietary software. If it is ever modified, the
  modified psycopg2 source must be made available.
* **certifi** (MPL-2.0) is used unmodified.
* Full licence texts ship with each package (`pip show -f <package>`, or the
  `*.dist-info/LICENSE*` files in the Python environment).

## Fonts

| Font | Licence | Where |
|---|---|---|
| Atkinson Hyperlegible (Braille Institute) | SIL Open Font License 1.1 | Storefront text, loaded from Google Fonts |
| Bricolage Grotesque | SIL Open Font License 1.1 | Headings, loaded from Google Fonts |

## Container images (Docker setup)

| Image | Licence |
|---|---|
| python:3.13-slim (Debian) | Python: PSF-2.0; Debian packages: their own free licences |
| postgres:16-alpine | PostgreSQL Licence (permissive) |
| valkey/valkey:8-alpine (Redis-compatible cache) | BSD-3-Clause |

## Other content

* **Demo product pictures** are drawn by the platform itself
  (`bees/product_art.py`, with Pillow) - no stock photos, nothing to license.
* **Icons** are inline SVG drawn for the project.
* **Outside services** (Stripe, PayPal, Google, Apple, Twilio, EasyPost,
  Shippo, Anthropic, Sentry, Supabase) are used through their public APIs under
  each provider's terms of service with the store owner's own account. No
  provider code is bundled beyond the client libraries listed above.
