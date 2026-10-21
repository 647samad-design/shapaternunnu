"""Video links: YouTube (incl. Live and Shorts), Vimeo, Twitch and .mp4."""
import re

YOUTUBE = re.compile(r"https://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/)|youtu\.be/)([A-Za-z0-9_-]{6,20})")
VIMEO = re.compile(r"https://(?:www\.|player\.)?vimeo\.com/(?:video/)?(\d{5,12})")
TWITCH = re.compile(r"https://(?:www\.)?twitch\.tv/([A-Za-z0-9_]{3,25})/?$")


def embed(url, live=False):
    """{"kind": "youtube"|"vimeo"|"twitch"|"file", "src": ...} or None."""
    url = (url or "").strip()
    if not url.startswith("https://"):
        return None
    m = YOUTUBE.match(url)
    if m:
        return {"kind": "youtube", "src": f"https://www.youtube-nocookie.com/embed/{m.group(1)}", "id": m.group(1)}
    m = VIMEO.match(url)
    if m:
        return {"kind": "vimeo", "src": f"https://player.vimeo.com/video/{m.group(1)}"}
    if live:
        m = TWITCH.match(url)
        if m:
            return {"kind": "twitch", "channel": m.group(1)}
    if re.search(r"\.(mp4|webm)(\?.*)?$", url, re.I):
        return {"kind": "file", "src": url}
    return None
