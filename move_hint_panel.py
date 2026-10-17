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
    normalized_infos = [engine_infos] if isinstance(engine_infos, dict) else list(engine_infos or [])
    for info in normalized_infos[:max(1, min(int(max_lines), 8))]:
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
            under_attack_before = bool(
                piece_before and board.is_attacked_by(not moving_side, move.from_square)
            )
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
            if (
                under_attack_before and piece_before is not None
                and not board.is_attacked_by(board.turn, to_square)
            ):
                notes.append(
                    f"moves {chess.piece_name(piece_before.piece_type)} out of attack"
                )
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
        score_cp = None
        mate_distance = None
        try:
            score_obj = info.get("score")
            if score_obj is not None:
                pov_score = score_obj.pov(root.turn)
                mate_distance = pov_score.mate()
                if mate_distance is not None:
                    score_text = f"M{mate_distance:+d}"
                    score_cp = pov_score.score(mate_score=100000)
                else:
                    cp = pov_score.score()
                    if cp is not None:
                        score_cp = int(cp)
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
            "score_cp": score_cp,
            "mate_distance": mate_distance,
            "sequence": sequence,
            "explanation": explanation,
            "moves": steps,
            "root_fen": str(board_fen),
        })
    return variations


def classify_variation_risk(variations, threshold_cp: int = 150):
    """Return the best scored line and the clearest losing candidate, if any."""
    lines = list(variations or [])
    scored = [
        line for line in lines
        if isinstance(line, dict) and isinstance(line.get("score_cp"), (int, float))
    ]
    if not scored:
        return (lines[0] if lines else None), None, None
    best = max(scored, key=lambda line: float(line["score_cp"]))
    best_cp = float(best["score_cp"])
    danger = []
    for line in scored:
        mate = line.get("mate_distance")
        drop_cp = best_cp - float(line["score_cp"])
        if (mate is not None and int(mate) < 0) or drop_cp >= max(1, int(threshold_cp)):
            item = dict(line)
            item["drop_cp"] = max(0.0, drop_cp)
            danger.append(item)
    bad = min(danger, key=lambda line: float(line.get("score_cp", 0))) if danger else None
    return best, bad, best_cp


class MoveHintBoardPanel:
    """Compact streamer-style text panel; arrows are drawn on the page's own board."""

    def __init__(
        self, parent, initial_mode="hint", on_mode_change=None,
        on_hint_change=None, on_board_arrow=None,
    ):
        import tkinter as tk

        self.tk = tk
        self.on_mode_change = on_mode_change
        self.on_board_arrow = on_board_arrow
        self.mode = "execute" if str(initial_mode).lower() in {"execute", "autoplay"} else "hint"
        self.hint_visible = True
        self.fen = ""
        self.board = None
        self.board_rect = None
        self.bottom_side = "white"
        self.suggestion = None
        self.premove_suggestion = None
        self.coach_lines = []
        self.coach_root_side = "black"
        self.coach_perspective = "opponent"
        self.coach_best_line = None
        self.coach_bad_line = None
        self._alive = True

        self.window = tk.Toplevel(parent)
        self.window.title("Live Board Analysis")
        self.window.geometry("500x245+16+18")
        self.window.minsize(440, 210)
        self.window.configure(background="#101827")
        try:
            self.window.attributes("-alpha", 0.88)
            self.window.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.window.protocol("WM_DELETE_WINDOW", self.hide)

        tk.Label(
            self.window, text="LIVE CHESS ANALYSIS",
            font=("Segoe UI", 12, "bold"), bg="#101827", fg="#e2e8f0",
            anchor="w",
        ).pack(fill="x", padx=12, pady=(8, 2))
        tk.Label(
            self.window,
            text="Native board arrows · stay visible until the board position changes",
            font=("Segoe UI", 8), bg="#101827", fg="#94a3b8", anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 5))

        self.move_text = tk.StringVar(value="Waiting for a live board position…")
        tk.Label(
            self.window, textvariable=self.move_text,
            font=("Segoe UI", 9, "bold"), bg="#182337", fg="#f8fafc",
            wraplength=470, justify="left", anchor="w", padx=8, pady=5,
        ).pack(fill="x", padx=10, pady=(0, 5))

        self.analysis_text = tk.StringVar(value="Waiting for the first move analysis…")
        tk.Label(
            self.window, textvariable=self.analysis_text,
            font=("Consolas", 9), bg="#182337", fg="#dbeafe",
            wraplength=470, justify="left", anchor="w", height=4,
            padx=8, pady=5,
        ).pack(fill="x", padx=10, pady=(0, 4))

        self.analysis_step_status = tk.StringVar(
            value="Default arrow = hint · blue = opponent line · red = avoid"
        )
        tk.Label(
            self.window, textvariable=self.analysis_step_status,
            font=("Segoe UI", 8), bg="#101827", fg="#cbd5e1",
            wraplength=470, justify="left", anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 5))

        buttons = tk.Frame(self.window, bg="#101827")
        buttons.pack(fill="x", padx=10, pady=(0, 8))
        self.mode_button = tk.Button(
            buttons, text="", command=self.toggle_mode,
            font=("Segoe UI", 9, "bold"), padx=8, pady=3,
            bg="#2563eb", fg="white", activebackground="#1d4ed8",
            activeforeground="white", relief="flat", cursor="hand2",
        )
        self.mode_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.visibility_button = tk.Button(
            buttons, text="", command=self.toggle_hint_visibility,
            font=("Segoe UI", 9, "bold"), padx=8, pady=3,
            bg="#15803d", fg="white", activebackground="#166534",
            activeforeground="white", relief="flat", cursor="hand2",
        )
        self.visibility_button.pack(side="left", fill="x", expand=True, padx=(4, 0))
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
                bg="#2563eb", activebackground="#1d4ed8",
            )
        else:
            self.mode_button.configure(
                text="MODE: AUTOPLAY · Switch to HINT [F8]",
                bg="#b91c1c", activebackground="#991b1b",
            )

    def _refresh_visibility_button(self):
        if self.hint_visible:
            self.visibility_button.configure(
                text="BOARD MARKS: ON [F9]",
                bg="#15803d", activebackground="#166534",
            )
        else:
            self.visibility_button.configure(
                text="BOARD MARKS: OFF [F9]",
                bg="#475569", activebackground="#334155",
            )

    def _draw_board_arrow(self, move, color):
        """Ask the browser board to draw a right-click arrow; never create a screen arrow overlay."""
        if not self.hint_visible or self.mode != "hint" or not callable(self.on_board_arrow):
            return False
        try:
            return bool(self.on_board_arrow(
                move, color, self.fen, self.bottom_side, self.board_rect,
            ))
        except Exception:
            self.analysis_step_status.set(
                "Native board drawing is unavailable here; move explanations remain visible."
            )
            return False

    @staticmethod
    def _line_reason(line):
        for step in list(line.get("moves") or [])[:2]:
            notes = step.get("notes") or []
            if notes:
                return f"{step.get('san', '')}: " + "; ".join(notes)
        return str(line.get("explanation") or "No forced tactic identified in this short line.")

    def _line_score(self, line):
        return str(line.get("score") or "eval n/a")

    def _risk_lines(self, lines, threshold_cp=150):
        return classify_variation_risk(lines, threshold_cp=threshold_cp)

    def _summary_text(self, lines, perspective, best_line, bad_line, best_cp):
        actor = "YOUR" if perspective == "you" else "OPPONENT"
        rows = []
        for index, line in enumerate(lines[:3], start=1):
            moves = list(line.get("moves") or [])
            if not moves:
                continue
            first = moves[0].get("san", "?")
            reply = moves[1].get("san", "reply n/a") if len(moves) > 1 else "reply n/a"
            marker = "BEST" if line is best_line else f"#{index}"
            rows.append(f"{marker} {first} → reply {reply} [{self._line_score(line)}]")
        if best_line:
            moves = list(best_line.get("moves") or [])
            best_move = moves[0].get("san", "?") if moves else "?"
            rows.insert(0, f"{actor} BEST: {best_move} · {self._line_reason(best_line)}")
        if bad_line:
            moves = list(bad_line.get("moves") or [])
            bad_move = moves[0].get("san", "?") if moves else "?"
            response = moves[1].get("san", "reply n/a") if len(moves) > 1 else "reply n/a"
            mate = bad_line.get("mate_distance")
            if mate is not None and int(mate) < 0:
                loss = f"allows mate in {abs(int(mate))}"
            elif bad_line.get("drop_cp") is not None:
                loss = f"eval drops about {float(bad_line['drop_cp']) / 100:.1f} pawns vs best"
            else:
                loss = "engine marks this line as dangerous"
            reply_explanation = ""
            if len(moves) > 1 and (moves[1].get("notes") or []):
                reply_explanation = " (" + str(moves[1].get("explanation") or "") + ")"
            rows.insert(1, f"AVOID {bad_move}: {loss}; line continues {response}{reply_explanation}")
        elif best_line and best_line.get("score_cp") is not None:
            rows.insert(1, "No clear blunder among these top lines at the current search depth.")
        return "\n".join(rows[:5])

    def toggle_mode(self, _event=None):
        if not self._alive:
            return "break"
        self.mode = toggle_output_mode(self.mode)
        self._refresh_mode_button()
        self.clear_suggestion()
        if self.mode == "execute":
            self.move_text.set("AUTOPLAY MODE · board markings are not submitted as moves")
            self.analysis_step_status.set("Analysis text is informational; it does not change the move selector.")
        else:
            self.move_text.set("HINT MODE · suggested move is marked on the original board")
            current = self.suggestion or self.premove_suggestion
            if current:
                self._draw_board_arrow(current, "premove" if self.premove_suggestion is current else "hint")
            if self.coach_best_line and self.coach_best_line.get("moves"):
                best_move = self.coach_best_line["moves"][0]
                if not (self.suggestion and best_move.get("uci") == self.suggestion.get("uci")):
                    self._draw_board_arrow(best_move, "best")
            if self.coach_bad_line and self.coach_bad_line.get("moves"):
                self._draw_board_arrow(self.coach_bad_line["moves"][0], "bad")
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
        if not self.hint_visible:
            self.move_text.set("BOARD MARKS OFF · current native arrows may remain until the board changes")
        else:
            current = self.suggestion or self.premove_suggestion
            if current:
                self._draw_board_arrow(
                    current, "premove" if self.premove_suggestion is current else "hint",
                )
            if self.coach_best_line and self.coach_best_line.get("moves"):
                self._draw_board_arrow(self.coach_best_line["moves"][0], "opponent" if self.coach_perspective != "you" else "best")
            if self.coach_bad_line and self.coach_bad_line.get("moves"):
                self._draw_board_arrow(self.coach_bad_line["moves"][0], "bad")
            self.move_text.set("BOARD MARKS ON · native arrows stay until position changes")
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
        try:
            self.window.destroy()
        except self.tk.TclError:
            pass

    def update_position(self, fen, bottom_side=None, board_rect=None):
        if not self._alive or chess is None or not fen:
            return
        try:
            board = chess.Board(str(fen))
        except (ValueError, TypeError):
            return
        side = "black" if str(bottom_side or "").strip().lower() == "black" else "white"
        changed = str(fen) != self.fen or side != self.bottom_side
        self.fen, self.board, self.bottom_side = str(fen), board, side
        self.board_rect = dict(board_rect) if isinstance(board_rect, dict) else None
        if changed:
            self.suggestion = None
            self.premove_suggestion = None
            self.coach_lines = []
            self.coach_best_line = None
            self.coach_bad_line = None
            self.move_text.set("Position changed · waiting for fresh move analysis")
            self.analysis_text.set("Recalculating top options for the new position…")
            self.analysis_step_status.set(
                "No timed fade: analysis refreshes only when the live board position changes."
            )

    def set_suggestion(self, position_fen, uci, san=None, rank=None, reason=None):
        if (
            not self._alive or self.mode != "hint"
            or str(position_fen) != self.fen or self.board is None
        ):
            return False
        try:
            hint = build_hint(self.fen, uci, san)
        except (ValueError, RuntimeError):
            self.move_text.set("Hint skipped · move is not legal for the current position")
            return False
        hint["rank"], hint["reason"] = str(rank or ""), str(reason or "")
        self.suggestion, self.premove_suggestion = hint, None
        if self.hint_visible:
            self._draw_board_arrow(hint, "hint")
        detail = f"Suggested move: {hint['san']} · {hint['uci'][:2]} → {hint['uci'][2:4]}"
        if hint["rank"]:
            detail += f" · {hint['rank']}"
        self.move_text.set(detail)
        return True

    def set_premove_suggestion(self, position_fen, uci, predicted_reply_uci):
        if (
            not self._alive or self.mode != "hint"
            or str(position_fen) != self.fen or self.board is None
        ):
            return False
        try:
            hint = build_premove_hint(position_fen, uci, predicted_reply_uci)
        except (ValueError, RuntimeError):
            return False
        self.suggestion, self.premove_suggestion = None, hint
        if self.hint_visible:
            self._draw_board_arrow(hint, "premove")
        self.move_text.set(
            f"PREMOVE idea: {hint['san']} after {hint['predicted_reply_san']} · suggestion only"
        )
        return True

    def set_coach_variations(
        self, position_fen, engine_infos, analysis_fen=None, perspective="opponent",
    ):
        """Show top-three choices and a risky line as persistent native board marks."""
        if not self._alive or not position_fen or str(position_fen) != self.fen:
            return 0
        root_fen = str(analysis_fen or position_fen)
        try:
            root_board = chess.Board(root_fen)
            self.coach_root_side = "white" if root_board.turn == chess.WHITE else "black"
            raw_infos = [engine_infos] if isinstance(engine_infos, dict) else list(engine_infos or [])
            self.coach_lines = build_coach_variations(
                root_fen, raw_infos, max_lines=3, max_plies=2,
            )
        except (RuntimeError, TypeError, ValueError):
            self.coach_lines = []
            self.coach_best_line = None
            self.coach_bad_line = None
            self.analysis_text.set("Analysis unavailable for this board position.")
            return 0

        self.coach_perspective = "you" if str(perspective).lower() == "you" else "opponent"
        self.coach_best_line, self.coach_bad_line, best_cp = self._risk_lines(self.coach_lines)
        if not self.coach_lines:
            self.analysis_text.set("Waiting for legal engine variations…")
            self.analysis_step_status.set("Text analysis remains active; no timed arrow playback is running.")
            return 0

        self.analysis_text.set(self._summary_text(
            self.coach_lines, self.coach_perspective,
            self.coach_best_line, self.coach_bad_line, best_cp,
        ))
        root_is_live = root_fen == str(position_fen) == self.fen
        if self.hint_visible and self.mode == "hint" and root_is_live:
            best_move = (
                (self.coach_best_line or {}).get("moves") or [None]
            )[0]
            bad_move = (
                (self.coach_bad_line or {}).get("moves") or [None]
            )[0]
            if best_move and not (
                self.suggestion and best_move.get("uci") == self.suggestion.get("uci")
            ):
                self._draw_board_arrow(
                    best_move, "best" if self.coach_perspective == "you" else "opponent",
                )
            if bad_move and (
                not best_move or bad_move.get("uci") != best_move.get("uci")
            ):
                self._draw_board_arrow(bad_move, "bad")
        if self.coach_bad_line:
            bad_steps = self.coach_bad_line.get("moves") or []
            bad_move = bad_steps[0].get("san", "?") if bad_steps else "?"
            self.analysis_step_status.set(
                f"RED = avoid {bad_move}; the text explains the eval loss / mating line. "
                "Marks stay until the position changes."
            )
        else:
            self.analysis_step_status.set(
                "Best line and the top three alternatives are listed above. Marks stay until a move changes the position."
            )
        return len(self.coach_lines)

    def clear_suggestion(self):
        self.suggestion = None
        self.premove_suggestion = None
        self.move_text.set("Waiting for a fresh hint")
