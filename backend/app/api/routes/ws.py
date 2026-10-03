import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.packet_trace import packet_trace_service
from app.services.scenario_store import scenario_store

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/scenarios/{scenario_id}")
async def scenario_ws(websocket: WebSocket, scenario_id: str) -> None:
    await websocket.accept()
    if not scenario_store.get(scenario_id):
        await websocket.close(code=4004)
        return

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def on_event(ev) -> None:
        if getattr(ev, "scenario_id", None) != scenario_id:
            return
        loop.call_soon_threadsafe(
            queue.put_nowait,
            {"type": ev.__class__.__name__, "data": ev.model_dump(mode="json")},
        )

    packet_trace_service.subscribe(on_event)

    async def sender() -> None:
        while True:
            msg = await queue.get()
            await websocket.send_text(json.dumps(msg, default=str))

    task = asyncio.create_task(sender())
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        packet_trace_service.unsubscribe(on_event)
