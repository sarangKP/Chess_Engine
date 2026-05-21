import asyncio
import logging
import os
import re
from asyncio.subprocess import PIPE
from typing import Optional

logger = logging.getLogger(__name__)

STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish")


class StockfishManager:
    def __init__(self, path: str = STOCKFISH_PATH):
        self._path = path
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._search_lock = asyncio.Lock()
        self._ever_started = False

    async def start(self) -> None:
        await self._spawn()

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.stdin.write(b"quit\n")
            await self._proc.stdin.drain()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._proc.kill()

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _spawn(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self._path,
            stdin=PIPE,
            stdout=PIPE,
            stderr=PIPE,
        )
        asyncio.create_task(self._drain_stderr())
        await self._send("uci")
        await self._read_until("uciok")
        self._ever_started = True

    async def _drain_stderr(self) -> None:
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                logger.warning("stockfish stderr: %s", line.decode().rstrip())
        except Exception:
            pass

    async def _ensure_alive(self) -> None:
        if not self.is_alive():
            if self._ever_started:
                await self._spawn()
            else:
                raise RuntimeError(
                    f"Stockfish not available at '{self._path}'. "
                    "Install it (e.g. `sudo apt install stockfish`) and restart."
                )

    async def _send(self, cmd: str) -> None:
        self._proc.stdin.write((cmd + "\n").encode())
        await self._proc.stdin.drain()

    async def _readline(self, timeout: float) -> str:
        line_bytes = await asyncio.wait_for(
            self._proc.stdout.readline(), timeout=timeout
        )
        if not line_bytes:  # EOF — process died
            self._ever_started = False
            raise RuntimeError("Stockfish process terminated unexpectedly")
        return line_bytes.decode().strip()

    async def _read_until(self, token: str, timeout: float = 10.0) -> list[str]:
        lines: list[str] = []
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"Stockfish did not respond with '{token}'")
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=remaining)
            decoded = line.decode().strip()
            if decoded:
                lines.append(decoded)
            if decoded.startswith(token):
                return lines

    async def is_ready(self) -> bool:
        await self._ensure_alive()
        await self._send("isready")
        try:
            await self._read_until("readyok")
            return True
        except (TimeoutError, Exception):
            return False

    async def new_game(self) -> None:
        await self._ensure_alive()
        await self._send("ucinewgame")
        await self._send("isready")
        await self._read_until("readyok")

    async def set_option(self, name: str, value: str) -> None:
        await self._ensure_alive()
        name = name.replace("\n", "").replace("\r", "")
        value = str(value).replace("\n", "").replace("\r", "")
        await self._send(f"setoption name {name} value {value}")

    async def get_best_move(
        self,
        move_history: list[str],
        depth: int = 15,
        thinking_callback=None,
    ) -> dict:
        await self._ensure_alive()
        line_timeout = depth * 3 + 15
        async with self._search_lock:
            if move_history:
                await self._send(f"position startpos moves {' '.join(move_history)}")
            else:
                await self._send("position startpos")

            await self._send(f"go depth {depth}")

            best_move = None
            score_cp: Optional[int] = None
            score_mate: Optional[int] = None
            pv: list[str] = []
            last_depth = 0

            while True:
                line = await self._readline(timeout=line_timeout)
                if not line:
                    continue

                if line.startswith("info depth"):
                    parsed = self._parse_info(line)
                    last_depth = parsed.get("depth", last_depth)
                    if "score_cp" in parsed:
                        score_cp = parsed["score_cp"]
                        score_mate = None
                    if "score_mate" in parsed:
                        score_mate = parsed["score_mate"]
                        score_cp = None
                    if "pv" in parsed:
                        pv = parsed["pv"]
                    if thinking_callback and last_depth > 0:
                        try:
                            await thinking_callback({
                                "depth": last_depth,
                                "score_cp": score_cp,
                                "score_mate": score_mate,
                                "pv": pv[:3],
                            })
                        except Exception:
                            pass

                elif line.startswith("bestmove"):
                    parts = line.split()
                    best_move = parts[1] if len(parts) > 1 else None
                    break

            return {
                "move": best_move,
                "score_cp": score_cp,
                "score_mate": score_mate,
                "depth": last_depth,
                "pv": pv,
            }

    async def evaluate_position(self, fen: str, depth: int = 15) -> dict:
        await self._ensure_alive()
        line_timeout = depth * 3 + 15
        async with self._search_lock:
            await self._send(f"position fen {fen}")
            await self._send(f"go depth {depth}")

            best_move: Optional[str] = None
            score_cp: Optional[int] = None
            score_mate: Optional[int] = None
            pv: list[str] = []
            last_depth = 0

            while True:
                line = await self._readline(timeout=line_timeout)
                if not line:
                    continue
                if line.startswith("info depth"):
                    parsed = self._parse_info(line)
                    last_depth = parsed.get("depth", last_depth)
                    if "score_cp" in parsed:
                        score_cp = parsed["score_cp"]
                        score_mate = None
                    if "score_mate" in parsed:
                        score_mate = parsed["score_mate"]
                        score_cp = None
                    if "pv" in parsed:
                        pv = parsed["pv"]
                elif line.startswith("bestmove"):
                    parts = line.split()
                    best_move = parts[1] if len(parts) > 1 else None
                    break

            return {
                "best_move": best_move,
                "score_cp": score_cp,
                "score_mate": score_mate,
                "depth": last_depth,
                "pv": pv,
            }

    @staticmethod
    def _parse_info(line: str) -> dict:
        result: dict = {}

        m = re.search(r"\bdepth (\d+)", line)
        if m:
            result["depth"] = int(m.group(1))

        m = re.search(r"\bscore cp (-?\d+)", line)
        if m:
            result["score_cp"] = int(m.group(1))

        m = re.search(r"\bscore mate (-?\d+)", line)
        if m:
            result["score_mate"] = int(m.group(1))

        m = re.search(r"\bpv ((?:[a-h][1-8][a-h][1-8][qrbn]? ?)+)", line)
        if m:
            result["pv"] = m.group(1).strip().split()

        return result
