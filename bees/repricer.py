"""Auto-repricer for sellers who share a product with other sellers
("Other sellers on this product").

Each rule says: never go below my minimum; beat (or match) the lowest
other seller's price; and, when nobody else is selling, go back to my
normal price. It runs every night and whenever a competing offer on the
same product changes price, so the seller doesn't have to watch
competitors all day. Only active, in-stock listings in the same condition
count as competitors. Every change is written on the rule ("last note")
and the price history, so the seller can see why a price moved.
"""
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone


def group_ids(product):
    """The master product and every offer on it."""
    from .models import Product
    master_id = product.offer_of_id or product.pk
    return set(Product.objects.filter(Q(pk=master_id) | Q(offer_of_id=master_id)).values_list("pk", flat=True))


def competitors(product):
    from .models import Product
    ids = group_ids(product) - {product.pk}
    return (Product.objects.live().filter(pk__in=ids, stock__gt=0, condition=product.condition)
            .exclude(seller_account_id=product.seller_account_id))


def target_price(rule, lowest):
    if lowest is None:
        return rule.max_price  # alone: back to the normal price (None = leave it)
    if rule.mode == "match":
        price = lowest
    else:
        price = lowest - rule.step
    return max(rule.min_price, price.quantize(Decimal("0.01")))


def apply_rule(rule, now=None):
    """Re-prices one listing. Returns the new price or None if unchanged."""
    from .models import Product, RepriceRule
    product = Product.objects.filter(pk=rule.product_id).first()
    now = now or timezone.now()
    if product is None or not rule.active:
        return None
    others = list(competitors(product).values_list("price", flat=True))
    lowest = min(others) if others else None
    new = target_price(rule, lowest)
    if lowest is None and new is None:
        note = "You're the only seller - price left as it is."
    elif lowest is None:
        note = f"You're the only seller - back to {new}."
    elif new == rule.min_price and (lowest - rule.step if rule.mode == "beat" else lowest) < rule.min_price:
        note = f"Lowest other seller is {lowest}; held at your minimum {rule.min_price}."
    else:
        note = f"Lowest other seller is {lowest}; {'beat' if rule.mode == 'beat' else 'matched'} at {new}."
    changed = None
    if new is not None and new != product.price:
        # .update() keeps this from triggering another repricing round.
        Product.objects.filter(pk=product.pk).update(price=new, updated_at=now)
        try:
            from .models import PriceHistory
            PriceHistory.objects.create(product=product, price=new)
        except Exception:
            pass
        changed = new
    RepriceRule.objects.filter(pk=rule.pk).update(last_run_at=now, last_note=note[:200])
    return changed


def run_all():
    from .models import RepriceRule
    changed = 0
    for rule in RepriceRule.objects.filter(active=True).select_related("product")[:5000]:
        if apply_rule(rule) is not None:
            changed += 1
    return changed


def on_price_change(product):
    """A listing in a shared group changed price: re-price the others."""
    from .models import RepriceRule
    ids = group_ids(product) - {product.pk}
    for rule in RepriceRule.objects.filter(active=True, product_id__in=ids):
        apply_rule(rule)
