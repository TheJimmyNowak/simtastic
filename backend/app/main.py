from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import packets, scenarios, simulation, ws
from app.services.shard_manager import ShardManager
from app.config import settings
from app.models.environment import LORA_PRESETS

app = FastAPI(title="Meshtastic Routing Simulator", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(scenarios.router)
app.include_router(simulation.router)
app.include_router(packets.router)
app.include_router(ws.router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/docker/cleanup-all")
def cleanup_all_docker() -> dict:
    """Emergency: remove all sim-* containers (orphans after uvicorn reload)."""
    removed = ShardManager.cleanup_all_sim_containers()
    return {"containers_removed": removed}


@app.get("/api/presets")
def list_presets() -> dict:
    return {k: v.model_dump() for k, v in LORA_PRESETS.items()}


@app.get("/api/config")
def app_config() -> dict:
    return {
        "dry_run": settings.dry_run,
        "meshtastic_tcp_host": settings.meshtastic_tcp_host,
        "meshtastic_image": settings.meshtastic_image,
    }


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
