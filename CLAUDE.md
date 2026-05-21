# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the service
.venv/bin/python main.py
# or
uv run python main.py

# Install / sync dependencies
uv sync
uv add <package>
```

The server starts on `http://0.0.0.0:8000`. Interactive API docs at `/docs`.

## Environment

- Python 3.10 (pinned in `.python-version`), managed with `uv`
- Stockfish binary path defaults to `/usr/games/stockfish`; override with `STOCKFISH_PATH=<path>`
- Install Stockfish: `sudo apt install stockfish`

## Architecture

Three layers share a single FastAPI app (`app.py`). All singletons live on `app.state` and are wired in the `lifespan` context manager:

| `app.state` key | Type | Purpose |
|---|---|---|
| `engine` | `StockfishManager` | owns the Stockfish subprocess |
| `game` | `GameStateManager` | owns the board and move history |
| `ws_manager` | `ConnectionManager` | broadcasts to WebSocket clients |
| `default_depth` | `int` | search depth used by auto-play |

**Layer 1 — `engine/stockfish_manager.py`**
Async subprocess wrapper around the Stockfish UCI protocol. An `asyncio.Lock` (`_search_lock`) serialises all `go` commands. The manager degrades gracefully if the binary is missing (`_ever_started` flag prevents retry loops). `get_best_move()` accepts an optional `thinking_callback` coroutine that fires on every `info depth` line — used by WebSocket to stream progressive updates.

**Layer 2 — `game/state_manager.py`**
Wraps a `python-chess` `Board`. Move history is stored as a flat list of UCI strings (e.g. `["e2e4", "e7e5"]`) — this list is replayed to Stockfish on every search via `position startpos moves …`. `make_move()` returns a rich dict including SAN, capture info, and special-move flags. `is_engine_turn()` compares the board's current side-to-move against `player_color` to decide whether to trigger auto-play.

**Layer 3 — `api/rest.py` + `api/websocket.py`**
REST router is mounted at `/game` and `/engine`. All REST responses use `{"ok": bool, "data": …, "error": str|null}`. Engine-dependent endpoints (`/game/move/best`, `/game/move/evaluate`) return `ok: false` with a message rather than 500 when Stockfish is unavailable.

The WebSocket endpoint (`/ws`) accepts JSON frames `{"event": "<name>", "data": {…}}`. On `move.submit` the handler validates, applies the move, then calls `_run_engine_turn()` if it's now the engine's side. Engine thinking is streamed as `engine.thinking` events before the final `engine.bestmove`.

**Single-game model:** there is exactly one `GameStateManager` instance. This matches the physical constraint of one robot arm and one board. Multi-session support would require per-connection state isolation.
