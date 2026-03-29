from __future__ import annotations

import asyncio
import sys
import os
import json
from datetime import datetime

import winpty
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

env = os.environ.copy()
env["TERM"] = "xterm-256color"
env["COLORTERM"] = "truecolor"

COMMAND_TO_RUN = sys.argv[1:] or ["cmd", "/k", "wt_startup.bat"]

app = FastAPI()


def encode_message(data: bytes, metadata: dict) -> bytes:
    header = "\n".join(f"{k}: {v}" for k, v in metadata.items())
    header += "\n\n"
    return header.encode("utf-8") + data


def decode_message(payload: bytes) -> tuple[bytes, dict]:
    try:
        header_part, data = payload.split(b"\n\n", 1)
        header_str = header_part.decode("utf-8")
        metadata = {}
        for line in header_str.split("\n"):
            if ": " in line:
                k, v = line.split(": ", 1)
                metadata[k] = v
        return data, metadata
    except Exception:
        return payload, {}


active_terminals = {}
terminal_history = {}
active_websocket = None
ws_lock = asyncio.Lock()


async def send_to_ws(websocket: WebSocket, data: bytes, metadata: dict = None):
    if metadata is None:
        metadata = {}
    metadata["Time"] = datetime.now().isoformat()
    msg = encode_message(data, metadata)
    async with ws_lock:
        try:
            await websocket.send_bytes(msg)
        except Exception:
            pass


async def pty_reader(terminal_id: int, pty: winpty.PtyProcess):
    loop = asyncio.get_running_loop()
    try:
        while pty.isalive():
            data = await loop.run_in_executor(None, pty.read, 4096)
            if data:
                data_bytes = data if isinstance(data, bytes) else data.encode("utf-8")
                terminal_history[terminal_id] += data_bytes
                
                global active_websocket
                ws = active_websocket
                if ws:
                    await send_to_ws(ws, data_bytes, {
                        "Type": "Output", 
                        "TerminalID": str(terminal_id)
                    })
    except Exception as e:
        print(f"PTY {terminal_id} error:", e)


@app.on_event("startup")
async def startup_event():
    for i in range(4):
        pty = winpty.PtyProcess.spawn(
            COMMAND_TO_RUN,
            dimensions=(24, 80),
            env=env,
        )
        active_terminals[i] = pty
        terminal_history[i] = bytearray()
        asyncio.create_task(pty_reader(i, pty))


@app.get("/")
async def index() -> FileResponse:
    return FileResponse("index.html")


@app.websocket("/ws")
async def terminal(websocket: WebSocket) -> None:
    await websocket.accept()
    
    global active_websocket
    active_websocket = websocket

    for term_id in active_terminals:
        # Send Control message to open terminal
        control_payload = json.dumps({"action": "openTerminal", "terminalID": term_id}).encode("utf-8")
        await send_to_ws(websocket, control_payload, {"Type": "Control"})
        
        # Send history
        history = terminal_history[term_id]
        if history:
            await send_to_ws(websocket, bytes(history), {"Type": "Output", "TerminalID": str(term_id)})

    try:
        while True:
            payload = await websocket.receive_bytes()
            if payload:
                data, metadata = decode_message(payload)
                msg_type = metadata.get("Type")
                
                if msg_type == "Input":
                    term_id_str = metadata.get("TerminalID")
                    if term_id_str and term_id_str.isdigit():
                        term_id = int(term_id_str)
                        pty = active_terminals.get(term_id)
                        if pty and pty.isalive():
                            try:
                                pty.write(data)
                            except TypeError:
                                pty.write(data.decode("utf-8"))
    except (WebSocketDisconnect, ConnectionResetError):
        pass
    finally:
        if active_websocket == websocket:
            active_websocket = None


if __name__ == "__main__":
    import uvicorn
    
    # Run the app on localhost at port 8000
    uvicorn.run(
        "main:app", 
        host="127.0.0.1", 
        port=8000, 
        reload=True
    )
