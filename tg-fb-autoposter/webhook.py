"""Facebook Messenger Webhook & approval system (multi-tenant).

- GET  /webhook : Meta verification handshake (accepts any tenant's VERIFY_TOKEN).
- POST /webhook : event feed; handles Postbacks (APPROVE / REJECT / EDIT) and
  free-text admin replies (edit instructions), then drives the publisher.

Tenant resolution:
  - Postbacks carry the user id in the payload:  KIND:<user_id>:<pending_id>.
  - Plain-text admin replies carry no tenant, so the sender's PSID is matched
    against each user's ADMIN_PSID to find the tenant.
"""
import json
import os

import requests
from flask import Blueprint, abort, request

from config import get_setting
import ai_engine
import publisher
from extensions import db
from models import User, config_to_dict
from store import get_store

bp = Blueprint("webhook", __name__)

_GRAPH = "https://graph.facebook.com"


def _setting(key, default="", cfg=None):
    if cfg is not None:
        return cfg.get(key) or default
    return get_setting(key, default)


def _messenger_api(params=None, data=None, files=None, cfg=None):
    request_params = {"access_token": _setting("MESSENGER_ACCESS_TOKEN", "", cfg)}
    if params:
        request_params.update(params)
    return requests.post(
        f"{_GRAPH}/{_setting('GRAPH_VERSION', 'v19.0', cfg)}/me/messages",
        params=request_params,
        data=data,
        files=files,
        timeout=30,
    )


def _send_text(psid, text, cfg=None):
    payload = {
        "recipient": json.dumps({"id": psid}),
        "message": json.dumps({"text": text}),
    }
    response = _messenger_api(data=payload, cfg=cfg)
    _check_response(response)


def _send_photo(psid, path, caption=None, cfg=None):
    payload = {
        "recipient": json.dumps({"id": psid}),
        "message": json.dumps({"attachment": {"type": "image", "payload": {"is_reusable": True}}}),
    }
    with open(path, "rb") as file_handle:
        response = _messenger_api(params=payload, files={"filedata": file_handle}, cfg=cfg)
    _check_response(response)
    if caption:
        _send_text(psid, caption, cfg)


def _send_buttons(psid, text, buttons, cfg=None):
    message = {
        "attachment": {
            "type": "template",
            "payload": {
                "template_type": "button",
                "text": text,
                "buttons": buttons,
            },
        }
    }
    payload = {
        "recipient": json.dumps({"id": psid}),
        "message": json.dumps(message),
    }
    response = _messenger_api(data=payload, cfg=cfg)
    _check_response(response)


def _check_response(response):
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text}
    if "error" in body:
        raise RuntimeError(body["error"].get("message", str(body["error"])))
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code}: {body}")


# ---------------------------------------------------------- tenant resolution
def _user_for_psid(psid):
    """Find the first enabled user whose ADMIN_PSID matches the sender."""
    for user in db.session.query(User).filter_by(is_active=True):
        psid_value = (config_to_dict(user).get("ADMIN_PSID") or "")
        if psid_value == psid:
            return user
    return None


def _store_and_cfg(user):
    return get_store(user.id), config_to_dict(user)


# ------------------------------------------------------------------ endpoints
@bp.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    accepted = (
        token
        and (
            token == get_setting("VERIFY_TOKEN")
            or any(
                config_to_dict(u).get("VERIFY_TOKEN") == token
                for u in db.session.query(User).filter_by(is_active=True)
            )
        )
    )
    if mode == "subscribe" and accepted:
        get_store(0).add_log("INFO", "webhook", "Messenger webhook verified by Meta")
        return challenge, 200
    get_store(0).add_log("WARN", "webhook", "Webhook verification failed (bad token)")
    abort(403)


@bp.route("/webhook", methods=["POST"])
def webhook_receive():
    data = request.get_json(force=True, silent=True) or {}
    for entry in data.get("entry", []):
        for messaging in entry.get("messaging", []):
            try:
                _handle_messaging(messaging)
            except Exception as exc:
                get_store(0).add_log("ERROR", "webhook", f"Event handling failed: {exc}")
    return "EVENT_RECEIVED", 200


# -------------------------------------------------------------- event router
def _handle_messaging(messaging):
    sender_psid = (messaging.get("sender") or {}).get("id")
    if not sender_psid:
        return

    message = messaging.get("message") or {}
    if message.get("is_echo"):
        return

    if messaging.get("postback"):
        payload = messaging["postback"].get("payload") or ""
        _handle_postback(sender_psid, payload)
        return

    user = _user_for_psid(sender_psid)
    if not user:
        return  # ignore everyone who is not a tenant admin

    text = (message.get("text") or "").strip()
    if "attachments" in message and not text:
        text = "[attachment received]"
    if text:
        _store_and_cfg(user)[0].add_log("INFO", "webhook", f"Admin reply: {text[:120]}")
        _handle_admin_reply(user, text)


def _handle_postback(psid, payload):
    kind, _, rest = payload.partition(":")  # rest == "<user_id>:<pending_id>"
    uid, _, pid = rest.partition(":")
    if not uid or not pid:
        return
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return

    user = db.session.get(User, uid)
    if not user or not user.is_active:
        return
    if (config_to_dict(user).get("ADMIN_PSID") or "") != psid:
        return  # sender is not this tenant's admin

    _, cfg = _store_and_cfg(user)
    store = get_store(uid)
    pending = store.get_pending(pid)

    if kind == "APPROVE":
        if not pending:
            _send_text(psid, f"❌ No pending post found for #{pid or '?'}.", cfg)
            return
        store.update_pending(pid, state="processing")
        try:
            result = publisher.publish(pending, cfg, store)
            store.remove_pending(pid)
            _send_text(psid, f"✅ Approved & published to the Page. FB response: {result}", cfg)
        except Exception as exc:
            store.update_pending(pid, state="awaiting")
            store.add_log("ERROR", "publisher", f"Approval publish failed: {exc}")
            _send_text(psid, f"❌ Publish failed: {exc}", cfg)

    elif kind == "REJECT":
        if pending:
            store.remove_pending(pid)
            _send_text(psid, f"🗑 Post #{pid} was rejected and cleared.", cfg)

    elif kind == "EDIT":
        if pending:
            store.update_pending(pid, state="editing", edit_instruction=None)
            _send_text(
                psid,
                "✏️ Post #{} marked for editing. Reply in this chat with the "
                "changes you want and it will be re-processed by Gemini.".format(pid),
                cfg,
            )
        else:
            _send_text(psid, f"❌ No pending post found for #{pid or '?'}.", cfg)


def _handle_admin_reply(user, text):
    store, cfg = _store_and_cfg(user)
    pending = store.find_editing_pending()
    if not pending:
        return  # no post waiting for an edit instruction

    psid = (cfg.get("ADMIN_PSID") or "").strip()
    pid = pending["id"]
    store.update_pending(pid, state="processing", edit_instruction=text)
    try:
        new_caption = ai_engine.rewrite_with_instruction(pending["rewritten"], text, cfg)
        store.update_pending(pid, rewritten=new_caption, state="awaiting")
        store.add_log("INFO", "ai", f"Admin edit applied to #{pid}")
        if psid:
            _send_text(psid, "✅ Caption updated per your instructions:", cfg)
        send_approval_request(user.id, pid, cfg, store)
    except Exception as exc:
        store.update_pending(pid, state="editing")
        store.add_log("ERROR", "ai", f"Admin edit failed: {exc}")
        if psid:
            _send_text(psid, f"❌ Edit failed: {exc}", cfg)


# ------------------------------------------------------------ approval sender
def send_approval_request(user_id, pid, cfg=None, user_store=None):
    """Send media preview + Approve/Reject/Edit buttons to the tenant admin."""
    store = user_store or get_store(user_id)
    pending = store.get_pending(pid)
    if not pending:
        return

    psid = _setting("ADMIN_PSID", "", cfg)
    if not psid:
        store.add_log("WARN", "webhook", "ADMIN_PSID not set; approval not sent")
        return

    media_path = pending.get("media_path")
    if media_path and os.path.exists(media_path):
        try:
            _send_photo(psid, media_path, cfg=cfg)
        except Exception as exc:
            store.add_log("WARN", "webhook", f"Media preview failed: {exc}")

    caption = (pending.get("rewritten") or pending.get("original") or "").strip()
    snippet = caption[:500] + ("..." if len(caption) > 500 else "")

    text = (
        f"📝 New Telegram post waiting for review (#{pid}):\n\n"
        f"{snippet}\n\n"
        f"{'📎 Attached media' if pending.get('media_path') else ''}"
        "Press a button below, or select ✏️ Edit and reply with instructions."
    ).strip()

    buttons = [
        {"type": "postback", "title": "✅ Approve", "payload": f"APPROVE:{user_id}:{pid}"},
        {"type": "postback", "title": "❌ Reject", "payload": f"REJECT:{user_id}:{pid}"},
        {"type": "postback", "title": "✏️ Edit", "payload": f"EDIT:{user_id}:{pid}"},
    ]
    _send_buttons(psid, text, buttons, cfg)
    store.add_log("INFO", "webhook", f"Approval request #{pid} sent to ADMIN_PSID")