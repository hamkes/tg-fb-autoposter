"""Multi-tenant Telethon session manager.

Each signed-up user owns an isolated BotSession: its own Telethon connection,
its own on-disk session file (data/tg_session_<user_id>), its own in-memory
store, and its own credentials read from models.UserConfig. start() / stop()
are per-user and safe to call repeatedly from any thread.
"""
import asyncio
import os
import threading

from telethon import TelegramClient, events

from config import DATA_DIR
import ai_engine
import publisher
import webhook
from models import User, config_to_dict
from store import get_store


def session_file_for(user_id):
    return str(DATA_DIR / f"tg_session_{user_id}")


class BotSession:
    """A single user's background Telegram listener."""

    def __init__(self, user_id):
        self.user_id = user_id
        self.store = get_store(user_id)
        self.session_file = session_file_for(user_id)
        self.media_dir = str(DATA_DIR / "media")
        self._thread = None
        self._loop = None
        self._client = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ accessors
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def cfg(self):
        """Fresh workspace config (dict of .env-style keys) from the DB."""
        from app import app as _flask_app
        from extensions import db as _db

        with _flask_app.app_context():
            user = _db.session.get(User, self.user_id)
            if user is None:
                return {}
            return config_to_dict(user)

    def start(self):
        with self._lock:
            if self.is_running():
                return False, "Bot is already running."
            self.store.set_status("STARTING")
            self._loop = None
            self._thread = threading.Thread(
                target=self._run, daemon=True, name=f"telegram-bot-{self.user_id}"
            )
            self._thread.start()
            return True, "Bot is starting..."

    def stop(self):
        with self._lock:
            if not self.is_running():
                self.store.set_status("STOPPED")
                return False, "Bot is not running."
            self.store.set_status("STOPPING")
            loop = self._loop
            if loop:
                try:
                    if loop.is_running() and self._client:
                        asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
                    else:
                        loop.call_soon_threadsafe(loop.stop)
                except Exception:
                    try:
                        loop.call_soon_threadsafe(loop.stop)
                    except Exception:
                        pass
            self._thread.join(timeout=15)
            self._thread = None
            self.store.set_status("STOPPED")
            return True, "Bot stopped."

    # -------------------------------------------------------------- lifecycle
    async def _shutdown(self):
        client = self._client
        self._client = None
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception as exc:
            self.store.add_log("ERROR", "bot", f"Bot stopped with error: {exc}")
            self.store.set_status("ERROR")
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None
            if self.store.get_status() not in ("ERROR", "STOPPED", "STOPPING"):
                self.store.set_status("STOPPED")

    async def _main(self):
        cfg = self.cfg()
        api_id = cfg.get("TELEGRAM_API_ID") or ""
        api_hash = cfg.get("TELEGRAM_API_HASH") or ""
        channel = cfg.get("TELEGRAM_CHANNEL") or ""
        if not api_id or not api_hash:
            raise RuntimeError("TELEGRAM_API_ID / TELEGRAM_API_HASH are not configured.")
        if not channel:
            raise RuntimeError("TELEGRAM_CHANNEL is not configured.")

        client = TelegramClient(self.session_file, int(api_id), api_hash)
        self._client = client
        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(
                f"Telegram session is not authorized. Run `python telegram_auth.py {self.user_id}` "
                "once, then start the bot again."
            )

        @client.on(events.NewMessage(chats=channel))
        async def _on_new_message(event):
            await self._handle_message(client, event)

        try:
            me = await client.get_me()
        except Exception:
            me = None
        self.store.add_log(
            "INFO",
            "bot",
            f"Listening to channel '{channel}' as "
            f"@{(me.username or me.first_name) if me else 'unknown'}",
        )
        self.store.set_status("RUNNING")
        await client.run_until_disconnected()
        self._client = None

    # ------------------------------------------------------------- messaging
    async def _handle_message(self, client, event):
        message = event.message
        if not message:
            return
        has_text = bool((message.text or "").strip())
        if not has_text and not message.media:
            return
        if getattr(message, "out", False):
            return

        original = message.text or ""
        media_path = None

        if message.media:
            try:
                downloaded = await client.download_media(message, file=self.media_dir)
                if downloaded:
                    media_path = str(downloaded)
                    self.store.add_log(
                        "INFO", "bot", f"Downloaded media: {os.path.basename(media_path)}"
                    )
            except Exception as exc:
                self.store.add_log("WARN", "bot", f"Media download failed: {exc}")

        self.store.add_log("INFO", "bot", f"New Telegram message id={message.id}")
        self.process_message(message.id, original, media_path)

    def process_message(self, message_id, original, media_path=None):
        """Shared tenant pipeline: AI rewrite -> pending -> publish (AUTOMATIC)
        or ask approval (MANUAL). Used by the Telegram listener and the 'Test
        Post' button. Returns {pending, published}."""
        cfg = self.cfg()
        store = self.store

        rewritten = original
        if (original or "").strip():
            try:
                rewritten = ai_engine.rewrite_for_facebook(original, cfg)
                store.add_log("INFO", "ai", "Caption rewritten by AI")
            except Exception as exc:
                store.add_log("ERROR", "ai", f"Rewrite failed: {exc} (posting original)")

        pending = store.add_pending(message_id, original, rewritten, media_path)
        outcome = {"pending": pending, "published": False}

        if cfg.get("MODE") == "MANUAL":
            try:
                webhook.send_approval_request(self.user_id, pending["id"], cfg, store)
                store.add_log("INFO", "bot", f"Approval request #{pending['id']} queued")
            except Exception as exc:
                store.add_log("ERROR", "webhook", f"Approval send failed: {exc}")
        else:
            try:
                result = publisher.publish(pending, cfg, store)
                store.remove_pending(pending["id"])
                outcome["published"] = True
                store.add_log("INFO", "publisher", f"Auto-published: {result}")
            except Exception as exc:
                store.add_log("ERROR", "publisher", f"Auto-publish failed: {exc}")
        return outcome


class BotManager:
    """Registry of per-user BotSession instances."""

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def get(self, user_id):
        with self._lock:
            if user_id not in self._sessions:
                self._sessions[user_id] = BotSession(user_id)
            return self._sessions[user_id]

    def start(self, user_id):
        return self.get(user_id).start()

    def stop(self, user_id):
        return self.get(user_id).stop()

    def is_running(self, user_id):
        return self.get(user_id).is_running()

    def status_of(self, user_id):
        return self.get(user_id).store.get_status()

    def stop_all(self):
        for user_id in list(self._sessions):
            try:
                self.stop(user_id)
            except Exception:
                pass


bot_manager = BotManager()

# Backward-compatible alias for any external scripts still importing it.
bot_runner = bot_manager