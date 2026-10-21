"""AI shopping assistant ("Ask"): turns a request like "a gift for my dad
who likes coffee, under 40" into a search over the real catalog and a short
answer that only mentions products that exist. Works without AI too
(smart search + a plain answer)."""
import json
import logging

from django.conf import settings
from .multistore import store_settings  # per-store values in multi-store mode

logger = logging.getLogger(__name__)
EXAMPLES = ["A gift for a 10 year old who likes drawing, under 30", "Something cosy for winter",
            "Skin care for dry skin", "Kitchen tools for making pasta"]


def _plan(question):
    """{"query": search words, "max": price or None} - asked to Claude when
    available, otherwise the question itself."""
    from .ai import _call_claude, parse_search
    if getattr(store_settings, "AI_API_KEY", ""):
        try:
            text = _call_claude(
                "A shopper asks an online store's assistant: \"" + question.replace('"', "'") + "\"\n"
                "Reply with only JSON: {\"query\": \"2-4 product keywords to search the catalog\", \"max_price\": number or null}.", 120)
            data = json.loads(text[text.index("{"):text.rindex("}") + 1])
            return {"query": str(data.get("query") or question)[:80], "max": data.get("max_price")}
        except Exception:
            logger.info("Shopping assistant plan fell back to plain search")
    parsed = parse_search(question)
    return {"query": " ".join(parsed["words"]) or question, "max": parsed["max"]}


def answer(question, request):
    from decimal import Decimal
    from .views import _smart_search, with_ratings
    plan = _plan(question)
    qs, _smart = _smart_search(plan["query"])
    if plan.get("max"):
        try:
            qs = qs.filter(price__lte=Decimal(str(plan["max"])))
        except Exception:
            pass
    products = list(with_ratings(qs.filter(stock__gt=0))[:6])
    text = ""
    if products and getattr(store_settings, "AI_API_KEY", ""):
        from .ai import _call_claude
        lines = "\n".join(f"- {p.name} ({p.price}): {(p.description or '')[:160]}" for p in products)
        try:
            text = _call_claude(
                f"You are a friendly shop assistant. The shopper asked: \"{question}\".\nProducts in stock:\n{lines}\n"
                "In 2-4 short sentences, recommend the best one or two of these for the shopper and say why. "
                "Only mention products from the list. No prices unless helpful, no markdown.", 220)
        except Exception:
            text = ""
    if not text:
        text = (f"Here are {len(products)} picks for \"{question}\" from our sellers." if products
                else "I couldn't find a match for that yet. Try different words, or browse today's deals.")
    return {"text": text, "products": products, "query": plan["query"]}
