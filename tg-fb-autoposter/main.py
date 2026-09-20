"""Thin wrapper so `python main.py` still boots the multi-tenant app."""
import os

from app import app

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"Dashboard: http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)