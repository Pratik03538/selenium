"""Separate animated mirror board for manual move hints on an authorized test site.

The panel never interacts with the source board. It mirrors FEN snapshots and
renders an animated arrow for a legal move selected by the existing strategy.
"""
from __future__ import annotations

try:
    import chess
except ImportError:  # pragma: no cover - tracker already requires python-chess for hints
    chess = None


PIECE_GLYPHS = {
    "P": "♙", "N": "♘", "B": "♗", "R": "♖", "Q": "♕", "K": "♔",
    "p": "♟", "n": "♞", "b": "♝", "r": "♜", "q": "♛", "k": "♚",
}


def build_hint(board_fen: str, uci: str, san: str | None = None) -> dict:
    """Validate and describe a strategy-selected move without submitting it."""
    if chess is None:
        raise RuntimeError("python-chess is required for the move hint board.")
    board = chess.Board(str(board_fen))
    move = chess.Move.from_uci(str(uci).strip())
    if move not in board.legal_moves:
        raise ValueError("The suggested move is not legal in the mirrored position.")
    return {
        "fen": str(board_fen),
        "uci": move.uci(),
        "san": str(san or board.san(move)),
        "from_square": move.from_square,
        "to_square": move.to_square,
        "promotion": move.promotion,
    }


def square_to_display(square: int, bottom_side: str = "white") -> tuple[int, int]:
    """Return (column, row) for a square, with White or Black at the bottom."""
    if chess is None:
        raise RuntimeError("python-chess is required for square coordinates.")
    file_index = chess.square_file(square)
    rank_index = chess.square_rank(square)
    if str(bottom_side).strip().lower() == "black":
        return 7 - file_index, rank_index
    return file_index, 7 - rank_index


class MoveHintBoardPanel:
    """Tk top-level chessboard that mirrors a FEN and animates a suggested move."""

    def __init__(self, parent):
        import tkinter as tk

        self.tk = tk
        self.window = tk.Toplevel(parent)
        self.window.title("Move Hint Board — Manual Play")
        self.window.geometry("660x735")
        self.window.minsize(590, 660)
        self.window.configure(background="#f4f6f8")
        self.window.protocol("WM_DELETE_WINDOW", self.hide)

        self.fen = ""
        self.board = None
        self.bottom_side = "white"
        self.suggestion = None
        self._animation_step = 0
        self._animation_job = None
        self._alive = True

        tk.Label(
            self.window,
            text="MOVE HINT BOARD",
            font=("Segoe UI", 15, "bold"),
            bg="#f4f6f8", fg="#172033",
        ).pack(fill="x", padx=12, pady=(10, 1))
        tk.Label(
            self.window,
            text="Live mirrored position • hints only • play the move on your original board",
            font=("Segoe UI", 9),
            bg="#f4f6f8", fg="#475569",
            wraplength=630,
        ).pack(fill="x", padx=12, pady=(0, 5))

        self.move_text = tk.StringVar(value="Waiting for a live board position…")
        tk.Label(
            self.window, textvariable=self.move_text,
            font=("Segoe UI", 11, "bold"),
            bg="#f4f6f8", fg="#166534",
            wraplength=630, justify="center",
        ).pack(fill="x", padx=12, pady=(0, 6))

        self.canvas = tk.Canvas(
            self.window, width=620, height=620,
            background="#f4f6f8", highlightthickness=0,
        )
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.canvas.bind("<Configure>", lambda _event: self._draw())
        self.window.withdraw()

    def show(self):
        if not self._alive:
            return
        try:
            self.window.deiconify()
            self.window.lift()
            self._draw()
        except self.tk.TclError:
            self._alive = False

    def hide(self):
        if not self._alive:
            return
        self._cancel_animation()
        try:
            self.window.withdraw()
        except self.tk.TclError:
            self._alive = False

    def shutdown(self):
        if not self._alive:
            return
        self._alive = False
        self._cancel_animation()
        try:
            self.window.destroy()
        except self.tk.TclError:
            pass

    def update_position(self, fen: str | None, bottom_side: str | None = None):
        if not self._alive or chess is None or not fen:
            return
        try:
            new_board = chess.Board(str(fen))
        except (ValueError, TypeError):
            return
        side = "black" if str(bottom_side or "").strip().lower() == "black" else "white"
        changed = str(fen) != self.fen
        orientation_changed = side != self.bottom_side
        if not changed and not orientation_changed:
            return
        if changed:
            self._cancel_animation()
            self.suggestion = None
            self.move_text.set("Live position updated • waiting for the next hint")
        self.fen = str(fen)
        self.board = new_board
        self.bottom_side = side
        self._draw()

    def set_suggestion(
        self, position_fen: str, uci: str, san: str | None = None,
        rank: str | None = None, reason: str | None = None,
    ) -> bool:
        """Show a legal hint for the exact current FEN; this does not play the move."""
        if not self._alive or str(position_fen) != self.fen or self.board is None:
            return False
        try:
            suggestion = build_hint(self.fen, uci, san)
        except (ValueError, RuntimeError):
            self.move_text.set("Hint skipped • move was not legal for the current position")
            return False
        suggestion["rank"] = str(rank or "")
        suggestion["reason"] = str(reason or "")
        self.suggestion = suggestion
        self._animation_step = 0
        self._cancel_animation()
        self._draw()
        self._schedule_animation()
        detail = f"{suggestion['san']}  ·  {suggestion['uci'][:2]} → {suggestion['uci'][2:4]}"
        if suggestion.get("promotion"):
            detail += f"  ·  promotes to {chess.piece_symbol(suggestion['promotion']).upper()}"
        if suggestion["rank"]:
            detail += f"  ·  {suggestion['rank']}"
        self.move_text.set(f"Suggested move: {detail} — play it on the original board")
        try:
            self.window.deiconify()
            self.window.lift()
        except self.tk.TclError:
            self._alive = False
            return False
        return True

    def clear_suggestion(self):
        self._cancel_animation()
        self.suggestion = None
        self.move_text.set("Live position updated • waiting for the next hint")
        self._draw()

    def _cancel_animation(self):
        if self._animation_job is not None:
            try:
                self.window.after_cancel(self._animation_job)
            except (self.tk.TclError, ValueError):
                pass
            self._animation_job = None

    def _schedule_animation(self):
        if not self._alive or self.suggestion is None:
            return
        try:
            self._animation_job = self.window.after(35, self._animate_tick)
        except self.tk.TclError:
            self._alive = False

    def _animate_tick(self):
        self._animation_job = None
        if not self._alive or self.suggestion is None:
            return
        self._animation_step = min(16, self._animation_step + 1)
        self._draw()
        if self._animation_step < 16:
            self._schedule_animation()

    def _square_center(self, square: int, left: float, top: float, cell: float):
        col, row = square_to_display(square, self.bottom_side)
        return left + (col + 0.5) * cell, top + (row + 0.5) * cell

    def _draw(self):
        if not self._alive or not hasattr(self, "canvas") or chess is None:
            return
        try:
            self.canvas.delete("all")
            width = max(320, int(self.canvas.winfo_width()))
            height = max(320, int(self.canvas.winfo_height()))
            board_size = max(280.0, min(width, height) - 52.0)
            cell = board_size / 8.0
            left = (width - board_size) / 2.0
            top = (height - board_size) / 2.0
            dark = "#78966b"
            light = "#edf0d5"
            font_size = max(20, int(cell * 0.68))

            for square in chess.SQUARES:
                col, row = square_to_display(square, self.bottom_side)
                x1 = left + col * cell
                y1 = top + row * cell
                x2, y2 = x1 + cell, y1 + cell
                is_light = (chess.square_file(square) + chess.square_rank(square)) % 2 == 1
                color = light if is_light else dark
                self.canvas.create_rectangle(
                    x1, y1, x2, y2, fill=color, outline=color,
                )

            if self.board is not None:
                for square, piece in self.board.piece_map().items():
                    cx, cy = self._square_center(square, left, top, cell)
                    glyph = PIECE_GLYPHS[piece.symbol()]
                    if piece.color:
                        self.canvas.create_text(
                            cx + 1, cy + 2, text=glyph,
                            font=("Segoe UI Symbol", font_size, "bold"),
                            fill="#263238",
                        )
                        self.canvas.create_text(
                            cx, cy, text=glyph,
                            font=("Segoe UI Symbol", font_size, "bold"),
                            fill="#fffdf4",
                        )
                    else:
                        self.canvas.create_text(
                            cx + 1, cy + 2, text=glyph,
                            font=("Segoe UI Symbol", font_size, "bold"),
                            fill="#e5e7eb",
                        )
                        self.canvas.create_text(
                            cx, cy, text=glyph,
                            font=("Segoe UI Symbol", font_size, "bold"),
                            fill="#18212f",
                        )

            # Coordinate labels are oriented to the side shown at the bottom.
            for index in range(8):
                file_char = chr(ord("a") + (7 - index if self.bottom_side == "black" else index))
                rank_char = str(index + 1 if self.bottom_side == "black" else 8 - index)
                self.canvas.create_text(
                    left + (index + 0.5) * cell, top + board_size + 12,
                    text=file_char, font=("Segoe UI", 8, "bold"), fill="#475569",
                )
                self.canvas.create_text(
                    left - 12, top + (index + 0.5) * cell,
                    text=rank_char, font=("Segoe UI", 8, "bold"), fill="#475569",
                )

            if self.suggestion is not None:
                start = self._square_center(self.suggestion["from_square"], left, top, cell)
                end = self._square_center(self.suggestion["to_square"], left, top, cell)
                from_col, from_row = square_to_display(self.suggestion["from_square"], self.bottom_side)
                to_col, to_row = square_to_display(self.suggestion["to_square"], self.bottom_side)
                for col, row in ((from_col, from_row), (to_col, to_row)):
                    self.canvas.create_rectangle(
                        left + col * cell + 2, top + row * cell + 2,
                        left + (col + 1) * cell - 2, top + (row + 1) * cell - 2,
                        outline="#facc15", width=4,
                    )
                progress = self._animation_step / 16.0
                current_end = (
                    start[0] + (end[0] - start[0]) * progress,
                    start[1] + (end[1] - start[1]) * progress,
                )
                self.canvas.create_line(
                    start[0], start[1], current_end[0], current_end[1],
                    fill="#111827", width=max(7, int(cell * 0.15)),
                    capstyle="round",
                )
                self.canvas.create_line(
                    start[0], start[1], current_end[0], current_end[1],
                    fill="#facc15", width=max(4, int(cell * 0.09)),
                    arrow="last" if progress >= 1.0 else "none",
                    arrowshape=(max(12, int(cell * 0.30)), max(14, int(cell * 0.32)), max(5, int(cell * 0.12))),
                    capstyle="round",
                )
        except self.tk.TclError:
            self._alive = False
