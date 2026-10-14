"""The shop's categories (Manage > Categories), cached.

Products keep the category's slug in ``Product.category``. A top-level
category's page also lists its subcategories' products.
"""
from django.core.cache import cache

KEY = "categories:v1"


def clear_cache():
    cache.delete(KEY)
    cache.delete("home:category_tiles")


def _load():
    data = cache.get(KEY)
    if data is None:
        from .models import Category
        rows = list(Category.objects.values("id", "slug", "name", "parent_id", "position", "is_active",
                                            "image_url", "description", "size_chart"))
        by_id = {r["id"]: r for r in rows}
        for r in rows:
            parent = by_id.get(r["parent_id"])
            r["parent"] = parent["slug"] if parent else ""
            # Hidden when it or its parent is hidden.
            r["visible"] = r["is_active"] and (parent is None or parent["is_active"])
        order = lambda r: (r["position"], r["name"].lower())  # noqa: E731
        tops = sorted([r for r in rows if not r["parent"]], key=order)
        ordered = []
        for top in tops:
            ordered.append(top)
            ordered += sorted([r for r in rows if r["parent"] == top["slug"]], key=order)
        # Children of a missing parent, just in case.
        ordered += [r for r in rows if r not in ordered]
        data = [{k: r[k] for k in ("slug", "name", "parent", "visible", "image_url", "description", "size_chart")}
                for r in ordered]
        cache.set(KEY, data, 3600)
    return data


def all_rows(include_hidden=False):
    return [r for r in _load() if include_hidden or r["visible"]]


def get(slug):
    for r in _load():
        if r["slug"] == slug:
            return r
    return None


def choices(include_hidden=False):
    """(slug, label) for every category, subcategories after their parent
    as "Parent › Child". For forms and filters."""
    names = {r["slug"]: r["name"] for r in _load()}
    return [(r["slug"], f"{names.get(r['parent'], '')} › {r['name']}" if r["parent"] else r["name"])
            for r in all_rows(include_hidden)]


def labels():
    """slug -> plain name, for every category (hidden ones too, so old
    products still show a name)."""
    return {r["slug"]: r["name"] for r in _load()}


def label(slug):
    return labels().get(slug, (slug or "").replace("-", " ").capitalize())


def is_valid(slug, include_hidden=True):
    return any(r["slug"] == slug for r in all_rows(include_hidden))


def top_level():
    """(slug, name) for the menu bar."""
    return [(r["slug"], r["name"]) for r in all_rows() if not r["parent"]]


def children(slug):
    return [(r["slug"], r["name"]) for r in all_rows() if r["parent"] == slug]


def with_children(slug):
    """The slug plus its subcategories' slugs (hidden ones excluded)."""
    return [slug] + [s for s, _n in children(slug)]


def size_chart(slug):
    """Rows of the size guide for a category (or its parent's), [] if none."""
    row = get(slug)
    text = (row or {}).get("size_chart") or ""
    if not text and row and row["parent"]:
        text = (get(row["parent"]) or {}).get("size_chart") or ""
    rows = [[c.strip() for c in line.split("|")] for line in text.splitlines() if line.strip()]
    return rows if len(rows) >= 2 else []
