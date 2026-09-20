"""Thread-safe, in-memory store for logs, pending approvals, publish history
and runtime state, isolated per user.

Each tenant gets its own _Store instance keyed by user_id (get_store), so logs,
approvals and history can never leak across accounts. Data is intentionally
volatile (credentials live in the DB via models.UserConfig), which keeps the
approval queue safe to clear on restart.
"""
import threading
import time
import uuid

from config import MEDIA_DIR


class _Store:
    def __init__(self):
        self._lock = threading.RLock()
        self._logs = []
        self._pending = {}
        self._history = []
        self._status = "STOPPED"
        self._mode = "AUTOMATIC"
        self._max_logs = 500

    # ------------------------------------------------------------------ state
    def get_status(self):
        with self._lock:
            return self._status

    def set_status(self, status):
        with self._lock:
            self._status = status
            self.add_log("INFO", "system", f"Bot status -> {status}")

    def get_mode(self):
        with self._lock:
            return self._mode

    def set_mode(self, mode):
        with self._lock:
            self._mode = mode
            self.add_log("INFO", "system", f"Operational mode -> {mode}")

    # ------------------------------------------------------------------- logs
    def add_log(self, level, source, message):
        with self._lock:
            self._logs.append(
                {
                    "ts": time.time(),
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "level": str(level).upper(),
                    "source": str(source),
                    "message": str(message),
                }
            )
            if len(self._logs) > self._max_logs:
                self._logs = self._logs[-self._max_logs:]

    def get_logs(self, limit=200):
        with self._lock:
            return list(reversed(self._logs[-limit:]))

    # ---------------------------------------------------------------- pending
    def add_pending(self, source_message_id, original, rewritten, media_path=None):
        with self._lock:
            pid = uuid.uuid4().hex[:8]
            pending = {
                "id": pid,
                "source_message_id": source_message_id,
                "original": original,
                "rewritten": rewritten,
                "media_path": str(media_path) if media_path else None,
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "state": "awaiting",
                "edit_instruction": None,
            }
            self._pending[pid] = pending
            self.add_log("INFO", "store", f"Pending post #{pid} created")
            return dict(pending)

    def get_pending(self, pid):
        with self._lock:
            entry = self._pending.get(pid)
            return dict(entry) if entry else None

    def all_pending(self):
        with self._lock:
            return [dict(p) for p in self._pending.values()]

    def update_pending(self, pid, **fields):
        with self._lock:
            entry = self._pending.get(pid)
            if entry:
                entry.update(fields)
            return dict(entry) if entry else None

    def find_editing_pending(self):
        with self._lock:
            for entry in self._pending.values():
                if entry["state"] == "editing":
                    return dict(entry)
            return None

    def remove_pending(self, pid):
        with self._lock:
            self._pending.pop(pid, None)
            self.add_log("INFO", "store", f"Pending post #{pid} removed")

    # ---------------------------------------------------------------- history
    def add_history(self, pid, caption, media_name, fb_response):
        with self._lock:
            self._history.append(
                {
                    "id": pid,
                    "caption": caption,
                    "media": media_name,
                    "fb_response": fb_response,
                    "published": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            if len(self._history) > 200:
                self._history = self._history[-200:]
            self.add_log("INFO", "publisher", f"Published #{pid} -> {fb_response}")

    def get_history(self, limit=50):
        with self._lock:
            return list(reversed(self._history[-limit:]))


store = _Store()

# Guest/legacy store (used when an anonymous request hits a service that was
# not yet migrated to a user context). Real tenants always use get_store().

_stores = {}
_stores_lock = threading.Lock()


def get_store(user_id):
    """Return the isolated _Store instance for the given user_id."""
    with _stores_lock:
        if user_id not in _stores:
            _stores[user_id] = _Store()
        return _stores[user_id]


def all_stores():
    with _stores_lock:
        return dict(_stores)