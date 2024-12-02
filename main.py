# main.py

import aiohttp
import os
import argparse
import subprocess
import sys
import ssl
import certifi
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse

from transports.services.helpers.daily_rest import DailyRESTHelper, DailyRoomParams

from loguru import logger
from colorama import Fore, Style

from dotenv import load_dotenv

# Load environment variables
load_dotenv(override=True)

# Configure logger with color
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | <level>{message}</level>",
    level="DEBUG",
)
logger.add("logs/voxagent.log", rotation="10 MB")

MAX_BOTS_PER_ROOM = 1

# Bot sub-process dict for status reporting and concurrency control
bot_procs = {}

daily_helpers = {}

def cleanup():
    """Clean up function to terminate all subprocesses."""
    for entry in bot_procs.values():
        proc = entry[0]
        proc.terminate()
        proc.wait()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the lifespan of the FastAPI application."""
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    aiohttp_session = aiohttp.ClientSession(connector=connector)
    daily_helpers["rest"] = DailyRESTHelper(
        daily_api_key=os.getenv("DAILY_API_KEY", ""),
        daily_api_url=os.getenv("DAILY_API_URL", "https://api.daily.co/v1"),
        aiohttp_session=aiohttp_session,
    )
    yield
    await aiohttp_session.close()
    cleanup()

app = FastAPI(lifespan=lifespan)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/start")
async def start_agent(request: Request):
    """Endpoint to start a new agent in a Daily.co room."""
    logger.info("Creating a new room...")
    
    # Define room properties as needed
    room_properties = {
        "start_video_off": True,
        "enable_recording": "cloud",
        # Add more properties if required
    }
    
    room_params = DailyRoomParams(properties=room_properties)
    room = await daily_helpers["rest"].create_room(room_params)
    room_url = room.get("url")
    room_name = room.get("name")
    logger.info(f"Room created: {room_url}")

    if not room_url or not room_name:
        logger.error("Room creation failed: Missing 'url' or 'name' property.")
        raise HTTPException(
            status_code=500,
            detail="Missing 'url' or 'name' property in response data. Cannot start agent without a target room!",
        )

    # Check for existing bots in the room
    num_bots_in_room = sum(
        1
        for proc in bot_procs.values()
        if proc[1] == room_url and proc[0].poll() is None
    )
    if num_bots_in_room >= MAX_BOTS_PER_ROOM:
        logger.error(f"Max bot limit reached for room: {room_url}")
        raise HTTPException(
            status_code=500, detail=f"Max bot limit reached for room: {room_url}"
        )

    # Get token for the room with an expiry time (e.g., 1 hour)
    expiry_time = 60 * 60  # 1 hour in seconds
    try:
        token = await daily_helpers["rest"].get_token(room_name, expiry_time=expiry_time)
    except Exception as e:
        logger.exception(f"Failed to get token for room: {room_url}")
        raise HTTPException(
            status_code=500, detail=f"Failed to get token for room: {room_url}"
        )

    if not token:
        logger.error(f"Failed to retrieve token for room: {room_url}")
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve token for room: {room_url}"
        )

    # Spawn a new agent subprocess
    try:
        proc = subprocess.Popen(
            [
                sys.executable, "runner.py", "-u", room_url, "-t", token
            ],
            bufsize=1,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        bot_procs[proc.pid] = (proc, room_url)
        logger.info(f"Agent started with PID {proc.pid} in room {room_url}")
    except Exception as e:
        logger.exception(f"Failed to start subprocess: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to start subprocess: {e}"
        )

    return RedirectResponse(room_url, status_code=302)

@app.get("/status/{pid}")
def get_status(pid: int):
    """Endpoint to check the status of a bot subprocess."""
    proc = bot_procs.get(pid)

    if not proc:
        logger.warning(f"Bot with PID {pid} not found.")
        raise HTTPException(
            status_code=404, detail=f"Bot with process id: {pid} not found"
        )

    if proc[0].poll() is None:
        status = "running"
    else:
        status = "finished"

    logger.info(f"Status checked for PID {pid}: {status}")
    return JSONResponse({"bot_id": pid, "status": status})

if __name__ == "__main__":
    import uvicorn

    default_host = os.getenv("HOST", "0.0.0.0")
    default_port = int(os.getenv("FAST_API_PORT", "7860"))

    parser = argparse.ArgumentParser(description="VoxAgent FastAPI server")
    parser.add_argument("--host", type=str, default=default_host, help="Host address")
    parser.add_argument(
        "--port", type=int, default=default_port, help="Port number"
    )
    parser.add_argument(
        "--reload", action="store_true", help="Reload code on change"
    )

    config = parser.parse_args()
    logger.info(f"To join a test room, visit http://localhost:{config.port}/start")
    uvicorn.run(
        "main:app",
        host=config.host,
        port=config.port,
        reload=config.reload,
    )
