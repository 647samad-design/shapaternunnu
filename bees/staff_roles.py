"""Store team roles: what each staff member may open in the store admin.

The owner (superuser) can do everything. Other staff get one role, chosen
in Manage > Team. Checked for every /manage/ page by StaffRoleMiddleware,
and used to hide sidebar links they can't open.
"""
from django.contrib import messages
from django.shortcuts import redirect

ROLES = [
    ("manager", "Manager - everything except keys and the team"),
    ("orders", "Orders & shipping"),
    ("support", "Customer support"),
    ("catalog", "Products & sellers"),
    ("finance", "Finance"),
    ("marketing", "Marketing"),
]
ROLE_HELP = {
    "manager": "Runs the store day to day: orders, products, sellers, money, marketing and settings.",
    "orders": "Orders, shipping labels, returns, problem reports, cash-on-delivery checks and customers.",
    "support": "Customer and seller messages, orders, returns, problem reports, reviews and customers.",
    "catalog": "Product approval, store products, categories, bulk import, reviews & questions and sellers.",
    "finance": "Reports, seller payouts, gift cards, coupons, seller plans and orders (to check payments).",
    "marketing": "Coupons, gift cards, email campaigns, sponsored products, referrals, pages & banners and reports.",
}

# Admin area for each URL prefix (longest prefix wins).
PATHS = [
    ("/manage/orders/", "orders"), ("/manage/courier/", "orders"), ("/manage/returns/", "returns"),
    ("/manage/problems/", "claims"), ("/manage/shipping/", "shipping"), ("/manage/cash-on-delivery/", "cod"),
    ("/manage/customers/", "customers"), ("/manage/support/", "messages"), ("/manage/messages/", "messages"),
    ("/manage/reviews/", "reviews"), ("/manage/products/", "products"), ("/manage/sellers/", "sellers"),
    ("/manage/reports/", "reports"), ("/manage/gift-cards/", "gift_cards"), ("/manage/coupons/", "coupons"),
    ("/manage/plans/", "plans"), ("/manage/referrals/", "referrals"), ("/manage/campaigns/", "campaigns"),
    ("/manage/ads/", "ads"), ("/manage/listing-reports/", "products"), ("/manage/promotions/", "coupons"), ("/manage/team/", "team"), ("/manage/keys/", "keys"), ("/manage/stores/", "keys"),
    ("/manage/settings/", "settings"), ("/manage/setup/", "settings"), ("/manage/taxes/", "settings"),
    ("/manage/currencies/", "settings"), ("/manage/translations/", "settings"), ("/manage/system/", "system"),
    ("/manage/categories/", "products"), ("/manage/quotes/", "orders"), ("/manage/pages/", "content"), ("/manage/channels/", "campaigns"), ("/manage/blog/", "content"), ("/manage/companies/", "customers"), ("/manage/locations/", "products"), ("/manage/integrations/", "settings"),
]
ACCESS = {
    "orders": {"orders", "returns", "claims", "shipping", "cod", "customers"},
    "support": {"orders", "returns", "claims", "messages", "reviews", "customers"},
    "catalog": {"products", "reviews", "sellers"},
    "finance": {"reports", "sellers", "gift_cards", "coupons", "plans", "orders", "referrals"},
    "marketing": {"coupons", "gift_cards", "campaigns", "ads", "referrals", "reports", "content"},
}
OWNER_ONLY = {"keys", "team"}


def area_for(path):
    best = ""
    area = "dashboard"
    for prefix, name in PATHS:
        if path.startswith(prefix) and len(prefix) > len(best):
            best, area = prefix, name
    return area


def role_of(user):
    if not getattr(user, "is_authenticated", False) or not user.is_staff:
        return None
    if user.is_superuser:
        return "owner"
    profile = getattr(user, "profile", None)
    role = getattr(profile, "staff_role", "") if profile else ""
    return role or "manager"


def can(user, area):
    role = role_of(user)
    if role is None:
        return False
    if role == "owner" or area == "dashboard":
        return True
    if area in OWNER_ONLY:
        return False
    if role == "manager":
        return True
    return area in ACCESS.get(role, set())


def areas(user):
    """Every area this user may open (for the sidebar)."""
    names = {a for _p, a in PATHS} | {"dashboard"}
    return {a for a in names if can(user, a)}


class StaffRoleMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if request.path.startswith("/manage/") and user is not None and user.is_authenticated and user.is_staff:
            area = area_for(request.path)
            if not can(user, area):
                messages.error(request, "Your team role doesn't include that part of the admin. Ask the store owner if you need it.")
                return redirect("manage_dashboard")
        return self.get_response(request)
