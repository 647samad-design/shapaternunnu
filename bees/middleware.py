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
    "/seller/settings/", "/seller/holiday/", "/manage/team/", "/manage/campaigns/",
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
        if ref and 4 <= len(ref) <= 12 and ref.isalnum():
            request.session["aff"] = ref.upper()  # affiliate links work for everyone
            if not request.user.is_authenticated:
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


class SecurityHeadersMiddleware:
    """Content-Security-Policy and Permissions-Policy for every page.

    The CSP stops injected <script src=...>, plugins, <base> hijacking and
    the site being framed by other sites. Inline scripts are still allowed
    (the templates use them); external scripts only from the analytics
    providers the owner turned on. CSP_REPORT_ONLY=True in .env only reports.
    """
    CSP = "; ".join([
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' https://www.googletagmanager.com https://www.google-analytics.com https://connect.facebook.net",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' data: https://fonts.gstatic.com",
        "img-src 'self' data: blob: https:",
        "media-src 'self' https:",
        "connect-src 'self' https://www.google-analytics.com https://*.google-analytics.com https://*.analytics.google.com https://www.facebook.com",
        "frame-src 'self' https://www.youtube.com https://www.youtube-nocookie.com https://player.vimeo.com https://player.twitch.tv",
        "worker-src 'self'",
        "manifest-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self' https:",
        "frame-ancestors 'none'",
        "upgrade-insecure-requests",
    ])

    def __init__(self, get_response):
        from django.conf import settings
        self.get_response = get_response
        cdn = getattr(settings, "CDN_URL", "") or ""
        csp = self.CSP
        if cdn.startswith("https://"):
            host = "/".join(cdn.split("/")[:3])
            csp = csp.replace("script-src 'self'", f"script-src 'self' {host}").replace("style-src 'self'", f"style-src 'self' {host}") \
                     .replace("font-src 'self'", f"font-src 'self' {host}")
        if getattr(settings, "DEBUG", False):
            csp = csp.replace("; upgrade-insecure-requests", "")
        self.csp = csp
        self.header = "Content-Security-Policy-Report-Only" if getattr(settings, "CSP_REPORT_ONLY", False) else "Content-Security-Policy"

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith("/admin/"):
            return response  # Django's own admin
        csp = self.csp
        extra = getattr(request, "csp_connect", None)
        if extra:  # e.g. the host of a product's 3D model
            csp = csp.replace("connect-src 'self'", "connect-src 'self' " + " ".join(extra), 1)
        script = getattr(request, "csp_script", None)
        if script:
            csp = csp.replace("script-src 'self'", "script-src 'self' " + " ".join(script), 1)
        response.setdefault(self.header, csp)
        response.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(self), usb=(), interest-cohort=()")
        return response
