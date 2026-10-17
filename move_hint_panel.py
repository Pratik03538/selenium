"""Controls for displaying legal move hints directly on the authorized test board.

The existing move-selection strategy remains in the tracker. This module only
validates the suggested move and manages hint/autoplay and visibility controls.
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


def toggle_hint_visibility(enabled: bool) -> bool:
    """Return the opposite visibility state for the main-board hint overlay."""
    return not bool(enabled)


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
    """Controls for a native overlay aligned with the live board window."""

    def __init__(self, parent, initial_mode="hint", on_mode_change=None, on_hint_change=None):
        import tkinter as tk
        from main_board_overlay import MainBoardHintOverlay

        self.tk = tk
        self.on_mode_change = on_mode_change
        self.mode = "execute" if str(initial_mode).lower() in {"execute", "autoplay"} else "hint"
        self.hint_visible = True
        self.fen = ""
        self.board = None
        self.bottom_side = "white"
        self.suggestion = None
        self.premove_suggestion = None
        self._alive = True
        self.overlay = MainBoardHintOverlay(parent)

        self.window = tk.Toplevel(parent)
        self.window.title("Board Hint Controls")
        self.window.geometry("480x235")
        self.window.minsize(430, 215)
        self.window.configure(background="#f4f6f8")
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        tk.Label(
            self.window, text="MAIN BOARD HINT CONTROLS",
            font=("Segoe UI", 14, "bold"), bg="#f4f6f8", fg="#172033",
        ).pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(
            self.window,
            text="Arrows are a separate transparent overlay aligned over the live board.",
            font=("Segoe UI", 9), bg="#f4f6f8", fg="#475569", wraplength=450,
        ).pack(fill="x", padx=12, pady=(0, 8))
        self.move_text = tk.StringVar(value="Waiting for live board position…")
        self.move_label = tk.Label(
            self.window, textvariable=self.move_text, font=("Segoe UI", 10, "bold"),
            bg="#f4f6f8", fg="#475569", wraplength=450, justify="center",
        )
        self.move_label.pack(fill="x", padx=12, pady=(0, 8))
        self.mode_button = tk.Button(
            self.window, text="", command=self.toggle_mode, font=("Segoe UI", 10, "bold"),
            padx=12, pady=5, bg="#1d4ed8", fg="white", activebackground="#1e40af",
            activeforeground="white", relief="flat", cursor="hand2",
        )
        self.mode_button.pack(fill="x", padx=12, pady=(0, 7))
        self.visibility_button = tk.Button(
            self.window, text="", command=self.toggle_hint_visibility,
            font=("Segoe UI", 10, "bold"), padx=12, pady=5,
            bg="#15803d", fg="white", activebackground="#166534",
            activeforeground="white", relief="flat", cursor="hand2",
        )
        self.visibility_button.pack(fill="x", padx=12, pady=(0, 10))
        self._refresh_mode_button()
        self._refresh_visibility_button()
        self.window.bind_all("<F8>", self._handle_mode_shortcut, add="+")
        self.window.bind("<F8>", self._handle_mode_shortcut, add="+")
        self.window.bind_all("<F9>", self._handle_visibility_shortcut, add="+")
        self.window.bind("<F9>", self._handle_visibility_shortcut, add="+")
        self.window.withdraw()

    def _refresh_mode_button(self):
        if self.mode == "hint":
            self.mode_button.configure(
                text="MODE: HINT · Switch to AUTOPLAY [F8]",
                bg="#1d4ed8", activebackground="#1e40af",
            )
        else:
            self.mode_button.configure(
                text="MODE: AUTOPLAY · Switch to HINT [F8]",
                bg="#b91c1c", activebackground="#991b1b",
            )

    def _refresh_visibility_button(self):
        if self.hint_visible:
            self.visibility_button.configure(
                text="HINT OVERLAY: ON · Toggle [F9]",
                bg="#15803d", activebackground="#166534",
            )
        else:
            self.visibility_button.configure(
                text="HINT OVERLAY: OFF · Toggle [F9]",
                bg="#475569", activebackground="#334155",
            )

    def toggle_mode(self, _event=None):
        if not self._alive:
            return "break"
        self.mode = toggle_output_mode(self.mode)
        self._refresh_mode_button()
        self.clear_suggestion()
        self.overlay.set_enabled(self.hint_visible and self.mode == "hint")
        if self.mode == "execute":
            self.move_text.set("AUTOPLAY MODE · existing bot output path is active")
            self.move_label.configure(fg="#b91c1c")
        else:
            self.move_text.set("HINT MODE · moves are shown over the main board")
            self.move_label.configure(fg="#166534")
        if callable(self.on_mode_change):
            self.on_mode_change(self.mode)
        return "break"

    def _handle_mode_shortcut(self, _event=None):
        return self.toggle_mode()

    def toggle_hint_visibility(self, _event=None):
        if not self._alive:
            return "break"
        self.hint_visible = toggle_hint_visibility(self.hint_visible)
        self._refresh_visibility_button()
        self.overlay.set_enabled(self.hint_visible and self.mode == "hint")
        if not self.hint_visible:
            self.move_text.set("Hint overlay OFF · bot output mode is unchanged")
        else:
            current = self.suggestion or self.premove_suggestion
            if self.mode == "hint" and current:
                color = "#ef4444" if self.premove_suggestion is current else "#facc15"
                self.overlay.set_hint(current, self.bottom_side, color)
                self.move_text.set("Hint overlay ON")
            else:
                self.move_text.set("Hint overlay ON · waiting for the next hint")
        return "break"

    def _handle_visibility_shortcut(self, _event=None):
        return self.toggle_hint_visibility()

    def show(self):
        if not self._alive:
            return
        try:
            self.window.deiconify()
            self.window.lift()
        except self.tk.TclError:
            self._alive = False

    def hide(self):
        if not self._alive:
            return
        try:
            self.window.withdraw()
        except self.tk.TclError:
            self._alive = False

    def shutdown(self):
        if not self._alive:
            return
        self._alive = False
        self.overlay.shutdown()
        try:
            self.window.destroy()
        except self.tk.TclError:
            pass

    def update_position(self, fen: str | None, bottom_side: str | None = None, board_rect=None):
        if not self._alive or chess is None or not fen:
            return
        try:
            board = chess.Board(str(fen))
        except (ValueError, TypeError):
            return
        side = "black" if str(bottom_side or "").strip().lower() == "black" else "white"
        changed = str(fen) != self.fen or side != self.bottom_side
        self.fen, self.board, self.bottom_side = str(fen), board, side
        self.overlay.update_geometry(board_rect, side)
        if changed:
            self.suggestion = None
            self.premove_suggestion = None
            self.overlay.clear()
            self.move_text.set("Live position updated · waiting for the next hint")
            self.move_label.configure(fg="#475569")

    def set_suggestion(self, position_fen: str, uci: str, san: str | None = None,
                       rank: str | None = None, reason: str | None = None) -> bool:
        if not self._alive or self.mode != "hint" or str(position_fen) != self.fen or self.board is None:
            return False
        try:
            hint = build_hint(self.fen, uci, san)
        except (ValueError, RuntimeError):
            self.move_text.set("Hint skipped · move is not legal for the current position")
            return False
        hint["rank"], hint["reason"] = str(rank or ""), str(reason or "")
        self.suggestion, self.premove_suggestion = hint, None
        if self.hint_visible:
            self.overlay.set_hint(hint, self.bottom_side, "#facc15")
            self.overlay.set_enabled(True)
            detail = f"Suggested move: {hint['san']} · {hint['uci'][:2]} → {hint['uci'][2:4]}"
            if hint["rank"]:
                detail += f" · {hint['rank']}"
            self.move_text.set(detail)
            self.move_label.configure(fg="#166534")
        else:
            self.move_text.set(f"Hint overlay OFF · selected {hint['san']}")
        return True

    def set_premove_suggestion(self, position_fen: str, uci: str, predicted_reply_uci: str) -> bool:
        if not self._alive or self.mode != "hint" or str(position_fen) != self.fen or self.board is None:
            return False
        try:
            hint = build_premove_hint(position_fen, uci, predicted_reply_uci)
        except (ValueError, RuntimeError):
            return False
        self.suggestion, self.premove_suggestion = None, hint
        if self.hint_visible:
            self.overlay.set_hint(hint, self.bottom_side, "#ef4444")
            self.overlay.set_enabled(True)
            self.move_text.set(
                f"RED PREMOVE: {hint['san']} · {hint['uci'][:2]} → {hint['uci'][2:4]} after {hint['predicted_reply_san']}"
            )
            self.move_label.configure(fg="#b91c1c")
        else:
            self.move_text.set(f"Hint overlay OFF · premove {hint['san']}")
        return True

    def clear_suggestion(self):
        self.suggestion = None
        self.premove_suggestion = None
        self.overlay.clear()
        self.move_label.configure(fg="#475569")
        self.move_text.set("Live position updated · waiting for the next hint")

