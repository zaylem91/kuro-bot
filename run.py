"""Main entry point for Kuro Bot.
Run from project root: python -O run.py
"""
import asyncio
import sys
import os

# Performance: on UNIX-like systems use uvloop (libuv) instead of the default
# asyncio event loop. No-op on Windows (os.name == "nt") or if uvloop isn't installed.
if os.name != "nt":
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.__main__ import bot

if __name__ == "__main__":
    bot.run()
