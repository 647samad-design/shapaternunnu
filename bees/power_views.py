"""Pages for Trust & safety, the photo studio, the repricer, price tests
and AI reply drafts."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from .ratelimit import ratelimit


# ---------------------------------------------------------------- Trust & safety (admin)

def manage_trust(request):
    from .manage_views import _log, _render
    from .models import Notification, Order, Product, Review, TrustFlag
    from .seller_center import _seller_users
    if request.method == "POST":
        flag = get_object_or_404(TrustFlag, pk=request.POST.get("flag"))
        action = request.POST.get("action")
        if action == "dismiss":
            flag.status, flag.closed_by = "dismissed", request.user
            flag.save(update_fields=["status", "closed_by"])
            messages.success(request, "Marked as not a problem.")
        elif action == "unlist" and flag.product_id:
            Product.objects.filter(pk=flag.product_id).update(approval_status="pending")
            product = flag.product
            if product.seller_account_id:
                Notification.objects.bulk_create([
                    Notification(user=u, link=f"/seller/product/{product.pk}/edit/",
                                 message=f"'{product.name}' was taken down for review: it looks like it uses another seller's photo. Upload your own photos to put it back."[:255])
                    for u in _seller_users(product.seller_account)])
            flag.status, flag.closed_by = "actioned", request.user
            flag.save(update_fields=["status", "closed_by"])
            _log(request, f"Took down product #{product.pk} (copied listing)")
            messages.success(request, f"'{product.name}' is hidden until it's approved again. The seller has been told.")
        elif action == "delete_reviews" and flag.review_ids:
            n = Review.objects.filter(pk__in=flag.review_ids).delete()[0]
            flag.status, flag.closed_by = "actioned", request.user
            flag.save(update_fields=["status", "closed_by"])
            _log(request, f"Deleted {n} suspicious review(s)")
            messages.success(request, f"Deleted {n} review{'s' if n != 1 else ''}.")
        elif action == "block_cod" and flag.order_ids:
            from .cod import phone_key
            from .models import CodBlock
            phones = {phone_key(o.phone) for o in Order.objects.filter(pk__in=flag.order_ids) if phone_key(o.phone)}
            for key in phones:
                CodBlock.objects.get_or_create(kind="phone", value=key, defaults={"reason": "Fake-order network (Trust & safety)",
                                                                                   "created_by": request.user})
            flag.status, flag.closed_by = "actioned", request.user
            flag.save(update_fields=["status", "closed_by"])
            _log(request, f"Blocked cash on delivery for {len(phones)} phone number(s)")
            messages.success(request, "Cash on delivery is now blocked for that phone number.")
        return redirect("manage_trust")
    if request.GET.get("run"):
        from .trustcheck import run_checks
        result = run_checks()
        messages.success(request, f"Checked: {result['fingerprinted']} new photos fingerprinted, "
                                  f"{result['copied'] + result['reviews'] + result['networks']} new finding(s).")
        return redirect("manage_trust")
    show = request.GET.get("show", "open")
    flags = TrustFlag.objects.filter(status="open") if show == "open" else TrustFlag.objects.exclude(status="open")
    return _render(request, "trust.html", {"section": "trust", "flags": flags.select_related("product", "other_product")[:200],
                                           "show": show, "open_count": TrustFlag.objects.filter(status="open").count()})


# ---------------------------------------------------------------- photo studio (seller)

@ratelimit("photo_studio", rate_limit=20, window_seconds=600)
def seller_photo_studio(request, pk):
    from . import photostudio
    from .models import Product, ProductImage
    from .security import validate_image_upload
    from .seller_views import _render
    from .trustcheck import open_stored
    product = get_object_or_404(Product, pk=pk, seller_account=request.seller)
    result = request.session.get("studio_result") if request.session.get("studio_product") == product.pk else None
    if request.method == "POST":
        action = request.POST.get("action", "make")
        if action in ("main", "extra") and result and photostudio.is_studio_url(result.get("url")):
            if action == "main":
                old = product.image_url
                Product.objects.filter(pk=product.pk).update(image_url=result["url"], image_hash="")
                if old and not ProductImage.objects.filter(product=product, image_url=old).exists() and product.extra_images.count() < 8:
                    ProductImage.objects.create(product=product, image_url=old)  # keep the original in the gallery
                messages.success(request, "Done! The studio photo is now the main picture (the old one moved to the gallery).")
            else:
                if product.extra_images.count() >= 8:
                    messages.error(request, "The gallery is full (8 pictures). Remove one first.")
                    return redirect("seller_photo_studio", pk=product.pk)
                ProductImage.objects.create(product=product, image_url=result["url"])
                messages.success(request, "Added to the gallery.")
            request.session.pop("studio_result", None)
            return redirect("seller_photo_studio", pk=product.pk)
        upload = request.FILES.get("photo")
        try:
            if upload is not None:
                validate_image_upload(upload)
                source = upload.read()
            else:
                img = open_stored(product.image_url)
                if img is None:
                    raise ValueError("Upload a photo to start (the current picture is a link to another website).")
                import io
                buf = io.BytesIO()
                img.save(buf, "PNG")
                source = buf.getvalue()
            background = request.POST.get("background", "white")
            data, method = photostudio.process(source, background if background in photostudio.BACKGROUNDS else "white",
                                               request.POST.get("shadow") == "1")
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect("seller_photo_studio", pk=product.pk)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("seller_photo_studio", pk=product.pk)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Photo studio failed for product %s", product.pk)
            messages.error(request, "That photo couldn't be processed. Please try a JPG or PNG photo.")
            return redirect("seller_photo_studio", pk=product.pk)
        url = photostudio.save(data)
        request.session["studio_product"] = product.pk
        request.session["studio_result"] = {"url": url, "method": method}
        return redirect("seller_photo_studio", pk=product.pk)
    return _render(request, "photo_studio.html", {"section": "products", "product": product, "result": result,
                                                  "pro": bool(photostudio._env("REMOVE_BG_API_KEY"))})


# ---------------------------------------------------------------- repricer (seller)

def seller_repricer(request):
    from django.db.models import Q
    from .models import Product, RepriceRule
    from .repricer import apply_rule, competitors
    from .seller_views import _render
    seller = request.seller
    shared = Product.objects.filter(seller_account=seller).exclude(approval_status="archived").filter(
        Q(offer_of__isnull=False) | Q(other_offers__isnull=False)).distinct().select_related("reprice_rule")
    if request.method == "POST":
        product = get_object_or_404(shared, pk=request.POST.get("product"))
        if request.POST.get("action") == "off":
            RepriceRule.objects.filter(product=product).update(active=False)
            messages.success(request, f"Repricer turned off for '{product.name}'.")
            return redirect("seller_repricer")
        try:
            low = Decimal(request.POST.get("min_price", "").strip())
            high = Decimal(request.POST["max_price"].strip()) if request.POST.get("max_price", "").strip() else None
            step = Decimal(request.POST.get("step", "0.10").strip() or "0.10")
        except (InvalidOperation, KeyError):
            messages.error(request, "Please enter prices as numbers, e.g. 24.50.")
            return redirect("seller_repricer")
        if low <= 0 or step < 0 or step > low or (high is not None and high < low):
            messages.error(request, "The minimum must be above 0, the normal price at least the minimum, and the step smaller than the minimum.")
            return redirect("seller_repricer")
        mode = request.POST.get("mode") if request.POST.get("mode") in ("beat", "match") else "beat"
        rule, _ = RepriceRule.objects.update_or_create(product=product, defaults={
            "min_price": low.quantize(Decimal("0.01")), "max_price": high.quantize(Decimal("0.01")) if high else None,
            "mode": mode, "step": step.quantize(Decimal("0.01")), "active": True})
        new = apply_rule(rule)
        rule.refresh_from_db()
        messages.success(request, f"Repricer on for '{product.name}'. {rule.last_note}" + (f" New price: {new}." if new is not None else ""))
        return redirect("seller_repricer")
    rows = []
    for p in shared:
        others = list(competitors(p).values_list("price", flat=True))
        rows.append({"product": p, "rule": getattr(p, "reprice_rule", None), "lowest": min(others) if others else None,
                     "count": len(others)})
    return _render(request, "repricer.html", {"section": "repricer", "rows": rows})


# ---------------------------------------------------------------- price tests (seller + admin)

def _price_tests_page(request, products, render, back):
    from . import pricetest
    from .models import PriceTest
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "start":
            product = get_object_or_404(products, pk=request.POST.get("product"))
            try:
                cost = request.POST.get("unit_cost", "").strip()
                pricetest.start(product, Decimal(request.POST.get("price_b", "").strip()), request.POST.get("days", 14),
                                Decimal(cost) if cost else None, request.user)
                messages.success(request, f"Price test started on '{product.name}'. Today it sells at {product.price}; tomorrow at the test price, and so on.")
            except (InvalidOperation, ValueError) as exc:
                messages.error(request, str(exc) if isinstance(exc, ValueError) and str(exc) else "Please enter the test price as a number.")
        elif action == "stop":
            test = get_object_or_404(PriceTest, pk=request.POST.get("test"), product__in=products, status="running")
            keep = request.POST.get("keep") if request.POST.get("keep") in ("a", "b") else "a"
            pricetest.finish(test, keep=keep, stopped=True)
            messages.success(request, f"Test stopped. '{test.product.name}' now sells at {test.price_a if keep == 'a' else test.price_b}.")
        return redirect(back)
    tests = PriceTest.objects.filter(product__in=products).select_related("product")[:50]
    rows = [{"test": t, "res": pricetest.results(t)} for t in tests]
    running = {t.product_id for t in tests if t.status == "running"}
    choices = [p for p in products.filter(is_auction=False, quote_only=False).order_by("name")[:500] if p.pk not in running]
    return render(request, "price_tests.html", {"section": "price_tests", "rows": rows, "choices": choices})


def seller_price_tests(request):
    from .models import Product
    from .seller_views import _render
    return _price_tests_page(request, Product.objects.filter(seller_account=request.seller).exclude(approval_status="archived"),
                             _render, "seller_price_tests")


def manage_price_tests(request):
    from .manage_views import _render
    from .models import Product
    return _price_tests_page(request, Product.objects.exclude(approval_status="archived"), _render, "manage_price_tests")


# ---------------------------------------------------------------- suggested chat replies (seller)

@require_POST
@ratelimit("reply_draft", rate_limit=30, window_seconds=600)
def reply_draft(request, pk):
    from .messaging import side_for
    from .models import Conversation
    from .replies import draft
    conv = get_object_or_404(Conversation.objects.select_related("seller", "buyer", "product"), pk=pk)
    if not request.user.is_authenticated or side_for(request.user, conv) != "seller":
        raise Http404
    text, used_ai = draft(conv)
    return JsonResponse({"draft": text, "ai": used_ai})
