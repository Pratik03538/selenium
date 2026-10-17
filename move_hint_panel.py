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


def toggle_output_mode(mode: str) -> str:
    """Return the other supported output mode without changing bot strategy."""
    return "execute" if str(mode).strip().lower() in {"hint", "manual"} else "hint"


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


def build_premove_hint(
    board_fen: str, uci: str, predicted_reply_uci: str,
) -> dict:
    """Validate a premove on the position after the predicted opponent reply."""
    if chess is None:
        raise RuntimeError("python-chess is required for the move hint board.")
    board = chess.Board(str(board_fen))
    predicted_reply = chess.Move.from_uci(str(predicted_reply_uci).strip())
    if predicted_reply not in board.legal_moves:
        raise ValueError("Predicted reply is not legal in the mirrored position.")
    predicted_reply_san = board.san(predicted_reply)
    board.push(predicted_reply)
    premove = chess.Move.from_uci(str(uci).strip())
    if premove not in board.legal_moves:
        raise ValueError("Premove is not legal after the predicted reply.")
    return {
        "fen": str(board_fen),
        "uci": premove.uci(),
        "san": board.san(premove),
        "predicted_reply_uci": predicted_reply.uci(),
        "predicted_reply_san": predicted_reply_san,
        "from_square": premove.from_square,
        "to_square": premove.to_square,
        "promotion": premove.promotion,
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

    def __init__(self, parent, initial_mode="hint", on_mode_change=None):
        import tkinter as tk

        self.tk = tk
        self.on_mode_change = on_mode_change
        self.mode = "execute" if str(initial_mode).lower() in {"execute", "autoplay"} else "hint"
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
        self.premove_suggestion = None
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
        self.move_label = tk.Label(
            self.window, textvariable=self.move_text,
            font=("Segoe UI", 11, "bold"),
            bg="#f4f6f8", fg="#166534",
            wraplength=630, justify="center",
        )
        self.move_label.pack(fill="x", padx=12, pady=(0, 6))

        self.mode_button = tk.Button(
            self.window,
            text="",
            command=self.toggle_mode,
            font=("Segoe UI", 10, "bold"),
            padx=12, pady=5,
            bg="#1d4ed8", fg="white",
            activebackground="#1e40af", activeforeground="white",
            relief="flat", cursor="hand2",
        )
        self.mode_button.pack(fill="x", padx=12, pady=(0, 8))
        self._refresh_mode_button()
        self.window.bind_all("<F8>", self._handle_mode_shortcut, add="+")
        self.window.bind("<F8>", self._handle_mode_shortcut, add="+")

        self.canvas = tk.Canvas(
            self.window, width=620, height=620,
            background="#f4f6f8", highlightthickness=0,
        )
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.canvas.bind("<Configure>", lambda _event: self._draw())
        self.window.withdraw()

    def _refresh_mode_button(self):
        if self.mode == "hint":
            self.mode_button.configure(
                text="MODE: HINT  ·  Switch to AUTOPLAY  [F8]",
                bg="#1d4ed8", activebackground="#1e40af",
            )
        else:
            self.mode_button.configure(
                text="MODE: AUTOPLAY  ·  Switch to HINT  [F8]",
                bg="#b91c1c", activebackground="#991b1b",
            )

    def toggle_mode(self, _event=None):
        """Toggle only the move-output path; existing move selection is untouched."""
        if not self._alive:
            return "break"
        next_mode = toggle_output_mode(self.mode)
        self.mode = next_mode
        self._refresh_mode_button()
        self.clear_suggestion()
        if next_mode == "execute":
            self.move_text.set(
                "AUTOPLAY MODE • existing bot output is active on the configured authorized test site"
            )
            self.move_label.configure(fg="#b91c1c")
        else:
            self.move_text.set(
                "HINT MODE • selected moves are shown here; play them manually on the original board"
            )
            self.move_label.configure(fg="#166534")
        if callable(self.on_mode_change):
            self.on_mode_change(next_mode)
        return "break"

    def _handle_mode_shortcut(self, _event=None):
        return self.toggle_mode()

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
            self.premove_suggestion = None
            self.move_text.set("Live position updated • waiting for the next hint")
            self.move_label.configure(fg="#475569")
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
        self.premove_suggestion = None
        self._animation_step = 0
        self._cancel_animation()
        self.move_label.configure(fg="#166534")
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

    def set_premove_suggestion(
        self, position_fen: str, uci: str, predicted_reply_uci: str,
    ) -> bool:
        """Show the existing strategy's premove as a red hint; never submit it."""
        if not self._alive or str(position_fen) != self.fen or self.board is None:
            return False
        try:
            premove_hint = build_premove_hint(position_fen, uci, predicted_reply_uci)
        except (ValueError, RuntimeError):
            return False
        self.suggestion = None
        self.premove_suggestion = premove_hint
        self._animation_step = 0
        self._cancel_animation()
        self.move_label.configure(fg="#b91c1c")
        self._draw()
        self._schedule_animation()
        detail = (
            f"RED PREMOVE: {premove_hint['san']}  ·  "
            f"{premove_hint['uci'][:2]} → {premove_hint['uci'][2:4]}  "
            f"after {premove_hint['predicted_reply_san']}"
        )
        if premove_hint.get("promotion"):
            detail += f"  ·  promotes to {chess.piece_symbol(premove_hint['promotion']).upper()}"
        self.move_text.set(f"{detail} — hint only, play manually")
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
        self.premove_suggestion = None
        self.move_label.configure(fg="#475569")
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
        if (
            not self._alive
            or (self.suggestion is None and self.premove_suggestion is None)
        ):
            return
        try:
            self._animation_job = self.window.after(12, self._animate_tick)
        except self.tk.TclError:
            self._alive = False

    def _animate_tick(self):
        self._animation_job = None
        if (
            not self._alive
            or (self.suggestion is None and self.premove_suggestion is None)
        ):
            return
        self._animation_step = min(8, self._animation_step + 1)
        self._draw()
        if self._animation_step < 8:
            self._schedule_animation()

    def _square_center(self, square: int, left: float, top: float, cell: float):
        col, row = square_to_display(square, self.bottom_side)
        return left + (col + 0.5) * cell, top + (row + 0.5) * cell

    def _draw_arrow(self, hint: dict, color: str, progress: float,
                    left: float, top: float, cell: float):
        start = self._square_center(hint["from_square"], left, top, cell)
        end = self._square_center(hint["to_square"], left, top, cell)
        from_col, from_row = square_to_display(hint["from_square"], self.bottom_side)
        to_col, to_row = square_to_display(hint["to_square"], self.bottom_side)
        for col, row in ((from_col, from_row), (to_col, to_row)):
            self.canvas.create_rectangle(
                left + col * cell + 2, top + row * cell + 2,
                left + (col + 1) * cell - 2, top + (row + 1) * cell - 2,
                outline=color, width=4,
            )
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
            fill=color, width=max(4, int(cell * 0.09)),
            arrow="last" if progress >= 1.0 else "none",
            arrowshape=(max(12, int(cell * 0.30)), max(14, int(cell * 0.32)), max(5, int(cell * 0.12))),
            capstyle="round",
        )

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
                self._draw_arrow(
                    self.suggestion, "#facc15", self._animation_step / 8.0,
                    left, top, cell,
                )
            if self.premove_suggestion is not None:
                self._draw_arrow(
                    self.premove_suggestion, "#ef4444", self._animation_step / 8.0,
                    left, top, cell,
                )
        except self.tk.TclError:
            self._alive = False
