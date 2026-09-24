"""Event loop factory for uvicorn that can always spawn subprocesses (Claude Code, subscription backend).

uvicorn 0.53 (uvicorn/loops/asyncio.py) uses a ProactorEventLoop on Windows only when it runs without
--reload/--workers; with them it switches to a SelectorEventLoop, which cannot start subprocesses
(asyncio raises NotImplementedError). A dotted path passed as `loop` is used as the factory as-is
(uvicorn/config.py: get_loop_factory), so

    uvicorn atlas.main:app --reload --loop atlas.eventloop:new_event_loop

keeps the Proactor loop on Windows even with --reload. `python -m atlas` does this for you.
"""

from __future__ import annotations

import asyncio
import sys


def new_event_loop() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()
