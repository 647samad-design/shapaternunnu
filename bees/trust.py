"""Trusted sellers: their listings skip the review queue when the store
turns on Store settings > Save time > Auto-approve trusted sellers."""


def trusted(seller):
    if seller is None:
        return False
    from .models import SiteSettings
    if not SiteSettings.load().auto_approve_trusted:
        return False
    return bool(seller.status == "approved" and seller.verified_at and seller.performance_level in ("excellent", "good"))
