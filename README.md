# Chess Engine Service

A three-layer service that wraps Stockfish's UCI protocol and exposes it over both a REST API and a WebSocket interface. The REST layer is designed for tool-use by LLM agents; the WebSocket layer is designed for real-time clients like a robot arm controller or a live frontend board.

```
Robot Arm  ──┐
             │
LLM Agent  ──┼──►  Chess Engine Service  ──►  Stockfish (UCI)
             │
Frontend   ──┘
```

---

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (package manager)
- Stockfish binary

```bash
sudo apt install stockfish        # Ubuntu/Debian
brew install stockfish            # macOS
```

---

## Quick Start

```bash
uv sync                           # install dependencies into .venv
uv run python main.py             # start on http://0.0.0.0:8000
```

Override the Stockfish path if it isn't at `/usr/games/stockfish`:

```bash
STOCKFISH_PATH=/usr/local/bin/stockfish uv run python main.py
```

Interactive API docs (Swagger UI) are available at `http://localhost:8000/docs` once the server is running.

---

## Architecture

The service has three layers. Each layer talks only to the one below it; all three are wired together at startup in `app.py` and attached to `app.state` so FastAPI route handlers can reach them without global variables.

```
┌─────────────────────────────────────────┐
│  Layer 3 — Interface                    │
│  api/rest.py        api/websocket.py    │
└────────────────┬────────────────────────┘
                 │
┌────────────────▼────────────────────────┐
│  Layer 2 — Game State                   │
│  game/state_manager.py                  │
└────────────────┬────────────────────────┘
                 │
┌────────────────▼────────────────────────┐
│  Layer 1 — Engine Process               │
│  engine/stockfish_manager.py            │
└─────────────────────────────────────────┘
```

### Layer 1 — `engine/stockfish_manager.py`

Owns the Stockfish child process. Communicates over stdin/stdout using the UCI protocol via `asyncio.create_subprocess_exec`. All I/O is async.

Key design decisions:

- **`asyncio.Lock` on every search.** Stockfish is single-threaded per session — only one `go` command can be in flight. `_search_lock` enforces this across both REST and WebSocket callers.
- **`_ever_started` flag.** On startup the manager attempts to spawn Stockfish. If the binary is missing it sets a flag and does not retry on every subsequent call (which would flood logs). Engine-dependent endpoints return a clean error message instead of a 500.
- **Thinking callback.** `get_best_move()` accepts an optional `thinking_callback` coroutine. It is called on every `info depth …` line Stockfish emits before the final `bestmove`, which lets the WebSocket layer stream live depth updates to connected clients.
- **Auto-restart.** If the process was previously alive and then dies (crash), the next call will respawn it.

UCI commands used:

| Command | When |
|---|---|
| `uci` | spawn — waits for `uciok` |
| `isready` | after `ucinewgame` — waits for `readyok` |
| `ucinewgame` | `new_game()` — clears engine hash tables |
| `setoption name … value …` | ELO limit, thread count |
| `position startpos moves <list>` | before every search |
| `go depth <n>` | triggers search |
| `quit` | clean shutdown |

### Layer 2 — `game/state_manager.py`

Maintains the canonical game state using a `python-chess` `Board` object.

**Move history** is stored as a flat list of UCI strings (e.g. `["e2e4", "e7e5", "g1f3"]`). Stockfish is stateless between calls — this list is replayed via `position startpos moves …` on every search request.

`make_move(uci)` does the following in order:
1. Parses the UCI string with `board.parse_uci()` — raises `ValueError` for malformed strings.
2. Checks legality against `board.legal_moves`.
3. Extracts pre-push metadata: captured piece (including en passant target square), castling flag.
4. Pushes the move, appends to history.
5. Returns a result dict with SAN notation, updated FEN, all special-move flags, and the new game status.

`is_engine_turn()` compares the board's current side-to-move against `player_color` to decide whether the service should auto-play. The rest of the game-over detection (`is_game_over()`) covers checkmate, stalemate, insufficient material, 75-move rule, and fivefold repetition — all via `python-chess`.

### Layer 3a — REST API (`api/rest.py`)

Mounted at `/game` and `/engine`. All responses share a consistent envelope:

```json
{ "ok": true,  "data": { … }, "error": null   }
{ "ok": false, "data": null,  "error": "reason" }
```

| Method | Path | Description |
|---|---|---|
| `POST` | `/game/new` | Start a new game. Body: `color` (`"white"`/`"black"`), `depth` (int), `elo` (int, optional — enables strength limiting) |
| `GET` | `/game/state` | Full board state: FEN, turn, history, captured pieces, status |
| `POST` | `/game/move` | Submit a move. Body: `uci` (e.g. `"e2e4"`). Validates legality before applying |
| `GET` | `/game/move/best` | Ask Stockfish for the best move in the current position. Query param: `depth`. Does **not** apply the move |
| `POST` | `/game/move/evaluate` | Evaluate an arbitrary FEN. Body: `fen`, `depth`. Returns best move + score |
| `GET` | `/game/history` | Move history as a list of UCI strings |
| `POST` | `/game/end` | End the game. Body: `reason` (`"resign"`, `"draw"`, etc.) |
| `GET` | `/engine/status` | Whether the Stockfish process is alive |
| `POST` | `/engine/options` | Set `depth`, `elo`, or `threads` mid-game |

### Layer 3b — WebSocket (`api/websocket.py`)

Single endpoint: `ws://localhost:8000/ws`

On connection the server immediately sends `engine.ready` and `game.state` so the client has full context without needing an extra request.

All frames are JSON with the shape `{ "event": "<name>", "data": { … } }`.

**Client → Server:**

| Event | Data | Description |
|---|---|---|
| `game.new` | `color`, `elo?`, `depth?` | Start a new game |
| `move.submit` | `uci` | Submit a human or arm move |
| `game.resign` | — | End the game |
| `engine.configure` | `depth?`, `elo?`, `threads?` | Adjust engine settings mid-game |

**Server → Client (broadcast to all connections):**

| Event | Data | Description |
|---|---|---|
| `game.state` | full state dict | Emitted after every move (human or engine) |
| `engine.thinking` | `depth`, `score_cp`, `score_mate`, `pv` | Progressive depth updates while Stockfish searches |
| `engine.bestmove` | `uci`, `san`, `score_cp`, `score_mate`, `depth`, `pv` | Final move decision, before it is applied to the board |
| `game.over` | `result`, `winner` | Checkmate, stalemate, draw, or resign |
| `move.illegal` | `uci`, `reason` | Sent only to the submitting client |
| `engine.ready` | `alive` | Sent on connect |
| `engine.error` | `message` | Engine failure during auto-play |

**Auto-play flow** (triggered when `move.submit` results in it being the engine's turn):

1. `_run_engine_turn()` calls `sf.get_best_move()` with a thinking callback.
2. Every `info depth` line from Stockfish fires the callback → broadcasts `engine.thinking`.
3. When Stockfish emits `bestmove`, the callback returns → `engine.bestmove` is broadcast.
4. The move is applied to `GameStateManager` → `game.state` is broadcast.
5. If the game is now over, `game.over` is broadcast.

The robot arm controller should listen for `engine.bestmove` to begin moving (source square, destination square, capture/castling/promotion flags are all in the `game.state` that follows).

---

## Data Formats

| Data | Format | Example |
|---|---|---|
| Board position | FEN string | `rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1` |
| Moves (internal) | UCI notation | `e2e4`, `e1g1` (castling), `e7e8q` (promotion) |
| Moves (display) | SAN notation | `e4`, `O-O`, `e8=Q` |
| Evaluation | Centipawns | `+43` = White slightly better, `-43` = Black slightly better |
| Forced mate | `score_mate` int | `3` = mate in 3 for the side to move |
| Piece identity | `python-chess` piece name | `"pawn"`, `"rook"`, `"queen"` |

---

## Concurrency Model

There is one `GameStateManager` and one `StockfishManager` instance shared across all connections (single-game server model). This matches the physical constraint: one board, one game at a time.

WebSocket connections are tracked in `ConnectionManager._connections`. All broadcast calls iterate this list and silently drop dead connections. The `_search_lock` in `StockfishManager` means that even if multiple WebSocket clients submit moves concurrently, Stockfish processes them one at a time.

---

## LLM Integration

**Yes — the endpoints are directly usable by any LLM agent framework** (Claude tool use, OpenAI function calling, LangChain, LlamaIndex, etc.). No changes to this service are required.

### How it works

Expose the four key REST endpoints as tools in your LLM's tool definition. The agent then drives the game entirely through HTTP calls:

| Tool (endpoint) | What the LLM uses it for |
|---|---|
| `GET /game/state` | Read the board — returns FEN, move history, whose turn, check/checkmate, captured pieces |
| `POST /game/move` | Play a move — validates legality, returns SAN notation and updated state |
| `GET /game/move/best` | Ask Stockfish for the best move + centipawn score + principal variation |
| `POST /game/move/evaluate` | Evaluate any position by FEN — useful for the LLM to reason about candidate moves |

### What the LLM can do with this

Because `game.state` returns the full move history in UCI notation, the LLM can:
- **Recognise openings** — e.g. `["e2e4","e7e5","g1f3","b1c3","f1c4"]` is the Italian Opening
- **Generate natural commentary** — *"You played the Italian — I'll counter with the Classical Defence"*
- **Reason about tactics** — use `evaluate` to compare candidate moves before committing
- **Act as opponent or coach** — either play its own moves or just comment on Stockfish's moves

### Minimal tool definition (Claude / Anthropic SDK example)

```python
tools = [
    {
        "name": "get_game_state",
        "description": "Returns the current chess board state: FEN, move history, whose turn, check/checkmate status.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "play_move",
        "description": "Play a move on the board. Input must be UCI notation (e.g. 'e2e4'). Returns updated board state.",
        "input_schema": {
            "type": "object",
            "properties": {"uci": {"type": "string", "description": "Move in UCI notation, e.g. 'e2e4'"}},
            "required": ["uci"],
        },
    },
    {
        "name": "get_best_move",
        "description": "Ask Stockfish for the best move in the current position. Returns move, score, and continuation.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]
```

Map each tool name to its corresponding HTTP call against `http://localhost:8000`.

### WebSocket alternative

For real-time integration (e.g. streaming commentary as Stockfish thinks), connect to `ws://localhost:8000/ws` instead. The agent receives `engine.thinking` events (progressive depth updates) and `game.state` after every move automatically — no polling needed.
