import asyncio
import logging
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RTSPStream:
    def __init__(self, rtsp_url):
        self.rtsp_url = rtsp_url
        self.is_running = False
    
    async def start(self):
        logger.info(f"Starting RTSP stream: {self.rtsp_url}")
        self.is_running = True
        return str(hash(self.rtsp_url))
    
    def stop(self):
        logger.info(f"Stopping RTSP stream: {self.rtsp_url}")
        self.is_running = False
    
    def get_latest_frame(self):
        return b"frame_data"

active_streams = {}
connected_clients = {}

app = FastAPI()

@app.get("/", response_class=HTMLResponse)
async def get_index():
    with open("frontend\index.html", "r") as file:
        return HTMLResponse(content=file.read(), status_code=200)

@app.post("/streams")
async def create_stream(stream_data: dict):
    rtsp_url = stream_data.get("rtsp_url")
    if not rtsp_url:
        raise HTTPException(status_code=400, detail="RTSP URL is required")
    
    stream = RTSPStream(rtsp_url)
    stream_id = await stream.start()
    
    active_streams[stream_id] = stream
    connected_clients[stream_id] = []
    
    return {"stream_id": stream_id}

@app.post("/streams/{stream_id}/reconnect")
async def reconnect_stream(stream_id: str):
    if stream_id not in active_streams:
        raise HTTPException(status_code=404, detail="Stream not found")
    
    stream = active_streams[stream_id]
    
    stream.stop()
    
    await asyncio.sleep(1)
    
    await stream.start()
    
    return {"status": "reconnecting", "stream_id": stream_id}

@app.delete("/streams/{stream_id}")
async def delete_stream(stream_id: str):
    if stream_id not in active_streams:
        raise HTTPException(status_code=404, detail="Stream not found")
    
    for client in connected_clients.get(stream_id, [])[:]:
        await client.close(code=1000, reason="Stream deleted")
    
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
        fail_count = 0
        max_fail_count = 50
        
        while stream.is_running:
            try:
                frame = stream.get_latest_frame()
                
                if frame and frame != last_frame:
                    await websocket.send_bytes(frame)
                    last_frame = frame
                    fail_count = 0
                else:
                    fail_count += 1
                    if fail_count >= max_fail_count:
                        logger.warning(f"No new frames received for WebSocket {stream_id} after {max_fail_count} attempts")
                        fail_count = 0
                
                await asyncio.sleep(0.033)
                
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error sending frame: {str(e)}")
                await asyncio.sleep(0.1)
    
    except WebSocketDisconnect:
        logger.info(f"Client disconnected from stream {stream_id}")
    except asyncio.CancelledError:
        logger.info(f"WebSocket task cancelled for stream {stream_id}")
    except Exception as e:
        logger.error(f"Error in WebSocket for stream {stream_id}: {str(e)}")
    finally:
        if stream_id in connected_clients and websocket in connected_clients[stream_id]:
            connected_clients[stream_id].remove(websocket)

@app.on_event("shutdown")
def shutdown_event():
    logger.info("Shutting down and cleaning up resources...")
    
    for stream_id, stream in list(active_streams.items()):
        try:
            stream.stop()
        except Exception as e:
            logger.error(f"Error stopping stream {stream_id}: {str(e)}")
    
    active_streams.clear()
    connected_clients.clear()

if __name__ == "__main__":
    import sys
    
    log_level = "info"
    
    for arg in sys.argv:
        if arg.startswith("--log-level="):
            log_level = arg.split("=")[1].lower()
    
    uvicorn_log_level = log_level
    if log_level == "debug":
        logging.basicConfig(level=logging.DEBUG)
    elif log_level == "warning":
        uvicorn_log_level = "warning"
    
    uvicorn.run(
        "main:app", 
        host="0.0.0.0", 
        port=8001, 
        log_level=uvicorn_log_level,
        reload=True
    )
