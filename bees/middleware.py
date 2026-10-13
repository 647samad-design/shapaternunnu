class NoCacheMiddleware:
    """
    Prevents the browser from caching pages (especially the back/forward cache),
    which was causing the navbar to briefly show "logged out" until a manual
    refresh after login, add-to-cart, or checkout redirects.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["Cache-Control"] = "no-cache, no-store, must-revalidate, private"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

# Two-step sign-in enforcement for staff
from django.contrib import messages  # noqa: E402
from django.shortcuts import redirect  # noqa: E402

PROTECTED = ("/manage/", "/admin/")


class StaffTwoFactorMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        user = getattr(request, "user", None)
        if user is not None and path.startswith(PROTECTED) and user.is_authenticated and user.is_staff:
            from django.conf import settings
            if getattr(settings, "DEMO_MODE", False) and user.username == "demo_admin":
                return self.get_response(request)
            from .models import Profile, SiteSettings
            if SiteSettings.load().require_staff_2fa:
                if not Profile.objects.filter(user=user, totp_enabled=True).exists():
                    messages.info(request, "Please set up two-step sign-in to open the store admin. It takes a minute and protects the store if your password is ever stolen.")
                    return redirect("two_factor_setup")
        if user is not None and path.startswith("/seller/") and user.is_authenticated and not user.is_staff:
            from django.conf import settings
            from .models import Profile, SiteSettings
            demo = getattr(settings, "DEMO_MODE", False) and user.username.startswith("demo_")
            if not demo and SiteSettings.load().require_seller_2fa and not Profile.objects.filter(user=user, totp_enabled=True).exists():
                messages.info(request, "Please set up two-step sign-in to open Seller Center. It protects your earnings if your password is ever stolen.")
                return redirect("two_factor_setup")
        return self.get_response(request)


# Demo mode: the demo accounts can look at everything and try the normal
# flows, but can't change store settings, passwords or security, so the
# demo stays usable for the next visitor.
DEMO_BLOCKED = (
    "/admin/", "/manage/settings/", "/manage/setup/", "/manage/system/", "/manage/plans/", "/manage/currencies/",
    "/account/", "/profile/", "/password-reset/", "/seller/team/",
    "/seller/settings/", "/seller/holiday/",
)


class DemoGuardMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings
        user = getattr(request, "user", None)
        if (getattr(settings, "DEMO_MODE", False) and request.method == "POST" and user is not None
                and user.is_authenticated and request.path.startswith(DEMO_BLOCKED)):
            from .management.commands.demo_setup import DEMO_USERNAMES
            if user.username in DEMO_USERNAMES:
                messages.info(request, "This is a demo account, so this change is turned off. Everything else works as normal.")
                from .security import safe_next_url
                return redirect(safe_next_url(request, request.META.get("HTTP_REFERER"), request.path))
        return self.get_response(request)


class ReferralCaptureMiddleware:
    """Remembers ?ref=CODE from any shared link (product, store, home) so
    the friend is credited when they sign up later in the visit."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        ref = request.GET.get("ref", "")
        if ref and 4 <= len(ref) <= 12 and ref.isalnum() and not request.user.is_authenticated:
            request.session["ref"] = ref.upper()
        return self.get_response(request)


class ErrorAlertMiddleware:
    """Records crashes (HTTP 500) so Admin > System check can show them -
    page, error type and time - without digging through server logs."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        from django.http import Http404
        from django.core.exceptions import PermissionDenied
        if isinstance(exception, (Http404, PermissionDenied)):
            return None
        from .alerts import SERVER_ERROR, log
        log(f"{SERVER_ERROR}: {request.method} {request.path[:80]} - {type(exception).__name__}: {str(exception)[:120]}")
        return None
