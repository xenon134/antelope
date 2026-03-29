from __future__ import annotations

import asyncio
import sys
import os

import winpty
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

env = os.environ.copy()
env["TERM"] = "xterm-256color"
env["COLORTERM"] = "truecolor"

COMMAND_TO_RUN = sys.argv[1:] or ["cmd"]

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
        env=env,
    )

    loop = asyncio.get_running_loop()

    async def pty_to_ws():
        """Reads from PTY and sends to WebSocket"""
        try:
            while pty.isalive():
                data = await loop.run_in_executor(None, pty.read, 4096)
                if data:
                    await websocket.send_bytes(data)
        except (EOFError, ConnectionResetError, WebSocketDisconnect):
            pass

    async def ws_to_pty():
        """Reads from WebSocket and writes to PTY"""
        try:
            while pty.isalive():
                # Receive data from the browser (term.onData)
                data = await websocket.receive_bytes()
                if data:
                    try:
                        # pty.write typically accepts bytes in modern winpty/pywinpty
                        pty.write(data)
                    except TypeError:
                        # Fallback for older pywinpty versions expecting str
                        pty.write(data.decode("utf-8"))
        except (WebSocketDisconnect, ConnectionResetError):
            pass

    # Run both tasks concurrently
    try:
        await asyncio.gather(pty_to_ws(), ws_to_pty())
    finally:
        pty.terminate(force=True)

if __name__ == "__main__":
    import uvicorn
    
    # Run the app on localhost at port 8000
    uvicorn.run(
        "main:app", 
        host="127.0.0.1", 
        port=8000, 
        reload=True
    )
