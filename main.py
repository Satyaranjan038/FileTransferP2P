import asyncio
import json
import random
import string
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Any
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from pydantic import BaseModel

app = FastAPI(title="P2P File Transfer - WebRTC Signaling Server")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure properly for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class SessionManager:
    def __init__(self):
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.connections: Dict[str, WebSocket] = {}
        self.user_to_session: Dict[str, str] = {}
        
    def generate_otp(self) -> str:
        """Generate 6-digit OTP"""
        return ''.join(random.choices(string.digits, k=6))
    
    def create_session(self) -> tuple[str, str]:
        """Create new session and return session_id and OTP"""
        session_id = str(uuid.uuid4())
        otp = self.generate_otp()
        
        self.sessions[session_id] = {
            'otp': otp,
            'created_at': datetime.now(),
            'status': 'waiting_for_peer',
            'users': [],
            'expires_at': datetime.now() + timedelta(minutes=10),
            'creator_id': None,
            'joiner_id': None
        }
        
        return session_id, otp
    
    def validate_otp(self, otp: str) -> Optional[str]:
        """Validate OTP and return session_id if valid"""
        current_time = datetime.now()
        
        for session_id, session in self.sessions.items():
            if (session['otp'] == otp and 
                session['expires_at'] > current_time and
                len(session['users']) < 2):
                return session_id
        
        return None
    
    def add_user_to_session(self, session_id: str, user_id: str, websocket: WebSocket) -> Dict[str, Any]:
        """Add user to session and return session info"""
        if session_id not in self.sessions:
            return {'success': False, 'error': 'Session not found'}
        
        session = self.sessions[session_id]
        
        if len(session['users']) >= 2:
            return {'success': False, 'error': 'Session full'}
        
        # Add user to session
        session['users'].append(user_id)
        self.connections[user_id] = websocket
        self.user_to_session[user_id] = session_id
        
        # Set roles
        if len(session['users']) == 1:
            session['creator_id'] = user_id
            role = 'creator'
        else:
            session['joiner_id'] = user_id
            session['status'] = 'connected'
            role = 'joiner'
        
        return {
            'success': True,
            'role': role,
            'session_status': session['status'],
            'users_count': len(session['users'])
        }
    
    def remove_user_from_session(self, user_id: str) -> None:
        """Remove user and cleanup session if empty"""
        if user_id in self.connections:
            del self.connections[user_id]
        
        if user_id in self.user_to_session:
            session_id = self.user_to_session[user_id]
            del self.user_to_session[user_id]
            
            if session_id in self.sessions:
                session = self.sessions[session_id]
                if user_id in session['users']:
                    session['users'].remove(user_id)
                    
                    # Update creator/joiner IDs
                    if session.get('creator_id') == user_id:
                        session['creator_id'] = None
                    if session.get('joiner_id') == user_id:
                        session['joiner_id'] = None
                
                # Delete session if empty
                if len(session['users']) == 0:
                    del self.sessions[session_id]
                else:
                    session['status'] = 'waiting_for_peer'
    
    def get_peer_connection(self, user_id: str) -> Optional[WebSocket]:
        """Get peer's WebSocket connection"""
        if user_id not in self.user_to_session:
            return None
        
        session_id = self.user_to_session[user_id]
        if session_id not in self.sessions:
            return None
        
        session = self.sessions[session_id]
        if len(session['users']) != 2:
            return None
        
        # Find peer user ID
        peer_id = None
        for uid in session['users']:
            if uid != user_id:
                peer_id = uid
                break
        
        return self.connections.get(peer_id) if peer_id else None
    
    def get_session_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session information"""
        if session_id not in self.sessions:
            return None
        
        session = self.sessions[session_id]
        return {
            'status': session['status'],
            'users_count': len(session['users']),
            'expires_at': session['expires_at'].isoformat(),
            'created_at': session['created_at'].isoformat()
        }
    
    def cleanup_expired_sessions(self) -> None:
        """Remove expired sessions"""
        current_time = datetime.now()
        expired_sessions = [
            session_id for session_id, session in self.sessions.items()
            if session['expires_at'] < current_time
        ]
        
        for session_id in expired_sessions:
            session = self.sessions[session_id]
            # Disconnect users from expired sessions
            for user_id in session['users']:
                if user_id in self.connections:
                    del self.connections[user_id]
                if user_id in self.user_to_session:
                    del self.user_to_session[user_id]
            del self.sessions[session_id]
        
        print(f"Cleaned up {len(expired_sessions)} expired sessions")

# Global session manager
session_manager = SessionManager()

# Pydantic models for API
class OTPRequest(BaseModel):
    otp: str

class OTPResponse(BaseModel):
    success: bool
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    message: str = ""

# REST API Endpoints
@app.post("/generate-otp")
async def generate_otp():
    """Generate new OTP for file transfer session"""
    try:
        session_id, otp = session_manager.create_session()
        return {
            "success": True,
            "otp": otp,
            "session_id": session_id,
            "expires_in_minutes": 10,
            "message": "OTP generated successfully"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate OTP: {str(e)}")

@app.post("/validate-otp", response_model=OTPResponse)
async def validate_otp(request: OTPRequest):
    """Validate OTP and prepare to join session"""
    try:
        session_id = session_manager.validate_otp(request.otp)
        
        if not session_id:
            return OTPResponse(
                success=False,
                message="Invalid or expired OTP"
            )
        
        user_id = str(uuid.uuid4())
        return OTPResponse(
            success=True,
            session_id=session_id,
            user_id=user_id,
            message="OTP validated successfully"
        )
    except Exception as e:
        return OTPResponse(
            success=False,
            message=f"Validation error: {str(e)}"
        )

@app.get("/session-status/{session_id}")
async def get_session_status(session_id: str):
    """Get current session status"""
    try:
        session_info = session_manager.get_session_info(session_id)
        
        if not session_info:
            raise HTTPException(status_code=404, detail="Session not found")
        
        return session_info
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving session: {str(e)}")

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "active_sessions": len(session_manager.sessions),
        "active_connections": len(session_manager.connections),
        "timestamp": datetime.now().isoformat()
    }

# WebSocket endpoint for WebRTC signaling
@app.websocket("/ws/{session_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str, user_id: str):
    """WebSocket endpoint for WebRTC signaling between peers"""
    await websocket.accept()
    
    try:
        # Add user to session
        result = session_manager.add_user_to_session(session_id, user_id, websocket)
        
        if not result['success']:
            await websocket.send_json({
                "type": "error",
                "message": result['error']
            })
            await websocket.close()
            return
        
        # Send connection confirmation
        await websocket.send_json({
            "type": "connected",
            "user_id": user_id,
            "role": result['role'],
            "session_status": result['session_status'],
            "users_count": result['users_count'],
            "message": "Connected to signaling server"
        })
        
        # Handle signaling messages
        while True:
            try:
                data = await websocket.receive_json()
                
                # Handle different types of signaling messages
                if data.get("type") in ["offer", "answer", "ice-candidate", "file-info", "transfer-complete"]:
                    # Forward signaling messages to peer
                    peer_connection = session_manager.get_peer_connection(user_id)
                    
                    if peer_connection:
                        await peer_connection.send_json({
                            "type": data.get("type"),
                            "from": user_id,
                            "data": data.get("data"),
                            "timestamp": datetime.now().isoformat()
                        })
                    else:
                        await websocket.send_json({
                            "type": "error",
                            "message": "Peer not connected"
                        })
                
                elif data.get("type") == "ping":
                    # Respond to ping with pong
                    await websocket.send_json({
                        "type": "pong",
                        "timestamp": datetime.now().isoformat()
                    })
                
                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Unknown message type: {data.get('type')}"
                    })
                    
            except WebSocketDisconnect:
                print(f"WebSocket disconnected for user {user_id}")
                break
            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "message": "Invalid JSON message"
                })
            except Exception as e:
                print(f"WebSocket error for user {user_id}: {e}")
                await websocket.send_json({
                    "type": "error",
                    "message": f"Error processing message: {str(e)}"
                })
                
    except Exception as e:
        print(f"WebSocket connection error for user {user_id}: {e}")
    finally:
        # Clean up user from session
        session_manager.remove_user_from_session(user_id)
        print(f"Cleaned up user {user_id} from session {session_id}")

# Background cleanup task
async def cleanup_task():
    """Periodic cleanup of expired sessions"""
    while True:
        await asyncio.sleep(60)  # Run every minute
        try:
            session_manager.cleanup_expired_sessions()
        except Exception as e:
            print(f"Cleanup task error: {e}")

@app.on_event("startup")
async def startup_event():
    """Start background tasks"""
    print("Starting P2P File Transfer Signaling Server...")
    print("Server handles only WebRTC signaling - files transfer directly between devices")
    asyncio.create_task(cleanup_task())

# Serve static files (frontend)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def read_root():
    """Serve the main application"""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>P2P File Transfer</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; text-align: center; }
            .container { max-width: 600px; margin: 0 auto; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚀 P2P File Transfer</h1>
            <p>Direct device-to-device file sharing up to 5GB</p>
            <p><a href="/static/index.html">Launch Application</a></p>
            <p><small>Files are transferred directly between devices - nothing is stored on our servers!</small></p>
        </div>
    </body>
    </html>
    """)

if __name__ == "__main__":
    print("🚀 Starting P2P File Transfer Server (Python 3.13.2)")
    print("📡 Server handles only WebRTC signaling")
    print("📁 Files transfer directly between devices")
    print("🌐 Access at: http://localhost:8000")
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )