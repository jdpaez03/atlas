"""WS /ws?since=<seq> — replay events with seq > since, then stream live."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["stream"])


@router.websocket("/ws")
async def stream(ws: WebSocket, since: int = 0) -> None:
    bus = ws.app.state.bus
    await ws.accept()
    queue = bus.subscribe()  # subscribe before replaying so nothing falls in the gap
    last = since
    try:
        for event in bus.history(since_seq=since):
            await ws.send_text(event.model_dump_json())
            last = event.seq
        while True:
            event = await queue.get()
            if event.seq <= last:
                continue  # already sent during replay
            await ws.send_text(event.model_dump_json())
            last = event.seq
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(queue)
