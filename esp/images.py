"""Shared image helpers: download, SVG detection, SVG→PNG conversion."""
import re
import httpx
import cairosvg


def is_svg(content: bytes, content_type: str = "") -> bool:
    if "svg" in content_type.lower():
        return True
    head = content[:512].lstrip().lower()
    return (head.startswith(b"<?xml") and b"<svg" in head[:512]) or head.startswith(b"<svg")


def svg_to_png(svg_bytes: bytes, scale: int = 4) -> bytes:
    """
    Render SVG to PNG. Upsamples small icons (scale) for crisp display.
    Preprocesses Figma quirks: CSS var() (cairosvg can't parse) and % dimensions.
    """
    text = svg_bytes.decode("utf-8", errors="ignore")
    text = re.sub(r'var\(\s*--[^,)]+,\s*([^)]+)\)', r'\1', text)

    output_width = output_height = None
    vb = re.search(r'viewBox="([\d.\s-]+)"', text)
    if vb:
        parts = vb.group(1).split()
        if len(parts) == 4:
            output_width = max(1, int(float(parts[2]))) * scale
            output_height = max(1, int(float(parts[3]))) * scale

    kwargs = {"bytestring": text.encode("utf-8")}
    if output_width and output_height:
        kwargs["output_width"] = output_width
        kwargs["output_height"] = output_height
    else:
        kwargs["scale"] = scale
    return cairosvg.svg2png(**kwargs)


def fetch_and_prepare(url: str) -> tuple[bytes, str, str]:
    """
    Download an image URL. If SVG, convert to PNG.
    Returns (image_bytes, extension, content_type).
    """
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        img = r.content
        ct = r.headers.get("content-type", "")

    if is_svg(img, ct):
        img = svg_to_png(img)
        return img, "png", "image/png"
    if "jpeg" in ct or "jpg" in ct:
        return img, "jpg", "image/jpeg"
    if "gif" in ct:
        return img, "gif", "image/gif"
    return img, "png", "image/png"
