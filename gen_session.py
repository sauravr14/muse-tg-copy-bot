"""Interactive helper: prints a Pyrogram-format session string for .env."""

import asyncio


async def _gen():
    from pyrogram import Client

    api_id = int(input("API_ID: ").strip())
    api_hash = input("API_HASH: ").strip()
    async with Client("gen_session", api_id=api_id, api_hash=api_hash,
                      in_memory=True) as app:
        me = await app.get_me()
        print(f"\nLogged in as {me.first_name} (@{me.username})")
        session = await app.export_session_string()
        print("\n=== SESSION_STRING (paste into .env) ===")
        print(session)
        print("=== keep this secret: it is full access to your account ===")


if __name__ == "__main__":
    asyncio.run(_gen())
