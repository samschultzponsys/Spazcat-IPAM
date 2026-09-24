"""
Logo + icon branding for Spazcat IPAM.

- logo_style: how the top-left wordmark (and login page brand) is painted:
  solid / gradient / outline fill, an optional pattern overlay and a glow.
  Stored as JSON in settings; the browser turns it into CSS (static/brand.js).
- icon_style: colors/shape of the stock grid icon, rendered here as SVG (for
  the favicon) and PNG (home-screen / manifest icons, via Pillow).
- A custom uploaded icon replaces the stock one. Raster uploads are decoded
  and re-encoded as PNG (nothing from the original file is served as-is);
  SVG uploads are checked for scripts and served with a CSP that blocks them.

Every icon URL carries ?v=<icon_version>, bumped on each change, so browsers
and reverse-proxy caches pick up a new icon immediately.
"""

import io
import json
import math
import os
import re

from flask import Response, jsonify, request

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
FILLS = ("gradient", "solid", "outline")
PATTERNS = ("none", "stripes", "diagonal", "dots", "checker", "grid")
SHAPES = ("rounded", "circle", "square")

LOGO_DEFAULT = {
    "fill": "gradient",
    "colors": ["#8b5cf6", "#f5a142"],
    "angle": 90,
    "pattern": "none",
    "pattern_color": "#ffffff",
    "pattern_opacity": 35,
    "pattern_size": 6,
    "outline_width": 2,
    "glow": False,
}
ICON_DEFAULT = {
    "fill": "gradient",
    "colors": ["#8b5cf6", "#f5a142"],
    "angle": 135,
    "tile_color": "#ffffff",
    "shape": "rounded",
}

# name -> (px, full-bleed square, grid scale)
PNG_ICONS = {
    "apple-touch-icon": (180, True, 0.9),
    "icon-192": (192, False, 1.0),
    "icon-512": (512, False, 1.0),
    "icon-maskable-512": (512, True, 0.7),
    "favicon-32": (32, False, 1.0),
}
MAX_UPLOAD = 2 * 1024 * 1024
MAX_SVG = 512 * 1024

_cache = {}   # (name, version) -> bytes


def _int(v, lo, hi, dflt):
    try:
        return max(lo, min(hi, int(float(v))))
    except (TypeError, ValueError):
        return dflt


def _hex(v, dflt):
    return v if isinstance(v, str) and HEX.match(v) else dflt


def clean_logo(raw):
    raw = raw if isinstance(raw, dict) else {}
    d = LOGO_DEFAULT
    colors = [c for c in (raw.get("colors") or []) if isinstance(c, str) and HEX.match(c)][:3]
    return {
        "fill": raw.get("fill") if raw.get("fill") in FILLS else d["fill"],
        "colors": colors or list(d["colors"]),
        "angle": _int(raw.get("angle", d["angle"]), 0, 360, d["angle"]),
        "pattern": raw.get("pattern") if raw.get("pattern") in PATTERNS else d["pattern"],
        "pattern_color": _hex(raw.get("pattern_color"), d["pattern_color"]),
        "pattern_opacity": _int(raw.get("pattern_opacity", d["pattern_opacity"]), 5, 100, d["pattern_opacity"]),
        "pattern_size": _int(raw.get("pattern_size", d["pattern_size"]), 2, 30, d["pattern_size"]),
        "outline_width": _int(raw.get("outline_width", d["outline_width"]), 1, 6, d["outline_width"]),
        "glow": bool(raw.get("glow", d["glow"])),
    }


def clean_icon(raw):
    raw = raw if isinstance(raw, dict) else {}
    d = ICON_DEFAULT
    colors = [c for c in (raw.get("colors") or []) if isinstance(c, str) and HEX.match(c)][:2]
    return {
        "fill": raw.get("fill") if raw.get("fill") in ("gradient", "solid") else d["fill"],
        "colors": colors or list(d["colors"]),
        "angle": _int(raw.get("angle", d["angle"]), 0, 360, d["angle"]),
        "tile_color": _hex(raw.get("tile_color"), d["tile_color"]),
        "shape": raw.get("shape") if raw.get("shape") in SHAPES else d["shape"],
    }


def load_json(text, cleaner):
    try:
        return cleaner(json.loads(text or "{}"))
    except ValueError:
        return cleaner({})


# ----------------------------------------------------------------------------
# Stock icon rendering
# ----------------------------------------------------------------------------

TILES = [(x, y, i % 2 == 0) for i, (x, y) in enumerate(
    (x, y) for y in (6.5, 13.5, 20.5) for x in (6.5, 13.5, 20.5))]   # (x, y, solid?)


def _grad_vec(angle, half):
    """Half gradient vector for a CSS-style angle (0 = to top, 90 = to right)
    over a square of side 2*half: the line spans corner to corner like CSS."""
    rad = math.radians(angle)
    sin, cos = math.sin(rad), math.cos(rad)
    length = half * (abs(sin) + abs(cos))
    return length * sin, -length * cos


def icon_svg(st, full=False, grid_scale=1.0):
    c1 = st["colors"][0]
    c2 = st["colors"][-1] if st["fill"] == "gradient" else c1
    # CSS angle convention: 0 = to top, 90 = to right
    dx, dy = _grad_vec(st["angle"], 16)
    shape = "square" if full else st["shape"]
    if shape == "circle":
        bg = '<circle cx="16" cy="16" r="16" fill="url(#g)"/>'
    else:
        rx = 0 if shape == "square" else 7
        bg = f'<rect width="32" height="32" rx="{rx}" fill="url(#g)"/>'
    # a circle loses its corners, so pull the grid in a little
    s = grid_scale * (0.85 if shape == "circle" else 1.0)
    t = 16 * (1 - s)
    faded = ' opacity="0.4"'
    tiles = "".join(
        f'<rect x="{x}" y="{y}" width="5.5" height="5.5" rx="1.2"{"" if solid else faded}/>'
        for x, y, solid in TILES)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">'
        f'<defs><linearGradient id="g" gradientUnits="userSpaceOnUse" '
        f'x1="{16 - dx:.3f}" y1="{16 - dy:.3f}" x2="{16 + dx:.3f}" y2="{16 + dy:.3f}">'
        f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/>'
        '</linearGradient></defs>'
        f'{bg}<g fill="{st["tile_color"]}" transform="translate({t:.3f} {t:.3f}) scale({s:.3f})">{tiles}</g></svg>'
    )


def _rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def icon_png(st, size, full, grid_scale):
    """Pillow rendering of the same design as icon_svg (4x supersampled)."""
    from PIL import Image, ImageDraw

    S = size * 4
    u = S / 32.0
    c1 = _rgb(st["colors"][0])
    c2 = _rgb(st["colors"][-1] if st["fill"] == "gradient" else st["colors"][0])

    # gradient along the chosen angle, computed on a small grid and scaled up
    n = 64
    gx, gy = _grad_vec(st["angle"], n / 2)
    L2 = gx * gx + gy * gy
    ramp = Image.new("L", (n, n))
    ramp.putdata([
        max(0, min(255, int(round(255 * (0.5 + ((x + 0.5 - n / 2) * gx + (y + 0.5 - n / 2) * gy) / (2 * L2))))))
        for y in range(n) for x in range(n)])
    ramp = ramp.resize((S, S), Image.BILINEAR)
    bg = Image.composite(Image.new("RGBA", (S, S), c2 + (255,)),
                         Image.new("RGBA", (S, S), c1 + (255,)), ramp)

    shape = "square" if full else st["shape"]
    mask = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(mask)
    if shape == "circle":
        md.ellipse((0, 0, S - 1, S - 1), fill=255)
    elif shape == "rounded":
        md.rounded_rectangle((0, 0, S - 1, S - 1), radius=int(7 * u), fill=255)
    else:
        md.rectangle((0, 0, S, S), fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(bg, (0, 0), mask)

    s = grid_scale * (0.85 if shape == "circle" else 1.0)
    t = 16 * (1 - s)
    tile = _rgb(st["tile_color"])
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for x, y, solid in TILES:
        x0, y0 = (t + x * s) * u, (t + y * s) * u
        w = 5.5 * s * u
        ld.rounded_rectangle((x0, y0, x0 + w, y0 + w), radius=1.2 * s * u,
                             fill=tile + ((255,) if solid else (102,)))
    out = Image.alpha_composite(out, layer)
    if full:  # home-screen icons: no transparent corners
        flat = Image.new("RGBA", (S, S), c1 + (255,))
        out = Image.alpha_composite(flat, out)
    out = out.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ----------------------------------------------------------------------------
# Uploads
# ----------------------------------------------------------------------------

_SVG_BAD = re.compile(
    r"<\s*(script|foreignobject|iframe|embed|object|audio|video|link|meta)\b"
    r"|\bon[a-z]+\s*="
    r"|javascript:|data:text/html"
    r"|(?:xlink:)?href\s*=\s*['\"]\s*(?!#)", re.I)


def _square(img, size):
    from PIL import Image
    img = img.convert("RGBA")
    w, h = img.size
    side = max(w, h)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - w) // 2, (side - h) // 2))
    return canvas.resize((size, size), Image.LANCZOS)


def save_upload(branding_dir, data, filename):
    """Validate + store an uploaded icon. Returns (kind, error)."""
    if len(data) > MAX_UPLOAD:
        return None, "File is larger than 2 MB"
    os.makedirs(branding_dir, exist_ok=True)
    head = data[:512].lstrip().lower()
    if filename.lower().endswith(".svg") or head.startswith(b"<?xml") or head.startswith(b"<svg"):
        if len(data) > MAX_SVG:
            return None, "SVG icons must be under 512 KB"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None, "SVG must be UTF-8 text"
        if "<svg" not in text.lower():
            return None, "Not an SVG file"
        if _SVG_BAD.search(text):
            return None, "SVG contains scripts, links or embedded content - export a plain SVG"
        _clear(branding_dir)
        with open(os.path.join(branding_dir, "icon.svg"), "w", encoding="utf-8") as fh:
            fh.write(text)
        return "svg", None
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if getattr(img, "n_frames", 1) > 1:        # ICO / animated: take the largest frame
            best = max(range(img.n_frames), key=lambda i: (img.seek(i), img.size[0] * img.size[1])[1])
            img.seek(best)
        img.load()
    except Exception:
        return None, "Unsupported image - use PNG, JPG, WebP, GIF, ICO or SVG"
    if min(img.size) < 16:
        return None, "Image is too small (at least 16×16)"
    _clear(branding_dir)
    _square(img, 512).save(os.path.join(branding_dir, "icon.png"), "PNG", optimize=True)
    return "png", None


def _clear(branding_dir):
    for f in ("icon.png", "icon.svg"):
        try:
            os.remove(os.path.join(branding_dir, f))
        except FileNotFoundError:
            pass


def custom_kind(branding_dir):
    if os.path.isfile(os.path.join(branding_dir, "icon.png")):
        return "png"
    if os.path.isfile(os.path.join(branding_dir, "icon.svg")):
        return "svg"
    return ""


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------

def register(app, get_db, get_setting, set_setting, branding_dir):

    def state(db):
        return (load_json(get_setting(db, "icon_style", "{}"), clean_icon),
                custom_kind(branding_dir),
                get_setting(db, "icon_version", "1") or "1")

    def cached(name, version, make):
        key = (name, version)
        if key not in _cache:
            if len(_cache) > 64:
                _cache.clear()
            _cache[key] = make()
        return _cache[key]

    def send(body, mimetype, extra=None):
        # revalidate every time; ?v= in the URL changes whenever the icon does
        headers = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}
        headers.update(extra or {})
        return Response(body, mimetype=mimetype, headers=headers)

    @app.route("/icon/favicon")
    @app.route("/favicon.ico")
    def icon_favicon():
        st, kind, ver = state(get_db())
        if kind == "svg":
            with open(os.path.join(branding_dir, "icon.svg"), "rb") as fh:
                return send(fh.read(), "image/svg+xml",
                            {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
        if kind == "png":
            return send(cached("fav-custom", ver, lambda: _custom_png(branding_dir, 64)), "image/png")
        return send(icon_svg(st), "image/svg+xml",
                    {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

    @app.route("/icon/<name>.png")
    def icon_png_route(name):
        if name not in PNG_ICONS:
            return Response("not found", status=404)
        st, kind, ver = state(get_db())
        size, full, scale = PNG_ICONS[name]
        if kind == "png":
            body = cached(f"{name}-custom", ver, lambda: _custom_png(branding_dir, size, full))
        else:  # stock (also the fallback for SVG uploads, which we don't rasterize)
            body = cached(name, ver, lambda: icon_png(st, size, full, scale))
        return send(body, "image/png")

    @app.route("/api/branding/icon", methods=["POST", "DELETE"])
    def api_icon_upload():
        db = get_db()
        if request.method == "DELETE":
            _clear(branding_dir)
        else:
            if request.content_length and request.content_length > MAX_UPLOAD + 4096:
                return jsonify({"ok": False, "error": "File is larger than 2 MB"}), 413
            f = request.files.get("file")
            if not f:
                return jsonify({"ok": False, "error": "No file received"}), 400
            kind, err = save_upload(branding_dir, f.read(MAX_UPLOAD + 1), f.filename or "")
            if err:
                return jsonify({"ok": False, "error": err}), 400
        bump_version(db)
        return jsonify({"ok": True, "custom": custom_kind(branding_dir)})

    def bump_version(db):
        try:
            v = int(get_setting(db, "icon_version", "1") or 1) + 1
        except ValueError:
            v = 2
        set_setting(db, "icon_version", v)
        db.commit()

    app.config["BRANDING_BUMP"] = bump_version


def _custom_png(branding_dir, size, full=False):
    from PIL import Image
    img = Image.open(os.path.join(branding_dir, "icon.png")).convert("RGBA")
    img = img.resize((size, size), Image.LANCZOS)
    if full:  # iOS/Android fill transparency with black - use a neutral dark instead
        bg = Image.new("RGBA", img.size, (10, 10, 12, 255))
        img = Image.alpha_composite(bg, img)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
