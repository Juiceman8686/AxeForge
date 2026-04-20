import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from database import Database
from miner_client import MinerClient
from tuner import HARD_MAX_TEMP, HARD_MAX_VOLTAGE, TunerConfig, TunerManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

db        = Database()
tuner_mgr = TunerManager(db)

# ── Shared live state ─────────────────────────────────────────────────────────
ws_clients: List[WebSocket] = []
# { miner_id: {"online": bool, "stats": dict|None, "last_metrics_save": float} }
live_cache: Dict[int, Dict] = {}


async def broadcast(data: dict):
    dead = []
    msg  = json.dumps(data)
    for ws in ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)


async def _polling_loop():
    """Poll every miner's API once per second; persist metrics every 10 s."""
    while True:
        miners = db.get_miners()
        for m in miners:
            mid    = m["id"]
            client = MinerClient(m["ip"])

            stats = await client.get_stats()
            prev  = live_cache.get(mid, {})

            if stats:
                live_cache[mid] = {
                    "online":    True,
                    "stats":     stats,
                    "last_save": prev.get("last_save", 0.0),
                }
                # Save to DB every ~10 s
                if time.time() - prev.get("last_save", 0.0) >= 10:
                    db.save_metrics(mid, stats)
                    live_cache[mid]["last_save"] = time.time()

                tuner_mgr.on_stats_update(mid, stats)
            else:
                was_online = prev.get("online", True)
                if was_online:
                    tuner_mgr.on_miner_offline(mid)
                live_cache[mid] = {
                    "online":    False,
                    "stats":     prev.get("stats"),   # keep last known
                    "last_save": prev.get("last_save", 0.0),
                }

        await asyncio.sleep(1)


async def _broadcast_loop():
    """Push state to all connected WebSocket clients every second."""
    while True:
        if ws_clients:
            miners  = db.get_miners()
            payload = []
            for m in miners:
                mid   = m["id"]
                cache = live_cache.get(mid, {"online": False, "stats": None})
                payload.append({
                    "miner":  m,
                    "online": cache["online"],
                    "stats":  cache["stats"],
                    "tuning": tuner_mgr.get_status(mid),
                })
            await broadcast({"type": "miners_update", "data": payload})
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_polling_loop())
    asyncio.create_task(_broadcast_loop())
    yield


app = FastAPI(title="AxeForge", version="1.0.0", lifespan=lifespan)

# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.append(ws)
    try:
        while True:
            await ws.receive_text()   # keep connection alive / receive pings
    except WebSocketDisconnect:
        pass
    finally:
        if ws in ws_clients:
            ws_clients.remove(ws)


# ── Miners ────────────────────────────────────────────────────────────────────

@app.get("/api/miners")
async def list_miners():
    miners = db.get_miners()
    out    = []
    for m in miners:
        cache = live_cache.get(m["id"], {"online": False, "stats": None})
        out.append({
            "miner":  m,
            "online": cache["online"],
            "stats":  cache["stats"],
            "tuning": tuner_mgr.get_status(m["id"]),
        })
    return out


@app.post("/api/miners")
async def add_miner(body: dict):
    ip = (body.get("ip") or "").strip()
    if not ip:
        raise HTTPException(400, "ip is required")

    # Try to pull name from AxeOS
    nickname = (body.get("nickname") or "").strip() or None
    if not nickname:
        client = MinerClient(ip)
        stats  = await client.get_stats()
        if stats:
            nickname = (
                stats.get("hostname")
                or stats.get("deviceModel")
                or stats.get("ASICModel")
            )

    miner_id = db.add_miner(ip, nickname or ip)
    miner = db.get_miner_by_id(miner_id)
    return {
        "miner":  miner,
        "online": False,
        "stats":  None,
        "tuning": tuner_mgr.get_status(miner_id),
    }


@app.patch("/api/miners/{miner_id}")
async def update_miner(miner_id: int, body: dict):
    if "nickname" in body:
        db.update_miner_nickname(miner_id, body["nickname"])
    return {"ok": True}


@app.delete("/api/miners/{miner_id}")
async def delete_miner(miner_id: int):
    await tuner_mgr.stop_tuning(miner_id)
    db.delete_miner(miner_id)
    live_cache.pop(miner_id, None)
    return {"ok": True}


# ── Tuning ────────────────────────────────────────────────────────────────────

@app.post("/api/miners/{miner_id}/tune/start")
async def start_tuning(miner_id: int, body: dict):
    miner = db.get_miner_by_id(miner_id)
    if not miner:
        raise HTTPException(404, "Miner not found")

    cache = live_cache.get(miner_id, {})
    if not cache.get("online"):
        raise HTTPException(400, "Miner is offline")

    settings = db.get_all_settings()

    raw_max_temp = float(body.get("max_temp", settings.get("max_temp", 65)))
    raw_max_volt = int(body.get("max_voltage", settings.get("max_voltage", 1250)))
    raw_max_freq = int(body.get("max_freq", settings.get("max_freq", 1000)))

    config = TunerConfig(
        priority           = body.get("priority", ["hashrate", "temp", "error_rate"]),
        step_mode          = body.get("step_mode", "slow"),
        max_temp           = min(raw_max_temp, HARD_MAX_TEMP),
        max_voltage        = min(raw_max_volt,  HARD_MAX_VOLTAGE),
        max_freq           = min(raw_max_freq,  HARD_MAX_FREQ),
        error_threshold    = float(body.get("error_threshold",
                                            settings.get("error_rate_threshold", 1.0))),
        time_limit_minutes = body.get("time_limit_minutes"),   # None = indefinite
        baseline_freq      = int(body.get("baseline_freq", 525)),
        baseline_voltage   = int(body.get("baseline_voltage", 1150)),
    )

    started = await tuner_mgr.start_tuning(miner_id, config)
    if not started:
        raise HTTPException(400, "Tuning already in progress for this miner")
    return {"ok": True}


@app.post("/api/miners/{miner_id}/tune/stop")
async def stop_tuning(miner_id: int):
    await tuner_mgr.stop_tuning(miner_id)
    return {"ok": True}


@app.post("/api/tune/stop-all")
async def stop_all():
    for m in db.get_miners():
        await tuner_mgr.stop_tuning(m["id"])
    return {"ok": True}


# ── Sessions / History ────────────────────────────────────────────────────────

@app.get("/api/sessions")
async def list_sessions(miner_id: Optional[int] = None):
    return db.get_sessions(miner_id)


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: int):
    sessions = db.get_sessions()
    session  = next((s for s in sessions if s["id"] == session_id), None)
    if not session:
        raise HTTPException(404, "Session not found")
    return {**session, "datapoints": db.get_session_datapoints(session_id)}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: int):
    db.delete_session(session_id)
    return {"ok": True}


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    return db.get_all_settings()


@app.patch("/api/settings")
async def update_settings(body: dict):
    # Enforce hard ceilings silently
    if "max_temp" in body:
        body["max_temp"] = str(min(float(body["max_temp"]), HARD_MAX_TEMP))
    if "max_voltage" in body:
        body["max_voltage"] = str(min(int(body["max_voltage"]), HARD_MAX_VOLTAGE))
    if "max_freq" in body:
        body["max_freq"] = str(min(int(body["max_freq"]), HARD_MAX_FREQ))
    for k, v in body.items():
        db.set_setting(k, str(v))
    return {"ok": True}


# ── Static files (frontend) ───────────────────────────────────────────────────

static_dir = Path(__file__).parent / "static"

@app.get("/")
async def root():
    return FileResponse(str(static_dir / "index.html"))

app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
