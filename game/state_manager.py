import asyncio
import uuid
from typing import Optional

import chess


class GameStateManager:
    def __init__(self):
        self.board = chess.Board()
        self.move_history: list[str] = []
        self.game_id: str = str(uuid.uuid4())
        self.player_color: str = "white"
        self._captured: list[str] = []
        self._active = False
        self._lock = asyncio.Lock()

    async def new_game(self, player_color: str = "white") -> dict:
        async with self._lock:
            self.board = chess.Board()
            self.move_history = []
            self.game_id = str(uuid.uuid4())
            self.player_color = player_color.lower()
            self._captured = []
            self._active = True
            return self.get_state()

    async def make_move(self, uci: str) -> dict:
        async with self._lock:
            try:
                move = self.board.parse_uci(uci)
            except ValueError as e:
                return {"ok": False, "error": str(e)}

            if move not in self.board.legal_moves:
                return {"ok": False, "error": "Illegal move"}

            captured_piece = None
            if self.board.is_capture(move):
                target_sq = move.to_square
                if self.board.is_en_passant(move):
                    # en passant — captured pawn is one rank behind the destination
                    direction = -8 if self.board.turn == chess.WHITE else 8
                    target_sq = move.to_square + direction
                piece = self.board.piece_at(target_sq)
                if piece:
                    captured_piece = chess.piece_name(piece.piece_type)

            san = self.board.san(move)
            is_castling = self.board.is_castling(move)
            is_en_passant = self.board.is_en_passant(move)
            is_capture = self.board.is_capture(move)

            self.board.push(move)
            self.move_history.append(uci)

            if captured_piece:
                self._captured.append(captured_piece)

            is_check = self.board.is_check()
            is_checkmate = self.board.is_checkmate()
            is_promotion = move.promotion is not None

            status = self._game_status()

            return {
                "ok": True,
                "uci": uci,
                "san": san,
                "fen_after": self.board.fen(),
                "turn": "white" if self.board.turn == chess.WHITE else "black",
                "is_capture": is_capture,
                "is_check": is_check,
                "is_checkmate": is_checkmate,
                "is_castling": is_castling,
                "is_en_passant": is_en_passant,
                "is_promotion": is_promotion,
                "captured_piece": captured_piece,
                "status": status,
            }

    def get_fen(self) -> str:
        return self.board.fen()

    def get_state(self) -> dict:
        status = self._game_status()
        winner = None
        if self.board.is_checkmate():
            winner = "black" if self.board.turn == chess.WHITE else "white"

        return {
            "game_id": self.game_id,
            "fen": self.board.fen(),
            "turn": "white" if self.board.turn == chess.WHITE else "black",
            "player_color": self.player_color,
            "move_history": self.move_history,
            "captured_pieces": self._captured,
            "status": status,
            "winner": winner,
            "is_check": self.board.is_check(),
            "fullmove_number": self.board.fullmove_number,
            "active": self._active,
        }

    def is_game_over(self) -> Optional[str]:
        if self.board.is_checkmate():
            return "checkmate"
        if self.board.is_stalemate():
            return "stalemate"
        if self.board.is_insufficient_material():
            return "draw"
        if self.board.is_seventyfive_moves():
            return "draw"
        if self.board.is_fivefold_repetition():
            return "draw"
        return None

    async def end_game(self, reason: str = "resign") -> dict:
        async with self._lock:
            self._active = False
            return {"game_id": self.game_id, "reason": reason, "status": "ended"}

    def is_engine_turn(self) -> bool:
        current = "white" if self.board.turn == chess.WHITE else "black"
        return current != self.player_color and self._active

    def _game_status(self) -> str:
        result = self.is_game_over()
        if result:
            return result
        return "active" if self._active else "ended"
