"""Installable app: phones and computers can add the store to the home
screen / dock. The service worker only shows a friendly offline page when
there is no connection - it never caches personal pages."""
import io

from django.core.cache import cache
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

ICON_SIZES = (192, 512)
OFFLINE_VERSION = "v2"


def _brand():
    from .models import SiteSettings
    return SiteSettings.load()


@require_GET
def manifest(request):
    brand = _brand()
    name = brand.site_name or "Store"
    data = {
        "name": name,
        "short_name": name[:12],
        "description": getattr(brand, "tagline", "") or name,
        "start_url": "/?source=app",
        "scope": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": brand.primary_color or "#0E3B43",
        "icons": [{"src": reverse("pwa_icon", args=[s]), "sizes": f"{s}x{s}", "type": "image/png", "purpose": "any maskable"} for s in ICON_SIZES],
        "shortcuts": [
            {"name": "My orders", "url": reverse("my_orders")},
            {"name": "Messages", "url": reverse("messages_inbox")},
        ],
    }
    response = JsonResponse(data, content_type="application/manifest+json")
    response["Cache-Control"] = "public, max-age=3600"
    return response


@require_GET
def icon(request, size):
    if size not in ICON_SIZES:
        raise Http404
    brand = _brand()
    import hashlib
    key = "pwa-icon:" + hashlib.md5(f"{size}|{brand.primary_color}|{brand.accent_color}|{brand.site_name}".encode()).hexdigest()
    png = cache.get(key)
    if png is None:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (size, size), brand.primary_color or "#0E3B43")
        draw = ImageDraw.Draw(img)
        letter = (brand.site_name or "S").strip()[:1].upper() or "S"
        try:
            font = ImageFont.load_default(size=int(size * 0.5))
        except TypeError:
            font = ImageFont.load_default()
        box = draw.textbbox((0, 0), letter, font=font)
        w, h = box[2] - box[0], box[3] - box[1]
        draw.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), letter, fill=brand.accent_color or "#F2B33D", font=font)
        out = io.BytesIO()
        img.save(out, "PNG")
        png = out.getvalue()
        cache.set(key, png, 86400)
    response = HttpResponse(png, content_type="image/png")
    response["Cache-Control"] = "public, max-age=86400"
    return response


SERVICE_WORKER = """
const CACHE = 'lumen-offline-%(version)s';
const OFFLINE = '%(offline)s';
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.add(new Request(OFFLINE, { cache: 'reload' }))).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  if (e.request.mode !== 'navigate') return;  // only page loads; everything else goes straight to the network
  e.respondWith(fetch(e.request).catch(() => caches.match(OFFLINE)));
});
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { title: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.title || 'Update', {
    body: d.body || '', icon: '/app-icon-192.png', badge: '/app-icon-192.png', tag: d.tag || undefined, data: { url: d.url || '/' }
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || '/', self.location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    for (const c of list) { if (c.url === url && 'focus' in c) return c.focus(); }
    return self.clients.openWindow(url);
  }));
});
"""


@require_GET
def service_worker(request):
    js = SERVICE_WORKER % {"version": OFFLINE_VERSION, "offline": reverse("offline")}
    response = HttpResponse(js, content_type="application/javascript")
    response["Cache-Control"] = "no-cache"
    response["Service-Worker-Allowed"] = "/"
    return response


@require_GET
def offline(request):
    return render(request, "bees/offline.html")
