from __future__ import annotations

import asyncio
import sys
import os
import json
from datetime import datetime

import winpty
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

import antelope_jobs

DEBUG_MODE = antelope_jobs.DEBUG_MODE
print('Emergency stop: taskkill -f -pid', os.getpid(), '\n')

MAX_PARALLEL = 8

env = os.environ.copy()
env["TERM"] = "xterm-256color"
env["COLORTERM"] = "truecolor"

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
active_websockets = set()
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


async def broadcast(data: bytes, metadata: dict = None):
    if metadata is None:
        metadata = {}
    metadata["Time"] = datetime.now().isoformat()
    msg = encode_message(data, metadata)
    async with ws_lock:
        disconnected = []
        for ws in active_websockets:
            try:
                await ws.send_bytes(msg)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            active_websockets.discard(ws)


async def broadcast_controlmsg(payload):
    await broadcast(json.dumps(payload).encode("utf-8"), {"Type": "Control"})


async def worker(slot_id: int, queue: asyncio.Queue):
    while True:
        try:
            job = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
            
        if slot_id in active_terminals:
            # Clear previous visual terminal if we are reusing this slot for a new job
            await broadcast_controlmsg({"action": "closeTerminal", "terminalID": slot_id})
                
        active_terminals[slot_id] = "starting"
        terminal_history[slot_id] = bytearray()
        
        await broadcast_controlmsg({"action": "openTerminal", "terminalID": slot_id})
            
        cmd = job.get_command()

        info_msg = ("\r\n\x1b[38;5;12m >" + ' '.join(cmd) + " \x1b[0m\r\n\r\n").encode("utf-8")
        terminal_history[slot_id] += info_msg
        await broadcast(info_msg, {"Type": "Output", "TerminalID": str(slot_id)})

        try:
            pty = winpty.PtyProcess.spawn(
                cmd,
                dimensions=(24, 80),
                env=env,
            )
            active_terminals[slot_id] = pty
            
            spawn_msg = f"\x1b[38;5;10m[Spawned winpty process PID={pty.pid} for {job.displayname}]\x1b[0m\r\n\r\n".encode("utf-8")
            terminal_history[slot_id] += spawn_msg
            await broadcast(spawn_msg, {"Type": "Output", "TerminalID": str(slot_id)})
            await broadcast_controlmsg({"action": "setTermTitle", "terminalID": slot_id, "title": '[%d] %s' % (pty.pid, job.displayname)})

            loop = asyncio.get_running_loop()
            while pty.isalive():
                data = await loop.run_in_executor(None, pty.read, 4096)
                if data:
                    data_bytes = data if isinstance(data, bytes) else data.encode("utf-8")
                    terminal_history[slot_id] += data_bytes
                    await broadcast(data_bytes, {"Type": "Output", "TerminalID": str(slot_id)})
                        
            # Job finished
            exit_code = pty.wait()
            fin_msg = f"\r\n\x1b[38;5;13m--- Finished {job.displayname} with Code={exit_code} ---\x1b[0m\r\n".encode("utf-8")
            terminal_history[slot_id] += fin_msg
            await broadcast(fin_msg, {"Type": "Output", "TerminalID": str(slot_id)})
                
        except Exception as e:
            err_msg = f"\r\n[Error: {e}]\r\n".encode("utf-8")
            terminal_history[slot_id] += err_msg
            await broadcast(err_msg, {"Type": "Output", "TerminalID": str(slot_id)})
            if slot_id in active_terminals:
                del active_terminals[slot_id]


@app.on_event("startup")
async def startup_event():
    queue = asyncio.Queue()
    for job in antelope_jobs.get_jobs(sys.argv[1:]):
        queue.put_nowait(job)
            
    print(f"Loaded {queue.qsize()} jobs into the encode queue.")
    for slot_id in range(MAX_PARALLEL):
        asyncio.create_task(worker(slot_id, queue))


@app.get("/")
async def index() -> FileResponse:
    return FileResponse("index.html")


@app.websocket("/ws")
async def terminal(websocket: WebSocket) -> None:
    await websocket.accept()
    
    active_websockets.add(websocket)

    init_payload = json.dumps({"action": "init", "maxParallel": MAX_PARALLEL}).encode("utf-8")
    await send_to_ws(websocket, init_payload, {"Type": "Control"})

    for term_id in list(active_terminals.keys()):
        # Send Control message to open terminal
        control_payload = json.dumps({"action": "openTerminal", "terminalID": term_id}).encode("utf-8")
        await send_to_ws(websocket, control_payload, {"Type": "Control"})
        
        # Send history
        history = terminal_history.get(term_id)
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
                        if pty and hasattr(pty, "isalive") and pty.isalive():
                            try:
                                pty.write(data)
                            except TypeError:
                                pty.write(data.decode("utf-8"))
    except (WebSocketDisconnect, ConnectionResetError):
        active_websockets.discard(websocket)


if __name__ == "__main__":
    import uvicorn
    
    # Run the app on localhost at port 8000
    uvicorn.run(
        "main:app", 
        host="127.0.0.1", 
        port=8000, 
        reload=True
    )
