"""AI shopping agents: Universal Commerce Protocol (UCP) and llms.txt.

UCP (ucp.dev, launched by Google with Shopify, Etsy, Walmart, Target and
others in January 2026) lets AI assistants such as Gemini and Google's AI
Mode discover a store and build a checkout for the shopper.

What this store supports:

* /.well-known/ucp - the business profile (services and capabilities)
* /ucp/v1/checkout-sessions - create, get, update, complete, cancel
  (the "dev.ucp.shopping.checkout" capability over REST)

Payment always happens on the store's own checkout: every session answers
with status "requires_escalation" and a continue_url. Opening that link
puts the agent's items in the shopper's cart and opens the normal checkout
(card, PayPal, cash on delivery, store credit...). No native payment handler
is offered, so no card data ever goes through an agent.

/llms.txt is a plain-text guide to the store for AI crawlers.
"""
import json
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .ratelimit import ratelimit
from .multistore import store_settings  # per-store values in multi-store mode

UCP_VERSION = "2026-04-08"
SPEC = f"https://ucp.dev/{UCP_VERSION}"
CAPABILITY = "dev.ucp.shopping.checkout"
MAX_LINES = 50
MAX_QTY = 99
LINK_DAYS = 7
ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "ISK", "UGX", "XAF", "XOF", "PYG", "RWF"}


def _enabled():
    from .models import SiteSettings
    if not SiteSettings.load().ai_agents:
        raise Http404


def _currency():
    return store_settings.STORE_CURRENCY.upper()


def minor(amount, currency=None):
    """Money as an integer in minor units (cents), as UCP expects."""
    exp = 0 if (currency or _currency()) in ZERO_DECIMAL else 2
    return int((Decimal(amount) * (10 ** exp)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _meta(status=None):
    data = {"version": UCP_VERSION, "capabilities": {CAPABILITY: [{"version": UCP_VERSION}]}}
    if status:
        data["status"] = status
    return data


def _error(code, content, http=400):
    return JsonResponse({"ucp": _meta("error"),
                         "messages": [{"type": "error", "code": code, "content": content, "severity": "recoverable"}]},
                        status=http)


# ---------------------------------------------------------------- profile

def profile(request):
    """GET /.well-known/ucp"""
    _enabled()
    endpoint = request.build_absolute_uri("/ucp/v1")
    body = {
        "ucp": {
            "version": UCP_VERSION,
            "services": {"dev.ucp.shopping": [{
                "version": UCP_VERSION, "spec": f"{SPEC}/specification/overview", "transport": "rest",
                "endpoint": endpoint, "schema": f"{SPEC}/services/shopping/rest.openapi.json"}]},
            "capabilities": {CAPABILITY: [{
                "version": UCP_VERSION, "spec": f"{SPEC}/specification/checkout",
                "schema": f"{SPEC}/schemas/shopping/checkout.json"}]},
            "payment_handlers": {},
        },
    }
    response = JsonResponse(body)
    response["Cache-Control"] = "public, max-age=3600"
    response["Access-Control-Allow-Origin"] = "*"
    return response


# ---------------------------------------------------------------- checkout

def _parse_lines(raw):
    """Agent line items -> {cart key: qty}. Raises ValueError."""
    from .cart import parse_key
    if not isinstance(raw, list) or not raw:
        raise ValueError("line_items must be a non-empty list.")
    if len(raw) > MAX_LINES:
        raise ValueError(f"At most {MAX_LINES} line items.")
    cart = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("Each line item must be an object.")
        item_id = str((entry.get("item") or {}).get("id", "")).strip()
        if not parse_key(item_id):
            raise ValueError(f"Unknown item id '{item_id[:40]}'. Use the product id from the store, or product-variant.")
        try:
            qty = int(entry.get("quantity", 1))
        except (TypeError, ValueError):
            raise ValueError("quantity must be a whole number.")
        if qty < 1 or qty > MAX_QTY:
            raise ValueError(f"quantity must be between 1 and {MAX_QTY}.")
        cart[item_id] = cart.get(item_id, 0) + qty
    return cart


def _priced(cart):
    """Prices the agent's items with the shop's own rules. Returns
    (rows, problems) - problems are messages about items left out or
    changed."""
    from .cart import lines
    rows, problems = [], []
    found = {r["key"]: r for r in lines(cart)}
    for key, qty in cart.items():
        row = found.get(key)
        if not row:
            problems.append({"type": "warning", "code": "item_unavailable", "path": f"$.line_items[?(@.item.id=='{key}')]",
                             "content": f"Item {key} isn't available (sold out, removed, or it needs a size/colour - use product-variant)."})
            continue
        product = row["product"]
        if product.is_auction or product.quote_only or row["stock"] <= 0:
            problems.append({"type": "warning", "code": "item_unavailable",
                             "content": f"{product.name} can't be bought this way."})
            continue
        low, high = product.order_limits()
        want = max(qty, low)
        if high:
            want = min(want, high)
        want = min(want, row["stock"])
        if want != qty:
            problems.append({"type": "warning", "code": "quantity_adjusted",
                             "content": f"{product.name}: quantity set to {want} (stock and order limits)."})
            from .cart import lines as reprice
            row = (reprice({key: want}) or [row])[0]
        rows.append(row)
    return rows, problems


def _session_body(request, session, rows=None, problems=None):
    from .models import SiteSettings
    if rows is None:
        rows, problems = _priced({l["id"]: l["quantity"] for l in session.lines})
    cur = session.currency
    items, subtotal = [], Decimal("0")
    for row in rows:
        p = row["product"]
        image = p.image_url if p.image_url.startswith("http") else request.build_absolute_uri(p.image_url)
        title = f"{p.name} ({row['label']})" if row["label"] else p.name
        items.append({
            "id": row["key"],
            "item": {"id": row["key"], "title": title[:200], "price": minor(row["unit_price"], cur), "image_url": image},
            "quantity": row["qty"],
            "totals": [{"type": "subtotal", "amount": minor(row["subtotal"], cur)}],
        })
        subtotal += row["subtotal"]
    msgs = list(problems or [])
    status = session.status
    if status == "requires_escalation":
        msgs.append({"type": "error", "code": "buyer_handoff", "severity": "requires_buyer_input",
                     "content": "The shopper chooses delivery and pays on the store's checkout. Shipping and tax are added there."})
    site = SiteSettings.load()
    body = {
        "ucp": _meta(),
        "id": str(session.id),
        "status": status,
        "currency": cur,
        "line_items": items,
        "totals": [{"type": "subtotal", "amount": minor(subtotal, cur)}, {"type": "total", "amount": minor(subtotal, cur)}],
        "messages": msgs,
        "links": [{"type": "terms_of_service", "url": request.build_absolute_uri("/terms/")},
                  {"type": "privacy_policy", "url": request.build_absolute_uri("/privacy/")}],
        "merchant": {"name": site.site_name},
    }
    if session.buyer:
        body["buyer"] = session.buyer
    if status == "requires_escalation":
        body["continue_url"] = request.build_absolute_uri(f"/agent-checkout/{session.id}/")
    return body


def _buyer(raw):
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, limit in (("email", 254), ("first_name", 60), ("last_name", 60), ("phone_number", 30)):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()[:limit]
    return out


def _read_json(request):
    try:
        return json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("The body must be JSON.")


def _agent(request):
    agent = request.headers.get("UCP-Agent", "").strip()
    return agent[:300]


@csrf_exempt
@ratelimit("ucp", rate_limit=60, window_seconds=60, methods=("GET", "POST", "PUT"))
def checkout_sessions(request):
    """POST /ucp/v1/checkout-sessions"""
    from .models import AgentCheckout
    _enabled()
    if request.method != "POST":
        return _error("method_not_allowed", "Use POST to create a checkout.", 405)
    agent = _agent(request)
    if not agent:
        return _error("missing_ucp_agent", "The UCP-Agent header is required.")
    key = request.headers.get("Idempotency-Key", "").strip()[:100]
    if key:
        earlier = AgentCheckout.objects.filter(idempotency_key=key, agent=agent,
                                               created_at__gte=timezone.now() - timedelta(hours=24)).first()
        if earlier:
            return JsonResponse(_session_body(request, earlier), status=201)
    try:
        data = _read_json(request)
        cart = _parse_lines(data.get("line_items"))
    except ValueError as exc:
        return _error("invalid_request", str(exc))
    rows, problems = _priced(cart)
    if not rows:
        # A business outcome, not a protocol error: nothing to sell.
        return JsonResponse({"ucp": _meta("success"), "messages": problems or [
            {"type": "error", "code": "out_of_stock", "severity": "recoverable", "content": "None of these items can be bought."}]})
    session = AgentCheckout.objects.create(
        lines=[{"id": r["key"], "quantity": r["qty"]} for r in rows], buyer=_buyer(data.get("buyer")),
        currency=_currency(), agent=agent, idempotency_key=key)
    return JsonResponse(_session_body(request, session, rows, problems), status=201)


def _get_session(pk):
    from .models import AgentCheckout
    return get_object_or_404(AgentCheckout, pk=pk, created_at__gte=timezone.now() - timedelta(days=LINK_DAYS))


@csrf_exempt
@ratelimit("ucp", rate_limit=60, window_seconds=60, methods=("GET", "POST", "PUT"))
def checkout_session(request, pk):
    """GET / PUT /ucp/v1/checkout-sessions/{id}"""
    _enabled()
    if not _agent(request):
        return _error("missing_ucp_agent", "The UCP-Agent header is required.")
    session = _get_session(pk)
    if request.method == "GET":
        return JsonResponse(_session_body(request, session))
    if request.method != "PUT":
        return _error("method_not_allowed", "Use GET or PUT.", 405)
    if session.status != "requires_escalation":
        return _error("not_editable", f"This checkout is {session.status} and can't be changed.", 409)
    try:
        data = _read_json(request)
        if "line_items" in data:
            cart = _parse_lines(data["line_items"])
            rows, problems = _priced(cart)
            if not rows:
                return _error("out_of_stock", "None of these items can be bought.", 422)
            session.lines = [{"id": r["key"], "quantity": r["qty"]} for r in rows]
        else:
            rows = problems = None
        if "buyer" in data:
            session.buyer = _buyer(data["buyer"])
    except ValueError as exc:
        return _error("invalid_request", str(exc))
    session.save()
    return JsonResponse(_session_body(request, session, rows, problems))


@csrf_exempt
@ratelimit("ucp", rate_limit=60, window_seconds=60, methods=("GET", "POST", "PUT"))
def checkout_action(request, pk, action):
    """POST /ucp/v1/checkout-sessions/{id}/complete | /cancel"""
    _enabled()
    if request.method != "POST":
        return _error("method_not_allowed", "Use POST.", 405)
    if not _agent(request):
        return _error("missing_ucp_agent", "The UCP-Agent header is required.")
    session = _get_session(pk)
    if action == "cancel":
        if session.status == "requires_escalation":
            session.status = "canceled"
            session.save(update_fields=["status", "updated_at"])
        return JsonResponse(_session_body(request, session))
    if action == "complete":
        # No payment handler is offered: the shopper always pays on the
        # store's checkout, so completing hands them over.
        if session.status == "canceled":
            return _error("not_editable", "This checkout was canceled.", 409)
        return JsonResponse(_session_body(request, session))
    raise Http404


def continue_checkout(request, pk):
    """The shopper opens the agent's link: its items go in their cart and
    the normal checkout opens."""
    _enabled()
    session = _get_session(pk)
    if session.status == "canceled":
        messages.error(request, "This checkout link was canceled. Your cart is unchanged.")
        return redirect("cart")
    cart = request.session.get("cart", {}) or {}
    added = 0
    for line in session.lines:
        key, qty = str(line.get("id")), int(line.get("quantity") or 1)
        cart[key] = max(cart.get(key, 0), qty)
        added += 1
    request.session["cart"] = cart
    if session.buyer.get("email"):
        request.session["agent_email"] = session.buyer["email"]
    request.session.modified = True
    from .views import _persist_cart
    _persist_cart(request)
    if not session.opened_at:
        session.opened_at = timezone.now()
        session.save(update_fields=["opened_at", "updated_at"])
    messages.success(request, f"Your shopping assistant added {added} item{'s' if added != 1 else ''} to your cart. Check them and choose how to pay.")
    return redirect("checkout")


# ---------------------------------------------------------------- llms.txt

def llms_txt(request):
    """A short guide to the store for AI assistants (llmstxt.org)."""
    from .categories import top_level
    from .models import SiteSettings
    site = SiteSettings.load()
    url = request.build_absolute_uri
    lines = [f"# {site.site_name}", "", f"> {site.tagline}", "",
             f"{site.site_name} is an online marketplace with products from independent sellers and brands. "
             f"Prices are in {_currency()}. Shoppers pay by card, PayPal, wallets or other methods on the store's checkout.", "",
             "## Shop", f"- [All products]({url('/products/')})", f"- [Today's deals]({url('/deals/')})"]
    for slug, name in top_level()[:12]:
        lines.append(f"- [{name}]({url('/category/' + slug + '/')})")
    lines += ["", "## For AI agents and apps",
              f"- [Product feed (Google format)]({url('/feeds/google.xml')}): every product with price, stock and barcode",
              f"- [Product feed (CSV)]({url('/feeds/products.csv')})"]
    if site.ai_agents:
        lines.append(f"- [Universal Commerce Protocol profile]({url('/.well-known/ucp')}): build a checkout for a shopper")
    lines += [f"- [REST API docs]({url('/api/v1/docs/')})", "", "## Policies",
              f"- [Shipping]({url('/pages/shipping-policy/')})", f"- [Returns: {site.return_days} days]({url('/help/')})",
              f"- [Buyer protection]({url('/protection/')})", f"- [Terms]({url('/terms/')})", f"- [Privacy]({url('/privacy/')})", ""]
    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")
