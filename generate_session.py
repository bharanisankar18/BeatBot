"""Run this ONCE to log in the assistant account:

    .venv\\Scripts\\python.exe generate_session.py

It reads API_ID / API_HASH from .env, asks for the assistant's phone number
and the login code Telegram sends, then saves SESSION_STRING into .env for you.
"""
import asyncio
import os
import re

from dotenv import load_dotenv
from pyrogram import Client

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def save_session(session: str) -> None:
    with open(ENV_PATH, encoding="utf-8") as f:
        text = f.read()
    line = f"SESSION_STRING={session}"
    if re.search(r"^SESSION_STRING=.*$", text, flags=re.M):
        text = re.sub(r"^SESSION_STRING=.*$", lambda _: line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(text)


async def main():
    load_dotenv(ENV_PATH)
    api_id = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()
    if not api_id.isdigit() or not api_hash:
        print("Fill in API_ID and API_HASH in the .env file first.")
        return

    print("=== BeatBot assistant login ===")
    print("Log in with the ASSISTANT account (the normal Telegram account that will")
    print("join video chats). Type the phone number with country code, e.g. +919876543210")
    print("Telegram will then send a login code to that account's Telegram app.\n")

    print("!! When asked 'Enter phone number or bot token', type a PHONE NUMBER,")
    print("!! NOT the bot token. The assistant must be a normal user account.\n")

    async with Client("assistant_login", api_id=int(api_id), api_hash=api_hash, in_memory=True) as app:
        me = await app.get_me()
        if me.is_bot:
            print("\nThat was a BOT token. The assistant must be a normal user account.")
            print("Run this script again and type a phone number instead.")
            return
        session = await app.export_session_string()

    save_session(session)
    print(f"\nDone! Logged in as {me.first_name} (id {me.id}).")
    print("SESSION_STRING has been saved into your .env file.")
    print("Now start the bot with:  start.bat   (or  .venv\\Scripts\\python.exe bot.py)")


if __name__ == "__main__":
    asyncio.run(main())
