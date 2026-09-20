"""Multi-tenant application entrypoint: Flask app + REST API consumed by the
per-user dashboard.

Auth & isolation model
----------------------
- Accounts: Flask-Login (+ Werkzeug password hashing). Users have a role
  (user | admin) and an enabled flag; disabled users cannot sign in.
- Workspaces: every API below reads/writes only the logged-in user's own
  UserConfig row and that user's isolated in-memory store
  (store.get_store(user.id)) / bot session (bot_runner.bot_manager).
- Admin only: /admin page and /api/admin/users*.

Run:
    python app.py       (or the thin alias  python main.py)
"""
import os
import secrets

import requests
from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from config import DATA_DIR, SECRET_KEYS, get_all_settings, get_setting
import ai_engine
import publisher
from extensions import db, login_manager
from models import User, apply_config_updates, config_to_dict
from store import get_store, store
from bot_runner import bot_manager
from webhook import bp as webhook_bp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.register_blueprint(webhook_bp)

# ------------------------------------------------------------ session secret
_SEED_FILE = str(DATA_DIR / "secret_key")
if os.getenv("SECRET_KEY"):
    app.secret_key = os.environ["SECRET_KEY"]
else:
    if not os.path.exists(_SEED_FILE):
        os.makedirs(os.path.dirname(_SEED_FILE), exist_ok=True)
        with open(_SEED_FILE, "w") as seed_file:
            seed_file.write(secrets.token_hex(32))
    with open(_SEED_FILE) as seed_file:
        app.secret_key = seed_file.read().strip()

# ------------------------------------------------------------------ database
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{os.path.join(DATA_DIR, 'app.db').replace(os.sep, '/')}",
)
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
}
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
login_manager.init_app(app)

# Legacy (guest) store mode mirrors the .env default for pre-migration paths.
store.set_mode(get_setting("MODE", "AUTOMATIC"))


@login_manager.unauthorized_handler
def _unauthorized():
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "message": "Authentication required"}), 401
    return redirect(url_for("login_page"))


with app.app_context():
    db.create_all()


# ---------------------------------------------------------------- decorators
def admin_required(func):
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            return jsonify({"ok": False, "message": "Admin access required"}), 403
        return func(*args, **kwargs)

    wrapper.__name__ = func.__name__
    return wrapper


def _workspace():
    """Resolve the current user's store + config dict."""
    user = current_user
    return get_store(user.id), config_to_dict(user)


# --------------------------------------------------------------------- pages
@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login_page"))


@app.route("/login")
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/register")
def register_page():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return render_template("register.html")


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", user=current_user)


@app.route("/admin")
@login_required
def admin_page():
    if not current_user.is_admin:
        return render_template("403.html"), 403
    return render_template("admin.html", user=current_user)


# --------------------------------------------------------------------- auth
@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if len(email) < 5 or "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"ok": False, "message": "Enter a valid email address."}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "message": "Password must be at least 6 characters."}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({"ok": False, "message": "An account with this email already exists."}), 409

    user = User(email=email, role="user", is_active=True)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    login_user(user)
    return jsonify({"ok": True, "user": user.as_dict()})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return jsonify({"ok": False, "message": "Incorrect email or password."}), 401
    if not user.is_active:
        return jsonify({"ok": False, "message": "This account was disabled by the admin."}), 403

    login_user(user)
    return jsonify({"ok": True, "user": user.as_dict()})


@app.route("/api/logout", methods=["POST"])
@login_required
def api_logout():
    logout_user()
    return jsonify({"ok": True})


@app.route("/api/me", methods=["GET"])
@login_required
def api_me():
    return jsonify({"user": current_user.as_dict()})


# -------------------------------------------------------------------- admin
@app.route("/api/admin/users", methods=["GET"])
@admin_required
def api_admin_users():
    users = User.query.order_by(User.id).all()
    return jsonify({"users": [user.as_dict() for user in users]})


@app.route("/api/admin/users/<int:user_id>", methods=["PATCH"])
@admin_required
def api_admin_user_update(user_id):
    target = db.session.get(User, user_id)
    if not target:
        return jsonify({"ok": False, "message": "User not found"}), 404
    if target.id == current_user.id:
        return jsonify({"ok": False, "message": "You cannot modify your own account here."}), 400

    data = request.get_json(silent=True) or {}
    if "active" in data:
        target.is_active = bool(data["active"])
    if "role" in data:
        role = str(data["role"]).strip()
        if role not in ("user", "admin"):
            return jsonify({"ok": False, "message": "role must be user or admin"}), 400
        target.role = role
    db.session.commit()
    return jsonify({"ok": True, "user": target.as_dict()})


# ------------------------------------------------------------------ status
@app.route("/api/status")
@login_required
def api_status():
    ustore, cfg = _workspace()
    return jsonify(
        {
            "status": ustore.get_status(),
            "mode": cfg.get("MODE") or "AUTOMATIC",
            "running": bot_manager.is_running(current_user.id),
        }
    )


@app.route("/api/control", methods=["POST"])
@login_required
def api_control():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    uid = current_user.id
    if action == "start":
        ok, message = bot_manager.start(uid)
    elif action == "stop":
        ok, message = bot_manager.stop(uid)
    else:
        return jsonify({"ok": False, "message": "action must be 'start' or 'stop'"}), 400
    return jsonify({"ok": ok, "message": message, "status": get_store(uid).get_status()})


@app.route("/api/mode", methods=["POST"])
@login_required
def api_mode():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    if mode not in ("AUTOMATIC", "MANUAL"):
        return jsonify({"ok": False, "message": "mode must be AUTOMATIC or MANUAL"}), 400
    apply_config_updates(current_user, {"MODE": mode})
    get_store(current_user.id).set_mode(mode)
    return jsonify({"ok": True, "mode": mode})


# ----------------------------------------------------------------------- env
@app.route("/api/env", methods=["GET"])
@login_required
def api_env_get():
    mask = request.args.get("mask", "1") != "0"
    payload = {}
    for key, value in config_to_dict(current_user).items():
        is_secret = key in SECRET_KEYS
        payload[key] = {
            "value": "" if (mask and is_secret) else value,
            "set": bool(value),
            "secret": is_secret,
        }
    return jsonify(payload)


@app.route("/api/env", methods=["POST"])
@login_required
def api_env_set():
    data = request.get_json(silent=True) or {}
    updates = {
        key: value
        for key, value in data.items()
        if key and value is not None and str(value).strip() != ""
    }
    if not updates:
        return jsonify({"ok": False, "message": "No values provided."}), 400
    saved = apply_config_updates(current_user, updates)
    if not saved:
        return jsonify({"ok": False, "message": "No valid settings provided."}), 400
    return jsonify({"ok": True, "updated": saved})


# ----------------------------------------------------------------------- data
@app.route("/api/logs")
@login_required
def api_logs():
    limit = int(request.args.get("limit", 200))
    return jsonify({"logs": get_store(current_user.id).get_logs(limit)})


@app.route("/api/pending")
@login_required
def api_pending():
    return jsonify({"pending": get_store(current_user.id).all_pending()})


@app.route("/api/history")
@login_required
def api_history():
    return jsonify({"history": get_store(current_user.id).get_history(50)})


@app.route("/api/pending/<pid>/approve", methods=["POST"])
@login_required
def api_pending_approve(pid):
    ustore, cfg = _workspace()
    pending = ustore.get_pending(pid)
    if not pending:
        return jsonify({"ok": False, "message": "Pending post not found"}), 404
    ustore.update_pending(pid, state="processing")
    try:
        result = publisher.publish(pending, cfg, ustore)
        ustore.remove_pending(pid)
        return jsonify({"ok": True, "result": result})
    except Exception as exc:
        ustore.update_pending(pid, state="awaiting")
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.route("/api/pending/<pid>/reject", methods=["POST"])
@login_required
def api_pending_reject(pid):
    ustore = get_store(current_user.id)
    if not ustore.get_pending(pid):
        return jsonify({"ok": False, "message": "Pending post not found"}), 404
    ustore.remove_pending(pid)
    return jsonify({"ok": True})


# ------------------------------------------------------------- connections
@app.route("/api/test-connections", methods=["POST"])
@login_required
def api_test_connections():
    """Reachability checks for the AI provider, the Facebook Page and the
    Telegram config, using the current workspace's credentials."""
    ustore, cfg = _workspace()
    results = {}

    ai_key = cfg.get("AI_API_KEY") or cfg.get("GEMINI_API_KEY")
    if ai_key:
        ok, detail = ai_engine.ping(cfg)
        results["ai"] = {"ok": bool(ok), "detail": detail}
    else:
        results["ai"] = {"ok": False, "detail": "AI_API_KEY / GEMINI_API_KEY is not set"}

    fb_token = cfg.get("PAGE_ACCESS_TOKEN")
    if fb_token:
        target = cfg.get("PAGE_ID") or "me"
        try:
            response = requests.get(
                f"https://graph.facebook.com/{cfg.get('GRAPH_VERSION') or 'v19.0'}/{target}",
                params={"access_token": fb_token},
                timeout=8,
            )
            payload = response.json()
            if "error" in payload:
                results["facebook"] = {
                    "ok": False,
                    "detail": payload["error"].get("message", "Invalid access token"),
                }
            else:
                results["facebook"] = {
                    "ok": True,
                    "detail": f"Connected to '{payload.get('name', target)}'",
                }
        except Exception as exc:
            results["facebook"] = {"ok": False, "detail": str(exc)[:160]}
    else:
        results["facebook"] = {"ok": False, "detail": "PAGE_ACCESS_TOKEN is not set"}

    missing = [
        name
        for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_CHANNEL")
        if not cfg.get(name)
    ]
    if missing:
        results["telegram"] = {"ok": False, "detail": "Missing fields: " + ", ".join(missing)}
    elif not (DATA_DIR / f"tg_session_{current_user.id}.session").exists():
        results["telegram"] = {
            "ok": False,
            "detail": f"Config complete, but no authorized session "
            f"({DATA_DIR / f'tg_session_{current_user.id}.session'}). "
            f"Run `python telegram_auth.py {current_user.id}` first.",
        }
    else:
        results["telegram"] = {
            "ok": True,
            "detail": f"Ready to listen to channel '{cfg.get('TELEGRAM_CHANNEL')}'",
        }

    for key, item in results.items():
        ustore.add_log("INFO" if item["ok"] else "WARN", "test", f"{key}: {item['detail']}")
    return jsonify(results)


@app.route("/api/test-post", methods=["POST"])
@login_required
def api_test_post():
    """Push a mock caption through this user's pipeline (AI rewrite -> publish
    in AUTOMATIC mode, or approval queue in MANUAL mode) without Telegram."""
    uid = current_user.id
    ustore, cfg = _workspace()
    mock_telegram_text = (
        "ðŸ”¥ ØªØ³Ø±ÙŠØ¨ ØªØ¬Ø±ÙŠØ¨ÙŠ Ø¬Ø¯ÙŠØ¯: Ø¥Ø¶Ø§ÙØ© Ø¨Ø·Ù„ ÙˆØ³ÙƒÙŠÙ† Ø¬Ø¯ÙŠØ¯ ÙÙŠ Ø§Ù„ØªØ­Ø¯ÙŠØ« Ø§Ù„Ù‚Ø§Ø¯Ù… "
        "Ù…Ø¹ ØªØ­Ø³ÙŠÙ†Ø§Øª Ø´Ø§Ù…Ù„Ø© Ù„Ù„Ø£Ø¯Ø§Ø¡!"
    )
    try:
        outcome = bot_manager.get(uid).process_message(0, mock_telegram_text)
        if outcome["published"]:
            return jsonify({"success": True, "message": "Test post published to Facebook."})
        if (cfg.get("MODE") or "AUTOMATIC") == "MANUAL":
            return jsonify({
                "success": True,
                "message": f"Test post queued for approval (#{outcome['pending']['id']}).",
            })
        return jsonify({
            "success": False,
            "message": "Test post could not be published (see activity log).",
        }), 500
    except Exception as exc:
        ustore.add_log("ERROR", "test", f"Test post failed: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"Dashboard: http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
