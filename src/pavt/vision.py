"""Optional cloud-vision OCR fallback.

This is the *third* extraction route and is entirely opt-in: the tool is fully
functional offline, and this module degrades to a no-op explanation when no
credentials are configured.

It exists because a vision-capable LLM reads unfamiliar invoice layouts -- rotated
scans, unusual languages, stamps -- far more robustly than a fixed OCR pipeline.
Calling it only as a fallback keeps the common path free, fast and offline.

Configuration (environment variables)
-------------------------------------
``OPENAI_API_KEY``      enables the OpenAI vision route (``gpt-4o-mini`` default)
``ANTHROPIC_API_KEY``   enables the Anthropic route (``claude-3-5-sonnet`` default)
``PAVT_VISION_MODEL``   overrides the model name
``PAVT_OCR_MODE``       ``vision`` forces OCR documents through a vision model first
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

_PROMPT = (
    "You are an accounts-payable OCR engine. Transcribe ALL text visible on this "
    "invoice page, preserving the reading order and keeping each table row on its "
    "own line. Output plain text only -- no commentary, no markdown fences. Keep "
    "every number exactly as printed, including thousands separators and currency "
    "symbols. Retain Chinese characters as Chinese."
)

_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def vision_enabled() -> bool:
    """True when some cloud-vision provider is configured."""
    return bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))


def should_prefer_vision() -> bool:
    """True when ``PAVT_OCR_MODE=vision`` asks for vision ahead of local OCR."""
    return os.environ.get("PAVT_OCR_MODE", "").strip().lower() == "vision"


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 120) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _openai_text(image_bytes: bytes, model: str) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    },
                ],
            }
        ],
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
    body = _post_json(_OPENAI_URL, payload, headers)
    return body["choices"][0]["message"]["content"] or ""


def _anthropic_text(image_bytes: bytes, model: str) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": model,
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": encoded,
                        },
                    },
                    {"type": "text", "text": _PROMPT},
                ],
            }
        ],
    }
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
    }
    body = _post_json(_ANTHROPIC_URL, payload, headers)
    return "".join(
        block.get("text", "")
        for block in body.get("content", [])
        if block.get("type") == "text"
    )


def vision_ocr(page_images) -> tuple[str, str]:
    """Transcribe page images with a cloud vision model.

    Args:
        page_images: PNG bytes per page, or raw PDF bytes when rasterisation failed.

    Returns:
        ``(text, warning)``.  ``text`` is empty when vision is unavailable or the
        call failed, and ``warning`` explains why in operator-readable language.
    """
    if not vision_enabled():
        return "", (
            "No OCR engine produced text and no vision fallback is configured "
            "(set OPENAI_API_KEY or ANTHROPIC_API_KEY to enable one)."
        )

    if isinstance(page_images, (bytes, bytearray)):
        try:
            from .extraction import render_pdf_pages

            images = render_pdf_pages(bytes(page_images))
        except Exception as exc:  # noqa: BLE001
            return "", f"Vision fallback could not rasterise the PDF: {exc}"
    else:
        images = [bytes(image) for image in (page_images or [])]

    if not images:
        return "", "Vision fallback had no page images to read."

    model = os.environ.get("PAVT_VISION_MODEL", "").strip()
    try:
        if os.environ.get("OPENAI_API_KEY"):
            text = "\n".join(
                _openai_text(image, model or "gpt-4o-mini") for image in images
            )
        else:
            text = "\n".join(
                _anthropic_text(image, model or "claude-3-5-sonnet-latest")
                for image in images
            )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        return "", f"Vision fallback HTTP {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001
        return "", f"Vision fallback failed: {exc}"

    return text, ""
