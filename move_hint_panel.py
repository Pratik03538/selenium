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


def build_coach_variations(board_fen: str, engine_infos, max_lines: int = 5, max_plies: int = 10) -> list[dict]:
    """Convert Stockfish MultiPV principal variations into legal coaching steps.

    This is display-only: it does not choose, submit, or change a bot move.
    """
    if chess is None:
        raise RuntimeError("python-chess is required for tactical coaching.")
    root = chess.Board(str(board_fen))
    variations = []
    for info in list(engine_infos or [])[:max(1, min(int(max_lines), 8))]:
        if not isinstance(info, dict):
            continue
        pv = list(info.get("pv") or [])
        if not pv:
            continue
        board = root.copy(stack=True)
        steps = []
        for raw_move in pv[:max(1, min(int(max_plies), 24))]:
            try:
                move = chess.Move.from_uci(raw_move) if isinstance(raw_move, str) else raw_move
            except (ValueError, TypeError):
                break
            if move not in board.legal_moves:
                break
            san = board.san(move)
            notes = []
            if board.is_capture(move):
                if board.is_en_passant(move):
                    notes.append("captures pawn en passant")
                else:
                    captured = board.piece_at(move.to_square)
                    notes.append(
                        f"captures {chess.piece_name(captured.piece_type)}"
                        if captured else "capture"
                    )
            if move.promotion:
                notes.append(f"promotes to {chess.piece_name(move.promotion)}")
            if board.is_castling(move):
                notes.append("kingside castle" if chess.square_file(move.to_square) == 6 else "queenside castle")
            side = "white" if board.turn == chess.WHITE else "black"
            from_square, to_square = move.from_square, move.to_square
            board.push(move)
            if board.is_checkmate():
                notes.append("checkmate")
            elif board.is_check():
                notes.append("check")
            steps.append({
                "uci": move.uci(),
                "san": san,
                "from_square": from_square,
                "to_square": to_square,
                "side": side,
                "ply": len(steps) + 1,
                "notes": notes,
            })
        if not steps:
            continue

        score_text = "evaluation unavailable"
        try:
            score_obj = info.get("score")
            if score_obj is not None:
                pov_score = score_obj.pov(root.turn)
                mate = pov_score.mate()
                if mate is not None:
                    score_text = f"Mate in {mate:+d}"
                else:
                    cp = pov_score.score()
                    if cp is not None:
                        score_text = f"{cp / 100:+.1f}"
        except (AttributeError, TypeError, ValueError):
            pass

        sequence = " → ".join(step["san"] for step in steps)
        variations.append({
            "index": len(variations) + 1,
            "label": f"Line {len(variations) + 1} · {steps[0]['san']} · {score_text}",
            "score": score_text,
            "sequence": sequence,
            "moves": steps,
            "root_fen": str(board_fen),
        })
    return variations


class MoveHintBoardPanel:
    """Separate overlay and controls for hints plus step-by-step tactical coaching."""

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
        self.coach_lines = []
        self.coach_line_index = 0
        self.coach_step_index = 0
        self.coach_window = None
        self.coach_play_job = None
        self.coach_playing = False
        self._alive = True
        self.overlay = MainBoardHintOverlay(parent)

        self.window = tk.Toplevel(parent)
        self.window.title("Board Hint Controls")
        self.window.geometry("500x285")
        self.window.minsize(450, 260)
        self.window.configure(background="#f4f6f8")
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        tk.Label(
            self.window, text="MAIN BOARD HINT CONTROLS",
            font=("Segoe UI", 14, "bold"), bg="#f4f6f8", fg="#172033",
        ).pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(
            self.window,
            text="Arrows use a separate transparent overlay aligned over the live board.",
            font=("Segoe UI", 9), bg="#f4f6f8", fg="#475569", wraplength=470,
        ).pack(fill="x", padx=12, pady=(0, 8))
        self.move_text = tk.StringVar(value="Waiting for live board position…")
        self.move_label = tk.Label(
            self.window, textvariable=self.move_text, font=("Segoe UI", 10, "bold"),
            bg="#f4f6f8", fg="#475569", wraplength=470, justify="center",
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
        self.visibility_button.pack(fill="x", padx=12, pady=(0, 7))
        self.coach_button = tk.Button(
            self.window, text="TACTICAL COACH · VARIATIONS",
            command=self.open_coach, font=("Segoe UI", 10, "bold"),
            padx=12, pady=5, bg="#6d28d9", fg="white", activebackground="#5b21b6",
            activeforeground="white", relief="flat", cursor="hand2",
        )
        self.coach_button.pack(fill="x", padx=12, pady=(0, 10))
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
            self._stop_coach_playback()
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
        if self.coach_window is not None and self.coach_window.winfo_exists():
            self._refresh_coach_status()
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
        self._stop_coach_playback()
        try:
            self.window.withdraw()
        except self.tk.TclError:
            self._alive = False

    def shutdown(self):
        if not self._alive:
            return
        self._alive = False
        self._stop_coach_playback()
        try:
            if self.coach_window is not None and self.coach_window.winfo_exists():
                self.coach_window.destroy()
        except self.tk.TclError:
            pass
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
            self._stop_coach_playback()
            self.suggestion = None
            self.premove_suggestion = None
            self.coach_lines = []
            self.coach_line_index = 0
            self.coach_step_index = 0
            self.overlay.clear()
            self.move_text.set("Live position updated · waiting for the next hint/analysis")
            self.move_label.configure(fg="#475569")
            if self.coach_window is not None and self.coach_window.winfo_exists():
                self._refresh_coach_lines()

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

    def set_coach_variations(self, position_fen: str, engine_infos) -> int:
        """Load the already-computed engine PVs for coaching without a second search."""
        if not self._alive or not position_fen or str(position_fen) != self.fen:
            return 0
        try:
            self.coach_lines = build_coach_variations(position_fen, engine_infos)
        except (RuntimeError, TypeError, ValueError):
            self.coach_lines = []
        self.coach_line_index = 0
        self.coach_step_index = 0
        if self.coach_window is not None and self.coach_window.winfo_exists():
            self._stop_coach_playback()
            self._refresh_coach_lines()
        return len(self.coach_lines)

    def open_coach(self):
        if not self._alive:
            return
        try:
            if self.coach_window is not None and self.coach_window.winfo_exists():
                self.coach_window.deiconify()
                self.coach_window.lift()
                self._refresh_coach_lines()
                return
            tk = self.tk
            win = tk.Toplevel(self.window)
            self.coach_window = win
            win.title("Tactical Coach · Candidate Lines")
            win.geometry("760x510")
            win.minsize(650, 430)
            win.configure(background="#f8fafc")
            win.protocol("WM_DELETE_WINDOW", self._close_coach)
            tk.Label(
                win, text="TACTICAL COACH",
                font=("Segoe UI", 15, "bold"), bg="#f8fafc", fg="#172033",
            ).pack(fill="x", padx=14, pady=(12, 3))
            tk.Label(
                win,
                text="Select a candidate line to inspect its move → reply → continuation. "
                     "Arrows are a visual replay only; the board position is not changed.",
                font=("Segoe UI", 9), bg="#f8fafc", fg="#475569",
                wraplength=720, justify="left",
            ).pack(fill="x", padx=14, pady=(0, 9))
            self.coach_list = tk.Listbox(
                win, height=7, font=("Consolas", 10),
                selectmode=tk.SINGLE, activestyle="dotbox",
                exportselection=False, bg="white", fg="#172033",
            )
            self.coach_list.pack(fill="both", expand=True, padx=14, pady=(0, 8))
            self.coach_list.bind("<<ListboxSelect>>", self._on_coach_line_selected)
            self.coach_summary = tk.StringVar(value="Waiting for engine variation lines…")
            tk.Label(
                win, textvariable=self.coach_summary,
                font=("Segoe UI", 10, "bold"), bg="#f8fafc", fg="#334155",
                wraplength=720, justify="left",
            ).pack(fill="x", padx=14, pady=(0, 5))
            self.coach_step_status = tk.StringVar(value="No line selected.")
            tk.Label(
                win, textvariable=self.coach_step_status,
                font=("Segoe UI", 10), bg="#f8fafc", fg="#475569",
                wraplength=720, justify="left",
            ).pack(fill="x", padx=14, pady=(0, 8))
            buttons = tk.Frame(win, bg="#f8fafc")
            buttons.pack(fill="x", padx=14, pady=(0, 12))
            tk.Button(buttons, text="◀ Previous", command=lambda: self._step_coach(-1)).pack(side="left", padx=(0, 6))
            tk.Button(buttons, text="Next ▶", command=lambda: self._step_coach(1)).pack(side="left", padx=6)
            self.coach_play_button = tk.Button(buttons, text="▶ Play line", command=self._toggle_coach_playback)
            self.coach_play_button.pack(side="left", padx=6)
            tk.Button(buttons, text="Restore hint", command=self._restore_primary_hint).pack(side="left", padx=6)
            tk.Button(buttons, text="Close", command=self._close_coach).pack(side="right")
            self._refresh_coach_lines()
        except self.tk.TclError:
            self.coach_window = None

    def _refresh_coach_lines(self):
        if self.coach_window is None:
            return
        try:
            if not self.coach_window.winfo_exists():
                return
            self.coach_list.delete(0, "end")
            if not self.coach_lines:
                self.coach_list.insert("end", "No MultiPV line yet · waiting for next engine search")
                self.coach_summary.set(
                    "A new legal position and Stockfish analysis are needed. "
                    "Opening-book moves may not include a tactical variation."
                )
                self.coach_step_status.set("No variation steps available.")
                return
            for line in self.coach_lines:
                self.coach_list.insert(
                    "end", f"{line['label']}   |   {line['sequence']}"
                )
            self.coach_line_index = min(self.coach_line_index, len(self.coach_lines) - 1)
            self.coach_list.selection_clear(0, "end")
            self.coach_list.selection_set(self.coach_line_index)
            self.coach_list.activate(self.coach_line_index)
            self._show_selected_coach_line()
        except self.tk.TclError:
            pass

    def _on_coach_line_selected(self, _event=None):
        if self.coach_list is None:
            return
        selection = self.coach_list.curselection()
        if not selection or not self.coach_lines:
            return
        self._stop_coach_playback()
        self.coach_line_index = int(selection[0])
        self.coach_step_index = 0
        self._show_selected_coach_line()

    def _show_selected_coach_line(self):
        if not self.coach_lines or self.coach_window is None:
            return
        line = self.coach_lines[self.coach_line_index]
        self.coach_summary.set(
            f"Evaluation from side-to-move: {line['score']}    ·    {line['sequence']}"
        )
        self.coach_step_index = min(self.coach_step_index, len(line["moves"]) - 1)
        self._show_coach_step()

    def _show_coach_step(self):
        if not self.coach_lines or self.coach_window is None:
            return
        line = self.coach_lines[self.coach_line_index]
        if not line["moves"]:
            return
        self.coach_step_index = max(0, min(self.coach_step_index, len(line["moves"]) - 1))
        move = line["moves"][self.coach_step_index]
        move_color = "#facc15" if self.coach_step_index % 2 == 0 else "#22d3ee"
        if self.hint_visible and self.mode == "hint":
            self.overlay.set_hint(move, self.bottom_side, move_color)
            self.overlay.set_enabled(True)
        elif not self.hint_visible:
            self.overlay.set_enabled(False)
        else:
            self.overlay.set_enabled(False)
        actor = "Candidate-side move" if self.coach_step_index % 2 == 0 else "Opponent reply / continuation"
        note_text = "; ".join(move.get("notes") or [])
        tactical_note = f" Tactical note: {note_text}." if note_text else ""
        self.coach_step_status.set(
            f"Step {self.coach_step_index + 1}/{len(line['moves'])}: "
            f"{actor} plays {move['san']} ({move['uci'][:2]} → {move['uci'][2:4]})."
            f"{tactical_note} Yellow = candidate move; cyan = reply/continuation."
        )

    def _step_coach(self, delta):
        if not self.coach_lines:
            return
        self._stop_coach_playback()
        line = self.coach_lines[self.coach_line_index]
        self.coach_step_index = (self.coach_step_index + int(delta)) % len(line["moves"])
        self._show_coach_step()

    def _toggle_coach_playback(self):
        if not self.coach_lines or self.coach_window is None:
            return
        if self.coach_playing:
            self._stop_coach_playback()
            return
        if not self.hint_visible or self.mode != "hint":
            self.coach_step_status.set("Turn Hint Overlay ON and switch to HINT mode to animate arrows.")
            return
        line = self.coach_lines[self.coach_line_index]
        if self.coach_step_index >= len(line["moves"]) - 1:
            self.coach_step_index = 0
        self.coach_playing = True
        self.coach_play_button.configure(text="Ⅱ Pause")
        self._show_coach_step()
        self.coach_play_job = self.coach_window.after(850, self._advance_coach_playback)

    def _advance_coach_playback(self):
        self.coach_play_job = None
        if not self.coach_playing or not self.coach_lines:
            return
        line = self.coach_lines[self.coach_line_index]
        if self.coach_step_index >= len(line["moves"]) - 1:
            self._stop_coach_playback()
            return
        self.coach_step_index += 1
        self._show_coach_step()
        try:
            self.coach_play_job = self.coach_window.after(850, self._advance_coach_playback)
        except self.tk.TclError:
            self._stop_coach_playback()

    def _stop_coach_playback(self):
        self.coach_playing = False
        if self.coach_play_job is not None and self.coach_window is not None:
            try:
                self.coach_window.after_cancel(self.coach_play_job)
            except (self.tk.TclError, ValueError):
                pass
        self.coach_play_job = None
        try:
            if self.coach_play_button.winfo_exists():
                self.coach_play_button.configure(text="▶ Play line")
        except (AttributeError, self.tk.TclError):
            pass

    def _restore_primary_hint(self):
        self._stop_coach_playback()
        current = self.suggestion or self.premove_suggestion
        if current and self.hint_visible and self.mode == "hint":
            color = "#ef4444" if self.premove_suggestion is current else "#facc15"
            self.overlay.set_hint(current, self.bottom_side, color)
            self.overlay.set_enabled(True)
        else:
            self.overlay.clear()

    def _refresh_coach_status(self):
        if self.coach_window is None or not self.coach_lines:
            return
        self._show_selected_coach_line()

    def _close_coach(self):
        self._stop_coach_playback()
        try:
            self.coach_window.withdraw()
        except (AttributeError, self.tk.TclError):
            self.coach_window = None
        self._restore_primary_hint()

    def clear_suggestion(self):
        self.suggestion = None
        self.premove_suggestion = None
        self.overlay.clear()
        self.move_label.configure(fg="#475569")
        self.move_text.set("Live position updated · waiting for the next hint")



