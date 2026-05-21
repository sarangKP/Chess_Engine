import json
from typing import Any

import chess
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState


class ConnectionManager:
    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections = [c for c in self._connections if c is not ws]

    async def broadcast(self, payload: dict) -> None:
        dead: list[WebSocket] = []
        for ws in self._connections:
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_text(json.dumps(payload))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def send(self, ws: WebSocket, payload: dict) -> None:
        await ws.send_text(json.dumps(payload))


async def ws_endpoint(ws: WebSocket) -> None:
    app = ws.app
    mgr: ConnectionManager = app.state.ws_manager
    gsm = app.state.game
    sf = app.state.engine

    await mgr.connect(ws)
    await mgr.send(ws, {"event": "engine.ready", "data": {"alive": sf.is_alive()}})
    await mgr.send(ws, {"event": "game.state", "data": gsm.get_state()})

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg: dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                await mgr.send(ws, {"event": "error", "data": {"message": "Invalid JSON"}})
                continue

            event = msg.get("event", "")
            data = msg.get("data", {})

            if event == "game.new":
                color = data.get("color", "white")
                if color not in ("white", "black"):
                    color = "white"
                elo = data.get("elo")
                depth = data.get("depth", app.state.default_depth)

                if sf.is_alive():
                    if elo is not None:
                        await sf.set_option("UCI_LimitStrength", "true")
                        await sf.set_option("UCI_Elo", str(elo))
                    else:
                        await sf.set_option("UCI_LimitStrength", "false")
                    await sf.new_game()

                state = await gsm.new_game(player_color=color)
                app.state.default_depth = depth
                await mgr.broadcast({"event": "game.state", "data": state})

            elif event == "move.submit":
                uci = data.get("uci", "")
                if not isinstance(uci, str) or not (4 <= len(uci) <= 5):
                    await mgr.send(ws, {
                        "event": "move.illegal",
                        "data": {"uci": uci, "reason": "Malformed UCI"},
                    })
                    continue

                result = await gsm.make_move(uci)

                if not result["ok"]:
                    await mgr.send(ws, {
                        "event": "move.illegal",
                        "data": {"uci": uci, "reason": result["error"]},
                    })
                    continue

                await mgr.broadcast({"event": "game.state", "data": gsm.get_state()})

                over = gsm.is_game_over()
                if over:
                    await mgr.broadcast({
                        "event": "game.over",
                        "data": {"result": over, "winner": gsm.get_state()["winner"]},
                    })
                    continue

                if gsm.is_engine_turn():
                    await _run_engine_turn(app, mgr, gsm, sf)

            elif event == "game.resign":
                await gsm.end_game("resign")
                await mgr.broadcast({
                    "event": "game.over",
                    "data": {"result": "resign", "winner": None},
                })

            elif event == "engine.configure":
                depth = data.get("depth")
                elo = data.get("elo")
                threads = data.get("threads")
                if sf.is_alive():
                    if threads is not None:
                        await sf.set_option("Threads", str(threads))
                    if elo is not None:
                        await sf.set_option("UCI_LimitStrength", "true")
                        await sf.set_option("UCI_Elo", str(elo))
                if depth is not None:
                    app.state.default_depth = depth
                await mgr.send(ws, {"event": "engine.configured", "data": {"engine_alive": sf.is_alive()}})

            else:
                await mgr.send(ws, {
                    "event": "error",
                    "data": {"message": f"Unknown event: {event}"},
                })

    except WebSocketDisconnect:
        mgr.disconnect(ws)


async def _run_engine_turn(app, mgr: ConnectionManager, gsm, sf) -> None:
    depth = app.state.default_depth

    async def thinking_cb(info: dict) -> None:
        await mgr.broadcast({"event": "engine.thinking", "data": info})

    try:
        result = await sf.get_best_move(
            gsm.move_history, depth=depth, thinking_callback=thinking_cb
        )
    except Exception as e:
        await mgr.broadcast({"event": "engine.error", "data": {"message": str(e)}})
        return

    best_uci = result.get("move")
    if not best_uci or best_uci == "(none)":
        return

    try:
        san = gsm.board.san(gsm.board.parse_uci(best_uci))
    except Exception:
        san = best_uci

    await mgr.broadcast({
        "event": "engine.bestmove",
        "data": {
            "uci": best_uci,
            "san": san,
            "score_cp": result.get("score_cp"),
            "score_mate": result.get("score_mate"),
            "depth": result.get("depth"),
            "pv": result.get("pv", [])[:3],
        },
    })

    move_result = await gsm.make_move(best_uci)
    if move_result["ok"]:
        await mgr.broadcast({"event": "game.state", "data": gsm.get_state()})
        over = gsm.is_game_over()
        if over:
            await mgr.broadcast({
                "event": "game.over",
                "data": {"result": over, "winner": gsm.get_state()["winner"]},
            })
