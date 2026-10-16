"""Engine-grounded Hinglish tutoring for a live chess game.

The coach explains the selected move and the engine's available principal
variations. It does not invent tactics: every prediction is labelled as an
engine line, and alternative-move warnings are based on scores supplied by the
caller's existing MultiPV search.
"""
from __future__ import annotations

import queue
import threading
from typing import Any

import chess


_PIECE_NAMES = {
    chess.PAWN: 'pawn',
    chess.KNIGHT: 'knight',
    chess.BISHOP: 'bishop',
    chess.ROOK: 'rook',
    chess.QUEEN: 'queen',
    chess.KING: 'king',
}


def _as_score(info: Any, pov: chess.Color) -> Any | None:
    if not isinstance(info, dict):
        return None
    score = info.get('score')
    if score is None:
        return None
    try:
        return score.pov(pov) if hasattr(score, 'pov') else score
    except (AttributeError, TypeError, ValueError):
        return None


def _score_value(score: Any) -> int | None:
    if score is None:
        return None
    try:
        return int(score.score(mate_score=100000))
    except (AttributeError, TypeError, ValueError):
        return None


def _mate_distance(score: Any) -> int | None:
    if score is None:
        return None
    try:
        return score.mate()
    except (AttributeError, TypeError, ValueError):
        return None


def _score_description(score: Any) -> str:
    if score is None:
        return 'evaluation unavailable'
    try:
        mate = score.mate()
    except (AttributeError, TypeError, ValueError):
        mate = None
    if mate is not None:
        if mate > 0:
            return f'hamari side ke liye forced mate in {mate}'
        return f'opponent ke paas forced mate in {abs(mate)}'
    value = _score_value(score)
    if value is None:
        return 'evaluation unavailable'
    pawns = value / 100.0
    if pawns > 0.05:
        return f'hamari side ko lagbhag +{pawns:.1f} pawn ka faayda'
    if pawns < -0.05:
        return f'opponent ko lagbhag {abs(pawns):.1f} pawn ka faayda'
    return 'position lagbhag equal'


def _move_name(board: chess.Board, move: chess.Move) -> str:
    san = board.san(move)
    source = chess.square_name(move.from_square)
    target = chess.square_name(move.to_square)
    return f'{san} ({source}→{target})'


def _pv_line(board: chess.Board, pv: Any, max_plies: int = 5) -> str:
    if not isinstance(pv, (list, tuple)):
        return ''
    position = board.copy(stack=False)
    san_moves: list[str] = []
    for move in list(pv)[:max_plies]:
        if move not in position.legal_moves:
            break
        try:
            san_moves.append(position.san(move))
            position.push(move)
        except (ValueError, AssertionError):
            break
    return ' '.join(san_moves)


def _capture_details(board: chess.Board, pv: Any) -> list[str]:
    """Point out observable checks/captures in the engine's predicted reply."""
    if not isinstance(pv, (list, tuple)) or len(pv) < 2:
        return []
    position = board.copy(stack=False)
    first, reply = pv[0], pv[1]
    if first not in position.legal_moves:
        return []
    position.push(first)
    if reply not in position.legal_moves:
        return []

    notes: list[str] = []
    try:
        reply_san = position.san(reply)
        if position.gives_check(reply):
            notes.append(f'{reply_san} se king par check aata hai')
        if position.is_capture(reply):
            captured = position.piece_at(reply.to_square)
            if position.is_en_passant(reply):
                captured = chess.Piece(chess.PAWN, not position.turn)
            if captured is not None:
                piece_name = _PIECE_NAMES.get(captured.piece_type, 'piece')
                square = chess.square_name(reply.to_square)
                notes.append(
                    f'{reply_san} se {square} par {piece_name} capture hota hai'
                )
    except (ValueError, AssertionError):
        return notes
    return notes


def _valid_candidates(
    board: chess.Board,
    engine_infos: Any,
) -> list[dict[str, Any]]:
    if isinstance(engine_infos, dict):
        infos = [engine_infos]
    elif isinstance(engine_infos, (list, tuple)):
        infos = list(engine_infos)
    else:
        return []

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for info in infos:
        if not isinstance(info, dict):
            continue
        pv = info.get('pv')
        if not isinstance(pv, (list, tuple)) or not pv:
            continue
        move = pv[0]
        if move not in board.legal_moves:
            continue
        uci = move.uci()
        if uci in seen:
            continue
        seen.add(uci)
        score = _as_score(info, board.turn)
        candidates.append({
            'move': move,
            'info': info,
            'pv': list(pv),
            'score': score,
            'value': _score_value(score),
        })
    return candidates


def build_coach_commentary(
    board: chess.Board,
    engine_infos: Any,
    selected_move: chess.Move,
    selected_info: Any = None,
    selection_meta: Any = None,
    max_alternatives: int = 2,
) -> list[str]:
    """Build compact commentary from existing engine PVs.

    board must be the position before selected_move is played. Scores are
    interpreted from the side-to-move's perspective.
    """
    if not isinstance(board, chess.Board) or selected_move not in board.legal_moves:
        return []

    try:
        selected_name = _move_name(board, selected_move)
    except (ValueError, AssertionError):
        selected_name = selected_move.uci()

    source = ''
    if isinstance(selection_meta, dict):
        source = str(selection_meta.get('source', '') or '')

    lines = [f'[COACH] Hamne {selected_name} chuna.']
    candidates = _valid_candidates(board, engine_infos)
    selected_candidate = next(
        (candidate for candidate in candidates if candidate['move'] == selected_move),
        None,
    )

    if selected_candidate is None and isinstance(selected_info, dict):
        selected_pv = selected_info.get('pv')
        if isinstance(selected_pv, (list, tuple)) and selected_pv and selected_pv[0] == selected_move:
            score = _as_score(selected_info, board.turn)
            selected_candidate = {
                'move': selected_move,
                'info': selected_info,
                'pv': list(selected_pv),
                'score': score,
                'value': _score_value(score),
            }

    if candidates:
        best = max(
            candidates,
            key=lambda candidate: (
                candidate['value'] if candidate['value'] is not None else -100001
            ),
        )
        try:
            best_name = _move_name(board, best['move'])
        except (ValueError, AssertionError):
            best_name = best['move'].uci()
        best_value = best['value']
    else:
        best = None
        best_name = ''
        best_value = None

    if selected_candidate is not None:
        selected_pv = selected_candidate['pv']
        line = _pv_line(board, selected_pv)
        score_text = _score_description(selected_candidate['score'])
        if line:
            lines.append(f'[COACH] Hamari move ki engine line: {line} — {score_text}.')
        else:
            lines.append(f'[COACH] Hamari move ka estimate: {score_text}.')

        if len(selected_pv) >= 2:
            try:
                reply_board = board.copy(stack=False)
                reply_board.push(selected_pv[0])
                reply = selected_pv[1]
                if reply in reply_board.legal_moves:
                    lines.append(
                        f'[COACH] Is line mein opponent ka likely jawab '
                        f'{_move_name(reply_board, reply)} hai.'
                    )
            except (ValueError, AssertionError):
                pass

        selected_score = selected_candidate['score']
        mate = _mate_distance(selected_score)
        best_mate = _mate_distance(best['score']) if best is not None else None
        if mate is not None and mate < 0:
            lines.append(
                f'[COACH WARNING] Is line mein opponent ke paas forced mate '
                f'in {abs(mate)} hai; move ko safe nahi samajhna chahiye.'
            )
        elif (
            best_value is not None
            and selected_candidate['value'] is not None
            and best is not None
            and best['move'] != selected_move
            and mate is None
            and best_mate is None
        ):
            loss_cp = best_value - selected_candidate['value']
            if loss_cp >= 250:
                lines.append(
                    f'[COACH WARNING] Engine ki top move {best_name} ke muqable '
                    f'mein hamari selected move ka estimate lagbhag '
                    f'{loss_cp / 100:.1f} pawn girata hai. Selector ka choice '
                    f'engine ki #1 recommendation ke barabar nahi hai.'
                )
            elif loss_cp >= 90:
                lines.append(
                    f'[COACH] Engine ki top line {best_name} hai; selected line '
                    f'uske muqable lagbhag {loss_cp / 100:.1f} pawn weaker hai.'
                )
        elif (
            best is not None
            and best['move'] != selected_move
            and best_mate is not None
            and best_mate > 0
            and mate is None
        ):
            lines.append(
                f'[COACH WARNING] Engine ki top line {best_name} forced mate in '
                f'{best_mate} dikhati hai, lekin selected move ki line forced mate '
                'confirm nahi karti.'
            )

        for note in _capture_details(board, selected_pv):
            lines.append(f'[COACH TACTIC] {note}.')
    elif source == 'GM_BOOK':
        lines.append(
            '[COACH] Yeh opening-book move hai; is move ki matching engine '
            'variation available nahi, isliye uski exact line claim nahi karunga.'
        )
    else:
        lines.append(
            '[COACH] Selected move ke liye matching engine variation nahi mili; '
            'isliye exact opponent reply ka daawa nahi karunga.'
        )

    shown = 0
    for engine_rank, candidate in enumerate(candidates, start=1):
        if candidate['move'] == selected_move:
            continue
        if engine_rank > 3 or shown >= max(0, int(max_alternatives)):
            continue
        try:
            alt_name = _move_name(board, candidate['move'])
        except (ValueError, AssertionError):
            alt_name = candidate['move'].uci()
        line = _pv_line(board, candidate['pv'])
        desc = _score_description(candidate['score'])
        detail = f'[COACH ALTERNATIVE] Engine #{engine_rank}: {alt_name}'
        if line:
            detail += f'; line: {line}'
        detail += f' — {desc}.'
        lines.append(detail)

        score = candidate['score']
        mate = _mate_distance(score)
        best_mate = _mate_distance(best['score']) if best is not None else None
        if mate is not None and mate < 0:
            lines.append(
                f'[COACH WARNING] Engine #{engine_rank} wali line opponent ko '
                f'forced mate in {abs(mate)} deti hai.'
            )
        elif (
            best_value is not None
            and candidate['value'] is not None
            and best is not None
            and candidate['move'] != best['move']
            and mate is None
            and best_mate is None
        ):
            drop_cp = best_value - candidate['value']
            if drop_cp >= 100:
                lines.append(
                    f'[COACH WARNING] Is alternative par best line ke muqable '
                    f'evaluation lagbhag {drop_cp / 100:.1f} pawn girti hai.'
                )
        for note in _capture_details(board, candidate['pv']):
            lines.append(f'[COACH TACTIC] Engine #{engine_rank}: {note}.')
        shown += 1

    if len(candidates) < 3:
        lines.append(
            '[COACH] Is search mein #2/#3 ke liye enough engine lines nahi mili; '
            'short-clock mode mein alternatives jaan-bujhkar kam calculate hote hain.'
        )
    return lines


def build_opponent_move_commentary(
    board_after_opponent: chess.Board,
    estimated_centipawn_loss: float | None,
) -> list[str]:
    """Describe the actual last opponent move using the reconstructed move stack."""
    if not isinstance(board_after_opponent, chess.Board) or not board_after_opponent.move_stack:
        return []
    position = board_after_opponent.copy(stack=True)
    move = position.pop()
    if move not in position.legal_moves:
        return []

    try:
        san = position.san(move)
        move_name = _move_name(position, move)
    except (ValueError, AssertionError):
        san = move.uci()
        move_name = san

    lines = [f'[COACH OPPONENT] Opponent ne {move_name} khela.']
    try:
        if position.gives_check(move):
            lines.append(
                '[COACH TACTIC] Yeh check hai; pehle king ki safety aur legal '
                'check-evasions dekho.'
            )
        if position.is_capture(move):
            captured = position.piece_at(move.to_square)
            if position.is_en_passant(move):
                captured = chess.Piece(chess.PAWN, board_after_opponent.turn)
            if captured is not None:
                piece_name = _PIECE_NAMES.get(captured.piece_type, 'piece')
                lines.append(
                    f'[COACH TACTIC] Opponent ke {san} ne hamara {piece_name} '
                    f'capture kiya; dekho kya recapture ya tactical reply available hai.'
                )
    except (ValueError, AssertionError):
        pass

    if estimated_centipawn_loss is not None:
        try:
            loss = max(0.0, float(estimated_centipawn_loss))
        except (TypeError, ValueError):
            loss = None
        if loss is not None:
            if loss <= 35:
                lines.append(
                    f'[COACH OPPONENT] Quick comparable search mein yeh best '
                    f'continuation ke kaafi kareeb hai (estimated loss {loss:.0f} cp).'
                )
                if position.is_capture(move) or position.gives_check(move):
                    lines.append(
                        '[COACH TACTIC] Forcing check/capture ke saath yeh '
                        'accurate reply lagta hai; agla response carefully calculate karo.'
                    )
            elif loss >= 100:
                lines.append(
                    f'[COACH OPPONENT] Quick search ke estimate mein opponent ne '
                    f'lagbhag {loss / 100:.1f} pawn ka chance miss kiya; '
                    f'hamaare liye counterplay ho sakta hai.'
                )
            else:
                lines.append(
                    f'[COACH OPPONENT] Move reasonable lagta hai, lekin quick '
                    f'comparison mein lagbhag {loss:.0f} cp ka difference hai.'
                )
    return lines


def voice_summary(commentary: list[str], max_chars: int = 220) -> str:
    """Choose a short spoken version, avoiding repetitive/detailed console lines."""
    if not commentary:
        return ''
    has_our_move = any(line.startswith('[COACH] Hamne') for line in commentary)
    if has_our_move:
        priorities = ('[COACH] Hamne', '[COACH] Is line', '[COACH WARNING]', '[COACH TACTIC]')
    else:
        priorities = ('[COACH OPPONENT] Opponent', '[COACH TACTIC]', '[COACH OPPONENT] Quick')
    selected: list[str] = []
    for prefix in priorities:
        item = next((line for line in commentary if line.startswith(prefix)), None)
        if item and item not in selected:
            item = item.replace('[COACH WARNING]', 'Dhyan dein.')
            item = item.replace('[COACH OPPONENT] ', '')
            item = item.replace('[COACH TACTIC] ', 'Tactical note: ')
            item = item.replace('[COACH] ', '')
            selected.append(item)
    summary = ' '.join(selected)
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1].rsplit(' ', 1)[0] + '…'
    return summary


class CoachSpeaker:
    """Non-blocking optional text-to-speech; speech never delays move submission."""

    def __init__(self, enabled: bool = False):
        self.enabled = bool(enabled)
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None
        if self.enabled:
            self._thread = threading.Thread(
                target=self._worker,
                name='chess-coach-voice',
                daemon=True,
            )
            self._thread.start()

    def speak(self, text: str) -> None:
        if not self.enabled or not text:
            return
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(text)
            except queue.Full:
                pass

    def _worker(self) -> None:
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty('rate', 178)
            engine.setProperty('volume', 0.95)
            try:
                voices = engine.getProperty('voices') or []
                preferred = None
                for marker in ('hi-in', 'hindi', 'en-in', 'india'):
                    for voice in voices:
                        description = (
                            str(getattr(voice, 'id', '')) + ' ' +
                            str(getattr(voice, 'name', '')) + ' ' +
                            str(getattr(voice, 'languages', ''))
                        ).lower()
                        if marker in description:
                            preferred = voice
                            break
                    if preferred is not None:
                        break
                if preferred is not None:
                    engine.setProperty('voice', preferred.id)
            except Exception:
                pass

            while True:
                text = self._queue.get()
                if text is None:
                    self._queue.task_done()
                    break
                try:
                    engine.say(text)
                    engine.runAndWait()
                except Exception:
                    pass
                finally:
                    self._queue.task_done()
            try:
                engine.stop()
            except Exception:
                pass
        except Exception as exc:
            print(
                '[COACH VOICE] Speech engine unavailable; console commentary '
                f'will continue ({type(exc).__name__}).',
                flush=True,
            )

    def close(self) -> None:
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(None)
            except queue.Empty:
                pass
            except queue.Full:
                pass
