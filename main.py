import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import secrets

app = FastAPI()
sessions = {}  # {otp: [ws_creator, ws_joiner]}

# Mount static folder
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.websocket("/ws/{role}")
async def websocket_endpoint(ws: WebSocket, role: str):
    await ws.accept()

    if role == "creator":
        otp = str(secrets.randbelow(900000) + 100000)  # 6-digit OTP
        sessions[otp] = [ws]
        await ws.send_json({"type": "otp", "otp": otp})

    elif role == "joiner":
        # First message must be OTP
        msg = await ws.receive_json()
        otp = msg.get("otp")
        if otp not in sessions:
            await ws.send_json({"type": "error", "message": "Invalid OTP"})
            await ws.close()
            return
        sessions[otp].append(ws)
        # notify creator + joiner
        await sessions[otp][0].send_json({"type": "connected"})
        await ws.send_json({"type": "connected"})

    try:
        while True:
            data = await ws.receive_text()
            # broadcast inside session
            for otp, conns in sessions.items():
                if ws in conns:
                    for conn in conns:
                        if conn != ws:
                            await conn.send_text(data)
    except WebSocketDisconnect:
        for otp, conns in list(sessions.items()):
            if ws in conns:
                conns.remove(ws)
            if not conns:
                del sessions[otp]

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
