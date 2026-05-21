from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.rest import router as game_router, engine_router
from api.websocket import ConnectionManager, ws_endpoint
from engine.stockfish_manager import StockfishManager
from game.state_manager import GameStateManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    sf = StockfishManager()
    try:
        await sf.start()
    except Exception as e:
        print(f"[warn] Stockfish failed to start: {e}")
        print("[warn] Engine features will be unavailable until Stockfish is installed.")

    app.state.engine = sf
    app.state.game = GameStateManager()
    app.state.ws_manager = ConnectionManager()
    app.state.default_depth = 15

    yield

    await sf.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Chess Engine Service",
        description="Stockfish-backed chess service with REST and WebSocket interfaces",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.include_router(game_router, prefix="/game", tags=["game"])
    app.include_router(engine_router, prefix="/engine", tags=["engine"])
    app.add_api_websocket_route("/ws", ws_endpoint)

    app.mount("/", StaticFiles(directory="static", html=True), name="static")

    return app


app = create_app()
