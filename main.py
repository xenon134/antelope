from __future__ import annotations

import asyncio

import winpty
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

COMMAND_TO_RUN = ["python", "script.py"]
DEFAULT_COLS = 220
DEFAULT_ROWS = 50

app = FastAPI()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse("index.html")


@app.websocket("/ws")
async def terminal(websocket: WebSocket) -> None:
    await websocket.accept()

    pty = winpty.PtyProcess.spawn(
        COMMAND_TO_RUN,
        dimensions=(DEFAULT_ROWS, DEFAULT_COLS),
    )

    loop = asyncio.get_event_loop()

    try:
        while pty.isalive():
            try:
                data: bytes = await asyncio.wait_for(
                    loop.run_in_executor(None, pty.read, 4096),
                    timeout=1.0,
                )
            except TimeoutError:
                continue
            if data:
                await websocket.send_bytes(data)
    except (WebSocketDisconnect, EOFError):
        pass
    finally:
        pty.terminate()
        await asyncio.sleep(0.1)
        if pty.isalive():
            pty.terminate(force=True)
