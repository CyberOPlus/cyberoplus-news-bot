#!/usr/bin/env python3
"""High-quality image selection for Cybero Plus Facebook posts.

Priority:
1) Real image(s) attached to the Telegram post.
2) If Telegram has no usable image, fetch the original external source and
   choose the highest-quality article/OG image.

Images are downloaded and validated before Facebook upload so broken, tiny,
logo/icon/avatar and tracking images are filtered out.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps


TIMEOUT = 25
MAX_DOWNLOAD_BYTES = 15 * 1024 * 1024
MIN_WIDTH = 640
MIN_HEIGHT = 320
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

BAD_IMAGE_HINTS = (
    "logo", "icon", "avatar", "favicon", "sprite", "emoji", "badge",
    "tracking", "pixel", "spacer", "placeholder", "loader", "adsystem",
    "doubleclick", "gravatar",
)


@dataclass
class ImageAsset:
    url: str
    content: bytes
    mime: str
    width: int
    height: int
    filename: str
    origin: str
    score: float


def _clean_url(url: str, base: str = "") -> str:
    url = (url or "").strip().replace("&amp;", "&")
    if not url:
        return ""
    return urljoin(base, url)


def _bad_url(url: str) -> bool:
    lowered = url.lower()
    return any(hint in lowered for hint in BAD_IMAGE_HINTS)


def _filename(url: str, mime: str) -> str:
    name = urlparse(url).path.rsplit("/", 1)[-1].split("?", 1)[0].strip()
    if not name or "." not in name:
        ext = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(mime, ".jpg")
        return "cyberoplus-image" + ext
    return name[-120:]


def _image_score(width: int, height: int, size: int, origin: str) -> float:
    area = width * height
    ratio = width / max(height, 1)

    score = float(area)
    if width >= 1200:
        score *= 1.35
    if width >= 1600:
        score *= 1.15
    if 1.2 <= ratio <= 2.1:
        score *= 1.18
    if size >= 120_000:
        score *= 1.08
    if origin == "telegram":
        score *= 1.25
    return score


def fetch_image(url: str, origin: str, referer: str = "") -> ImageAsset | None:
    url = _clean_url(url)
    if not url or _bad_url(url):
        return None

    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer

    try:
        with requests.get(url, headers=headers, timeout=TIMEOUT, stream=True) as response:
            response.raise_for_status()
            content_type = (response.headers.get("content-type") or "").split(";", 1)[0].lower()
            if content_type and not content_type.startswith("image/"):
                return None

            chunks = []
            total = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    return None
                chunks.append(chunk)

        content = b"".join(chunks)
        if len(content) < 12_000:
            return None

        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            detected = Image.MIME.get(image.format or "", "") or content_type or "image/jpeg"

        if width < MIN_WIDTH or height < MIN_HEIGHT:
            return None

        ratio = width / max(height, 1)
        if ratio < 0.35 or ratio > 3.5:
            return None

        return ImageAsset(
            url=url,
            content=content,
            mime=detected,
            width=width,
            height=height,
            filename=_filename(url, detected),
            origin=origin,
            score=_image_score(width, height, len(content), origin),
        )
    except Exception:
        return None


def _srcset_best(srcset: str, base: str) -> list[str]:
    candidates: list[tuple[int, str]] = []
    for part in (srcset or "").split(","):
        bits = part.strip().split()
        if not bits:
            continue
        url = _clean_url(bits[0], base)
        width = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                width = int(bits[1][:-1])
            except ValueError:
                pass
        candidates.append((width, url))
    return [url for _, url in sorted(candidates, reverse=True) if url]


def _jsonld_images(value: Any, base: str) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"image", "thumbnailUrl", "contentUrl"}:
                if isinstance(child, str):
                    found.append(_clean_url(child, base))
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, str):
                            found.append(_clean_url(item, base))
                        elif isinstance(item, dict):
                            found.extend(_jsonld_images(item, base))
                elif isinstance(child, dict):
                    found.extend(_jsonld_images(child, base))
            else:
                found.extend(_jsonld_images(child, base))
    elif isinstance(value, list):
        for item in value:
            found.extend(_jsonld_images(item, base))
    return found


def source_image_candidates(source_url: str) -> list[str]:
    if not source_url:
        return []

    try:
        response = requests.get(source_url, headers=HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    base = response.url
    candidates: list[str] = []

    def add(url: str) -> None:
        clean = _clean_url(url, base)
        if clean and clean.startswith(("http://", "https://")) and not _bad_url(clean):
            if clean not in candidates:
                candidates.append(clean)

    # Highest-confidence article hero metadata first.
    for selector, attr in (
        ('meta[property="og:image:secure_url"]', "content"),
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        ('meta[name="twitter:image:src"]', "content"),
        ('link[rel="image_src"]', "href"),
    ):
        for node in soup.select(selector):
            add(str(node.get(attr, "")))

    # Structured data often contains a larger original image than og:image.
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or "")
        except Exception:
            continue
        for url in _jsonld_images(data, base):
            add(url)

    # Then article/body images, preferring the largest srcset variants.
    selectors = (
        "article img", "main img", ".article img", ".post img", ".entry-content img",
        ".content img", "figure img",
    )
    for selector in selectors:
        for image in soup.select(selector):
            for url in _srcset_best(str(image.get("srcset", "")), base):
                add(url)
            add(str(image.get("data-src", "")))
            add(str(image.get("data-lazy-src", "")))
            add(str(image.get("src", "")))

    return candidates[:30]


def resolve_post_images(
    telegram_urls: list[str],
    source_url: str,
    max_images: int = 10,
) -> tuple[list[ImageAsset], dict[str, Any]]:
    """Return validated high-quality images plus diagnostics."""

    diagnostics: dict[str, Any] = {
        "telegram_candidates": len(telegram_urls),
        "source_fallback_used": False,
        "source_candidates": 0,
        "selected": [],
    }

    telegram_assets: list[ImageAsset] = []
    for url in telegram_urls[:max_images * 2]:
        asset = fetch_image(url, "telegram")
        if asset:
            telegram_assets.append(asset)

    # Respect the Telegram post's own visuals when they exist and are usable.
    if telegram_assets:
        telegram_assets.sort(key=lambda asset: asset.score, reverse=True)
        chosen = telegram_assets[:max_images]
        diagnostics["selected"] = [
            {
                "origin": a.origin,
                "width": a.width,
                "height": a.height,
                "url": a.url,
            }
            for a in chosen
        ]
        return chosen, diagnostics

    # No usable Telegram image: inspect the original external source.
    if source_url:
        diagnostics["source_fallback_used"] = True
        candidates = source_image_candidates(source_url)
        diagnostics["source_candidates"] = len(candidates)

        assets: list[ImageAsset] = []
        for url in candidates[:20]:
            asset = fetch_image(url, "source", referer=source_url)
            if asset:
                assets.append(asset)

        if assets:
            # Only one source image: choose the best article hero, not a gallery dump.
            best = max(assets, key=lambda asset: asset.score)
            diagnostics["selected"] = [{
                "origin": best.origin,
                "width": best.width,
                "height": best.height,
                "url": best.url,
            }]
            return [best], diagnostics

    return [], diagnostics


def normalize_for_facebook(asset: ImageAsset) -> ImageAsset:
    """Preserve original quality whenever possible; normalize unsupported/huge images."""

    if asset.mime in {"image/jpeg", "image/png"} and len(asset.content) <= 10 * 1024 * 1024:
        return asset

    with Image.open(io.BytesIO(asset.content)) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode not in {"RGB", "L"}:
            background = Image.new("RGB", image.size, "white")
            if "A" in image.getbands():
                background.paste(image, mask=image.getchannel("A"))
            else:
                background.paste(image)
            image = background
        else:
            image = image.convert("RGB")

        # Never upscale. Only tame exceptionally large assets.
        if image.width > 3000:
            new_height = round(image.height * (3000 / image.width))
            image = image.resize((3000, new_height), Image.Resampling.LANCZOS)

        output = io.BytesIO()
        image.save(output, format="JPEG", quality=95, optimize=True, subsampling=0)
        content = output.getvalue()

    return ImageAsset(
        url=asset.url,
        content=content,
        mime="image/jpeg",
        width=image.width,
        height=image.height,
        filename="cyberoplus-high-quality.jpg",
        origin=asset.origin,
        score=asset.score,
    )
