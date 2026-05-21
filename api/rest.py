from fastapi import APIRouter, Request
from pydantic import BaseModel
from typing import Optional

router = APIRouter()


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _err(msg: str) -> dict:
    return {"ok": False, "data": None, "error": msg}


# ── Request models ────────────────────────────────────────────────────────────

class NewGameRequest(BaseModel):
    color: str = "white"
    elo: Optional[int] = None
    depth: int = 15


class MoveRequest(BaseModel):
    uci: str


class EvaluateRequest(BaseModel):
    fen: str
    depth: int = 15


class EndGameRequest(BaseModel):
    reason: str = "resign"


class EngineOptionsRequest(BaseModel):
    depth: Optional[int] = None
    elo: Optional[int] = None
    threads: Optional[int] = None


# ── Game routes ───────────────────────────────────────────────────────────────

@router.post("/new")
async def new_game(req: NewGameRequest, request: Request):
    gsm = request.app.state.game
    sf = request.app.state.engine

    if sf.is_alive():
        if req.elo is not None:
            await sf.set_option("UCI_LimitStrength", "true")
            await sf.set_option("UCI_Elo", str(req.elo))
        else:
            await sf.set_option("UCI_LimitStrength", "false")
        await sf.new_game()

    state = gsm.new_game(player_color=req.color)
    request.app.state.default_depth = req.depth
    return _ok(state)


@router.get("/state")
async def get_state(request: Request):
    return _ok(request.app.state.game.get_state())


@router.post("/move")
async def make_move(req: MoveRequest, request: Request):
    gsm = request.app.state.game
    result = gsm.make_move(req.uci)
    if not result["ok"]:
        return _err(result["error"])

    # Broadcast updated state over WebSocket
    await request.app.state.ws_manager.broadcast(
        {"event": "game.state", "data": gsm.get_state()}
    )

    over = gsm.is_game_over()
    if over:
        await request.app.state.ws_manager.broadcast(
            {"event": "game.over", "data": {"result": over, "winner": gsm.get_state()["winner"]}}
        )

    return _ok(result)


@router.get("/move/best")
async def get_best_move(request: Request, depth: int = 15):
    gsm = request.app.state.game
    sf = request.app.state.engine

    if gsm.is_game_over():
        return _err("Game is already over")
    if not sf.is_alive():
        return _err("Stockfish engine is not available")

    try:
        result = await sf.get_best_move(gsm.move_history, depth=depth)
    except Exception as e:
        return _err(str(e))
    return _ok(result)


@router.post("/move/evaluate")
async def evaluate_position(req: EvaluateRequest, request: Request):
    sf = request.app.state.engine
    if not sf.is_alive():
        return _err("Stockfish engine is not available")
    try:
        result = await sf.evaluate_position(req.fen, depth=req.depth)
    except Exception as e:
        return _err(str(e))
    return _ok(result)


@router.get("/history")
async def get_history(request: Request):
    gsm = request.app.state.game
    return _ok({"history": gsm.move_history, "game_id": gsm.game_id})


@router.post("/end")
async def end_game(req: EndGameRequest, request: Request):
    result = request.app.state.game.end_game(reason=req.reason)
    await request.app.state.ws_manager.broadcast(
        {"event": "game.over", "data": {"result": req.reason, "winner": None}}
    )
    return _ok(result)


# ── Engine routes ─────────────────────────────────────────────────────────────

engine_router = APIRouter()


@engine_router.get("/status")
async def engine_status(request: Request):
    alive = request.app.state.engine.is_alive()
    return _ok({"alive": alive, "path": request.app.state.engine._path})


@engine_router.post("/options")
async def set_options(req: EngineOptionsRequest, request: Request):
    sf = request.app.state.engine
    if sf.is_alive():
        if req.threads is not None:
            await sf.set_option("Threads", str(req.threads))
        if req.elo is not None:
            await sf.set_option("UCI_LimitStrength", "true")
            await sf.set_option("UCI_Elo", str(req.elo))
    if req.depth is not None:
        request.app.state.default_depth = req.depth
    return _ok({"applied": True, "engine_alive": sf.is_alive()})
