import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import Dict, Optional
from datetime import datetime, timedelta
import secrets
import json

app = FastAPI()

# sessions[otp] = {
#   "creator": Optional[WebSocket],
#   "joiner": Optional[WebSocket],
#   "created_at": datetime,
#   "expires_at": datetime,
# }
sessions: Dict[str, dict] = {}

SESSION_TTL_MINUTES = 10

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")

def new_otp() -> str:
    return str(secrets.randbelow(900000) + 100000)

def get_session(otp: str) -> Optional[dict]:
    session = sessions.get(otp)
    if not session:
        return None
    # Expire old sessions
    if datetime.utcnow() > session["expires_at"]:
        try:
            if session["creator"]:
                _ = session["creator"].close()
            if session["joiner"]:
                _ = session["joiner"].close()
        except Exception:
            pass
        sessions.pop(otp, None)
        return None
    return session

async def notify(ws: Optional[WebSocket], payload: dict):
    if ws is None:
        return
    try:
        await ws.send_json(payload)
    except Exception:
        pass

def touch_session(otp: str):
    """Extend session life a bit on activity."""
    s = sessions.get(otp)
    if s:
        s["expires_at"] = datetime.utcnow() + timedelta(minutes=SESSION_TTL_MINUTES)

@app.websocket("/ws/creator")
async def ws_creator(ws: WebSocket):
    await ws.accept()

    # The creator MAY send a first JSON message: {"resume": true, "otp": "123456"}
    otp = None
    try:
        first = await ws.receive_text()
        try:
            msg = json.loads(first)
        except Exception:
            msg = None
        if isinstance(msg, dict) and msg.get("resume") and msg.get("otp"):
            otp_try = str(msg["otp"])
            session = get_session(otp_try)
            if session:
                # attach as creator
                # close previous creator if any
                try:
                    if session["creator"] and session["creator"] != ws:
                        await session["creator"].close()
                except Exception:
                    pass
                session["creator"] = ws
                otp = otp_try
                await notify(ws, {"type": "otp", "otp": otp})
                # if joiner already there, notify both connected
                if session["joiner"]:
                    await notify(session["creator"], {"type": "connected"})
                    await notify(session["joiner"], {"type": "connected"})
            else:
                # No such session; fall through to create new
                pass
        else:
            # Not a resume — treat it as normal (we'll handle as a new session)
            pass
    except Exception:
        # No first message, proceed creating a session
        pass

    if otp is None:
        # create new session
        otp = new_otp()
        sessions[otp] = {
            "creator": ws,
            "joiner": None,
            "created_at": datetime.utcnow(),
            "expires_at": datetime.utcnow() + timedelta(minutes=SESSION_TTL_MINUTES),
        }
        await notify(ws, {"type": "otp", "otp": otp})

    try:
        while True:
            data = await ws.receive_text()
            touch_session(otp)
            # Route to joiner if present
            session = get_session(otp)
            if not session:
                await notify(ws, {"type": "error", "message": "Session expired"})
                await ws.close()
                break
            await notify(session["joiner"], json.loads(data) if data and data[0] in "{[" else {"type": "text", "data": data})
    except WebSocketDisconnect:
        session = sessions.get(otp)
        if session and session.get("creator") is ws:
            session["creator"] = None
        # Do not delete yet; allow resume within TTL

@app.websocket("/ws/joiner")
async def ws_joiner(ws: WebSocket):
    await ws.accept()
    otp = None
    try:
        # First message must include OTP
        msg = await ws.receive_json()
        otp = str(msg.get("otp"))
        session = get_session(otp)
        if not session:
            await notify(ws, {"type": "error", "message": "Invalid or expired OTP"})
            await ws.close()
            return

        # attach joiner (replace old if any)
        try:
            if session["joiner"] and session["joiner"] != ws:
                await session["joiner"].close()
        except Exception:
            pass

        session["joiner"] = ws
        touch_session(otp)

        # notify both
        await notify(session["creator"], {"type": "connected"})
        await notify(session["joiner"], {"type": "connected"})

        # message loop
        while True:
            data = await ws.receive_text()
            touch_session(otp)
            session = get_session(otp)
            if not session:
                await notify(ws, {"type": "error", "message": "Session expired"})
                await ws.close()
                break
            await notify(session["creator"], json.loads(data) if data and data[0] in "{[" else {"type": "text", "data": data})

    except WebSocketDisconnect:
        session = sessions.get(otp or "")
        if session and session.get("joiner") is ws:
            session["joiner"] = None
        # Keep session to allow rejoin within TTL

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
