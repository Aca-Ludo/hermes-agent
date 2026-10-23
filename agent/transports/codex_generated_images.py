"""Materialize native Codex images before the live/history notification fan-out."""

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from PIL import Image

from agent.provider_media import GENERATED_SUBDIR, cache_dir


_IMAGE_ERROR = {"success": False, "error": "Codex generated image could not be saved."}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_ENCODED_IMAGE_CHARS = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
MAX_IMAGE_PIXELS = 40_000_000


def normalize_image_notification(note: dict) -> dict:
    """Return a copy with image bytes replaced by a durable profile-local result."""
    params = note.get("params") or {}
    item = params.get("item") or {}
    if item.get("type") != "imageGeneration" or note.get("method") not in {"item/started", "item/completed"}:
        return note
    if note["method"] == "item/started":
        safe_item = {"type": "imageGeneration", "id": item.get("id") or ""}
        return {**note, "params": {**params, "item": safe_item}}
    filename = None
    try:
        if item.get("status") != "completed" or item.get("error"):
            raise ValueError("Image generation did not complete")
        encoded = item.get("result")
        if not isinstance(encoded, str) or not encoded or len(encoded) > MAX_ENCODED_IMAGE_CHARS:
            raise ValueError("Missing image data")
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds byte limit")
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("Image exceeds pixel limit")
            extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}.get(image.format or "")
            if extension is None:
                raise ValueError("Unsupported image format")
            image.verify()
        # verify() checks container integrity, but JPEG requires a full decode to detect truncation.
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
        directory = cache_dir(f"{GENERATED_SUBDIR}/images")
        fd, filename = tempfile.mkstemp(prefix="codex-image-", suffix=f".{extension}", dir=directory)
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
        result = {"success": True, "image": str(Path(filename).resolve())}
    except (ValueError, TypeError, OSError, SyntaxError, Image.DecompressionBombError):
        if filename is not None:
            with suppress(OSError):
                Path(filename).unlink(missing_ok=True)
        result = dict(_IMAGE_ERROR)
    safe_item = {"type": "imageGeneration", "id": item.get("id") or "", "result": result}
    return {**note, "params": {**params, "item": safe_item}}


def image_completion_payload(item: dict) -> tuple[str, bool]:
    """The same small JSON result drives both Desktop cards and persisted history."""
    result = item.get("result")
    if isinstance(result, dict) and result.get("success") is True and isinstance(result.get("image"), str):
        result = {"success": True, "image": result["image"]}
    else:
        result = _IMAGE_ERROR
    return json.dumps(result, ensure_ascii=False), result.get("success") is not True
