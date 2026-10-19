"""Store admin: categories, information pages and home-page banners."""
from datetime import datetime

from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.text import slugify

from .manage_views import _log, _render, staff_required
from .models import Category, HomeBanner, Page, Product, Promotion


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

def _category_fields(request, instance=None):
    """Clean values from the category form, or raises ValueError."""
    name = request.POST.get("name", "").strip()[:60]
    if not name:
        raise ValueError("Give the category a name.")
    parent = None
    parent_id = request.POST.get("parent", "")
    if parent_id:
        parent = Category.objects.filter(pk=parent_id if parent_id.isdigit() else 0).first()
        if parent is None:
            raise ValueError("Choose a valid parent category.")
        if instance is not None and (parent.pk == instance.pk or parent.parent_id == instance.pk):
            raise ValueError("A category can't be inside itself.")
        if parent.parent_id:
            raise ValueError("Subcategories can only be one level deep - choose a top-level parent.")
        if instance is not None and instance.children.exists():
            raise ValueError(f"'{instance.name}' has its own subcategories, so it has to stay top-level.")
    try:
        position = max(0, min(int(request.POST.get("position") or 0), 9999))
    except ValueError:
        position = 0
    image = request.POST.get("image_url", "").strip()[:500]
    if image and not (image.startswith("https://") or image.startswith("http://") or image.startswith("/")):
        raise ValueError("The picture must be a web address (https://...).")
    return {"name": name, "parent": parent, "position": position, "image_url": image,
            "description": request.POST.get("description", "").strip()[:300],
            "size_chart": request.POST.get("size_chart", "").strip()[:4000],
            "is_active": bool(request.POST.get("is_active"))}


@staff_required
def categories(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "add":
                fields = _category_fields(request)
                slug = slugify(request.POST.get("slug", "").strip() or fields["name"])[:30].strip("-")
                if not slug:
                    raise ValueError("Use letters or numbers in the address.")
                if Category.objects.filter(slug=slug).exists():
                    raise ValueError(f"A category with the address '{slug}' already exists.")
                cat = Category.objects.create(slug=slug, **fields)
                _log(request, f"Added category {cat.name} ({slug})")
                messages.success(request, f"'{cat.name}' added.")
            elif action == "save":
                cat = get_object_or_404(Category, pk=request.POST.get("id"))
                for key, value in _category_fields(request, cat).items():
                    setattr(cat, key, value)
                cat.save()
                _log(request, f"Edited category {cat.name}")
                messages.success(request, f"'{cat.name}' saved.")
            elif action == "delete":
                cat = get_object_or_404(Category, pk=request.POST.get("id"))
                count = Product.objects.filter(category=cat.slug).count()
                target = None
                if count:
                    target = Category.objects.filter(slug=request.POST.get("move_to", "")).exclude(pk=cat.pk).first()
                    if target is None:
                        raise ValueError(f"'{cat.name}' has {count} products - choose where to move them first.")
                with transaction.atomic():
                    if target is not None:
                        Product.objects.filter(category=cat.slug).update(category=target.slug)
                        Promotion.objects.filter(category=cat.slug).update(category=target.slug)
                    cat.children.update(parent=cat.parent)
                    cat.delete()
                _log(request, f"Deleted category {cat.name}" + (f", moved {count} products to {target.name}" if target else ""))
                messages.success(request, f"'{cat.name}' deleted." + (f" Its {count} products are now in '{target.name}'." if target else ""))
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("manage_categories")
    counts = dict(Product.objects.exclude(approval_status="archived").values_list("category").annotate(n=Count("id")))
    all_cats = list(Category.objects.all())
    tops = [c for c in all_cats if not c.parent_id or c.parent_id not in {x.pk for x in all_cats}]
    rows = []
    for top in sorted(tops, key=lambda c: (c.position, c.name.lower())):
        kids = sorted([c for c in all_cats if c.parent_id == top.pk], key=lambda c: (c.position, c.name.lower()))
        top.n = counts.get(top.slug, 0)
        top.total = top.n + sum(counts.get(k.slug, 0) for k in kids)
        rows.append((top, 0))
        for kid in kids:
            kid.n = kid.total = counts.get(kid.slug, 0)
            rows.append((kid, 1))
    return _render(request, "categories.html", {
        "section": "categories", "rows": rows, "tops": [c for c, depth in rows if depth == 0],
        "everything": [(c.slug, c.name) for c, _d in rows]})


# ---------------------------------------------------------------------------
# Pages & banners
# ---------------------------------------------------------------------------

def _when(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return timezone.make_aware(datetime.fromisoformat(value))
    except ValueError:
        raise ValueError("Use a valid date and time.")


@staff_required
def pages(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action in ("add_banner", "save_banner"):
                banner = HomeBanner() if action == "add_banner" else get_object_or_404(HomeBanner, pk=request.POST.get("id"))
                title = request.POST.get("title", "").strip()[:80]
                link = request.POST.get("link", "").strip()[:300]
                image = request.POST.get("image_url", "").strip()[:500]
                if not title or not link:
                    raise ValueError("A banner needs a title and a link.")
                if not (link.startswith("/") or link.startswith("https://") or link.startswith("http://")):
                    raise ValueError("The link must start with / (a page of this store) or https://")
                if image and not (image.startswith("https://") or image.startswith("http://") or image.startswith("/")):
                    raise ValueError("The picture must be a web address (https://...).")
                banner.title, banner.link, banner.image_url = title, link, image
                banner.subtitle = request.POST.get("subtitle", "").strip()[:160]
                banner.button_label = request.POST.get("button_label", "").strip()[:30] or "Shop now"
                banner.starts_at, banner.ends_at = _when(request.POST.get("starts_at")), _when(request.POST.get("ends_at"))
                if banner.starts_at and banner.ends_at and banner.ends_at <= banner.starts_at:
                    raise ValueError("The end time must be after the start time.")
                try:
                    banner.position = max(0, min(int(request.POST.get("position") or 0), 999))
                except ValueError:
                    banner.position = 0
                banner.is_active = bool(request.POST.get("is_active"))
                banner.save()
                _log(request, f"Saved home banner '{banner.title}'")
                messages.success(request, f"Banner '{banner.title}' saved.")
            elif action == "delete_banner":
                banner = get_object_or_404(HomeBanner, pk=request.POST.get("id"))
                banner.delete()
                _log(request, f"Deleted home banner '{banner.title}'")
                messages.success(request, "Banner deleted.")
            elif action == "delete_page":
                page = get_object_or_404(Page, pk=request.POST.get("id"))
                page.delete()
                _log(request, f"Deleted page '{page.title}'")
                messages.success(request, f"'{page.title}' deleted." + (" The built-in text is shown again." if page.is_built_in else ""))
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("manage_pages")
    own = {p.slug: p for p in Page.objects.all()}
    built_in = [(slug, title, own.get(slug)) for slug, title in Page.BUILT_IN]
    custom = [p for p in own.values() if not p.is_built_in]
    return _render(request, "pages.html", {
        "section": "pages", "built_in": built_in, "custom": sorted(custom, key=lambda p: (p.position, p.title)),
        "banners": HomeBanner.objects.all(), "now": timezone.now()})


RESERVED = {"new", "builtin"}


@staff_required
def page_edit(request, pk=None, builtin=None):
    from .views import BUILT_IN_PAGES
    from . import content
    page = get_object_or_404(Page, pk=pk) if pk else None
    if builtin:
        if builtin not in BUILT_IN_PAGES:
            return redirect("manage_pages")
        page = Page.objects.filter(slug=builtin).first()
        if page is None:
            default = BUILT_IN_PAGES[builtin]()
            page = Page(slug=builtin, title=default["page_title"], body=content.sections_to_body(default["sections"]))
    if request.method == "POST":
        page = page or Page()
        title = request.POST.get("title", "").strip()[:120]
        body = request.POST.get("body", "").strip()[:50000]
        error = None
        if not title or not body:
            error = "Write a title and some text."
        elif not page.slug:
            slug = slugify(request.POST.get("slug", "").strip() or title)[:60].strip("-")
            if not slug or slug in RESERVED or slug in dict(Page.BUILT_IN):
                error = "Choose another address for this page."
            elif Page.objects.filter(slug=slug).exists():
                error = f"A page with the address '{slug}' already exists."
            else:
                page.slug = slug
        if error:
            messages.error(request, error)
            page.title, page.body = title, body
        else:
            page.title, page.body = title, body
            page.meta_description = request.POST.get("meta_description", "").strip()[:170]
            page.is_published = bool(request.POST.get("is_published"))
            page.show_in_footer = bool(request.POST.get("show_in_footer"))
            page.save()
            _log(request, f"Saved page '{page.title}'")
            messages.success(request, f"'{page.title}' saved.")
            return redirect("manage_pages")
    return _render(request, "page_form.html", {"section": "pages", "page": page or Page(is_published=True, show_in_footer=True)})


# ---------------------------------------------------------------------------
# Collection points
# ---------------------------------------------------------------------------

@staff_required
def pickup_points(request):
    from decimal import Decimal, InvalidOperation
    from .countries import COUNTRIES
    from .models import PickupPoint
    names = dict(COUNTRIES)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save":
            point = PickupPoint.objects.filter(pk=request.POST.get("id") or 0).first() or PickupPoint()
            country = request.POST.get("country", "").strip().upper()[:2]
            try:
                fee = Decimal(request.POST.get("fee") or "0").quantize(Decimal("0.01"))
            except InvalidOperation:
                fee = None
            name, address, city = (request.POST.get(f, "").strip() for f in ("name", "address", "city"))
            if not (name and address and city) or country not in names or fee is None or fee < 0:
                messages.error(request, "Fill in the name, address, city, country and a fee (0 for free).")
            else:
                point.name, point.address, point.city, point.country, point.fee = name[:100], address[:255], city[:100], country, fee
                point.postal_code = request.POST.get("postal_code", "").strip()[:20]
                point.hours = request.POST.get("hours", "").strip()[:120]
                point.active = True
                point.save()
                _log(request, f"Saved collection point {point}")
                messages.success(request, f"{point} saved.")
        elif action in ("toggle", "delete"):
            point = get_object_or_404(PickupPoint, pk=request.POST.get("id"))
            if action == "delete":
                point.delete()
            else:
                point.active = not point.active
                point.save(update_fields=["active"])
            _log(request, f"Collection point {point}: {action}")
        return redirect("manage_pickup_points")
    points = [(p, names.get(p.country, p.country)) for p in PickupPoint.objects.all()]
    return _render(request, "pickup_points.html", {"section": "shipping", "points": points, "countries": COUNTRIES})
