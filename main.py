from __future__ import annotations

import asyncio
import sys
import os
import json
from datetime import datetime

import winpty
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse


DEBUG_MODE = False
print('Emergency stop: taskkill -f -pid', os.getpid(), '\n')

# Configuration
EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.mpeg', '.3gp', '.ts'}
OUTPUT_DIR = "ultrafast"
MAX_PARALLEL = 8
TARGET_DIR = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "."
OUTPUT_PATH = os.path.join(TARGET_DIR, OUTPUT_DIR)

# ffmpeg -threads 1 -i %INPUT% -map 0 -vf scale=1366:768:flags=lanczos -c:v libx264 -crf 23 -preset ultrafast -c:a copy -c:s copy %OUT%
get_command = lambda port, input_path: [
    "ffmpeg.bat",
    "-threads", "1",
    *(("-to", "10") if DEBUG_MODE else ()),  # only the first 10 seconds
    "-i", input_path, "-map", "0",
    "-vf", "scale=1366:768:flags=lanczos",
    "-c:v", "libx264", "-crf", "23", "-preset", "ultrafast",
    "-c:a", "copy", "-c:s", "copy", "-y",
    os.path.join(OUTPUT_PATH, os.path.basename(input_path))
]

# # ffmpeg -threads 1 -i %INPUT% -map 0 -pix_fmt yuv420p -c:v h264_nvenc -rc vbr -cq 23 -preset p1 -c:a copy -c:s copy %OUTPUT%
# get_command = lambda port, input_path: [
#     "ffmpeg.bat",
#     "-threads", "1",
#     *(("-to", "10") if DEBUG_MODE else ()),  # only the first 10 seconds
#     "-i", input_path, "-map", "0",
#     "-vf", "scale=1366:768:flags=lanczos",
#     "-c:v", "h264_nvenc", "-rc", "vbr", "-cq", "23",
#     "-c:a", "copy", "-c:s", "copy", "-y",
#     os.path.join(OUTPUT_PATH, os.path.basename(input_path))
# ]


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


async def worker(slot_id: int, queue: asyncio.Queue):
    global active_websocket
    while True:
        try:
            filename = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
            
        if slot_id in active_terminals:
            # Clear previous visual terminal if we are reusing this slot for a new job
            if active_websocket:
                await send_to_ws(active_websocket, json.dumps({"action": "closeTerminal", "terminalID": slot_id}).encode("utf-8"), {"Type": "Control"})
                
        active_terminals[slot_id] = "starting"
        terminal_history[slot_id] = bytearray()
        
        if active_websocket:
            await send_to_ws(active_websocket, json.dumps({"action": "openTerminal", "terminalID": slot_id}).encode("utf-8"), {"Type": "Control"})
            
        cmd = get_command(0, filename)
        
        info_msg = f"\r\n\x1b[38;5;12m--- Starting processing job for {filename} ---\x1b[0m\r\n\r\n".encode("utf-8")
        terminal_history[slot_id] += info_msg
        if active_websocket:
            await send_to_ws(active_websocket, info_msg, {"Type": "Output", "TerminalID": str(slot_id)})

        try:
            pty = winpty.PtyProcess.spawn(
                cmd,
                dimensions=(24, 80),
                env=env,
            )
            active_terminals[slot_id] = pty
            
            spawn_msg = f"\x1b[38;5;10m[Spawned winpty process for FFmpeg ID={pty.pid}]\x1b[0m\r\n\r\n".encode("utf-8")
            terminal_history[slot_id] += spawn_msg
            if active_websocket:
                await send_to_ws(active_websocket, spawn_msg, {"Type": "Output", "TerminalID": str(slot_id)})

            loop = asyncio.get_running_loop()
            while pty.isalive():
                data = await loop.run_in_executor(None, pty.read, 4096)
                if data:
                    data_bytes = data if isinstance(data, bytes) else data.encode("utf-8")
                    terminal_history[slot_id] += data_bytes
                    if active_websocket:
                        await send_to_ws(active_websocket, data_bytes, {"Type": "Output", "TerminalID": str(slot_id)})
                        
            # Job finished
            exit_code = pty.wait()
            fin_msg = f"\r\n\x1b[38;5;13m--- Finished {filename} with Code={exit_code} ---\x1b[0m\r\n".encode("utf-8")
            terminal_history[slot_id] += fin_msg
            if active_websocket:
                await send_to_ws(active_websocket, fin_msg, {"Type": "Output", "TerminalID": str(slot_id)})
                
        except Exception as e:
            err_msg = f"\r\n[Error: {e}]\r\n".encode("utf-8")
            terminal_history[slot_id] += err_msg
            if active_websocket:
                await send_to_ws(active_websocket, err_msg, {"Type": "Output", "TerminalID": str(slot_id)})
            if slot_id in active_terminals:
                del active_terminals[slot_id]


@app.on_event("startup")
async def startup_event():
    os.makedirs(OUTPUT_PATH, exist_ok=True)
    queue = asyncio.Queue()
    try:
        filenames = os.listdir(TARGET_DIR)
    except FileNotFoundError:
        filenames = []
        print(f"Directory not found: {TARGET_DIR}")
        
    for filename in filenames:
        full_path = os.path.join(TARGET_DIR, filename)
        if os.path.isfile(full_path) and any(filename.lower().endswith(ext) for ext in EXTENSIONS):
            queue.put_nowait(full_path)
            
    print(f"Loaded {queue.qsize()} files from {TARGET_DIR} into the encode queue.")
    for slot_id in range(MAX_PARALLEL):
        asyncio.create_task(worker(slot_id, queue))


@app.get("/")
async def index() -> FileResponse:
    return FileResponse("index.html")


@app.websocket("/ws")
async def terminal(websocket: WebSocket) -> None:
    await websocket.accept()
    
    global active_websocket
    active_websocket = websocket

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
