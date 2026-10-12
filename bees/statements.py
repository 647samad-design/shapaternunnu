"""Monthly seller statement as a PDF (Seller Center > Earnings).

Lists every sale in the month with the commission taken, refunds, payouts
sent, and the totals - something a seller can hand to an accountant."""
from datetime import date, datetime, time
from decimal import Decimal
from io import BytesIO

from django.utils import timezone

ZERO = Decimal("0")


def month_bounds(key):
    """'2026-09' -> (aware start, aware end, date). Falls back to last month."""
    today = timezone.localdate()
    try:
        y, m = (int(x) for x in key.split("-"))
        first = date(y, m, 1)
    except (ValueError, AttributeError):
        first = (today.replace(day=1) - timezone.timedelta(days=1)).replace(day=1)
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    tz = timezone.get_current_timezone()
    return (timezone.make_aware(datetime.combine(first, time.min), tz),
            timezone.make_aware(datetime.combine(nxt, time.min), tz), first)


def months_with_sales(seller, limit=18):
    from django.db.models.functions import TruncMonth
    from .models import OrderItem
    rows = (OrderItem.objects.filter(seller_account=seller).annotate(m=TruncMonth("order__created_at"))
            .values_list("m", flat=True).distinct().order_by("-m")[:limit])
    return [timezone.localtime(m).strftime("%Y-%m") if timezone.is_aware(m) else m.strftime("%Y-%m") for m in rows if m]


def data(seller, key):
    from .models import OrderItem, ReturnRequest
    start, end, first = month_bounds(key)
    items = list(OrderItem.objects.filter(seller_account=seller, order__created_at__gte=start, order__created_at__lt=end)
                 .counted().select_related("order").order_by("order__created_at"))
    refunds = list(ReturnRequest.objects.filter(order_item__seller_account=seller, status="refunded",
                                                 order_item__order__created_at__lt=end)
                   .filter(created_at__gte=start, created_at__lt=end).select_related("order_item__order"))
    payouts = list(seller.payouts.filter(status="paid", paid_at__gte=start, paid_at__lt=end).order_by("paid_at"))
    sales = sum((i.subtotal for i in items), ZERO)
    commission = sum((i.commission_amount for i in items), ZERO)
    refunded = sum((r.order_item.seller_earning for r in refunds), ZERO)
    return {
        "start": start, "end": end, "month": first, "items": items, "refunds": refunds, "payouts": payouts,
        "sales": sales, "commission": commission, "earned": sales - commission, "refunded": refunded,
        "net": sales - commission - refunded, "paid_out": sum((p.amount for p in payouts), ZERO),
        "units": sum(i.quantity for i in items),
    }


def pdf(seller, key):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from .models import SiteSettings
    from .templatetags.bees_extras import money
    site = SiteSettings.load()
    d = data(seller, key)
    brand = colors.HexColor(site.primary_color or "#0E3B43")
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"{seller.display_name} statement {d['month']:%B %Y}")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], alignment=0, fontSize=20, textColor=brand, spaceAfter=2)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=9, textColor=colors.HexColor("#56606B"))
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12, spaceBefore=12, spaceAfter=6)
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontSize=8.5, leading=11)

    def esc(text):
        from xml.sax.saxutils import escape
        return escape(str(text))

    story = [
        Paragraph(f"{esc(site.site_name)} - seller statement", h1),
        Paragraph(f"<b>{esc(seller.display_name)}</b> · {d['month']:%B %Y} · generated {timezone.localdate():%d %b %Y}", small),
        Spacer(1, 10),
    ]
    summary = [
        ["Sales", money(d["sales"]), "Items sold", str(d["units"])],
        ["Commission", "- " + money(d["commission"]), "Orders", str(len({i.order_id for i in d["items"]}))],
        ["Refunds (returns & problems)", "- " + money(d["refunded"]), "Payouts sent this month", money(d["paid_out"])],
        ["Net earnings", money(d["net"]), "", ""],
    ]
    t = Table(summary, colWidths=[52 * mm, 34 * mm, 52 * mm, 40 * mm])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9.5), ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("ALIGN", (3, 0), (3, -1), "RIGHT"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F4F6F7")), ("FONTNAME", (0, 3), (1, 3), "Helvetica-Bold"),
        ("LINEABOVE", (0, 3), (1, 3), 1, brand), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [t, Paragraph("Sales", h2)]
    rows = [["Date", "Order", "Product", "Qty", "Sale", "Commission", "You earn"]]
    for i in d["items"]:
        rows.append([timezone.localtime(i.order.created_at).strftime("%d %b"), f"#{i.order_id}", Paragraph(esc(i.product_name), cell),
                     str(i.quantity), money(i.subtotal), money(i.commission_amount), money(i.seller_earning)])
    if len(rows) == 1:
        rows.append(["", "", "No sales this month.", "", "", "", ""])
    st = Table(rows, colWidths=[16 * mm, 16 * mm, 66 * mm, 10 * mm, 24 * mm, 24 * mm, 22 * mm], repeatRows=1)
    st.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8.5), ("BACKGROUND", (0, 0), (-1, 0), brand), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("ALIGN", (3, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F8F9")]),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#DDE2E6")),
    ]))
    story.append(st)
    if d["refunds"]:
        story.append(Paragraph("Refunds", h2))
        rr = [["Date", "Order", "Product", "Taken from earnings"]] + [
            [timezone.localtime(r.created_at).strftime("%d %b"), f"#{r.order_item.order_id}", Paragraph(esc(r.order_item.product_name), cell),
             "- " + money(r.order_item.seller_earning)] for r in d["refunds"]]
        rt = Table(rr, colWidths=[18 * mm, 18 * mm, 100 * mm, 42 * mm])
        rt.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8.5), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                                ("ALIGN", (3, 0), (3, -1), "RIGHT"), ("LINEBELOW", (0, 0), (-1, 0), 0.8, brand)]))
        story.append(rt)
    if d["payouts"]:
        story.append(Paragraph("Payouts", h2))
        pr = [["Date", "Method", "Reference", "Amount"]] + [
            [timezone.localtime(p.paid_at).strftime("%d %b"), p.get_method_display() or "-", p.reference or "-", money(p.amount)] for p in d["payouts"]]
        pt = Table(pr, colWidths=[18 * mm, 40 * mm, 78 * mm, 42 * mm])
        pt.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8.5), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                                ("ALIGN", (3, 0), (3, -1), "RIGHT"), ("LINEBELOW", (0, 0), (-1, 0), 0.8, brand)]))
        story.append(pt)
    story += [Spacer(1, 14), Paragraph(
        f"Sales are counted by order date. Cancelled and unpaid orders are left out. Earnings become available for payout once an item "
        f"is delivered and its {site.return_days}-day return window has passed."
        + (f" Questions: {esc(site.support_email)}" if site.support_email else ""), small)]
    doc.build(story)
    return buf.getvalue(), d["month"]
