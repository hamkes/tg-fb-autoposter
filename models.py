"""SQLAlchemy models for the multi-tenant platform.

Tables
------
users          - accounts (email + password hash, role, enabled flag).
user_configs   - per-user workspace settings (replaces the single .env file).

Workspace isolation rule: every row in every table below carries a user_id, and
all REST routes resolve the row through the logged-in user's id (never through
global lookups).

Works with PostgreSQL/Supabase out of the box and with SQLite locally via the
DATABASE_URL setting.
"""
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from config import SECRET_KEYS
from extensions import db, login_manager


def _now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------- users
class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="user", server_default="user")
    # Flask-Login bool used for the "enabled/disabled by admin" flag.
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default="1")
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_now, server_default=db.func.now()
    )

    config = db.relationship(
        "UserConfig", uselist=False, back_populates="user", cascade="all, delete-orphan"
    )

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

    @property
    def is_admin(self):
        return self.role == "admin"

    def as_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "role": self.role,
            "active": bool(self.is_active),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ------------------------------------------------------------- user configs
class UserConfig(db.Model):
    """One row per user, holding that workspace's credentials & preferences.

    Column names mirror the old .env keys (lowercased) so the migration from
    the single-user .env story is mechanical.
    """

    __tablename__ = "user_configs"

    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    telegram_api_id = db.Column(db.String(255), nullable=False, default="", server_default="")
    telegram_api_hash = db.Column(db.String(255), nullable=False, default="", server_default="")
    telegram_phone = db.Column(db.String(255), nullable=False, default="", server_default="")
    telegram_channel = db.Column(db.String(255), nullable=False, default="", server_default="")

    gemini_api_key = db.Column(db.String(255), nullable=False, default="", server_default="")
    gemini_model = db.Column(db.String(120), nullable=False, default="gemini-2.5-flash")
    ai_api_key = db.Column(db.String(255), nullable=False, default="", server_default="")
    ai_model = db.Column(db.String(120), nullable=False, default="", server_default="")
    ai_base_url = db.Column(db.String(255), nullable=False, default="", server_default="")

    page_id = db.Column(db.String(120), nullable=False, default="", server_default="")
    page_access_token = db.Column(db.String(255), nullable=False, default="", server_default="")
    messenger_access_token = db.Column(db.String(255), nullable=False, default="", server_default="")
    verify_token = db.Column(db.String(255), nullable=False, default="", server_default="")
    admin_psid = db.Column(db.String(120), nullable=False, default="", server_default="")
    public_base_url = db.Column(db.String(255), nullable=False, default="", server_default="")

    graph_version = db.Column(db.String(24), nullable=False, default="v19.0", server_default="v19.0")
    mode = db.Column(db.String(24), nullable=False, default="AUTOMATIC", server_default="AUTOMATIC")
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_now, server_default=db.func.now()
    )

    user = db.relationship("User", back_populates="config")

    # .env-style key -> model column mapping (used by the REST layer).
    FIELD_MAP = {
        "TELEGRAM_API_ID": "telegram_api_id",
        "TELEGRAM_API_HASH": "telegram_api_hash",
        "TELEGRAM_PHONE": "telegram_phone",
        "TELEGRAM_CHANNEL": "telegram_channel",
        "GEMINI_API_KEY": "gemini_api_key",
        "GEMINI_MODEL": "gemini_model",
        "AI_API_KEY": "ai_api_key",
        "AI_MODEL": "ai_model",
        "AI_BASE_URL": "ai_base_url",
        "PAGE_ID": "page_id",
        "PAGE_ACCESS_TOKEN": "page_access_token",
        "MESSENGER_ACCESS_TOKEN": "messenger_access_token",
        "VERIFY_TOKEN": "verify_token",
        "ADMIN_PSID": "admin_psid",
        "PUBLIC_BASE_URL": "public_base_url",
        "GRAPH_VERSION": "graph_version",
        "MODE": "mode",
    }


def get_config_row(user):
    """Fetch (creating on first use) the UserConfig row for a user object."""
    if user.config is None:
        user.config = UserConfig(user_id=user.id)
        db.session.add(user.config)
        db.session.commit()
        return user.config
    return user.config


def config_to_dict(user):
    """Return the whole workspace config as a .env-style dict."""
    row = get_config_row(user)
    return {key: (getattr(row, col) or "") for key, col in UserConfig.FIELD_MAP.items()}


def config_is_secret(key):
    return key in SECRET_KEYS


def apply_config_updates(user, updates):
    """Persist non-empty values from a dict of .env-style keys.

    Returns the list of keys actually saved.
    """
    row = get_config_row(user)
    saved = []
    for key, value in updates.items():
        col = UserConfig.FIELD_MAP.get(key)
        if col is None or value is None:
            continue
        raw = str(value).strip()
        if key == "TELEGRAM_API_ID" and raw and not raw.isdigit():
            continue
        setattr(row, col, raw)
        saved.append(key)
    if saved:
        row.updated_at = _now()
        db.session.commit()
    return saved