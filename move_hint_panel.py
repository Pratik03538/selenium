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


def build_coach_variations(board_fen: str, engine_infos, max_lines: int = 3, max_plies: int = 8) -> list[dict]:
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
            piece_before = board.piece_at(move.from_square)
            moving_side = board.turn
            side = "white" if moving_side == chess.WHITE else "black"
            from_square, to_square = move.from_square, move.to_square

            if piece_before and piece_before.piece_type == chess.PAWN:
                if chess.square_file(to_square) in {3, 4} and chess.square_rank(to_square) in {3, 4}:
                    notes.append("claims central space")
            if piece_before and piece_before.piece_type in {chess.KNIGHT, chess.BISHOP}:
                home_rank = 0 if moving_side == chess.WHITE else 7
                if chess.square_rank(from_square) == home_rank:
                    notes.append("develops a minor piece")
            if board.is_castling(move):
                notes.append("kingside castle" if chess.square_file(move.to_square) == 6 else "queenside castle")

            board.push(move)
            if board.is_checkmate():
                notes.append("checkmate")
            elif board.is_check():
                notes.append("check")

            moved_piece = board.piece_at(to_square)
            if moved_piece is not None:
                enemy_targets = []
                for target_square in board.attacks(to_square):
                    target_piece = board.piece_at(target_square)
                    if (
                        target_piece is not None
                        and target_piece.color == board.turn
                        and target_piece.piece_type in {
                            chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN, chess.KING
                        }
                    ):
                        enemy_targets.append(chess.piece_name(target_piece.piece_type))
                if len(enemy_targets) >= 2:
                    notes.append("fork threat against " + " and ".join(enemy_targets[:3]))

            steps.append({
                "uci": move.uci(),
                "san": san,
                "from_square": from_square,
                "to_square": to_square,
                "side": side,
                "ply": len(steps) + 1,
                "notes": notes,
                "explanation": (
                    "; ".join(dict.fromkeys(notes))
                    if notes else "engine continuation; watch the opponent's reply"
                ),
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
        noted_steps = [
            f"{step['san']}: {step['explanation']}"
            for step in steps if step.get("notes")
        ]
        explanation = (
            " · ".join(noted_steps[:4])
            if noted_steps else
            "No forced capture/check motif identified in this line; compare the continuation and evaluation."
        )
        variations.append({
            "index": len(variations) + 1,
            "label": f"Line {len(variations) + 1} · {steps[0]['san']} · {score_text}",
            "score": score_text,
            "sequence": sequence,
            "explanation": explanation,
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
        self.coach_root_side = "black"
        self.coach_window = None
        self.coach_play_job = None
        self.coach_playing = False
        self._alive = True
        self.overlay = MainBoardHintOverlay(parent)

        self.window = tk.Toplevel(parent)
        self.window.title("Board Hint Controls")
        self.window.geometry("520x390")
        self.window.minsize(480, 350)
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
        tk.Label(
            self.window, text="LIVE ANALYSIS · OPPONENT TOP 3 + OUR REPLIES",
            font=("Segoe UI", 9, "bold"), bg="#f4f6f8", fg="#6d28d9",
            anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 3))
        self.analysis_text = tk.StringVar(value="Waiting for the next position…")
        tk.Label(
            self.window, textvariable=self.analysis_text,
            font=("Consolas", 9), bg="#ffffff", fg="#334155",
            wraplength=490, justify="left", anchor="w", height=4,
            padx=8, pady=5, relief="solid", bd=1,
        ).pack(fill="x", padx=12, pady=(0, 4))
        self.analysis_step_status = tk.StringVar(
            value="Yellow = main hint · small cyan = opponent move · small violet = our reply"
        )
        tk.Label(
            self.window, textvariable=self.analysis_step_status,
            font=("Segoe UI", 9), bg="#f4f6f8", fg="#6d28d9",
            wraplength=490, justify="left", anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 8))
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
            if self.coach_lines:
                self._start_auto_coach_playback()
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
        self._stop_coach_playback()
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
            if self.mode == "hint" and self.coach_lines:
                self._start_auto_coach_playback()
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
            self.analysis_text.set("Waiting for fresh top-3 opponent replies…")
            self.analysis_step_status.set(
                "Analysis arrows play one at a time; the yellow hint stays separate."
            )

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

    def set_coach_variations(
        self, position_fen: str, engine_infos, analysis_fen: str | None = None,
    ) -> int:
        """Show the top three opponent options and auto-play their tactical continuations."""
        if not self._alive or not position_fen or str(position_fen) != self.fen:
            return 0
        root_fen = str(analysis_fen or position_fen)
        try:
            root_board = chess.Board(root_fen)
            self.coach_root_side = "white" if root_board.turn == chess.WHITE else "black"
            self.coach_lines = build_coach_variations(
                root_fen, engine_infos, max_lines=3, max_plies=2,
            )
        except (RuntimeError, TypeError, ValueError):
            self.coach_lines = []
            self.coach_root_side = "black"
        self._stop_coach_playback()
        self.coach_line_index = 0
        self.coach_step_index = 0
        if not self.coach_lines:
            self.analysis_text.set("No legal reply lines yet · waiting for fresh analysis…")
            self.analysis_step_status.set("The main hint remains available on the board.")
            return 0

        summary_rows = []
        for index, line in enumerate(self.coach_lines[:3], start=1):
            moves = line["moves"][:2]
            if not moves:
                continue
            opponent_move = moves[0]["san"]
            our_reply = moves[1]["san"] if len(moves) > 1 else "reply unavailable"
            summary_rows.append(
                f"{index}. Opp: {opponent_move} → Us: {our_reply}"
            )
        self.analysis_text.set("\n".join(summary_rows))
        if self.hint_visible and self.mode == "hint":
            self._start_auto_coach_playback()
        else:
            self.analysis_step_status.set(
                "Analysis ready · switch back to Hint mode to replay the small arrows."
            )
        return len(self.coach_lines)

    def _start_auto_coach_playback(self):
        if (
            not self._alive or not self.coach_lines or not self.hint_visible
            or self.mode != "hint" or self.coach_playing
        ):
            return
        self._stop_coach_playback()
        self.coach_line_index = 0
        self.coach_step_index = 0
        self.coach_playing = True
        self._show_auto_coach_step()
        try:
            self.coach_play_job = self.window.after(150, self._advance_auto_coach_step)
        except self.tk.TclError:
            self.coach_play_job = None
            self.coach_playing = False

    def _show_auto_coach_step(self):
        if not self.coach_lines or not self.coach_playing:
            return
        line = self.coach_lines[self.coach_line_index]
        if not line["moves"]:
            return
        self.coach_step_index = max(
            0, min(self.coach_step_index, len(line["moves"]) - 1)
        )
        move = line["moves"][self.coach_step_index]
        is_opponent_move = move.get("side") == self.coach_root_side
        arrow_color = "#38bdf8" if is_opponent_move else "#c084fc"
        if self.hint_visible and self.mode == "hint":
            self.overlay.set_coach_hint(move, arrow_color)
            self.overlay.set_enabled(True)
        actor = "OPPONENT" if is_opponent_move else "YOUR TACTICAL RESPONSE"
        explanation = move.get("explanation") or "Review this reply and the resulting position."
        self.analysis_step_status.set(
            f"Line {self.coach_line_index + 1}/3 · Step "
            f"{self.coach_step_index + 1}/{len(line['moves'])} · "
            f"{actor}: {move['san']} — {explanation}"
        )

    def _advance_auto_coach_step(self):
        self.coach_play_job = None
        if not self.coach_playing or not self.coach_lines:
            return
        line = self.coach_lines[self.coach_line_index]
        if self.coach_step_index + 1 < len(line["moves"]):
            self.coach_step_index += 1
        elif self.coach_line_index + 1 < len(self.coach_lines):
            self.coach_line_index += 1
            self.coach_step_index = 0
        else:
            self._stop_coach_playback(clear_arrow=True)
            self.analysis_step_status.set(
                "Analysis replay complete · top 3 choices remain listed above."
            )
            return
        self._show_auto_coach_step()
        try:
            self.coach_play_job = self.window.after(150, self._advance_auto_coach_step)
        except self.tk.TclError:
            self._stop_coach_playback(clear_arrow=True)

    def _stop_coach_playback(self, clear_arrow=True):
        self.coach_playing = False
        if self.coach_play_job is not None:
            try:
                self.window.after_cancel(self.coach_play_job)
            except (self.tk.TclError, ValueError):
                pass
        self.coach_play_job = None
        if clear_arrow and getattr(self, "overlay", None) is not None:
            try:
                self.overlay.clear_coach_hint()
            except (AttributeError, self.tk.TclError):
                pass

    def clear_suggestion(self):
        self.suggestion = None
        self.premove_suggestion = None
        self.overlay.clear()
        self.move_label.configure(fg="#475569")
        self.move_text.set("Live position updated · waiting for the next hint")



