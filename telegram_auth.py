"""One-time Telegram session authentication (per user).

Run this after the user has configured TELEGRAM_API_ID / TELEGRAM_API_HASH
(and phone) in their workspace. It creates data/tg_session_<user_id>, which the
background bot for that user reuses across restarts.

Usage:  python telegram_auth.py <user_id>
"""
import asyncio
import sys

from telethon import TelegramClient

from app import app
from bot_runner import session_file_for
from models import User, config_to_dict


def _resolve_user(user_id):
    with app.app_context():
        return app.extensions["sqlalchemy"].session.get(User, int(user_id))


async def main():
    if len(sys.argv) < 2:
        print("Usage: python telegram_auth.py <user_id>")
        sys.exit(1)
    user_id = int(sys.argv[1])

    user = _resolve_user(user_id)
    if user is None:
        print(f"ERROR: no user with id={user_id}.")
        sys.exit(1)

    cfg = config_to_dict(user)
    api_id = cfg.get("TELEGRAM_API_ID")
    api_hash = cfg.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        print("ERROR: set TELEGRAM_API_ID and TELEGRAM_API_HASH in that user's dashboard first.")
        sys.exit(1)

    phone = cfg.get("TELEGRAM_PHONE")
    if not phone:
        phone = input("Enter the Telegram phone number (with country code): ").strip()

    session = session_file_for(user_id)
    client = TelegramClient(session, int(api_id), api_hash)
    await client.start(phone=phone)
    me = await client.get_me()
    print(f"Authenticated as @{me.username or me.first_name} @ {me.id}")
    print(f"Session saved to {session}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())