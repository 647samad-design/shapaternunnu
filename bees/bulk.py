"""Bulk products: spreadsheet (CSV / Excel) import with preview, export,
template download and bulk price/stock changes.

Used by Seller Center (a seller's own products) and the store admin (store
products; can also update any seller's product by id).

Rules match the normal product form:
* a row with an ``id`` updates that product, a row without one creates a new
  product;
* new seller products, and changes to the name / category / photo /
  description, go to review before they show in the shop; price and stock
  changes are live straight away;
* products with sizes/colours keep their per-option stock (the stock column
  is ignored for them).
"""
import csv
import io
import json
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from django.http import HttpResponse

MAX_ROWS = 2000
MAX_BYTES = 5 * 1024 * 1024
COLUMNS = ["id", "name", "category", "price", "old_price", "stock", "description", "image_url",
           "weight_grams", "seo_title", "seo_description"]
EXPORT_ONLY = ["status", "has_options", "link"]
HELP = {
    "id": "Leave empty to add a new product. Keep it to update an existing one.",
    "name": "Required for new products.",
    "category": "One of the category codes (see the 'categories' sheet / list).",
    "price": "Required for new products. Numbers only, e.g. 19.99",
    "old_price": "Optional 'was' price, higher than price.",
    "stock": "Whole number. Ignored for products with sizes/colours.",
    "description": "Optional.",
    "image_url": "Direct https:// link to the main photo. Required for new products.",
    "weight_grams": "Optional, for courier rates.",
    "seo_title": "Optional (max 70).",
    "seo_description": "Optional (max 160).",
}
REVIEWED = ("name", "category", "image_url", "description")
OPTION_COLUMNS = ["product", "size", "color", "price", "stock", "sku"]
OPTION_HELP = {
    "product": "The product's id, or its exact name (also for a new product in the products sheet).",
    "size": "e.g. S, M, L or 42. Leave empty if only colours differ.",
    "color": "e.g. Red. Leave empty if only sizes differ.",
    "price": "Optional - empty uses the product price.",
    "stock": "Whole number for this size/colour.",
    "sku": "Optional stock code.",
}


class BulkError(Exception):
    pass


# ---------------------------------------------------------------------------
# Reading files
# ---------------------------------------------------------------------------

def read_file(upload):
    """(product rows, option rows) from a CSV or XLSX upload. An Excel file
    may have a "products" sheet and an "options" sheet (sizes/colours); a
    CSV is either a products file or an options file (it has a "product"
    column and size/color columns)."""
    if upload.size > MAX_BYTES:
        raise BulkError("That file is too big (5 MB max). Split it into smaller files.")
    name = (upload.name or "").lower()
    raw = upload.read()
    if name.endswith((".xlsx", ".xlsm")) or raw[:2] == b"PK":
        products, options = _read_xlsx(raw, with_options=True)
    elif name.endswith((".csv", ".txt")) or not name:
        products, options = _read_csv(raw), []
        if products and "product" in products[0] and ("size" in products[0] or "color" in products[0]):
            products, options = [], products
    else:
        raise BulkError("Upload a .csv or .xlsx file (Excel: File > Save as > CSV or Excel Workbook).")
    if not products and not options:
        raise BulkError("The file has no product rows.")
    if len(products) > MAX_ROWS or len(options) > MAX_ROWS * 5:
        raise BulkError(f"Up to {MAX_ROWS} products per file, please.")
    return products, options


def read_rows(upload):
    """Returns a list of dicts (lower-case column names) from a CSV or XLSX upload."""
    if upload.size > MAX_BYTES:
        raise BulkError("That file is too big (5 MB max). Split it into smaller files.")
    name = (upload.name or "").lower()
    raw = upload.read()
    if name.endswith((".xlsx", ".xlsm")) or raw[:2] == b"PK":
        rows = _read_xlsx(raw)
    elif name.endswith((".csv", ".txt")) or not name:
        rows = _read_csv(raw)
    else:
        raise BulkError("Upload a .csv or .xlsx file (Excel: File > Save as > CSV or Excel Workbook).")
    if not rows:
        raise BulkError("The file has no product rows.")
    if len(rows) > MAX_ROWS:
        raise BulkError(f"Up to {MAX_ROWS} rows per file, please. This one has {len(rows)}.")
    return rows


def _read_csv(raw):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    return _to_dicts(list(reader))


def _read_xlsx(raw, with_options=False):
    try:
        from openpyxl import load_workbook
    except ImportError:  # pragma: no cover
        raise BulkError("Excel files need the openpyxl package on the server. Upload a CSV instead.") from None
    try:
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception:
        raise BulkError("We couldn't open that Excel file. Save it again as .xlsx or CSV and retry.") from None
    def sheet(ws, limit):
        table = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i > limit + 1:
                table.append(["x"])  # enough to trigger the size message
                break
            table.append(["" if v is None else (format(v, "f").rstrip("0").rstrip(".") if isinstance(v, float) else str(v)) for v in row])
        return table
    ws = wb["products"] if "products" in wb.sheetnames else wb.worksheets[0]
    products = _to_dicts(sheet(ws, MAX_ROWS))
    options = []
    if with_options and "options" in wb.sheetnames:
        options = _to_dicts(sheet(wb["options"], MAX_ROWS * 5), need=("product",))
    wb.close()
    return (products, options) if with_options else products


def _to_dicts(table, need=None):
    table = [r for r in table if any(str(c).strip() for c in r)]
    if not table:
        return []
    header = [str(h).strip().lower().replace(" ", "_").replace("colour", "color") for h in table[0]]
    if need and not all(n in header for n in need):
        raise BulkError("The options sheet needs a 'product' column (product id or name), plus size, color and stock.")
    if not need and "name" not in header and "id" not in header and "product" not in header:
        raise BulkError("The first row must be the column names (id, name, category, price, stock, ...). Download the template to see them.")
    rows = []
    for r in table[1:]:
        rows.append({header[i]: str(r[i]).strip() for i in range(min(len(header), len(r))) if header[i]})
    return rows


# ---------------------------------------------------------------------------
# Checking rows
# ---------------------------------------------------------------------------

def _dec(value, label, minimum=None):
    value = (value or "").replace(",", "").strip()
    if not value:
        return None
    try:
        out = Decimal(value).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise BulkError(f"{label} must be a number")
    if minimum is not None and out < minimum:
        raise BulkError(f"{label} must be at least {minimum}")
    if out > Decimal("99999999"):
        raise BulkError(f"{label} is too large")
    return out


def _int(value, label, maximum=1_000_000):
    value = (value or "").replace(",", "").strip()
    if not value:
        return None
    try:
        out = int(Decimal(value))
    except (InvalidOperation, ValueError):
        raise BulkError(f"{label} must be a whole number")
    if out < 0 or out > maximum:
        raise BulkError(f"{label} must be between 0 and {maximum:,}")
    return out


def _category(value):
    from .models import Product
    value = (value or "").strip()
    if not value:
        return None
    choices = dict(Product.CATEGORY_CHOICES)
    if value in choices:
        return value
    by_label = {label.lower(): code for code, label in Product.CATEGORY_CHOICES}
    if value.lower() in by_label:
        return by_label[value.lower()]
    raise BulkError(f"unknown category '{value[:30]}'")


def _clean(row):
    """Returns only the fields present in the row, validated."""
    from .views import is_image_link
    out = {}
    if row.get("name", "") != "":
        out["name"] = row["name"][:255]
    cat = _category(row.get("category"))
    if cat:
        out["category"] = cat
    for key, label, minimum in (("price", "Price", Decimal("0.01")), ("old_price", "Old price", Decimal("0"))):
        if key in row:
            value = _dec(row.get(key), label, minimum)
            if value is not None or key == "old_price":
                out[key] = value
    if row.get("stock", "") != "":
        out["stock"] = _int(row["stock"], "Stock")
    if "description" in row and row["description"] != "":
        out["description"] = row["description"][:10000]
    if row.get("image_url"):
        if not is_image_link(row["image_url"]):
            raise BulkError("image_url must be a direct https:// link")
        out["image_url"] = row["image_url"][:500]
    if row.get("weight_grams"):
        out["weight_grams"] = _int(row["weight_grams"], "Weight", maximum=100000)
    if row.get("seo_title"):
        out["seo_title"] = row["seo_title"][:70]
    if row.get("seo_description"):
        out["seo_description"] = " ".join(row["seo_description"].split())[:170]
    return out


def plan(rows, products, seller=None, store=False):
    """Works out what each row would do without saving anything.

    ``products`` is the queryset the user may update. Returns a dict with
    ``rows`` (each: line, action create/update/skip/error, name, changes,
    errors, review) and counts."""
    ids = set()
    for row in rows:
        try:
            if row.get("id"):
                ids.add(int(row["id"]))
        except ValueError:
            pass
    existing = {p.id: p for p in products.filter(id__in=ids)}
    room = None
    if seller is not None and seller.product_limit is not None:
        room = max(seller.product_limit - seller.products.count(), 0)
    out = []
    seen = set()
    for n, row in enumerate(rows, start=2):  # line 1 = headings
        entry = {"line": n, "action": "skip", "name": row.get("name", "")[:80], "changes": [], "errors": [], "review": False, "data": {}}
        try:
            data = _clean(row)
            if row.get("id"):
                try:
                    pid = int(row["id"])
                except ValueError:
                    raise BulkError("id must be a number (or empty for a new product)")
                if pid in seen:
                    raise BulkError(f"product {pid} appears twice")
                seen.add(pid)
                product = existing.get(pid)
                if product is None:
                    raise BulkError(f"product {pid} isn't yours or doesn't exist")
                entry["id"], entry["name"] = pid, product.name[:80]
                if product.has_variants and "stock" in data:
                    data.pop("stock")
                    entry["note"] = "stock is set per size/colour"
                if data.get("old_price") is not None and data["old_price"] <= data.get("price", product.price):
                    data["old_price"] = None
                changes = {k: v for k, v in data.items() if getattr(product, k) != v}
                if not changes:
                    entry["action"] = "same"
                else:
                    entry["action"] = "update"
                    entry["changes"] = [f"{k.replace('_', ' ')}: {_show(getattr(product, k))} → {_show(v)}" for k, v in changes.items()]
                    entry["review"] = not store and any(k in REVIEWED for k in changes)
                    entry["data"] = changes
            else:
                missing = [k for k in ("name", "category", "price", "image_url") if not data.get(k)]
                if missing:
                    raise BulkError("new products need " + ", ".join(missing))
                if room is not None:
                    if room <= 0:
                        raise BulkError("your plan's product limit is reached")
                    room -= 1
                if data.get("old_price") is not None and data["old_price"] <= data["price"]:
                    data["old_price"] = None
                data.setdefault("stock", 10)
                entry["action"] = "create"
                entry["review"] = not store
                entry["data"] = data
                entry["changes"] = [f"new · {_show(data['price'])} · stock {data['stock']}"]
        except BulkError as exc:
            entry["action"] = "error"
            entry["errors"].append(str(exc))
        out.append(entry)
    counts = {k: sum(1 for e in out if e["action"] == k) for k in ("create", "update", "same", "error")}
    return {"rows": out, "counts": counts}


def _show(value):
    if value is None or value == "":
        return "—"
    text = str(value)
    return text if len(text) <= 40 else text[:37] + "…"


def plan_options(rows, products, product_plan=None):
    """Sizes/colours per product. Each product's options in the file replace
    its current ones (matched by size + colour, so order history stays)."""
    new_names = {}
    for e in (product_plan or {}).get("rows", []):
        if e["action"] == "create":
            new_names[e["data"]["name"].strip().lower()] = e["data"]["name"]
    groups, order = {}, []
    errors = []
    for n, row in enumerate(rows, start=2):
        ref = (row.get("product") or "").strip()
        try:
            if not ref:
                raise BulkError("product is empty")
            key = None
            if ref.isdigit():
                product = products.filter(pk=int(ref)).first()
                if product is None:
                    raise BulkError(f"product {ref} isn't yours or doesn't exist")
                key = ("id", product.id, product.name)
            elif ref.lower() in new_names:
                key = ("new", new_names[ref.lower()], new_names[ref.lower()])
            else:
                matches = list(products.filter(name__iexact=ref)[:2])
                if len(matches) != 1:
                    raise BulkError(f"no single product called '{ref[:40]}'" if not matches else f"several products are called '{ref[:40]}' - use the id")
                key = ("id", matches[0].id, matches[0].name)
            size, color = (row.get("size") or "").strip()[:40], (row.get("color") or "").strip()[:40]
            if not size and not color:
                raise BulkError("add a size or a colour")
            stock = _int(row.get("stock"), "Stock")
            if stock is None:
                raise BulkError("stock is empty")
            price = _dec(row.get("price"), "Price", Decimal("0.01"))
            option = {"size": size, "color": color, "stock": stock, "price": str(price) if price is not None else None,
                      "sku": (row.get("sku") or "").strip()[:60]}
        except BulkError as exc:
            errors.append({"line": n, "action": "error", "name": ref[:80], "changes": [], "errors": [str(exc)], "review": False, "data": {}})
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        if any(o["size"].lower() == size.lower() and o["color"].lower() == color.lower() for o in groups[key]):
            errors.append({"line": n, "action": "error", "name": ref[:80], "changes": [], "errors": [f"{size or ''} {color or ''} is listed twice"], "review": False, "data": {}})
            continue
        groups[key].append(option)
    out = []
    for key in order:
        kind, ident, name = key
        opts = groups[key]
        label = ", ".join(" ".join(x for x in (o["size"], o["color"]) if x) for o in opts[:6]) + (f" +{len(opts) - 6}" if len(opts) > 6 else "")
        out.append({"line": "", "action": "options", "name": name[:80], "id": ident if kind == "id" else None,
                    "new_name": ident if kind == "new" else None, "errors": [], "review": False,
                    "changes": [f"{len(opts)} size/colour option{'s' if len(opts) != 1 else ''}: {label} · stock {sum(o['stock'] for o in opts)}"],
                    "data": {"options": opts}})
    return out + errors


def apply_options(product, options):
    from .models import ProductVariant
    existing = {(v.size.lower(), v.color.lower()): v for v in product.variants.all()}
    keep = set()
    for position, o in enumerate(options):
        v = existing.get((o["size"].lower(), o["color"].lower())) or ProductVariant(product=product)
        v.size, v.color, v.stock, v.sku, v.position = o["size"], o["color"], o["stock"], o["sku"], position
        v.price = Decimal(o["price"]) if o.get("price") else None
        v.save()
        keep.add(v.id)
    ProductVariant.objects.filter(product=product).exclude(id__in=keep).delete()
    product.sync_variants()


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def stash(user, result):
    """Keeps a checked import for 30 minutes so the user can confirm it."""
    key = f"bulk:{user.pk}"
    payload = [{"id": e.get("id"), "action": e["action"], "review": e["review"],
                "data": {k: str(v) if isinstance(v, Decimal) else v for k, v in e["data"].items()}}
               for e in result["rows"] if e["action"] in ("create", "update")]
    payload += [{"id": e.get("id"), "new_name": e.get("new_name"), "action": "options", "review": False, "data": e["data"]}
                for e in result.get("options", []) if e["action"] == "options"]
    cache.set(key, json.dumps(payload), 1800)


def unstash(user):
    key = f"bulk:{user.pk}"
    raw = cache.get(key)
    cache.delete(key)
    return json.loads(raw) if raw else None


def apply(entries, products, seller=None, store=False, user=None):
    """Saves a stashed plan. Returns (created, updated, sent_to_review)."""
    from django.db import transaction
    from .models import AuditLog, Product
    created = updated = review = 0
    decimals = ("price", "old_price")
    made = {}
    with transaction.atomic():
        for e in entries:
            if e["action"] == "options":
                continue
            data = {k: (Decimal(v) if k in decimals and v is not None else v) for k, v in e["data"].items()}
            if e["action"] == "create":
                if seller is not None and not seller.can_add_product:
                    break
                made[data["name"].strip().lower()] = Product.objects.create(
                    **data,
                    seller_account=seller,
                    seller_name=seller.display_name if seller else "Official Store",
                    approval_status="approved" if store else "pending",
                )
                created += 1
                review += int(not store)
                continue
            product = products.select_for_update().filter(pk=e["id"]).first()
            if product is None:
                continue
            for k, v in data.items():
                setattr(product, k, v)
            if product.old_price is not None and product.old_price <= product.price:
                product.old_price = None
            if not store and any(k in REVIEWED for k in data) and product.approval_status not in ("pending", "archived"):
                product.approval_status = "pending"
                review += 1
            product.save()  # back-in-stock / price-drop signals run as usual
            updated += 1
        with_options = 0
        for e in entries:
            if e["action"] != "options":
                continue
            product = made.get((e.get("new_name") or "").strip().lower()) if e.get("new_name") else products.filter(pk=e["id"]).first()
            if product is not None:
                apply_options(product, e["data"]["options"])
                with_options += 1
        if user is not None:
            AuditLog.objects.create(user=user, action=f"Bulk import: {created} added, {updated} updated, options for {with_options}"[:255])
    return created, updated, review


def bulk_change(products, mode, value, seller_category=""):
    """Price/stock change for many products at once. Returns how many changed.

    mode: 'price_pct' (+/- percent), 'price_add' (+/- amount), 'stock_set'."""
    from django.db import transaction
    changed = 0
    qs = products.exclude(approval_status="archived")
    if seller_category:
        qs = qs.filter(category=seller_category)
    with transaction.atomic():
        for p in qs.select_for_update():
            if mode == "price_pct":
                new = (p.price * (Decimal(100) + value) / Decimal(100)).quantize(Decimal("0.01"))
            elif mode == "price_add":
                new = (p.price + value).quantize(Decimal("0.01"))
            elif mode == "stock_set":
                if p.has_variants:
                    continue
                if p.stock != int(value):
                    p.stock = int(value)
                    p.save()
                    changed += 1
                continue
            else:
                raise BulkError("Unknown change.")
            new = max(new, Decimal("0.01"))
            if new != p.price:
                p.price = new
                if p.old_price is not None and p.old_price <= p.price:
                    p.old_price = None
                p.save()
                changed += 1
    return changed


# ---------------------------------------------------------------------------
# Export / template
# ---------------------------------------------------------------------------

def _rows_for(products, request=None):
    for p in products.order_by("id"):
        link = request.build_absolute_uri(f"/product/{p.id}/") if request else ""
        yield [p.id, p.name, p.category, p.price, p.old_price if p.old_price is not None else "", p.stock, p.description,
               p.image_url, p.weight_grams or "", p.seo_title, p.seo_description,
               p.get_approval_status_display(), "yes" if p.has_variants else "", link]


def _option_rows(products):
    from .models import ProductVariant
    for v in ProductVariant.objects.filter(product__in=products).select_related("product").order_by("product_id", "position", "id"):
        yield [v.product_id, v.size, v.color, v.price if v.price is not None else "", v.stock, v.sku]


def export(products, fmt, filename, request=None):
    header = COLUMNS + EXPORT_ONLY
    rows = list(_rows_for(products, request))
    if fmt == "xlsx":
        return _xlsx_response([header] + rows, filename, with_help=False, options=[OPTION_COLUMNS] + list(_option_rows(products)))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for r in rows:
        writer.writerow([_safe_cell(c) for c in r])
    resp = HttpResponse("﻿" + buf.getvalue(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
    return resp


def template(fmt):
    example = ["", "Ceramic mug 350ml", "grocery", "12.50", "15.00", "40", "Hand-glazed stoneware mug.",
               "https://images.example.com/mug.jpg", "450", "", ""]
    if fmt == "xlsx":
        return _xlsx_response([COLUMNS, example], "products-template", with_help=True,
                              options=[OPTION_COLUMNS, ["Ceramic mug 350ml", "", "White", "", "25", "MUG-W"], ["Ceramic mug 350ml", "", "Black", "13.50", "15", "MUG-B"]])
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(COLUMNS)
    writer.writerow(example)
    resp = HttpResponse("﻿" + buf.getvalue(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="products-template.csv"'
    return resp


def _safe_cell(value):
    """Stops spreadsheet formula injection (=, +, -, @ at the start)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _xlsx_response(table, filename, with_help, options=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from .models import Product
    wb = Workbook()
    ws = wb.active
    ws.title = "products"
    for row in table:
        ws.append([float(c) if isinstance(c, Decimal) else _safe_cell(c) for c in row])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    for col, width in zip("ABCDEFGHIJKLMN", (8, 34, 16, 10, 10, 8, 40, 40, 12, 24, 30, 14, 10, 30)):
        ws.column_dimensions[col].width = width
    if options:
        opt = wb.create_sheet("options")
        for row in options:
            opt.append([float(c) if isinstance(c, Decimal) else _safe_cell(c) for c in row])
        for cell in opt[1]:
            cell.font = Font(bold=True)
        opt.freeze_panes = "A2"
        for col, width in zip("ABCDEF", (30, 10, 14, 10, 8, 16)):
            opt.column_dimensions[col].width = width
    if with_help:
        info = wb.create_sheet("how to fill")
        info.append(["column", "what to put"])
        for key in COLUMNS:
            info.append([key, HELP[key]])
        info.append([])
        info.append(["options sheet", "Sizes / colours. One row per option; a product's options in the file replace its current ones."])
        for key in OPTION_COLUMNS:
            info.append([f"options: {key}", OPTION_HELP[key]])
        info.column_dimensions["A"].width = 18
        info.column_dimensions["B"].width = 80
    cats = wb.create_sheet("categories")
    cats.append(["category code", "name"])
    for code, label in Product.CATEGORY_CHOICES:
        cats.append([code, label])
    cats.column_dimensions["A"].width = 20
    cats.column_dimensions["B"].width = 34
    buf = io.BytesIO()
    wb.save(buf)
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{filename}.xlsx"'
    return resp
