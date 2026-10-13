"""REST API views (version 1). See bees/api/__init__.py and API.md."""
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404
from django.urls import reverse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.generics import GenericAPIView, ListAPIView, RetrieveAPIView
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from ..models import ApiToken, Order, OrderEvent, OrderItem, Product, Profile, Review, SellerAccount, Wishlist
from . import serializers as s

TOKEN_DAYS = int(getattr(settings, "API_TOKEN_DAYS", 90))
MONEY_ROLES = ("owner", "admin")


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

class IsSeller(permissions.BasePermission):
    """Signed in and working for a seller account (owner or team member).
    Sets request.seller and request.seller_role."""
    message = "This account has no seller account. Apply to sell on the website first."

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        from ..views import get_seller_account_for_user
        seller, role = get_seller_account_for_user(request.user)
        if seller is None:
            return False
        request.seller, request.seller_role = seller, role
        return True


def require_approved(request):
    if request.seller.status != "approved":
        raise PermissionDenied("Your seller account isn't approved yet, so products and orders can't be changed.")


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

class ApiRoot(APIView):
    """Links to every part of the API."""

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        def u(name, *args):
            return request.build_absolute_uri(reverse(f"api:{name}", args=args))
        from ..models import SiteSettings
        return Response({
            "name": f"{SiteSettings.load().site_name} API",
            "version": "1",
            "currency": s.store_currency(),
            "docs": u("docs"), "schema": u("schema"),
            "catalog": {"categories": u("categories"), "products": u("products")},
            "auth": {"token": u("token")},
            "account": {"me": u("me"), "orders": u("orders"), "wishlist": u("wishlist")},
            "seller": {"account": u("seller"), "products": u("seller-products"), "orders": u("seller-orders")},
        })


# ---------------------------------------------------------------------------
# Catalog (public)
# ---------------------------------------------------------------------------

class CategoryList(APIView):
    @extend_schema(responses=s.CategorySerializer(many=True))
    def get(self, request):
        counts = dict(Product.objects.live().values_list("category").annotate(n=Count("id")))
        data = [{"code": code, "label": label, "product_count": counts.get(code, 0)}
                for code, label in Product.CATEGORY_CHOICES]
        return Response(data)


PRODUCT_FILTERS = [
    OpenApiParameter("search", str, description="Words to search for. Understands phrases like 'red hoodie under 40'."),
    OpenApiParameter("category", str, description="Category code (see /categories/)."),
    OpenApiParameter("seller_id", int, description="Only this seller's products."),
    OpenApiParameter("min_price", float), OpenApiParameter("max_price", float),
    OpenApiParameter("min_rating", float), OpenApiParameter("in_stock", bool),
    OpenApiParameter("deals", bool, description="Only products in a running deal."),
    OpenApiParameter("sort", str, enum=["price_asc", "price_desc", "newest", "rating"]),
]


class ProductList(ListAPIView):
    """Products customers can buy right now, with search, filters and sorting.
    When a search finds nothing, a spelling correction is tried and returned
    in ``corrected``."""
    serializer_class = s.ProductListSerializer
    corrected = ""

    def get_queryset(self):
        from ..views import _apply_filters, _apply_sort, _smart_search, with_ratings
        params = self.request.query_params
        query = (params.get("search") or params.get("q") or "").strip()[:150]
        self.corrected = ""
        if query:
            qs, _smart = _smart_search(query)
            if not qs.exists():
                from ..autocomplete import correct
                fixed = correct(query)
                if fixed:
                    fixed_qs, _ = _smart_search(fixed)
                    if fixed_qs.exists():
                        qs, self.corrected = fixed_qs, fixed
        else:
            qs = Product.objects.live()
        category = params.get("category")
        if category:
            qs = qs.filter(category=category)
        seller_id = params.get("seller_id")
        if seller_id:
            if not str(seller_id).isdigit():
                raise ValidationError({"seller_id": "Must be a number."})
            qs = qs.filter(seller_account_id=int(seller_id))
        if params.get("deals") in ("1", "true", "True"):
            qs = qs.filter(id__in=Product.objects.flash().values("id"))
        qs = _apply_filters(with_ratings(qs), self.request)
        return _apply_sort(qs, params.get("sort", ""))

    @extend_schema(parameters=PRODUCT_FILTERS)
    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        if self.corrected:
            response.data["corrected"] = self.corrected
        return response


class ProductDetail(RetrieveAPIView):
    serializer_class = s.ProductDetailSerializer

    def get_queryset(self):
        from ..views import with_ratings
        return with_ratings(Product.objects.live()).prefetch_related("variants", "extra_images")


class ProductReviews(ListAPIView):
    serializer_class = s.ReviewSerializer

    def get_queryset(self):
        product = get_object_or_404(Product.objects.live(), pk=self.kwargs["pk"])
        return Review.objects.filter(product=product).order_by("-created_at")


class SellerDetail(RetrieveAPIView):
    """A seller's public store profile."""
    serializer_class = s.SellerPublicSerializer
    queryset = SellerAccount.objects.filter(status="approved").select_related("user")

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------

TOKEN_RESPONSE = inline_serializer("TokenResponse", {
    "token": serializers.CharField(), "token_type": serializers.CharField(),
    "expires_at": serializers.DateTimeField(allow_null=True), "user": serializers.DictField(),
})


class AuthToken(GenericAPIView):
    """POST: sign in and get a token (shown once - store it safely).
    DELETE: sign out (revokes the token used for this request)."""
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"
    serializer_class = s.TokenRequestSerializer

    @extend_schema(request=s.TokenRequestSerializer, responses={201: TOKEN_RESPONSE, 400: OpenApiResponse(description="Wrong details or code needed")})
    def post(self, request):
        ser = s.TokenRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        identifier = ser.validated_data["username"].strip()
        username = identifier
        if "@" in identifier:
            matches = list(User.objects.filter(email__iexact=identifier).values_list("username", flat=True)[:2])
            if len(matches) == 1:
                username = matches[0]
        user = authenticate(request._request, username=username, password=ser.validated_data["password"])
        if user is None or not user.is_active:
            raise ValidationError({"detail": "Incorrect username/email or password."})
        profile = Profile.objects.filter(user=user).first()
        if profile and profile.totp_enabled:
            code = (ser.validated_data.get("code") or "").strip()
            if not code:
                return Response({"detail": "This account uses two-step sign-in. Send the 6-digit code from the "
                                           "authenticator app (or a backup code) as 'code'.", "code_required": True},
                                status=status.HTTP_400_BAD_REQUEST)
            if not _check_second_factor(profile, code):
                raise ValidationError({"code": "That code didn't match. Use the newest code from the app, or a backup code."})
        token, key = ApiToken.issue(user, name=ser.validated_data.get("name") or "API token", days=TOKEN_DAYS)
        from ..models import AuditLog
        AuditLog.objects.create(user=user, action=f"API token created ({token.prefix}…, {token.name})"[:255])
        return Response({"token": key, "token_type": "Bearer",
                         "expires_at": token.expires_at.isoformat() if token.expires_at else None,
                         "user": {"id": user.id, "username": user.username}}, status=status.HTTP_201_CREATED)


class AuthTokenRevoke(APIView):
    """Sign out: revokes the token sent with this request."""
    permission_classes = [permissions.IsAuthenticated]

    def _revoke(self, request):
        if not isinstance(request.auth, ApiToken):
            raise ValidationError({"detail": "Send the token you want to revoke in the Authorization header."})
        request.auth.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=None, responses={204: None})
    def post(self, request):
        return self._revoke(request)

    @extend_schema(request=None, responses={204: None})
    def delete(self, request):
        return self._revoke(request)


def _check_second_factor(profile, code):
    from .. import twofactor
    step = twofactor.verify(profile.totp_secret, code, profile.totp_last_step)
    if step is not None:
        return Profile.objects.filter(pk=profile.pk, totp_last_step__lt=step).update(totp_last_step=step) == 1
    remaining = twofactor.use_backup_code(profile.backup_codes, code)
    if remaining is not None:
        Profile.objects.filter(pk=profile.pk).update(backup_codes=remaining)
        return True
    return False


# ---------------------------------------------------------------------------
# Account (signed-in customer)
# ---------------------------------------------------------------------------

class Me(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        user = request.user
        profile = Profile.objects.filter(user=user).first()
        from ..views import get_seller_account_for_user
        seller, role = get_seller_account_for_user(user)
        return Response({
            "id": user.id, "username": user.username, "email": user.email,
            "name": user.get_full_name(),
            "email_verified": bool(profile and profile.email_verified),
            "two_step_sign_in": bool(profile and profile.totp_enabled),
            "store_credit": s.money(profile.store_credit if profile else Decimal("0")),
            "reward_points": profile.loyalty_points if profile else 0,
            "currency": s.store_currency(),
            "seller": {"id": seller.id, "name": seller.display_name, "status": seller.status, "role": role} if seller else None,
        })


def _customer_orders(user):
    return (Order.objects.filter(user=user)
            .prefetch_related(Prefetch("items", queryset=OrderItem.objects.select_related("variant", "seller_account")),
                              Prefetch("events", queryset=OrderEvent.objects.order_by("created_at", "id")))
            .order_by("-created_at"))


class OrderList(ListAPIView):
    """Your orders, newest first."""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = s.OrderSerializer

    def get_queryset(self):
        return _customer_orders(self.request.user)

    @extend_schema(operation_id="orders_list", responses=OpenApiTypes.OBJECT)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class OrderDetail(RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = s.OrderSerializer

    def get_queryset(self):
        return _customer_orders(self.request.user)

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class WishlistView(APIView):
    """GET: saved products. POST {product_id}: save one."""
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses=s.ProductListSerializer(many=True))
    def get(self, request):
        from ..views import with_ratings
        ids = Wishlist.objects.filter(user=request.user).values("product_id")
        products = with_ratings(Product.objects.live().filter(id__in=ids)).order_by("-id")
        return Response(s.ProductListSerializer(products, many=True, context={"request": request}).data)

    @extend_schema(request=s.WishlistAddSerializer, responses={201: OpenApiTypes.OBJECT})
    def post(self, request):
        ser = s.WishlistAddSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        product = Product.objects.live().filter(pk=ser.validated_data["product_id"]).first()
        if product is None:
            raise NotFound("No product with that id is for sale.")
        if Wishlist.objects.filter(user=request.user).count() >= 500:
            raise ValidationError({"detail": "Your wishlist is full (500 products)."})
        _obj, created = Wishlist.objects.get_or_create(user=request.user, product=product)
        return Response({"product_id": product.id, "saved": True},
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class WishlistItem(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={204: None})
    def delete(self, request, product_id):
        Wishlist.objects.filter(user=request.user, product_id=product_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Seller Center
# ---------------------------------------------------------------------------

class SellerAccountView(APIView):
    """The seller account you work for. Money figures are shown to the owner
    and team admins only (same as the Seller Center)."""
    permission_classes = [IsSeller]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        seller, role = request.seller, request.seller_role
        plan = seller.active_plan
        data = {
            "id": seller.id, "name": seller.display_name, "status": seller.status, "role": role,
            "account_type": seller.account_type, "verified": seller.is_verified, "top_seller": seller.top_seller,
            "on_vacation": seller.vacation_mode,
            "plan": {"name": plan.name, "expires_at": seller.plan_expires_at.isoformat() if seller.plan_expires_at else None} if plan else None,
            "product_limit": seller.product_limit,
            "products": seller.products.exclude(approval_status="archived").count(),
        }
        if role in MONEY_ROLES:
            data["money"] = {
                "currency": s.store_currency(),
                "commission_rate_percent": s.money(Decimal(str(seller.effective_commission_rate))),
                "lifetime_sales": s.money(seller.lifetime_sales),
                "balance_available": s.money(Decimal(str(seller.amount_owed))),
                "paid_out": s.money(seller.total_paid_out),
            }
        return Response(data)


SELLER_PRODUCT_TABS = {
    "live": lambda qs: qs.live(), "review": lambda qs: qs.filter(approval_status="pending"),
    "rejected": lambda qs: qs.filter(approval_status="rejected"),
    "low": lambda qs: qs.filter(stock__gt=0, stock__lte=5), "out": lambda qs: qs.filter(stock__lte=0),
}


def _seller_products(seller):
    from ..views import with_ratings
    return with_ratings(seller.products.exclude(approval_status="archived")).prefetch_related("variants", "extra_images")


def _save_rows(request, rows, product=None):
    """Creates or updates a product through the bulk-import rules (validation,
    plan limit, review). Returns the saved product."""
    from .. import bulk
    seller = request.seller
    products = seller.products.exclude(approval_status="archived")
    result = bulk.plan(rows, products, seller=seller)
    entry = result["rows"][0]
    if entry["action"] == "error":
        raise ValidationError({"detail": entry["errors"][0]})
    if entry["action"] == "same":
        return product
    stash = [{"id": entry.get("id"), "action": entry["action"], "review": entry["review"],
              "data": {k: str(v) if isinstance(v, Decimal) else v for k, v in entry["data"].items()}}]
    before = set(seller.products.values_list("id", flat=True)) if product is None else None
    created, _updated, _review = bulk.apply(stash, products, seller=seller)
    from ..models import AuditLog
    if product is None:
        if not created:
            raise ValidationError({"detail": "Your plan's product limit is reached. Upgrade your plan to add more."})
        new = seller.products.exclude(id__in=before).order_by("-id").first()
        AuditLog.objects.create(user=request.user, action=f"API: added product #{new.pk} '{new.name}' (in review)"[:255])
        return new
    AuditLog.objects.create(user=request.user, action=f"API: updated product #{product.pk} ({', '.join(entry['data'])})"[:255])
    return product


class SellerProductList(ListAPIView):
    """GET: your products (``status``: live, review, rejected, low, out).
    POST: add a product - it goes to the store's review before it's shown."""
    permission_classes = [IsSeller]
    serializer_class = s.SellerProductSerializer

    def get_queryset(self):
        qs = _seller_products(self.request.seller)
        tab = self.request.query_params.get("status")
        if tab in SELLER_PRODUCT_TABS:
            qs = SELLER_PRODUCT_TABS[tab](qs)
        q = (self.request.query_params.get("search") or "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(variants__sku__icontains=q)).distinct()
        return qs.order_by("-created_at", "-id")

    @extend_schema(parameters=[OpenApiParameter("status", str, enum=list(SELLER_PRODUCT_TABS)), OpenApiParameter("search", str)])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(request=s.SellerProductWriteSerializer, responses={201: s.SellerProductSerializer})
    def post(self, request):
        require_approved(request)
        ser = s.SellerProductWriteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        missing = [f for f in ("name", "category", "price", "image_url") if f not in ser.validated_data]
        if missing:
            raise ValidationError({f: "This field is required for a new product." for f in missing})
        product = _save_rows(request, [ser.as_row()])
        product = _seller_products(request.seller).get(pk=product.pk)
        return Response(s.SellerProductSerializer(product, context={"request": request}).data, status=status.HTTP_201_CREATED)


class SellerProductDetail(RetrieveAPIView):
    """GET one product. PATCH to change it: price and stock are live at once;
    name, category, photo and description changes go back to review."""
    permission_classes = [IsSeller]
    serializer_class = s.SellerProductSerializer

    def get_queryset(self):
        return _seller_products(self.request.seller)

    @extend_schema(request=s.SellerProductWriteSerializer, responses=s.SellerProductSerializer)
    def patch(self, request, pk):
        require_approved(request)
        product = self.get_object()
        ser = s.SellerProductWriteSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        if not ser.validated_data:
            raise ValidationError({"detail": "Send at least one field to change."})
        row = ser.as_row()
        row["id"] = str(product.pk)
        _save_rows(request, [row], product=product)
        product = self.get_queryset().get(pk=product.pk)
        return Response(s.SellerProductSerializer(product, context={"request": request}).data)


SELLER_ORDER_TABS = ["to_pack", "packed", "in_transit", "delivered", "unpaid", "cancelled"]


def _seller_order_qs(seller, tab=None):
    from ..seller_views import _filter_items
    from .. import seller_center as sc
    items = sc.sold_items(seller)
    if tab:
        items = _filter_items(items, tab)
    mine = OrderItem.objects.filter(seller_account=seller).select_related("variant", "seller_account")
    return (Order.objects.filter(id__in=items.values("order_id"))
            .prefetch_related(Prefetch("items", queryset=mine))
            .order_by("-created_at"))


class SellerOrderList(ListAPIView):
    """Orders with your items (only your items are shown)."""
    permission_classes = [IsSeller]
    serializer_class = s.SellerOrderSerializer

    def get_queryset(self):
        tab = self.request.query_params.get("status")
        if tab and tab not in SELLER_ORDER_TABS:
            raise ValidationError({"status": f"Use one of: {', '.join(SELLER_ORDER_TABS)}."})
        return _seller_order_qs(self.request.seller, tab)

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if hasattr(self.request, "seller"):
            ctx.update(seller=self.request.seller, show_money=self.request.seller_role in MONEY_ROLES)
        return ctx

    @extend_schema(operation_id="seller_orders_list", parameters=[OpenApiParameter("status", str, enum=SELLER_ORDER_TABS)],
                   responses=OpenApiTypes.OBJECT)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class SellerOrderDetail(SellerOrderList):
    def get_queryset(self):
        return _seller_order_qs(self.request.seller)

    @extend_schema(operation_id="seller_orders_retrieve", responses=OpenApiTypes.OBJECT)
    def get(self, request, pk):
        order = get_object_or_404(self.get_queryset(), pk=pk)
        return Response(self.get_serializer(order).data)


class SellerOrderFulfil(APIView):
    """Mark your items in an order as packed, handed to the courier (with
    courier and tracking number) or delivered. The customer is emailed, just
    like from the Seller Center."""
    permission_classes = [IsSeller]

    @extend_schema(request=s.FulfilSerializer, responses=OpenApiTypes.OBJECT)
    def post(self, request, pk):
        require_approved(request)
        from .. import fulfilment
        seller = request.seller
        items = list(OrderItem.objects.filter(seller_account=seller, order_id=pk).select_related("order"))
        if not items:
            raise NotFound("No order with your items has that id.")
        ser = s.FulfilSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        order = items[0].order
        reason = fulfilment.blocked_reason(order)
        if reason:
            raise ValidationError({"detail": reason})
        courier = (ser.validated_data.get("courier_name") or "").strip()[:60]
        tracking = (ser.validated_data.get("tracking_number") or "").strip()[:60]
        only_seller = not order.items.exclude(seller_account=seller).exists()
        if only_seller and (courier or tracking):
            Order.objects.filter(pk=order.pk).update(courier_name=courier or order.courier_name,
                                                     tracking_number=tracking or order.tracking_number)
        try:
            changed, moved = fulfilment.set_items_status(items, ser.validated_data["status"], user=request.user, forward_only=True)
        except fulfilment.FulfilmentError as exc:
            raise ValidationError({"detail": str(exc)})
        fresh = get_object_or_404(_seller_order_qs(seller), pk=pk)
        data = s.SellerOrderSerializer(fresh, context={"request": request, "seller": seller,
                                                       "show_money": request.seller_role in MONEY_ROLES}).data
        data["changed_items"] = len(changed)
        data["customer_notified"] = bool(changed) and ser.validated_data["status"] in ("handed_to_courier", "delivered")
        return Response(data)

