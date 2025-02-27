# Old code:

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import asyncio
import subprocess
import cv2
import logging
import os
import threading
import time
import uuid
from typing import Dict, List
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="RTSP Stream Viewer")

os.makedirs("temp", exist_ok=True)

active_streams: Dict[str, Dict] = {}
connected_clients: Dict[str, List[WebSocket]] = {}

class RTSPStream:
    def __init__(self, rtsp_url: str, stream_id: str = None):
        self.rtsp_url = rtsp_url
        self.stream_id = stream_id or str(uuid.uuid4())
        self.process = None
        self.is_running = False
        self.frame_buffer = []
        self.buffer_lock = threading.Lock()
        self.max_buffer_size = 5
        self.fps = 0
    
    async def start(self):
        if self.is_running:
            return
        
        self.is_running = True
        threading.Thread(target=self._capture_frames, daemon=True).start()
        
        logger.info(f"Started stream {self.stream_id} for {self.rtsp_url}")
        return self.stream_id
    
    def _capture_frames(self):

        command = [
            'ffmpeg',
            '-rtsp_transport', 'tcp',             # Use TCP for more reliable streaming
            '-stimeout', '5000000',               # Socket timeout in microseconds (5 sec)
            '-i', self.rtsp_url,
            '-vsync', '0',                        # No frame rate conversion
            '-flags', 'low_delay',                # Low delay flags
            '-fflags', 'nobuffer+discardcorrupt', # Discard corrupt frames, don't buffer
            '-strict', 'experimental',            # Allow experimental codecs
            '-vcodec', 'rawvideo',
            '-pix_fmt', 'bgr24',
            '-f', 'rawvideo',
            '-an',                                # Disable audio processing
            '-sn',                                # Disable subtitle processing
            '-r', '30',                           # Target 30 fps
            '-'
        ]



        # command = [
        #     'ffmpeg',
        #     '-rtsp_transport', 'tcp',
        #     '-i', self.rtsp_url,
        #     '-vsync', '0',
        #     '-copyts',
        #     '-vcodec', 'rawvideo',
        #     '-pix_fmt', 'bgr24',
        #     '-f', 'rawvideo',
        #     '-'
        # ]
        
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=10**8,
                start_new_session=True 
            )
            
            cap = cv2.VideoCapture(self.rtsp_url)
            
            if not cap.isOpened():
                logger.error(f"Failed to open RTSP stream: {self.rtsp_url}")
                self.is_running = False
                return
                
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            prev_time = time.time()
            frame_count = 0
            
            while self.is_running:
                ret, frame = cap.read()
                if not ret:
                    logger.warning(f"Failed to receive frame from {self.rtsp_url}")
                    time.sleep(0.1)
                    continue
                
                frame_count += 1
                if time.time() - prev_time >= 1:
                    self.fps = frame_count
                    frame_count = 0
                    prev_time = time.time()

                _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                
                with self.buffer_lock:
                    self.frame_buffer.append(jpeg.tobytes())
                    if len(self.frame_buffer) > self.max_buffer_size:
                        self.frame_buffer.pop(0)
            
            cap.release()
            
        except Exception as e:
            logger.error(f"Error in capture thread for {self.rtsp_url}: {str(e)}")
            self.is_running = False
    
    def get_latest_frame(self):
        with self.buffer_lock:
            if not self.frame_buffer:
                return None
            return self.frame_buffer[-1]
    
    def get_fps(self):
        return self.fps
    
    def stop(self):
        self.is_running = False
        logger.info(f"Stopped stream {self.stream_id}")

@app.get("/", response_class=HTMLResponse)
async def get_index():
    with open('../frontend/index.html', 'r') as file:
        return file.read()

@app.post("/streams")
async def create_stream(rtsp_data: dict):
    rtsp_url = rtsp_data.get("rtsp_url")
    if not rtsp_url:
        raise HTTPException(status_code=400, detail="RTSP URL is required")
    
    stream = RTSPStream(rtsp_url)
    stream_id = await stream.start()
    
    active_streams[stream_id] = stream
    connected_clients[stream_id] = []
    
    return {"stream_id": stream_id}

@app.delete("/streams/{stream_id}")
async def delete_stream(stream_id: str):
    if stream_id not in active_streams:
        raise HTTPException(status_code=404, detail="Stream not found")
    
    for client in connected_clients.get(stream_id, [])[:]:
        await client.close()
    
    active_streams[stream_id].stop()
    
    del active_streams[stream_id]
    if stream_id in connected_clients:
        del connected_clients[stream_id]
    
    return {"status": "stopped"}

@app.websocket("/ws/{stream_id}")
async def websocket_endpoint(websocket: WebSocket, stream_id: str):
    if stream_id not in active_streams:
        await websocket.close(code=1008, reason="Stream not found")
        return
    
    await websocket.accept()
    
    if stream_id not in connected_clients:
        connected_clients[stream_id] = []
    connected_clients[stream_id].append(websocket)
    
    try:
        stream = active_streams[stream_id]
        
        last_frame = None
        while stream.is_running:
            frame = stream.get_latest_frame()
            
            if frame and frame != last_frame:
                await websocket.send_bytes(frame)
                last_frame = frame
            
            await asyncio.sleep(0.03)
    
    except WebSocketDisconnect:
        logger.info(f"Client disconnected from stream {stream_id}")
    except Exception as e:
        logger.error(f"Error in WebSocket for stream {stream_id}: {str(e)}")
    finally:
        if stream_id in connected_clients and websocket in connected_clients[stream_id]:
            connected_clients[stream_id].remove(websocket)

@app.on_event("shutdown")
def shutdown_event():
    for stream_id, stream in active_streams.items():
        stream.stop()
    active_streams.clear()
    connected_clients.clear()
