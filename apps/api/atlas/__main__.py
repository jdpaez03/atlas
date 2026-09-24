"""Start the ATLAS API: `uv run python -m atlas [--host 127.0.0.1] [--port 8000] [--reload]`.

Use this instead of `uvicorn atlas.main:app --reload` on Windows: uvicorn's --reload switches to a
selector event loop there, which cannot start Claude Code (the subscription backend). This entry point
always hands uvicorn a Proactor loop on Windows (atlas/eventloop.py), with or without --reload.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m atlas", description="Run the ATLAS API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="restart on code changes (dev)")
    args = parser.parse_args(argv)
    loop = "atlas.eventloop:new_event_loop" if sys.platform == "win32" else "auto"
    uvicorn.run(
        "atlas.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(Path(__file__).resolve().parent)] if args.reload else None,
        loop=loop,
    )


if __name__ == "__main__":
    main()
