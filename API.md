# Lumen Market REST API (v1)

A JSON API for mobile apps, partner integrations and automation. It uses the
same rules as the website: product review, plan limits, fulfilment checks and
customer emails all behave exactly the same whether a change comes from the
site or from the API.

| | |
|---|---|
| Base URL | `https://YOUR-DOMAIN/api/v1/` |
| Interactive docs (try every call) | `https://YOUR-DOMAIN/api/v1/docs/` |
| OpenAPI 3 schema (for code generators, Postman, Insomnia) | `https://YOUR-DOMAIN/api/v1/schema/` |
| Format | JSON in and out (`Content-Type: application/json`) |
| Money | Strings with two decimals in the store currency, e.g. `"19.99"` |
| Dates | ISO 8601 with time zone |
| Lists | Paginated: `?page=2&page_size=50` (24 by default, 100 max). Response: `count`, `next`, `previous`, `results` |

## Sign in

```http
POST /api/v1/auth/token/
{"username": "sara@example.com", "password": "...", "name": "iPhone app"}
```

```json
{"token": "lm_7Qe...", "token_type": "Bearer", "expires_at": "2027-01-11T10:00:00+00:00",
 "user": {"id": 12, "username": "sara"}}
```

Send the token with every request:

```http
Authorization: Bearer lm_7Qe...
```

* `username` can be the username or the email address.
* Accounts with **two-step sign-in** get `400 {"code_required": true}` first;
  send the request again with `"code": "123456"` (authenticator app) or a backup code.
* Tokens last 90 days (`API_TOKEN_DAYS` setting). Only a SHA-256 hash is
  stored, so a token is shown once and can't be recovered, only replaced.
* Sign out: `POST /api/v1/auth/logout/` (or `DELETE`) revokes the token used.
* Customers see and revoke their app sign-ins under **Account > Password &
  security > Apps & API access**. Changing the password revokes all tokens.
* Sign-in is limited to 10 attempts per minute per address.

## Endpoints

### Catalog (public, no sign-in)

| Method | Path | What it does |
|---|---|---|
| GET | `/api/v1/` | Index with links to everything |
| GET | `/categories/` | Categories (with `parent` for subcategories) and the number of products for sale. `?category=` on `/products/` includes subcategories |
| GET | `/products/` | Products for sale. Filters below |
| GET | `/products/{id}/` | One product: photos, sizes/colours, buying guide, seller |
| GET | `/products/{id}/reviews/` | Reviews, newest first |
| GET | `/sellers/{id}/` | A seller's public store profile |

`/products/` filters: `search` (understands "red hoodie under 40"; when nothing
matches, a spelling correction is tried and returned as `corrected`),
`category`, `seller_id`, `min_price`, `max_price`, `min_rating`, `in_stock=1`,
`deals=1`, `sort` = `price_asc` | `price_desc` | `newest` | `rating`.

### Account (signed-in customer)

| Method | Path | What it does |
|---|---|---|
| GET | `/me/` | Profile, store credit, reward points, seller role if any |
| GET | `/orders/` | Your orders, newest first |
| GET | `/orders/{id}/` | One order: items, totals, delivery address, tracking, timeline, invoice link |
| GET | `/wishlist/` | Saved products |
| POST | `/wishlist/` | Save a product: `{"product_id": 42}` |
| DELETE | `/wishlist/{product_id}/` | Remove it |

Checkout and payment happen on the website's secure checkout page (Stripe,
PayPal or cash on delivery), so card details never pass through an app. An
app opens the product's `url` or the cart page in a browser view for that step.

### Seller Center (seller owners and team members)

| Method | Path | What it does |
|---|---|---|
| GET | `/seller/` | Your seller account. Money figures only for the owner and team admins |
| GET | `/seller/products/` | Your products. `status` = `live`, `review`, `rejected`, `low`, `out`; `search` |
| POST | `/seller/products/` | Add a product (goes to the store's review first) |
| GET | `/seller/products/{id}/` | One of your products |
| PATCH | `/seller/products/{id}/` | Change it (rules below) |
| GET | `/seller/orders/` | Orders with your items. `status` = `to_pack`, `packed`, `in_transit`, `delivered`, `unpaid`, `cancelled` |
| GET | `/seller/orders/{id}/` | One order: your items, your sale and earnings, delivery address, whether it can ship |
| POST | `/seller/orders/{id}/fulfil/` | Mark your items packed / handed to the courier / delivered |

Product fields: `name`, `category`, `price`, `old_price`, `stock`,
`description`, `image_url` (direct https link), `weight_grams`, `seo_title`,
`seo_description`. A new product needs `name`, `category`, `price` and
`image_url`.

Rules (same as the Seller Center and bulk import):
* New products, and changes to name, category, photo or description, go to
  the store's review before customers see them. Price and stock changes are
  live immediately.
* Your plan's product limit applies.
* Only approved sellers can add or change products and ship orders.
* For products with sizes/colours, stock is managed per option (website or
  bulk import).

Fulfil body:

```json
{"status": "handed_to_courier", "courier_name": "DHL", "tracking_number": "JD014600006281234"}
```

`status` = `packed` | `handed_to_courier` | `delivered`. Items only move
forward. Cancelled, unpaid, refunded and unconfirmed cash orders are refused
with a reason. The customer gets the same email and notification as from the
website; the response has `customer_notified: true`.

## Errors

| Status | Meaning |
|---|---|
| 400 | Something in the request is wrong. The body names the field: `{"price": ["..."]}` or `{"detail": "..."}` |
| 401 | Missing, wrong or expired token |
| 403 | Signed in, but not allowed (e.g. not a seller, or seller not approved yet) |
| 404 | Not found, or not yours |
| 429 | Too many requests. Wait and retry (`Retry-After` header) |

## Limits

120 requests/minute without a token, 600/minute with one (settings
`API_RATE_ANON`, `API_RATE_USER`). Behind a proxy, set `NUM_PROXIES` so limits
apply per visitor, not per proxy.

## Quick test with curl

```bash
BASE=https://YOUR-DOMAIN/api/v1
curl "$BASE/products/?search=serum&sort=price_asc"
TOKEN=$(curl -s -X POST "$BASE/auth/token/" -H "Content-Type: application/json" \
  -d '{"username":"you@example.com","password":"..."}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
curl -H "Authorization: Bearer $TOKEN" "$BASE/me/"
curl -H "Authorization: Bearer $TOKEN" "$BASE/seller/orders/?status=to_pack"
```

## For developers extending the API

Code: `bees/api/` (`views.py`, `serializers.py`, `auth.py`, `urls.py`). Tests:
`ApiTests` in `bees/tests.py`. New endpoints appear in `/api/v1/docs/`
automatically; describe them with `@extend_schema` from drf-spectacular. Keep
business rules in the shared modules (`bulk.py`, `fulfilment.py`, ...) so the
website and the API never disagree.
