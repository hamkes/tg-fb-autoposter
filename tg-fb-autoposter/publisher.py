"""Facebook Page publisher backed by the Meta Graph API.

Publishes a tenant's pending post (text, photo or video) using that user's own
PAGE_ACCESS_TOKEN. Credentials come from the tenant cfg dict (.env-style keys)
when supplied; otherwise the legacy .env globals are used. History/logs are
written to the tenant's isolated store.
"""
import os

import requests

from config import get_setting
from store import store

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp"}


def _setting(key, default="", cfg=None):
    if cfg is not None:
        return cfg.get(key) or default
    return get_setting(key, default)


def _api(endpoint, params=None, files=None, cfg=None):
    url = f"https://graph.facebook.com/{_setting('GRAPH_VERSION', 'v19.0', cfg)}{endpoint}"
    request_params = dict(params or {})
    request_params.setdefault("access_token", _setting("PAGE_ACCESS_TOKEN", "", cfg))
    response = requests.post(url, params=request_params, files=files, timeout=60)
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text}
    if "error" in payload:
        raise RuntimeError(payload["error"].get("message", str(payload["error"])))
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code}: {payload}")
    return payload


def _media_kind(path):
    ext = os.path.splitext(path or "")[1].lower()
    return "video" if ext in VIDEO_EXTS else "image"


def publish(pending, cfg=None, user_store=None):
    """Publish a pending post dict. Returns the raw Graph API response.

    cfg: tenant config dict (.env-style keys) or None to use .env globals.
    user_store: tenant's isolated _Store (defaults to the legacy singleton).
    """
    page_id = _setting("PAGE_ID", "", cfg)
    page_token = _setting("PAGE_ACCESS_TOKEN", "", cfg)
    if not page_id or not page_token:
        raise RuntimeError("PAGE_ID / PAGE_ACCESS_TOKEN are not configured")

    caption = (pending.get("rewritten") or pending.get("original") or "").strip()
    if not caption and not (pending.get("media_path") and os.path.exists(pending["media_path"])):
        raise RuntimeError("Nothing to publish: caption and media are both empty")

    media_path = pending.get("media_path")
    payload = None

    if media_path and os.path.exists(media_path):
        kind = _media_kind(media_path)
        if kind == "video":
            endpoint = f"/{page_id}/videos"
            with open(media_path, "rb") as file_handle:
                payload = _api(
                    endpoint,
                    params={"message": caption, "description": caption},
                    files={"source": file_handle},
                    cfg=cfg,
                )
        else:
            endpoint = f"/{page_id}/photos"
            with open(media_path, "rb") as file_handle:
                payload = _api(
                    endpoint,
                    params={"message": caption, "caption": caption},
                    files={"source": file_handle},
                    cfg=cfg,
                )
    else:
        payload = _api(f"/{page_id}/feed", params={"message": caption}, cfg=cfg)

    out_store = user_store or store
    out_store.add_history(
        pending.get("id"),
        caption,
        os.path.basename(media_path) if media_path and os.path.exists(media_path) else None,
        payload,
    )
    return payload