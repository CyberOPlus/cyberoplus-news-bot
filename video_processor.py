#!/usr/bin/env python3
"""Resolve Telegram videos, validate them, and add Cybero Plus branding."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


TIMEOUT = 35
# Telegram downloads are streamed to disk, never buffered in RAM. The higher
# ceiling allows long, high-quality licensed videos while retaining a hard stop.
MAX_VIDEO_BYTES = int(os.environ.get("MAX_TELEGRAM_VIDEO_BYTES", str(2 * 1024 * 1024 * 1024)))
PROCESS_TIMEOUT_FLOOR = int(os.environ.get("VIDEO_PROCESS_TIMEOUT_FLOOR_SECONDS", "420"))
PROCESS_TIMEOUT_CAP = int(os.environ.get("VIDEO_PROCESS_TIMEOUT_CAP_SECONDS", "3000"))
VIDEO_CRF = int(os.environ.get("VIDEO_CRF", "18"))
VIDEO_PRESET = os.environ.get("VIDEO_PRESET", "veryfast").strip() or "veryfast"
BRAND_TEXT = "www.CyberoPlus.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class VideoAsset:
    path: Path
    source_url: str
    filename: str
    mime: str
    width: int
    height: int
    duration: float
    size: int
    branded: bool = True


def _allowed_video_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return (
        host == "telegram-cdn.org"
        or host.endswith(".telegram-cdn.org")
        or host == "telesco.pe"
        or host.endswith(".telesco.pe")
    )


def resolve_telegram_video_urls(
    known_urls: list[str],
    telegram_post_url: str,
) -> list[str]:
    """Prefer a fresh Telegram CDN URL, then stored candidates."""
    urls: list[str] = []

    def add(value: str) -> None:
        value = str(value or "").strip().replace("&amp;", "&")
        if _allowed_video_url(value) and value not in urls:
            urls.append(value)

    if telegram_post_url:
        try:
            response = requests.get(
                telegram_post_url,
                params={"embed": "1", "single": "1"},
                headers=HEADERS,
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            for video in soup.select(
                ".tgme_widget_message_video_player video[src], "
                ".tgme_widget_message_video_wrap video[src], "
                "video.tgme_widget_message_video[src]"
            ):
                add(str(video.get("src", "")))
        except Exception:
            pass

    for url in known_urls:
        add(url)

    return urls


def download_video(url: str, destination: Path) -> Path:
    if not _allowed_video_url(url):
        raise RuntimeError("Rejected non-Telegram video URL.")

    headers = dict(HEADERS)
    headers["Referer"] = "https://t.me/"
    total = 0
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        with requests.get(
            url,
            headers=headers,
            timeout=(TIMEOUT, 90),
            stream=True,
            allow_redirects=True,
        ) as response:
            response.raise_for_status()
            if not _allowed_video_url(response.url):
                raise RuntimeError("Telegram video redirected to an untrusted host.")

            content_type = (
                response.headers.get("content-type") or ""
            ).split(";", 1)[0].lower()
            if content_type and not (
                content_type.startswith("video/")
                or content_type in {"application/octet-stream", "binary/octet-stream"}
            ):
                raise RuntimeError(f"Unexpected video content type: {content_type}")

            declared = response.headers.get("content-length", "").strip()
            if declared:
                try:
                    if int(declared) > MAX_VIDEO_BYTES:
                        raise RuntimeError("Telegram video exceeds configured size limit.")
                except ValueError:
                    pass

            with destination.open("wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_VIDEO_BYTES:
                        raise RuntimeError("Telegram video exceeds configured size limit.")
                    handle.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    if total < 32_000:
        destination.unlink(missing_ok=True)
        raise RuntimeError("Downloaded Telegram video is unexpectedly small.")
    return destination


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )


def probe_video(path: Path) -> dict[str, float | int | str]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed on this runner.")

    result = _run(
        [
            ffprobe,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name,duration",
            "-show_entries", "format=duration,size",
            "-of", "json",
            str(path),
        ],
        timeout=45,
    )
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError("Video has no readable video stream.")

    stream = streams[0]
    fmt = payload.get("format") or {}
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)

    try:
        duration = float(stream.get("duration") or fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0

    if width < 160 or height < 160:
        raise RuntimeError(f"Video dimensions are too small: {width}x{height}")
    if duration <= 0:
        raise RuntimeError("Video duration could not be validated.")

    return {
        "width": width,
        "height": height,
        "duration": duration,
        "codec": str(stream.get("codec_name") or ""),
        "size": int(fmt.get("size") or path.stat().st_size),
    }


def probe_audio_codec(path: Path) -> str:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed on this runner.")

    result = _run(
        [
            ffprobe,
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout=45,
    )
    return (result.stdout or "").strip().lower()


def _processing_timeout(duration: float) -> int:
    """Scale CPU budget with video length while keeping a hard workflow-safe cap."""
    calculated = int(max(0.0, duration) * 1.8 + 180)
    return min(PROCESS_TIMEOUT_CAP, max(PROCESS_TIMEOUT_FLOOR, calculated))


def _font_file() -> str:
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    )
    for path in candidates:
        if Path(path).exists():
            return path
    return ""


def brand_video(
    input_path: Path,
    output_path: Path,
    *,
    clip_start_seconds: float | None = None,
    max_clip_seconds: float | None = None,
) -> VideoAsset:
    """Add the site watermark with one high-quality encode and no resizing/FPS change."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed on this runner.")

    info = probe_video(input_path)
    width = int(info["width"])
    height = int(info["height"])
    source_duration = float(info["duration"])
    font_size = max(22, min(48, round(min(width, height) / 28)))
    font_file = _font_file()
    audio_codec = probe_audio_codec(input_path)

    drawtext = []
    if font_file:
        drawtext.append(f"fontfile={font_file}")
    drawtext.extend(
        [
            f"text={BRAND_TEXT}",
            "fontcolor=white",
            f"fontsize={font_size}",
            "box=1",
            "boxcolor=black@0.48",
            "boxborderw=10",
            "x=w-text_w-22",
            "y=h-text_h-22",
        ]
    )

    start = max(0.0, float(clip_start_seconds or 0.0))
    available_duration = max(0.1, source_duration - start)
    if max_clip_seconds is not None and float(max_clip_seconds) > 0:
        effective_duration = min(available_duration, float(max_clip_seconds))
    else:
        effective_duration = available_duration

    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
    ]
    if start > 0:
        command.extend(["-ss", f"{start:.3f}"])
    command.extend(
        [
            "-i", str(input_path),
            "-map", "0:v:0",
            "-map", "0:a?",
        ]
    )
    if max_clip_seconds is not None and float(max_clip_seconds) > 0:
        command.extend(["-t", f"{effective_duration:.3f}"])

    command.extend(
        [
            "-vf", "drawtext=" + ":".join(drawtext),
            "-c:v", "libx264",
            "-preset", VIDEO_PRESET,
            "-crf", str(VIDEO_CRF),
            "-pix_fmt", "yuv420p",
        ]
    )

    # Preserve AAC audio bit-for-bit when possible. Only transcode audio when
    # the source codec is not MP4/Facebook-friendly.
    if audio_codec == "aac":
        command.extend(["-c:a", "copy"])
    else:
        command.extend(["-c:a", "aac", "-b:a", "192k"])

    command.extend(
        [
            "-movflags", "+faststart",
            "-map_metadata", "-1",
            str(output_path),
        ]
    )

    try:
        _run(command, timeout=_processing_timeout(effective_duration))
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "")[-1200:]
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg watermarking failed: {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"FFmpeg watermarking timed out for a {effective_duration:.0f}s video."
        ) from exc

    if not output_path.exists() or output_path.stat().st_size < 32_000:
        raise RuntimeError("FFmpeg did not create a usable branded video.")

    branded = probe_video(output_path)
    return VideoAsset(
        path=output_path,
        source_url="",
        filename="cyberoplus-video.mp4",
        mime="video/mp4",
        width=int(branded["width"]),
        height=int(branded["height"]),
        duration=float(branded["duration"]),
        size=output_path.stat().st_size,
        branded=True,
    )

def prepare_branded_video(
    known_urls: list[str],
    telegram_post_url: str,
    workdir: Path,
    *,
    clip_start_seconds: float | None = None,
    max_clip_seconds: float | None = None,
) -> VideoAsset:
    candidates = resolve_telegram_video_urls(known_urls, telegram_post_url)
    if not candidates:
        raise RuntimeError("Telegram exposed a video player but no downloadable video URL.")

    errors: list[str] = []
    for index, url in enumerate(candidates[:4]):
        input_path = workdir / f"telegram-video-{index}.mp4"
        output_path = workdir / f"cyberoplus-video-{index}.mp4"
        try:
            download_video(url, input_path)
            asset = brand_video(
                input_path,
                output_path,
                clip_start_seconds=clip_start_seconds,
                max_clip_seconds=max_clip_seconds,
            )
            asset.source_url = url
            return asset
        except Exception as exc:
            errors.append(str(exc))
            input_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)

    raise RuntimeError(" | ".join(errors[-4:]) or "No Telegram video candidate succeeded.")
