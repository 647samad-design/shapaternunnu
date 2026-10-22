"""Photo studio: turns a phone photo into a clean shop photo.

1. Cut-out: the product is separated from its background. With
   REMOVE_BG_API_KEY (remove.bg) a professional AI cut-out is used; without
   it the built-in cut-out works for products shot on a plain wall, table
   or sheet: it learns the background colour from the edges of the photo
   and removes everything of that colour that touches the edges.
2. Studio: the product is centred on a square, evenly lit background
   (white, light grey or cream) with a soft shadow, gently brightened and
   sharpened - the look marketplaces ask for on main images.
If no clear background can be found, the photo is still squared,
straightened and enhanced instead of being cut badly.
"""
import io
import json
import logging
import statistics
import urllib.request
import uuid

logger = logging.getLogger(__name__)

BACKGROUNDS = {"white": (255, 255, 255), "grey": (242, 243, 245), "cream": (250, 246, 238)}
SIZE = 1600


def _env(name):
    from .multistore import env
    return env(name)


def load(file_or_bytes):
    from PIL import Image, ImageOps
    data = file_or_bytes if isinstance(file_or_bytes, (bytes, bytearray)) else file_or_bytes.read()
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "transparency" in img.info or img.mode in ("LA", "P") else "RGB")
    if max(img.size) > 2400:
        img.thumbnail((2400, 2400), Image.LANCZOS)
    return img


# ---------------------------------------------------------------- cut-out

def _border_pixels(img, step=3):
    w, h = img.size
    px = img.load()
    out = []
    for x in range(0, w, step):
        out.append(px[x, 0]); out.append(px[x, h - 1])
    for y in range(0, h, step):
        out.append(px[0, y]); out.append(px[w - 1, y])
    return out


def builtin_cutout(img):
    """RGBA cut-out, or None when the background isn't plain enough."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
    rgb = img.convert("RGB")
    work = rgb.copy()
    work.thumbnail((640, 640), Image.LANCZOS)
    border = _border_pixels(work)
    bg = tuple(int(statistics.median(p[i] for p in border)) for i in range(3))
    spread = max(statistics.pstdev([p[i] for p in border]) for i in range(3))
    if spread > 38:
        return None  # busy background (a room, a street): not safe to cut automatically
    threshold = int(max(18, min(56, spread * 2.6 + 14)))
    diff = ImageChops.difference(work, Image.new("RGB", work.size, bg))
    r, g, b = diff.split()
    dist = ImageChops.lighter(ImageChops.lighter(r, g), b)
    candidate = dist.point(lambda v: 255 if v < threshold else 0)
    # Background = pixels of the background colour connected to the edges.
    w, h = candidate.size
    px = candidate.load()
    seeds = [(x, 0) for x in range(0, w, 4)] + [(x, h - 1) for x in range(0, w, 4)] + \
            [(0, y) for y in range(0, h, 4)] + [(w - 1, y) for y in range(0, h, 4)]
    for xy in seeds:
        if px[xy] == 255:
            ImageDraw.floodfill(candidate, xy, 128, thresh=0)
    # Enclosed pockets of clearly background colour (inside a mug handle,
    # between a bag's straps) are background too - but only when they are
    # very close to the background colour, so white products stay whole.
    close = dist.point(lambda v: 255 if v < threshold * 0.6 else 0)
    label = 10
    total = w * h
    for y in range(0, h, 5):
        for x in range(0, w, 5):
            if px[x, y] == 255 and close.getpixel((x, y)) == 255 and label < 120:
                before = candidate.histogram()[label]
                ImageDraw.floodfill(candidate, (x, y), label, thresh=0)
                area = candidate.histogram()[label] - before
                if 0.0008 * total <= area <= 0.08 * total:
                    candidate = candidate.point(lambda v, lab=label: 128 if v == lab else v)
                    px = candidate.load()
                label += 1
    mask = candidate.point(lambda v: 0 if v == 128 else 255)
    mask = mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))   # remove specks
    mask = mask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))   # close small holes
    hist = mask.histogram()
    share = sum(hist[128:]) / float(w * h)
    if share < 0.03 or share > 0.97:
        return None
    full = mask.resize(rgb.size, Image.LANCZOS).filter(ImageFilter.GaussianBlur(1.2))
    out = rgb.convert("RGBA")
    out.putalpha(full)
    return out


def removebg_cutout(img):
    """Cut-out with remove.bg (needs REMOVE_BG_API_KEY). None on failure."""
    key = _env("REMOVE_BG_API_KEY")
    if not key:
        return None
    from PIL import Image
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92)
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"size\"\r\n\r\nauto\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image_file\"; filename=\"photo.jpg\"\r\n"
            "Content-Type: image/jpeg\r\n\r\n").encode() + buf.getvalue() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request("https://api.remove.bg/v1.0/removebg", data=body, method="POST",
                                 headers={"X-Api-Key": key, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            out = Image.open(io.BytesIO(resp.read()))
            out.load()
            return out.convert("RGBA")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("errors", [{}])[0].get("title", "")
        except Exception:
            detail = ""
        logger.warning("remove.bg refused the photo: %s %s", exc.code, detail)
    except Exception:
        logger.warning("remove.bg unreachable", exc_info=True)
    return None


# ---------------------------------------------------------------- studio

def _enhance(img):
    """Gentle: a little more light and contrast, a light sharpen. Colours
    must stay true to the product (returns are often 'colour not as shown')."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    rgb = img.convert("RGB")
    rgb = Image.blend(rgb, ImageOps.autocontrast(rgb, cutoff=0.5), 0.3)
    rgb = ImageEnhance.Brightness(rgb).enhance(1.03)
    return rgb.filter(ImageFilter.UnsharpMask(radius=1.4, percent=45, threshold=3))


def compose(cutout, background="white", shadow=True, size=SIZE, padding=0.09):
    """Centres a cut-out on a square studio background."""
    from PIL import Image, ImageDraw, ImageFilter
    color = BACKGROUNDS.get(background, BACKGROUNDS["white"])
    alpha = cutout.getchannel("A")
    box = alpha.point(lambda v: 255 if v > 24 else 0).getbbox()
    if not box:
        return None
    obj = cutout.crop(box)
    inner = int(size * (1 - 2 * padding))
    scale = min(inner / max(obj.size), 2.5)  # fill the frame; never blow a tiny photo up more than 2.5x
    obj = obj.resize((max(int(obj.width * scale), 1), max(int(obj.height * scale), 1)), Image.LANCZOS)
    enhanced = _enhance(obj)
    enhanced.putalpha(obj.getchannel("A"))
    canvas = Image.new("RGB", (size, size), color)
    x = (size - obj.width) // 2
    y = (size - obj.height) // 2
    if shadow:
        layer = Image.new("L", (size, size), 0)
        sw, sh = int(obj.width * 0.78), max(int(obj.height * 0.07), 10)
        sx, sy = (size - sw) // 2, min(y + obj.height - sh // 2, size - sh - 4)
        ImageDraw.Draw(layer).ellipse((sx, sy, sx + sw, sy + sh), fill=90)
        layer = layer.filter(ImageFilter.GaussianBlur(size * 0.018))
        canvas.paste(Image.new("RGB", (size, size), tuple(max(c - 70, 0) for c in color)), (0, 0), layer)
    canvas.paste(enhanced, (x, y), enhanced)
    return canvas


def squared(img, background="white", size=SIZE, padding=0.04):
    """No cut-out possible: fit the enhanced photo on a square canvas."""
    from PIL import Image
    rgb = _enhance(img)
    inner = int(size * (1 - 2 * padding))
    scale = min(inner / max(rgb.size), 2.5)
    rgb = rgb.resize((max(int(rgb.width * scale), 1), max(int(rgb.height * scale), 1)), Image.LANCZOS)
    canvas = Image.new("RGB", (size, size), BACKGROUNDS.get(background, BACKGROUNDS["white"]))
    canvas.paste(rgb, ((size - rgb.width) // 2, (size - rgb.height) // 2))
    return canvas


def process(source, background="white", shadow=True):
    """Returns (jpeg_bytes, method) where method is 'remove.bg', 'built-in'
    or 'enhanced' (no cut-out was possible)."""
    img = load(source)
    method = "enhanced"
    cut = None
    if img.mode == "RGBA" and img.getchannel("A").getextrema()[0] < 250:
        cut, method = img, "transparent"
    if cut is None:
        cut = removebg_cutout(img)
        method = "remove.bg" if cut is not None else method
    if cut is None:
        cut = builtin_cutout(img)
        method = "built-in" if cut is not None else "enhanced"
    result = compose(cut, background, shadow) if cut is not None else None
    if result is None:
        result, method = squared(img, background), "enhanced"
    out = io.BytesIO()
    result.save(out, "JPEG", quality=88, optimize=True, progressive=True)
    return out.getvalue(), method


def save(data):
    """Stores a studio photo; returns its public URL."""
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage
    name = default_storage.save(f"products/studio/{uuid.uuid4().hex}.jpg", ContentFile(data))
    return default_storage.url(name)


def is_studio_url(url):
    from django.core.files.storage import default_storage
    prefix = default_storage.url("products/studio/x.jpg")[:-5]
    return bool(url) and url.startswith(prefix) and ".." not in url
