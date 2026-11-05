"""Generate the PERMANENT attendance QR code (identifies the website, never an employee).

Usage:
  python scripts/generate_qr.py                      # uses FRONTEND_URL from .env
  python scripts/generate_qr.py --url https://attendance.example.com --out storage/qr/entrance.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import qrcode  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from qrcode.constants import ERROR_CORRECT_H  # noqa: E402


def make_qr(url: str, out: Path, caption: bool = True) -> Path:
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_H, box_size=40, border=4)  # ~2000px+, survives wear/glare
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    if caption:
        w, h = img.size
        canvas = Image.new("RGB", (w, h + 360), "white")
        canvas.paste(img, (0, 0))
        d = ImageDraw.Draw(canvas)
        try:
            big, small = ImageFont.truetype("arialbd.ttf", 110), ImageFont.truetype("arial.ttf", 60)
        except OSError:
            big = small = ImageFont.load_default()
        for text, y, font in (("Scan to mark attendance", h + 30, big), ("GWC Data.AI", h + 170, small),
                              (url, h + 260, small)):
            tw = d.textlength(text, font=font)
            d.text(((w - tw) / 2, y), text, fill="black", font=font)
        img = canvas
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, dpi=(300, 300))
    return out


def main() -> int:
    from app.config import get_settings

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", help="Frontend website URL (default: FRONTEND_URL from environment/.env)")
    p.add_argument("--out", default=str(ROOT / "storage" / "qr" / "attendance_qr.png"))
    p.add_argument("--no-caption", action="store_true")
    p.add_argument("--allow-http", action="store_true", help="allow non-HTTPS URL (testing only)")
    a = p.parse_args()
    url = (a.url or get_settings().frontend_url).strip()   # the QR opens the FRONTEND (the page employees use)
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        print("ERROR: URL must be absolute http(s)://...", file=sys.stderr)
        return 2
    if u.scheme != "https" and not a.allow_http and u.hostname not in ("localhost", "127.0.0.1"):
        print("ERROR: production QR codes must use https:// (use --allow-http to override)", file=sys.stderr)
        return 2
    if u.query or u.fragment:
        print("ERROR: URL must not carry query/fragment data (the QR must be identical for everyone)", file=sys.stderr)
        return 2
    path = make_qr(url, Path(a.out), caption=not a.no_caption)
    print(f"QR code for {url}\nSaved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
