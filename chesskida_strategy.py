"""ChessKida fast-main-baseline move-selection logic, separated from board I/O.

This module retains Stockfish candidate selection, mate/capture/promotion handling,
evaluation helpers, adaptive accuracy state, and the original thinking-delay policy.
It deliberately contains no screen capture, scrcpy, Selenium, or mouse code.
"""
from __future__ import annotations

import random
import time

import chess
import chess.engine

# Constants used by the extracted strategy/timing functions.
ANALYSIS_TIME = 0.010
ANALYSIS_DEPTH = 8
BOT_EVERY_MOVE_DELAY_MIN = 0.000
BOT_EVERY_MOVE_DELAY_MAX = 0.010
BOT_THINKING_ENABLED_DEFAULT = True
BOT_THINKING_START_AFTER_MOVE = 8
BOT_BOOK_INSTANT_MOVES = 8
BOT_ENDGAME_PAUSE_MIN = 0.15
BOT_ENDGAME_PAUSE_MAX = 0.75
BOT_ENDGAME_PAUSE_CHANCE = 0.15
BOT_PAUSE_MOVE_COOLDOWN_MIN = 5
BOT_PAUSE_MOVE_COOLDOWN_MAX = 12
BOT_PAUSE_ELIGIBILITY_SCALE = 0.55
BOT_POST_50_MOVE = 50
BOT_MATE_PAUSE_MIN_MATE = 1
BOT_MATE_PAUSE_MAX_MATE = 5
BOT_THINK_PAUSE_MIN = 2.0
BOT_THINK_PAUSE_MAX = 5.0
BOT_MATE_PAUSE_MIN = 2.0
BOT_MATE_PAUSE_MAX = 5.0
BOT_THINK_BUDGET_MIN = 10.0
BOT_THINK_BUDGET_MAX = 15.0
BOT_THINK_ELIGIBILITY_MIN = 0.18
BOT_THINK_ELIGIBILITY_MAX = 0.65
BOT_THINK_REASONABLE_CP = 60
BOT_THINK_UNCERTAINTY_CP = 75
BOT_THINK_SHARP_EVAL_CP = 80
BOT_THINK_STRATEGIC_LEGAL_MOVES = 24
HUMAN_SELECTION_DIVERSITY_EXTRA_DROP = 0.04
TRAINING_MULTI_PV = 15
FORCE_BEST_MIN_CP = 200
FORCE_BEST_IMPROVEMENT_FRACTION = 0.65
HUMAN_ADVANTAGE_START_CP = 400
HUMAN_ADVANTAGE_MAINTAIN_BAND_CP = 30
HUMAN_ADVANTAGE_PROTECT_BAND_CP = 30
HUMAN_ADVANTAGE_HOLD_MIN_MOVES = 1
HUMAN_ADVANTAGE_HOLD_MAX_MOVES = 3
HUMAN_ADVANTAGE_GROWTH_STEP_MIN_CP = 20
HUMAN_ADVANTAGE_GROWTH_STEP_MAX_CP = 45
HUMAN_ADVANTAGE_GROWTH_TRIGGER_CP = 15
HUMAN_ADVANTAGE_GROWTH_PREFERENCE_MIN_CP = 10
HUMAN_ADVANTAGE_GROWTH_PREFERENCE_MAX_CP = 35
HUMAN_WINNING_GUARD_START_CP = 60
HUMAN_WINNING_GUARD_MAX_DROP_CP = 45
HUMAN_WINNING_GUARD_RANK_CAP = 5
HUMAN_STRONG_WINNING_CP = 300
HUMAN_STRONG_WINNING_RANK_CAP = 3
HUMAN_WINNING_NON_BEST_CHANCE = 0.45
HUMAN_STRONG_WINNING_NON_BEST_CHANCE = 0.25
FREE_CAPTURE_MIN_VALUE = 3
FREE_CAPTURE_MAX_CP_DROP = 100
FREE_CAPTURE_SAFETY_DEPTH = 10
FREE_CAPTURE_SAFETY_MAX_CP_DROP = 250
FREE_CAPTURE_SAFETY_MAX_PLY = 4
MATE_FORCE_FAST_MAX = 5
MATE_GRACE_MIN = 6
MATE_GRACE_MAX = 15
MATE_SUSTAIN_MIN_MOVES = 2
MATE_SUSTAIN_MAX_MOVES = 3
MATE_SLOWER_LINE_CHANCE = 0.35
HUMAN_SELECTION_EXTRA_DROP_CP = 70
HUMAN_SELECTION_DISTANCE_CP = 80.0
HUMAN_SELECTION_NON_BEST_CHANCE = 0.88
HUMAN_SELECTION_TOP12_MAX_CHANCE = 0.10
MATE_CLEANUP_MAX_REMAINING_PIECES = 1
MATE_CLEANUP_MIN_VALUE = 1
MIN_POSITIVE_CP = 5
OPPONENT_ACCURACY_WINDOW = 12
OPPONENT_MIN_SAMPLES = 5
ADAPTIVE_MIN_TARGET = 80.0
ADAPTIVE_MAX_TARGET = 95.0
ADAPTIVE_SAFETY_MARGIN = 1.0
ADAPTIVE_STRONG_THRESHOLD = 90.0
ADAPTIVE_VERY_STRONG_THRESHOLD = 94.0

# Stateful strategy and timing values retained across moves in a game.
_mate_progress_target_mate = None
_mate_progress_hold_moves = 0
_mate_progress_hold_limit = 2
_advantage_progress_target_cp = None
_advantage_target_cp = None
_advantage_hold_moves = 0
_advantage_hold_limit = 2
_advantage_last_selected_cp = None
_advantage_light_drop_used = False
_advantage_down_streak = 0
_advantage_progress_hold_moves = 0
_advantage_progress_hold_limit = 2
_advantage_progress_side = None
_last_bot_natural_delay = None
_bot_timing_move_count = 0
_thinking_budget_remaining = None
_thinking_budget_total = None
_thinking_move_cooldown_remaining = 0
_mate_pause_used = False

def score_to_cp(score):
    if score.is_mate():
        mate = score.mate()

        if mate is None:
            return 0

        return (
            100000
            if mate > 0
            else -100000
        )

    value = score.score(
        mate_score=100000
    )

    return (
        0
        if value is None
        else int(value)
    )

def format_eval(cp):
    if cp >= 100000:
        return "MATE WHITE"

    if cp <= -100000:
        return "MATE BLACK"

    return f"{cp / 100.0:+.2f}"

def favor_text(cp):
    if cp >= 100000:
        return "WHITE MATE"

    if cp <= -100000:
        return "BLACK MATE"

    if abs(cp) <= 8:
        return "EQUAL"

    side = (
        "WHITE"
        if cp > 0
        else "BLACK"
    )

    return (
        f"{side} "
        f"+{abs(cp) / 100.0:.2f}"
    )

def material_value(
    piece_type
):
    return {
        chess.PAWN: 100,
        chess.KNIGHT: 320,
        chess.BISHOP: 330,
        chess.ROOK: 500,
        chess.QUEEN: 900,
        chess.KING: 0,
    }.get(
        piece_type,
        0
    )

def side_material(
    board,
    color
):
    total = 0

    for square, piece in board.piece_map().items():
        if piece.color == color:
            total += material_value(
                piece.piece_type
            )

    return total

def classify_move_quality(
    before_board,
    after_board,
    move,
    mover_color,
    best_move,
    best_eval_cp,
    after_eval_cp
):
    if move == best_move:
        if (
            before_board.is_castling(move)
            or move.promotion is not None
            or before_board.gives_check(move)
        ):
            return "BEST"

        return "EXCELLENT"

    if best_eval_cp >= 100000:
        if after_eval_cp >= 100000:
            return "BRILLIANT*"

        return "BLUNDER"

    loss = max(
        0,
        best_eval_cp - after_eval_cp
    )

    material_before = side_material(
        before_board,
        mover_color
    )

    material_after = side_material(
        after_board,
        mover_color
    )

    sacrifice = (
        material_after
        < material_before
    )

    if loss <= 10 and sacrifice:
        return "BRILLIANT*"

    if loss <= 10:
        return "EXCELLENT"

    if loss <= 25:
        return "GREAT"
    if loss <= 70:
        return "GOOD"

    if loss <= 150:
        return "INACCURACY"

    if loss <= 300:
        return "MISTAKE"

    return "BLUNDER"

def natural_bot_move_delay(
    board,
    move,
    engine_result=None,
    selection_meta=None,
    moves_since_buffer=0,
    thinking_enabled=BOT_THINKING_ENABLED_DEFAULT,
    previous_eval_white_cp=None,
    initial_time_seconds=None,
    remaining_time_seconds=None,
    increment_seconds=0.0,
    is_opening_book_move=False
):
    """Return a small move delay plus clock-bounded optional pauses.

    Timing policy:
      * the first eight bot moves never receive a long pause
      * the first eight available book moves and forcing/tactical moves are immediate
      * optional pauses are capped at 60% of the observed starting clock
      * a 10-15 second fallback budget is used only when clocks are unavailable
      * low-clock and late/endgame states allow only short pauses
    """
    global _last_bot_natural_delay
    global _bot_timing_move_count
    global _thinking_budget_remaining
    global _thinking_budget_total
    global _thinking_move_cooldown_remaining
    global _mate_pause_used

    if _thinking_budget_remaining is None or len(board.move_stack) <= 1:
        _bot_timing_move_count = 0
        if initial_time_seconds is not None and float(initial_time_seconds) > 0:
            initial_clock = float(initial_time_seconds)
            # Allocate at most 60% of the starting clock to optional pauses.
            _thinking_budget_total = min(
                initial_clock * 0.60,
                max(0.0, initial_clock - min(3.0, max(0.5, initial_clock * 0.08)))
            )
        else:
            # Fallback when the configured test site does not expose a readable clock.
            _thinking_budget_total = random.uniform(
                BOT_THINK_BUDGET_MIN,
                BOT_THINK_BUDGET_MAX
            )
        _thinking_budget_remaining = _thinking_budget_total
        _thinking_move_cooldown_remaining = 0
        _mate_pause_used = False

    _bot_timing_move_count += 1

    # Keep a very small default delay for ordinary moves.
    base_delay = random.uniform(
        BOT_EVERY_MOVE_DELAY_MIN,
        BOT_EVERY_MOVE_DELAY_MAX
    )
    delay = base_delay
    _last_bot_natural_delay = base_delay

    # Never let optional pauses consume the clock reserve needed to finish and
    # confirm a move. The remaining clock is an observation, not a fake timer.
    if remaining_time_seconds is not None and _thinking_budget_remaining is not None:
        try:
            remaining_clock = max(0.0, float(remaining_time_seconds))
            clock_reserve = max(0.25, min(2.0, remaining_clock * 0.08))
            safe_pause_budget = max(0.0, remaining_clock - clock_reserve)
            _thinking_budget_remaining = min(
                float(_thinking_budget_remaining), safe_pause_budget
            )
        except (TypeError, ValueError):
            pass

    # Opening-book moves are already known; do not wait unnecessarily on the
    # first eight book selections. The site must still confirm the move.
    if is_opening_book_move and _bot_timing_move_count <= BOT_BOOK_INSTANT_MOVES:
        _last_bot_natural_delay = 0.0
        return 0.0

    # Forced/tactical replies should not consume an artificial pause budget.
    try:
        if (
            board.is_check()
            or board.is_capture(move)
            or board.is_castling(move)
            or move.promotion is not None
            or board.gives_check(move)
        ):
            _last_bot_natural_delay = 0.0
            return 0.0
    except Exception:
        pass

    # First eight bot moves: no long pause.
    if _bot_timing_move_count <= BOT_THINKING_START_AFTER_MOVE:
        return max(0.0, float(delay))

    if not thinking_enabled:
        return max(0.0, float(delay))

    # Late/endgame positions use occasional short pauses rather than the
    # regular multi-second pause. Piece count complements move-count detection.
    try:
        endgame_mode = (
            _bot_timing_move_count > BOT_POST_50_MOVE
            or len(board.piece_map()) <= 10
        )
    except Exception:
        endgame_mode = _bot_timing_move_count > BOT_POST_50_MOVE

    if endgame_mode:
        if _thinking_budget_remaining is None or _thinking_budget_remaining <= 0.05:
            return max(0.0, float(delay))
        pause_max = min(
            BOT_ENDGAME_PAUSE_MAX,
            float(_thinking_budget_remaining)
        )
        if remaining_time_seconds is not None:
            try:
                remaining_clock = max(0.0, float(remaining_time_seconds))
                reserve = max(0.25, min(1.0, remaining_clock * 0.10))
                pause_max = min(pause_max, max(0.0, remaining_clock - reserve))
            except (TypeError, ValueError):
                pass
        if (
            pause_max < BOT_ENDGAME_PAUSE_MIN
            or random.random() >= BOT_ENDGAME_PAUSE_CHANCE
        ):
            return max(0.0, float(delay))
        pause_duration = random.uniform(BOT_ENDGAME_PAUSE_MIN, pause_max)
        _thinking_budget_remaining = max(
            0.0, float(_thinking_budget_remaining) - pause_duration
        )
        _last_bot_natural_delay = delay + pause_duration
        print(
            f"[BOT PAUSE] duration={pause_duration:.3f}s "
            f"reason=ENDGAME_SHORT remaining_budget={_thinking_budget_remaining:.3f}s "
            f"moves={_bot_timing_move_count}",
            flush=True
        )
        return max(0.0, float(delay + pause_duration))

    if (
        _thinking_budget_remaining is None
        or _thinking_budget_remaining <= BOT_THINK_PAUSE_MIN
    ):
        return max(0.0, float(delay))

    # Minimum move gap after every real pause.
    # The refractory length itself is randomized from 5-12 moves. This is
    # only a hard minimum; once it expires, the current position must still
    # pass the situation-based stochastic eligibility gate below.
    if _thinking_move_cooldown_remaining > 0:
        _thinking_move_cooldown_remaining -= 1
        return max(0.0, float(delay))

    signal_score = 0.0

    # Multiple reasonable moves / evaluation uncertainty from MultiPV.
    scores = []
    if isinstance(engine_result, list):
        for info in engine_result[:TRAINING_MULTI_PV]:
            try:
                score_obj = info.get("score")
                if score_obj is None:
                    continue

                cp = score_to_cp(
                    score_obj.pov(board.turn)
                )

                if cp is not None:
                    scores.append(int(cp))
            except Exception:
                continue

    if len(scores) >= 2:
        top_cp = scores[0]

        reasonable_count = sum(
            1
            for cp in scores
            if abs(cp - top_cp) <= BOT_THINK_REASONABLE_CP
        )

        if reasonable_count >= 2:
            signal_score += 0.34

        second_gap = abs(scores[1] - top_cp)

        if second_gap <= BOT_THINK_UNCERTAINTY_CP:
            signal_score += 0.24

    # Position immediately after a tactical/forcing change.
    if board.move_stack:
        try:
            previous_board = board.copy(stack=True)
            last_move = previous_board.pop()

            previous_was_tactical = (
                previous_board.is_capture(last_move)
                or previous_board.is_castling(last_move)
                or last_move.promotion is not None
                or previous_board.gives_check(last_move)
            )

            if previous_was_tactical:
                signal_score += 0.22
        except Exception:
            pass

    # Sharp evaluation change relative to the previous completed analysis.
    if (
        previous_eval_white_cp is not None
        and scores
    ):
        try:
            current_eval_white_cp = score_to_cp(
                engine_result[0]["score"].pov(chess.WHITE)
            )

            if (
                current_eval_white_cp is not None
                and abs(
                    int(current_eval_white_cp)
                    - int(previous_eval_white_cp)
                ) >= BOT_THINK_SHARP_EVAL_CP
            ):
                signal_score += 0.22
        except Exception:
            pass

    # Strategic decision point: many legal choices.
    try:
        legal_count = board.legal_moves.count()
    except Exception:
        legal_count = 0

    if legal_count >= BOT_THINK_STRATEGIC_LEGAL_MOVES:
        signal_score += 0.14

    # Strongly forcing current moves are poor pause candidates.
    try:
        forced_move = (
            board.is_check()
            or board.is_capture(move)
            or board.is_castling(move)
            or move.promotion is not None
            or board.gives_check(move)
        )
    except Exception:
        forced_move = False

    if forced_move:
        signal_score *= 0.35

    if signal_score <= 0.0:
        return max(0.0, float(delay))

    # Position-dependent random eligibility.
    #
    # Important: there is NO target like "pause again at move 8/10/12".
    # The 5-8 block above is only a hard minimum. Once it expires, this
    # stochastic position gate is evaluated independently on each move.
    # Weak/quiet positions tend to wait longer; genuinely rich positions
    # can pause soon after the minimum block.
    eligibility = min(
        BOT_THINK_ELIGIBILITY_MAX,
        max(
            BOT_THINK_ELIGIBILITY_MIN,
            (
                BOT_THINK_ELIGIBILITY_MIN
                + signal_score * BOT_PAUSE_ELIGIBILITY_SCALE
            )
        )
    )

    if random.random() >= eligibility:
        return max(0.0, float(delay))

    # Budget safety.
    if _thinking_budget_remaining <= BOT_THINK_PAUSE_MIN:
        return max(0.0, float(delay))

    pause_max = min(
        BOT_THINK_PAUSE_MAX,
        float(_thinking_budget_remaining)
    )
    if remaining_time_seconds is not None:
        try:
            remaining_clock = max(0.0, float(remaining_time_seconds))
            reserve = max(0.25, min(2.0, remaining_clock * 0.08))
            pause_max = min(pause_max, max(0.0, remaining_clock - reserve))
        except (TypeError, ValueError):
            pass

    if pause_max < BOT_THINK_PAUSE_MIN:
        return max(0.0, float(delay))

    pause_duration = random.uniform(
        BOT_THINK_PAUSE_MIN,
        pause_max
    )

    _thinking_budget_remaining = max(
        0.0,
        float(_thinking_budget_remaining) - pause_duration
    )

    # Only the randomized refractory minimum is set here. The next
    # pause is NOT scheduled for a specific move; after this block, the
    # current position must pass a fresh situation-based eligibility test.
    _thinking_move_cooldown_remaining = random.randint(
        BOT_PAUSE_MOVE_COOLDOWN_MIN,
        BOT_PAUSE_MOVE_COOLDOWN_MAX
    )

    delay += pause_duration

    print(
        "[BOT PAUSE] "
        f"duration={pause_duration:.3f}s "
        f"remaining_budget={_thinking_budget_remaining:.3f}s "
        f"next_pause_block={_thinking_move_cooldown_remaining} moves "
        f"eligibility={eligibility:.2f} "
        f"move={_bot_timing_move_count}",
        flush=True
    )

    return max(0.0, float(delay))

def build_analysis(
    engine,
    before_board,
    after_board,
    move,
    best_info=None
):
    mover_color = before_board.turn

    try:
        if best_info is None:
            best_info = engine.analyse(
                before_board,
                chess.engine.Limit(
                    depth=ANALYSIS_DEPTH,
                    time=ANALYSIS_TIME
                )
            )

        best_score_obj = best_info.get(
            "score"
        )

        if best_score_obj is None:
            return None

        best_score_white = (
            best_score_obj.pov(
                chess.WHITE
            )
        )

        best_eval_cp = score_to_cp(
            best_score_white
        )

        best_move = best_info.get(
            "pv",
            [None]
        )[0]

        after_info = engine.analyse(
            after_board,
            chess.engine.Limit(
                depth=ANALYSIS_DEPTH,
                time=ANALYSIS_TIME
            )
        )

        after_score_obj = after_info.get(
            "score"
        )

        if after_score_obj is None:
            return None

        after_score_white = (
            after_score_obj.pov(
                chess.WHITE
            )
        )

        after_eval_cp = score_to_cp(
            after_score_white
        )

        quality = classify_move_quality(
            before_board,
            after_board,
            move,
            mover_color,
            best_move,
            score_to_cp(
                best_score_obj.pov(
                    mover_color
                )
            ),
            score_to_cp(
                after_score_obj.pov(
                    mover_color
                )
            )
        )

        pv = []
        temp = after_board.copy()

        for pv_move in after_info.get(
            "pv",
            []
        )[:6]:
            if pv_move not in temp.legal_moves:
                break

            pv.append(
                temp.san(
                    pv_move
                )
            )

            temp.push(
                pv_move
            )

        return {
            "eval_cp": after_eval_cp,
            "eval_text": format_eval(
                after_eval_cp
            ),
            "favor": favor_text(
                after_eval_cp
            ),
            "quality": quality,
            "last_move": (
                after_board.peek().uci()
                if after_board.move_stack
                else move.uci()
            ),
            "best_move": (
                best_move.uci()
                if best_move is not None
                else "-"
            ),
            "depth": after_info.get(
                "depth",
                ANALYSIS_DEPTH
            ),
            "pv": (
                " ".join(pv)
                if pv
                else "-"
            )
        }

    except Exception as exc:
        print(
            f"[ANALYSIS ERROR] {exc}"
        )

        return None


# ============================================================
# STOCKFISH MOVE SELECTION: #1 - #8
# ============================================================

def safe_drop_fraction(
    current_cp
):
    current_eval = (
        current_cp / 100.0
    )

    if current_eval < 2.0:
        return 0.10

    if current_eval < 3.0:
        return 0.15

    if current_eval < 4.0:
        return 0.20

    if current_eval < 5.0:
        return 0.25

    return 0.30

def opponent_recent_accuracy(
    match_history
):
    if not match_history:
        return None, 0

    recent = match_history[
        -OPPONENT_ACCURACY_WINDOW:
    ]

    n = len(recent)

    weights = list(
        range(1, n + 1)
    )

    total_weight = float(
        sum(weights)
    )

    weighted = sum(
        score * weight
        for score, weight
        in zip(
            recent,
            weights
        )
    )

    return (
        weighted / total_weight,
        n
    )

def adaptive_accuracy_profile(
    opponent_accuracy,
    sample_count
):
    max_rank = TRAINING_MULTI_PV - 1

    if (
        opponent_accuracy is None
        or sample_count < OPPONENT_MIN_SAMPLES
    ):
        return {
            "opponent_accuracy": opponent_accuracy,
            "target_accuracy": 88.0,
            "max_rank": max_rank,
            "max_eval_drop": 0.12,
            "state": (
                f"WARMUP "
                f"{sample_count}/"
                f"{OPPONENT_MIN_SAMPLES}"
            ),
        }

    score = float(
        opponent_accuracy
    )

    target = min(
        ADAPTIVE_MAX_TARGET,
        max(
            ADAPTIVE_MIN_TARGET,
            score + ADAPTIVE_SAFETY_MARGIN
        )
    )

    if score < 70.0:
        max_eval_drop = 0.18
        state = "OPPONENT LIGHT"

    elif score < 80.0:
        max_eval_drop = 0.14
        state = "OPPONENT MEDIUM"

    elif score < 88.0:
        max_eval_drop = 0.10
        state = "OPPONENT STRONG"

    elif score < ADAPTIVE_STRONG_THRESHOLD:
        max_eval_drop = 0.075
        state = "OPPONENT VERY STRONG"

    elif score < ADAPTIVE_VERY_STRONG_THRESHOLD:
        max_eval_drop = 0.050
        state = "OPPONENT ELITE"

    else:
        max_eval_drop = 0.035
        state = "OPPONENT EXTREME"

    return {
        "opponent_accuracy": score,
        "target_accuracy": target,
        "max_rank": max_rank,
        "max_eval_drop": max_eval_drop,
        "state": state,
    }

def find_free_mate_cleanup_capture(board, candidates):
    """Find a safe capture of free opponent material during mate play.

    A capture qualifies when the target contains a non-king opponent piece,
    the capturing piece cannot be legally recaptured on the destination
    square immediately after the capture, and the opponent still has more
    than one non-king piece overall. Prefer higher-value free material; for
    equal values prefer a move that is already present in MultiPV.
    """
    mover = board.turn
    candidate_map = {
        candidate["move"]: candidate
        for candidate in candidates
    }

    opponent_material = sum(
        1
        for piece in board.piece_map().values()
        if (
            piece.color != mover
            and piece.piece_type != chess.KING
        )
    )

    if (
        opponent_material
        <= MATE_CLEANUP_MAX_REMAINING_PIECES
    ):
        return None

    piece_values = {
        chess.PAWN: 1,
        chess.KNIGHT: 3,
        chess.BISHOP: 3,
        chess.ROOK: 5,
        chess.QUEEN: 9,
    }

    captures = []

    for move in board.legal_moves:
        victim = board.piece_at(
            move.to_square
        )

        if victim is None:
            continue

        if victim.color == mover:
            continue

        victim_value = piece_values.get(
            victim.piece_type,
            0
        )

        if victim_value < MATE_CLEANUP_MIN_VALUE:
            continue

        trial = board.copy(
            stack=False
        )

        trial.push(move)

        # "Free" means the capturing piece is not immediately capturable
        # by any legal reply on the destination square.
        immediately_recapturable = any(
            reply.to_square == move.to_square
            for reply in trial.legal_moves
        )

        if immediately_recapturable:
            continue

        candidate = candidate_map.get(
            move
        )

        captures.append(
            {
                "move": move,
                "victim": victim,
                "victim_value": victim_value,
                "candidate": candidate,
                "rank": (
                    candidate["rank"]
                    if candidate is not None
                    else None
                ),
                "cp": (
                    candidate["cp"]
                    if candidate is not None
                    else None
                ),
            }
        )

    if not captures:
        return None

    max_value = max(
        item["victim_value"]
        for item in captures
    )

    best_value_captures = [
        item
        for item in captures
        if item["victim_value"] == max_value
    ]

    candidate_captures = [
        item
        for item in best_value_captures
        if item["candidate"] is not None
    ]

    if candidate_captures:
        selected = max(
            candidate_captures,
            key=lambda item: (
                item["cp"],
                -item["rank"]
            )
        )
    else:
        selected = random.choice(
            best_value_captures
        )

    return selected

def find_priority_queen_promotion(
    board,
    candidates,
    best_cp
):
    """Prefer a safe QUEEN promotion whenever promotion is available.

    Queen promotion is the default conversion choice. Underpromotion is left
    to the engine only when no legal queen promotion exists or the queen move
    is clearly unsafe and a better engine-backed promotion is available.
    """
    candidate_map = {
        candidate["move"]: candidate
        for candidate in candidates
    }

    queen_promotions = [
        move
        for move in board.legal_moves
        if move.promotion == chess.QUEEN
    ]

    if not queen_promotions:
        return None

    choices = []

    for move in queen_promotions:
        candidate = candidate_map.get(move)

        try:
            after = board.copy(stack=False)
            after.push(move)
        except Exception:
            continue

        if after.is_checkmate():
            return {
                "move": move,
                "info": candidate["info"] if candidate else None,
                "rank": candidate["rank"] if candidate else 0,
                "cp": candidate["cp"] if candidate else best_cp,
                "reason_tag": "QUEEN PROMOTION MATE"
            }

        # Never force a queen promotion when the newly promoted queen can be
        # immediately captured and the engine did not consider the move.
        if candidate is None:
            immediate_queen_loss = any(
                reply.to_square == move.to_square
                for reply in after.legal_moves
            )
            if immediate_queen_loss:
                continue

        candidate_cp = (
            int(candidate["cp"])
            if candidate is not None
            else best_cp
        )

        if candidate is not None and candidate_cp < best_cp - FREE_CAPTURE_MAX_CP_DROP:
            continue

        choices.append(
            {
                "move": move,
                "info": candidate["info"] if candidate else None,
                "rank": candidate["rank"] if candidate else 0,
                "cp": candidate_cp,
                "reason_tag": "QUEEN PROMOTION"
            }
        )

    if not choices:
        return None

    choices.sort(
        key=lambda item: (
            item["cp"],
            -item["rank"]
        ),
        reverse=True
    )

    return choices[0]

def free_capture_position_safe(
    board,
    move
):
    """Check that a free capture does not immediately expose our position.

    The capture is accepted only when the resulting position does not give the
    opponent a mate-in-one and does not leave a major/minor own piece hanging
    for a free immediate capture. This protects the rest of our material while
    still allowing genuinely free captures.
    """
    try:
        after = board.copy(stack=False)
        after.push(move)

        # Never choose a free capture that walks into an immediate checkmate.
        for reply in after.legal_moves:
            trial = after.copy(stack=False)
            trial.push(reply)

            if trial.is_checkmate():
                return False

        piece_values = {
            chess.PAWN: 1,
            chess.KNIGHT: 3,
            chess.BISHOP: 3,
            chess.ROOK: 5,
            chess.QUEEN: 9,
            chess.KING: 100,
        }

        # After our free capture, reject any immediate opponent capture that
        # wins one of our major/minor pieces without an immediate recapture.
        for reply in after.legal_moves:
            victim = after.piece_at(reply.to_square)

            if victim is None or victim.color != board.turn:
                continue

            victim_value = piece_values.get(
                victim.piece_type,
                0
            )

            # Pawns are allowed to remain tactically loose; the requested
            # protection is for the rest of our important pieces.
            if victim_value < 3:
                continue

            after_reply = after.copy(stack=False)
            after_reply.push(reply)

            can_recapture = any(
                response.to_square == reply.to_square
                for response in after_reply.legal_moves
            )

            if can_recapture:
                continue

            return False

        return True

    except Exception:
        # Safety-first: if the position cannot be checked reliably, do not
        # promote this move to a "free capture" automatically.
        return False

def free_capture_engine_safe(
    board,
    move,
    engine,
    reference_cp
):
    """Second-stage safety check for a genuinely free capture.

    Uses a shallow engine line after the capture to reject:
    - forced mate against us in M1-M5;
    - a large evaluation collapse;
    - a major/minor own piece being lost in the first ~2 opponent moves
      without an immediate recapture in the principal variation.

    This is only applied to free-capture candidates, not ordinary engine
    captures, so normal move selection stays fast.
    """
    if engine is None:
        return True

    try:
        after = board.copy(stack=False)
        after.push(move)

        info = engine.analyse(
            after,
            chess.engine.Limit(
                depth=FREE_CAPTURE_SAFETY_DEPTH
            )
        )

        score_obj = info.get("score")
        if score_obj is not None:
            our_score = score_obj.pov(board.turn)
            mate = our_score.mate()

            # Never enter a forced mate M1-M5 after grabbing a "free" piece.
            if (
                mate is not None
                and mate < 0
                and abs(int(mate)) <= 5
            ):
                return False

            cp = our_score.score(
                mate_score=100000
            )
            if (
                cp is not None
                and reference_cp is not None
                and cp < int(reference_cp) - FREE_CAPTURE_SAFETY_MAX_CP_DROP
            ):
                return False

        # Tactical material safety over the first 1-2 opponent moves.
        # Look only at the engine PV, keeping this check bounded and fast.
        pv = info.get("pv", [])
        probe = after

        for ply_index, reply in enumerate(
            pv[:FREE_CAPTURE_SAFETY_MAX_PLY]
        ):
            if reply not in probe.legal_moves:
                break

            mover = probe.turn
            victim_square = reply.to_square

            if probe.is_en_passant(reply):
                victim_square = (
                    reply.to_square - 8
                    if mover == chess.WHITE
                    else reply.to_square + 8
                )

            victim = probe.piece_at(victim_square)

            if (
                mover != board.turn
                and victim is not None
                and victim.color == board.turn
                and victim.piece_type in {
                    chess.KNIGHT,
                    chess.BISHOP,
                    chess.ROOK,
                    chess.QUEEN,
                }
            ):
                victim_value = material_value(
                    victim.piece_type
                )

                after_opponent_capture = probe.copy(
                    stack=False
                )
                after_opponent_capture.push(reply)

                recaptured = any(
                    response.to_square == reply.to_square
                    for response in after_opponent_capture.legal_moves
                )

                if not recaptured and victim_value >= 300:
                    return False

            probe.push(reply)

        return True

    except Exception:
        # Safety-first for this optional second-stage check: when the engine
        # cannot evaluate the candidate reliably, do not elevate it as a safe
        # free capture.
        return False

def find_priority_capture(
    board,
    candidates,
    best_cp,
    force_endgame=False,
    engine=None
):
    """Prefer safe captures before human-like wandering.

    A capture can come from MultiPV=15 or, for a genuinely free piece, from
    the complete legal move list. This applies to captures by every piece
    type, including king captures; the king itself is never a legal victim.
    """
    candidate_map = {
        candidate["move"]: candidate
        for candidate in candidates
    }

    own_material = side_material(board, board.turn)
    enemy_material = side_material(board, not board.turn)

    non_king_pieces = sum(
        1
        for piece in board.piece_map().values()
        if piece.piece_type != chess.KING
    )

    winning_material = own_material > enemy_material
    endgame = force_endgame or non_king_pieces <= 10

    choices = []

    for move in board.legal_moves:
        if not board.is_capture(move):
            continue

        victim_square = move.to_square
        if board.is_en_passant(move):
            victim_square = (
                move.to_square - 8
                if board.turn == chess.WHITE
                else move.to_square + 8
            )

        victim = board.piece_at(victim_square)
        if victim is None or victim.color == board.turn:
            continue

        victim_value = material_value(victim.piece_type)
        if victim_value <= 0:
            continue

        mover_piece = board.piece_at(move.from_square)
        mover_value = (
            material_value(mover_piece.piece_type)
            if mover_piece is not None
            else 0
        )

        try:
            after = board.copy(stack=False)
            after.push(move)
        except Exception:
            continue

        if after.is_checkmate():
            return {
                "move": move,
                "info": candidate_map.get(move, {}).get("info")
                    if move in candidate_map
                    else None,
                "rank": (
                    candidate_map[move]["rank"]
                    if move in candidate_map
                    else 0
                ),
                "cp": (
                    candidate_map[move]["cp"]
                    if move in candidate_map
                    else best_cp
                ),
                "victim_value": victim_value,
                "reason_tag": "CHECKMATE CAPTURE"
            }

        immediate_replies = [
            reply
            for reply in after.legal_moves
            if reply.to_square == move.to_square
        ]

        # A truly free capture: no opponent piece can immediately take the
        # capturing piece. The deeper engine safety pass is applied later
        # after candidates are ranked, so normal move selection stays fast.
        free_capture = (
            not immediate_replies
            and free_capture_position_safe(board, move)
        )

        candidate = candidate_map.get(move)
        candidate_cp = (
            int(candidate["cp"])
            if candidate is not None
            else best_cp
        )

        # Candidate captures from MultiPV are trusted inside a reasonable
        # engine-score band. Non-MultiPV captures are used only when they are
        # genuinely free; this prevents forcing poisoned captures.
        if candidate is None:
            if not free_capture:
                continue
        else:
            if candidate_cp < best_cp - FREE_CAPTURE_MAX_CP_DROP:
                continue

            # Do not trade a high-value piece for a lower-value victim simply
            # because it happens to be a capture, unless the engine itself
            # selected it inside the normal safety band.
            if (
                not free_capture
                and victim_value < mover_value
                and winning_material
            ):
                continue

        # In a winning endgame, free captures get an even stronger priority.
        # Otherwise, only take them ahead of the fuzzy shuffle when the
        # position is materially/evaluationally favorable.
        if not free_capture:
            if endgame and winning_material:
                # Equal-or-better trades are acceptable in liquidation mode.
                if victim_value < mover_value:
                    continue
            elif candidate is None:
                continue

        choices.append(
            {
                "move": move,
                "info": (
                    candidate["info"]
                    if candidate is not None
                    else None
                ),
                "rank": (
                    candidate["rank"]
                    if candidate is not None
                    else 99
                ),
                "cp": candidate_cp,
                "victim_value": victim_value,
                "free": free_capture,
                "mover_value": mover_value,
                "reason_tag": (
                    "FREE CAPTURE"
                    if free_capture
                    else "SAFE LIQUIDATION CAPTURE"
                )
            }
        )

    if not choices:
        return None

    # First take the highest-value free target. In endgames, also prefer
    # favorable liquidation captures over wandering moves.
    choices.sort(
        key=lambda item: (
            1 if item["free"] else 0,
            item["victim_value"],
            item["cp"],
            -item["rank"]
        ),
        reverse=True
    )

    # Free pieces remain the highest capture priority, but only after
    # the candidate survives the 1-2 move material / M1-M5 mate safety test.
    free_choices = [
        item
        for item in choices
        if item["free"]
    ]

    if free_choices and engine is not None:
        free_choices.sort(
            key=lambda item: (
                item["victim_value"],
                item["cp"],
                -item["rank"]
            ),
            reverse=True
        )

        for free_choice in free_choices:
            if free_capture_engine_safe(
                board,
                free_choice["move"],
                engine,
                best_cp
            ):
                return free_choice
    elif free_choices:
        return free_choices[0]

    selected = choices[0]

    if (
        selected["free"]
        or endgame
        or winning_material
    ):
        # A free capture that failed the deeper safety check must not be
        # re-selected as a fallback. Prefer the best ordinary capture instead.
        if selected["free"] and engine is not None:
            safe_alternatives = [
                item
                for item in choices
                if not item["free"]
            ]
            if safe_alternatives:
                return max(
                    safe_alternatives,
                    key=lambda item: (
                        item["cp"],
                        -item["rank"]
                    )
                )
            return None
        return selected

    return None

def find_safe_free_capture(
    board,
    candidates,
    best_cp
):
    """Find a genuinely free major/minor piece to capture.

    A capture is "free" here only when the captured piece is worth at least a
    minor piece and the capturing piece cannot be immediately taken back.
    The candidate must still be reasonably close to the current best engine
    score so a poisoned/tactical capture is not forced.
    """
    choices = []

    for candidate in candidates:
        move = candidate["move"]
        victim_square = move.to_square

        if board.is_en_passant(move):
            victim_square = (
                move.to_square - 8
                if board.turn == chess.WHITE
                else move.to_square + 8
            )

        victim = board.piece_at(victim_square)

        if victim is None or victim.color == board.turn:
            continue

        victim_value = material_value(victim.piece_type)
        if victim_value < FREE_CAPTURE_MIN_VALUE * 100:
            continue

        try:
            after = board.copy(stack=False)
            after.push(move)
        except Exception:
            continue

        # No legal reply may capture the moved piece. That is the key
        # distinction between a genuinely free piece and an ordinary exchange.
        immediate_take = any(
            reply.to_square == move.to_square
            for reply in after.legal_moves
        )

        if immediate_take:
            continue

        if not free_capture_position_safe(board, move):
            continue

        candidate_cp = int(candidate.get("cp", best_cp))
        if candidate_cp < best_cp - FREE_CAPTURE_MAX_CP_DROP:
            continue

        choices.append(
            (
                victim_value,
                candidate_cp,
                -int(candidate.get("rank", 99)),
                candidate
            )
        )

    if not choices:
        return None

    choices.sort(reverse=True, key=lambda item: item[:3])
    return choices[0][3]

def candidate_hangs_own_piece(
    board,
    move
):
    """Reject obvious one-move giveaways.

    This is intentionally conservative: a check/mate or a materially winning
    capture can still be a deliberate sacrifice. Otherwise a moved piece that
    can be taken next move without a recapture is rejected when that sequence
    loses material.
    """
    try:
        if board.gives_check(move):
            return False

        before_victim = board.piece_at(move.to_square)
        victim_value = (
            material_value(before_victim.piece_type)
            if before_victim is not None
            and before_victim.color != board.turn
            else 0
        )

        moved_piece = board.piece_at(move.from_square)
        if moved_piece is None:
            return True

        moved_value = material_value(moved_piece.piece_type)

        after = board.copy(stack=False)
        after.push(move)

        if after.is_checkmate():
            return False

        for reply in after.legal_moves:
            if reply.to_square != move.to_square:
                continue

            reply_piece = after.piece_at(reply.from_square)
            if reply_piece is None:
                continue

            after_reply = after.copy(stack=False)
            after_reply.push(reply)

            recaptured = any(
                response.to_square == reply.to_square
                for response in after_reply.legal_moves
            )

            if recaptured:
                continue

            # Net material swing after the opponent takes our moved piece.
            # Negative means the move simply gave material away.
            net = victim_value - moved_value

            if net < 0 and moved_value >= 100:
                return True

        return False

    except Exception:
        # Safety-first fallback: unknown candidates are not rejected here;
        # the existing engine score and physical verification remain in force.
        return False

def choose_stockfish_move(
    board,
    multipv_infos,
    previous_eval_white_cp=None,
    opponent_accuracy=None,
    opponent_sample_count=0,
    opponent_pressure=False,
    engine=None
):
    # Reset the historical fuzzy state at a fresh match/new game.
    # This is retained from the fast-main-match-controls branch so a previous
    # game's advantage/mate progression never leaks into the next game.
    global _advantage_target_cp
    global _advantage_hold_moves
    global _advantage_hold_limit
    global _advantage_last_selected_cp
    global _advantage_light_drop_used
    global _advantage_down_streak
    global _mate_progress_target_mate
    global _mate_progress_hold_moves
    global _mate_progress_hold_limit

    if len(board.move_stack) <= 1:
        _advantage_target_cp = None
        _advantage_hold_moves = 0
        _advantage_hold_limit = random.randint(
            HUMAN_ADVANTAGE_HOLD_MIN_MOVES,
            HUMAN_ADVANTAGE_HOLD_MAX_MOVES
        )
        _advantage_last_selected_cp = None
        _advantage_light_drop_used = False
        _advantage_down_streak = 0
        _mate_progress_target_mate = None
        _mate_progress_hold_moves = 0
        _mate_progress_hold_limit = random.randint(
            MATE_SUSTAIN_MIN_MOVES,
            MATE_SUSTAIN_MAX_MOVES
        )
    if not multipv_infos:
        return (
            None,
            None,
            {
                "rank": 0,
                "current_cp": 0,
                "selected_cp": 0,
                "reason": "no MultiPV candidates"
            }
        )

    mover = board.turn
    candidates = []

    for rank, info in enumerate(
        multipv_infos[
            :TRAINING_MULTI_PV
        ]
    ):
        pv = info.get(
            "pv",
            []
        )

        if not pv:
            continue

        move = pv[0]

        if move not in board.legal_moves:
            continue

        score_obj = info.get(
            "score"
        )

        if score_obj is None:
            continue

        pov_score = score_obj.pov(
            mover
        )

        cp = pov_score.score(
            mate_score=100000
        )

        if cp is None:
            cp = 0

        candidates.append({
            "rank": rank,
            "move": move,
            "info": info,
            "cp": int(cp),
            "mate": pov_score.mate(),
        })

    if not candidates:
        return (
            None,
            None,
            {
                "rank": 0,
                "current_cp": 0,
                "selected_cp": 0,
                "reason": "no legal MultiPV candidates"
            }
        )

    best = candidates[0]
    best_cp = best["cp"]

    profile = adaptive_accuracy_profile(
        opponent_accuracy,
        opponent_sample_count
    )

    adaptive_max_rank = int(
        profile["max_rank"]
    )

    adaptive_max_drop = float(
        profile["max_eval_drop"]
    )

    reference_cp = None

    if previous_eval_white_cp is not None:
        reference_cp = (
            previous_eval_white_cp
            if mover == chess.WHITE
            else -previous_eval_white_cp
        )

    if (
        reference_cp is not None
        and reference_cp >= FORCE_BEST_MIN_CP
        and not (
            best["mate"] is not None
            and best["mate"] > 0
            and best["mate"] <= MATE_GRACE_MAX
        )
    ):
        improvement_fraction = (
            (
                best_cp
                - reference_cp
            )
            / max(
                1,
                abs(reference_cp)
            )
        )

        if (
            improvement_fraction
            >= FORCE_BEST_IMPROVEMENT_FRACTION
        ):
            selected = best

            return (
                selected["move"],
                selected["info"],
                {
                    "rank": selected["rank"],
                    "current_cp": best_cp,
                    "selected_cp": selected["cp"],
                    "reason": (
                        f"FORCED #1 | "
                        f"reference="
                        f"+{reference_cp/100:.2f} "
                        f"best="
                        f"+{best_cp/100:.2f} "
                        f"improvement="
                        f"{improvement_fraction*100:.0f}%"
                    )
                }
            )

    if (
        best["mate"] is not None
        and best["mate"] > 0
    ):
        # During the human-like mating window, clear genuinely free
        # material first. This is intentionally before the M4 force rule:
        # if the opponent blunders a queen/rook/minor/pawn and it is safely
        # capturable, take it before closing the game. When only one
        # non-king piece remains, proceed with the normal mate plan.
        if (
            MATE_GRACE_MIN
            <= best["mate"]
            <= MATE_GRACE_MAX
        ):
            cleanup = find_free_mate_cleanup_capture(
                board,
                candidates
            )

            if cleanup is not None:
                selected = cleanup["candidate"]

                if selected is None:
                    selected = {
                        "move": cleanup["move"],
                        "info": best["info"],
                        "rank": None,
                        "cp": best_cp,
                        "mate": None,
                    }
                else:
                    selected = dict(
                        selected
                    )
                    selected["move"] = cleanup["move"]

                victim_name = chess.piece_name(
                    cleanup["victim"].piece_type
                ).upper()

                rank_text = (
                    f"#{cleanup['rank'] + 1}"
                    if cleanup["rank"] is not None
                    else "FREE"
                )

                return (
                    selected["move"],
                    selected["info"],
                    {
                        "rank": cleanup["rank"],
                        "current_cp": best_cp,
                        "selected_cp": cleanup["cp"],
                        "source": "MATE_CLEANUP",
                        "reason": (
                            f"MATE CLEANUP | "
                            f"FREE {victim_name} "
                            f"TARGET="
                            f"{chess.square_name(selected['move'].to_square)} "
                            f"RANK={rank_text}"
                        )
                    }
                )

        # M4 or closer: stop humanizing and take the fastest mate immediately.
        if best["mate"] <= MATE_FORCE_FAST_MAX:
            _mate_progress_target_mate = None
            _mate_progress_hold_moves = 0
            _mate_progress_hold_limit = MATE_SUSTAIN_MIN_MOVES

            selected = best

            return (
                selected["move"],
                selected["info"],
                {
                    "rank": selected["rank"],
                    "current_cp": best_cp,
                    "selected_cp": selected["cp"],
                    "reason": (
                        f"MATE FORCE | "
                        f"M{selected['mate']} "
                        f"RANK=#"
                        f"{selected['rank'] + 1}"
                    )
                }
            )

        # The human-like mate window remains M5-M15. Outside that window,
        # keep the existing direct #1 behavior.
        if not (
            MATE_GRACE_MIN
            <= best["mate"]
            <= MATE_GRACE_MAX
        ):
            _mate_progress_target_mate = None
            _mate_progress_hold_moves = 0
            _mate_progress_hold_limit = MATE_SUSTAIN_MIN_MOVES

            selected = best

            return (
                selected["move"],
                selected["info"],
                {
                    "rank": selected["rank"],
                    "current_cp": best_cp,
                    "selected_cp": selected["cp"],
                    "reason": (
                        f"MATE #1 | "
                        f"M{selected['mate']} "
                        f"RANK=#"
                        f"{selected['rank'] + 1}"
                    )
                }
            )

        # Start/recover the sustained mate target. The target represents the
        # mate level the bot is currently willing to play around. It can move
        # faster only one step at a time; it may move slower when the actual
        # engine mate itself has become slower because the opponent defended.
        if _mate_progress_target_mate is None:
            _mate_progress_target_mate = best["mate"]
            _mate_progress_hold_moves = 0
            _mate_progress_hold_limit = random.randint(
                MATE_SUSTAIN_MIN_MOVES,
                MATE_SUSTAIN_MAX_MOVES
            )
        elif best["mate"] > _mate_progress_target_mate:
            _mate_progress_target_mate = best["mate"]
            _mate_progress_hold_moves = 0
            _mate_progress_hold_limit = random.randint(
                MATE_SUSTAIN_MIN_MOVES,
                MATE_SUSTAIN_MAX_MOVES
            )
        elif best["mate"] < _mate_progress_target_mate:
            # Do not follow M10 -> M9 -> M8 immediately. Hold the current
            # target for a few moves first, then improve it by exactly one.
            _mate_progress_hold_moves += 1

            if (
                _mate_progress_hold_moves
                >= _mate_progress_hold_limit
            ):
                _mate_progress_target_mate = max(
                    MATE_GRACE_MIN,
                    _mate_progress_target_mate - 1
                )
                _mate_progress_hold_moves = 0
                _mate_progress_hold_limit = random.randint(
                    MATE_SUSTAIN_MIN_MOVES,
                    MATE_SUSTAIN_MAX_MOVES
                )

        target_mate = int(
            _mate_progress_target_mate
        )

        # Never jump multiple mate steps in a single decision. The active
        # target is the anchor; one slower line is allowed to preserve a
        # natural sustain feel, but it cannot become an accumulating +5 cap.
        allowed_mate_max = target_mate + 1

        mate_candidates = [
            c
            for c in candidates
            if (
                c["mate"] is not None
                and c["mate"] > 0
                and target_mate
                <= c["mate"]
                <= allowed_mate_max
            )
        ]

        # If the engine's current best mate is already faster than the target
        # but no exact target line exists, allow the current best only when
        # the target has caught up to that one-step improvement. Otherwise
        # keep the slower candidate to sustain the plan.
        if not mate_candidates:
            exact_best = [
                c
                for c in candidates
                if (
                    c["mate"] is not None
                    and c["mate"] == best["mate"]
                )
            ]

            if exact_best:
                selected = max(
                    exact_best,
                    key=lambda c: c["rank"]
                )

                return (
                    selected["move"],
                    selected["info"],
                    {
                        "rank": selected["rank"],
                        "current_cp": best_cp,
                        "selected_cp": selected["cp"],
                        "reason": (
                            f"MATE ADAPT | "
                            f"TARGET=M{target_mate} "
                            f"BEST=M{best['mate']} "
                            f"SELECTED=M{selected['mate']} "
                            f"RANK=#"
                            f"{selected['rank'] + 1}"
                        )
                    }
                )

            selected = best

            return (
                selected["move"],
                selected["info"],
                {
                    "rank": selected["rank"],
                    "current_cp": best_cp,
                    "selected_cp": selected["cp"],
                    "reason": (
                        f"MATE BEST | "
                        f"TARGET=M{target_mate} "
                        f"BEST=M{best['mate']} "
                        f"RANK=#"
                        f"{selected['rank'] + 1}"
                    )
                }
            )

        exact_target = [
            c
            for c in mate_candidates
            if c["mate"] == target_mate
        ]

        slower_target = [
            c
            for c in mate_candidates
            if c["mate"] == allowed_mate_max
        ]

        if exact_target:
            # Same mate distance: keep the earlier rule—take the lowest
            # MultiPV line, e.g. #1 M6/#2 M6/#3 M6 -> #3.
            selected = max(
                exact_target,
                key=lambda c: c["rank"]
            )

            # Occasionally sustain with the one-step slower mate when it is
            # available, but never jump several mate moves at once.
            if (
                slower_target
                and random.random() < MATE_SLOWER_LINE_CHANCE
            ):
                selected = max(
                    slower_target,
                    key=lambda c: c["rank"]
                )
        else:
            selected = max(
                slower_target,
                key=lambda c: c["rank"]
            )

        return (
            selected["move"],
            selected["info"],
            {
                "rank": selected["rank"],
                "current_cp": best_cp,
                "selected_cp": selected["cp"],
                "reason": (
                    f"MATE SUSTAIN | "
                    f"TARGET=M{target_mate} "
                    f"BEST=M{best['mate']} "
                    f"SELECTED=M{selected['mate']} "
                    f"HOLD={_mate_progress_hold_moves}/"
                    f"{_mate_progress_hold_limit} "
                    f"RANK=#"
                    f"{selected['rank'] + 1}"
                )
            }
        )

    if best_cp > MIN_POSITIVE_CP:
        # Convert an extra pawn when a queen promotion is genuinely
        # available. Promotion is a stronger conversion priority than
        # wandering/shuffling in a winning position.
        priority_promotion = find_priority_queen_promotion(
            board,
            candidates,
            best_cp
        )

        if priority_promotion is not None:
            return (
                priority_promotion["move"],
                priority_promotion["info"],
                {
                    "rank": priority_promotion["rank"],
                    "current_cp": best_cp,
                    "selected_cp": priority_promotion["cp"],
                    "source": "QUEEN_PROMOTION",
                    "reason": (
                        f"{priority_promotion['reason_tag']} | "
                        f"TARGET={chess.square_name(priority_promotion['move'].to_square)}"
                    )
                }
            )

        # Before normal fuzzy selection, prefer safe captures so "human-like"
        # does not become "wander around while a free piece is hanging".
        priority_capture = find_priority_capture(
            board,
            candidates,
            best_cp,
            engine=engine
        )

        if priority_capture is not None:
            return (
                priority_capture["move"],
                priority_capture["info"],
                {
                    "rank": priority_capture["rank"],
                    "current_cp": best_cp,
                    "selected_cp": priority_capture["cp"],
                    "source": "CAPTURE_PRIORITY",
                    "reason": (
                        f"{priority_capture['reason_tag']} | "
                        f"TARGET={chess.square_name(priority_capture['move'].to_square)} "
                        f"VICTIM={priority_capture['victim_value']/100:.1f} "
                        f"{'FREE' if priority_capture['free'] else 'LIQUIDATION'}"
                    )
                }
            )

        normal_max_drop = safe_drop_fraction(
            best_cp
        )

        max_drop = min(
            normal_max_drop,
            adaptive_max_drop
        )

        floor_cp = max(
            5,
            int(
                best_cp
                * (
                    1.0
                    - max_drop
                )
            )
        )

        global _advantage_progress_target_cp
        global _advantage_progress_hold_moves
        global _advantage_progress_hold_limit
        global _advantage_progress_side

        advantage_mode = False
        advantage_maintain = False
        advantage_growth = False
        advantage_target_cp = None

        if best_cp < HUMAN_ADVANTAGE_START_CP:
            _advantage_progress_target_cp = None
            _advantage_progress_hold_moves = 0
            _advantage_progress_hold_limit = random.randint(
                HUMAN_ADVANTAGE_HOLD_MIN_MOVES,
                HUMAN_ADVANTAGE_HOLD_MAX_MOVES
            )
            _advantage_progress_side = None

        else:
            if _advantage_progress_side != mover:
                _advantage_progress_target_cp = None
                _advantage_progress_hold_moves = 0
                _advantage_progress_hold_limit = random.randint(
                    HUMAN_ADVANTAGE_HOLD_MIN_MOVES,
                    HUMAN_ADVANTAGE_HOLD_MAX_MOVES
                )
                _advantage_progress_side = mover

            if _advantage_progress_target_cp is None:
                _advantage_progress_target_cp = best_cp
                _advantage_progress_hold_moves = 0
                _advantage_progress_hold_limit = random.randint(
                    HUMAN_ADVANTAGE_HOLD_MIN_MOVES,
                    HUMAN_ADVANTAGE_HOLD_MAX_MOVES
                )

            # Do not lower the stored winning target for a small evaluation
            # fluctuation. Only reset it when the engine's best itself has
            # fallen materially below the protected winning level.
            if (
                best_cp
                < _advantage_progress_target_cp
                - HUMAN_ADVANTAGE_PROTECT_BAND_CP
            ):
                _advantage_progress_target_cp = best_cp
                _advantage_progress_hold_moves = 0

            advantage_target_cp = int(
                _advantage_progress_target_cp
            )

            advantage_mode = True

            # Every few actual Stockfish moves, make a progress move. This is
            # independent of whether the evaluation changed on the previous
            # move, so the bot cannot sit on +6.0 for dozens of moves simply
            # because the short engine scores happen to repeat.
            _advantage_progress_hold_moves += 1

            progress_due = (
                _advantage_progress_hold_moves
                >= _advantage_progress_hold_limit
                or
                best_cp
                >= advantage_target_cp
                + HUMAN_ADVANTAGE_GROWTH_TRIGGER_CP
                or
                opponent_pressure
            )

            if progress_due:
                # If the position genuinely improved, advance only a small
                # step toward the new best instead of jumping straight there.
                if best_cp > advantage_target_cp + 10:
                    growth_step = random.randint(
                        HUMAN_ADVANTAGE_GROWTH_STEP_MIN_CP,
                        HUMAN_ADVANTAGE_GROWTH_STEP_MAX_CP
                    )

                    _advantage_progress_target_cp = min(
                        best_cp,
                        advantage_target_cp + growth_step
                    )
                    advantage_target_cp = int(
                        _advantage_progress_target_cp
                    )
                    advantage_growth = True

                else:
                    # Even without a visible CP jump, deliberately use a
                    # stronger move from the current safe top end so the game
                    # keeps developing instead of repeating a passive hold.
                    advantage_growth = True

                _advantage_progress_hold_moves = 0
                _advantage_progress_hold_limit = random.randint(
                    HUMAN_ADVANTAGE_HOLD_MIN_MOVES,
                    HUMAN_ADVANTAGE_HOLD_MAX_MOVES
                )
            else:
                advantage_maintain = True

            # Protect the current winning advantage. +6 should not casually
            # fall toward +4 just because a lower MultiPV move exists.
            floor_cp = max(
                floor_cp,
                int(
                    advantage_target_cp
                    - HUMAN_ADVANTAGE_PROTECT_BAND_CP
                )
            )
        # Keep the original safety floor for winning positions, but in
        # normal/early play allow a small extra human-like evaluation band.
        selection_floor_cp = floor_cp

        winning_guard = (
            best_cp >= HUMAN_WINNING_GUARD_START_CP
        )

        if not advantage_mode:
            selection_floor_cp = max(
                5,
                floor_cp - HUMAN_SELECTION_EXTRA_DROP_CP
            )

        # Once ahead, keep human-like variation inside a tight evaluation
        # band. The old 70cp extra drop could turn a winning position into
        # equality for the sake of rank shuffling.
        if winning_guard:
            winning_drop_floor = max(
                5,
                best_cp - HUMAN_WINNING_GUARD_MAX_DROP_CP
            )
            selection_floor_cp = max(
                selection_floor_cp,
                winning_drop_floor
            )

        winning_rank_cap = (
            HUMAN_STRONG_WINNING_RANK_CAP
            if best_cp >= HUMAN_STRONG_WINNING_CP
            else HUMAN_WINNING_GUARD_RANK_CAP
        )

        effective_rank_cap = (
            min(adaptive_max_rank, winning_rank_cap)
            if winning_guard
            else adaptive_max_rank
        )

        safe = [
            c
            for c in candidates
            if (
                c["cp"] >= selection_floor_cp
                and c["cp"] > 0
                and c["rank"] <= effective_rank_cap
                and (
                    not winning_guard
                    or not candidate_hangs_own_piece(
                        board,
                        c["move"]
                    )
                )
            )
        ]

        if not safe:
            safe = [best]

        if not safe:
            safe = [best]

        non_best = [
            c
            for c in safe
            if c["rank"] > 0
        ]

        non_best_chance = (
            HUMAN_STRONG_WINNING_NON_BEST_CHANCE
            if best_cp >= HUMAN_STRONG_WINNING_CP
            else HUMAN_WINNING_NON_BEST_CHANCE
            if winning_guard
            else HUMAN_SELECTION_NON_BEST_CHANCE
        )

        if (
            len(non_best) >= 2
            and random.random() < non_best_chance
        ):
            pool = non_best
        else:
            pool = safe

        roll = random.random()

        if roll < 0.45:
            target_drop = random.uniform(
                0.00,
                max_drop * 0.35
            )

        elif roll < 0.78:
            target_drop = random.uniform(
                max_drop * 0.35,
                max_drop * 0.70
            )

        elif roll < 0.95:
            target_drop = random.uniform(
                max_drop * 0.70,
                max_drop * 0.90
            )

        else:
            target_drop = random.uniform(
                max_drop * 0.90,
                max_drop
            )

        desired_cp = max(
            floor_cp,
            int(
                best_cp
                * (
                    1.0
                    - target_drop
                )
            )
        )

        if advantage_mode:
            if advantage_maintain:
                desired_cp = min(
                    best_cp,
                    max(
                        floor_cp,
                        int(
                            advantage_target_cp
                            + random.uniform(
                                -HUMAN_ADVANTAGE_MAINTAIN_BAND_CP * 0.20,
                                HUMAN_ADVANTAGE_MAINTAIN_BAND_CP * 0.20
                            )
                        )
                    )
                )
            else:
                growth_preference_cp = random.randint(
                    HUMAN_ADVANTAGE_GROWTH_PREFERENCE_MIN_CP,
                    HUMAN_ADVANTAGE_GROWTH_PREFERENCE_MAX_CP
                )
                desired_cp = min(
                    best_cp,
                    max(
                        floor_cp,
                        int(
                            advantage_target_cp
                            + growth_preference_cp
                        )
                    )
                )

        if advantage_mode:
            if advantage_growth:
                # Progress moves must still feel human-like. Do not collapse
                # the pool to only the top few centipawn lines: that makes
                # advantage growth select #1 repeatedly. Keep every safe
                # MultiPV=15 candidate available and let the fuzzy rank/score
                # weighting decide among them.
                growth_pool = [
                    c
                    for c in safe
                    if c["cp"] >= floor_cp
                ]

                if growth_pool:
                    pool = growth_pool
                else:
                    pool = safe
            else:
                maintain_min = max(
                    floor_cp,
                    int(
                        advantage_target_cp
                        - HUMAN_ADVANTAGE_MAINTAIN_BAND_CP
                    )
                )

                maintain_pool = [
                    c
                    for c in safe
                    if (
                        c["cp"] >= maintain_min
                        and c["cp"] <= best_cp
                    )
                ]

                if maintain_pool:
                    pool = maintain_pool

        if advantage_mode and advantage_growth:
            # Keep #1/#2 available, but spread lower-rank preference instead
            # of making #3 the default. Growth remains protected by the CP
            # preference above.
            rank_factors = {
                0: 0.50,
                1: 0.62,
                2: 0.82,
                3: 1.06,
                4: 1.15,
                5: 1.16,
                6: 1.14,
                7: 1.11,
                8: 1.08,
                9: 1.05,
                10: 1.02,
                11: 1.00,
                12: 0.98,
                13: 0.96,
                14: 0.94,
            }
        else:
            # Spread the lower-rank preference across #3-#8 so #3 does not
            # become the default answer when several safe choices exist.
            rank_factors = {
                0: 0.55,
                1: 0.68,
                2: 0.82,
                3: 1.06,
                4: 1.15,
                5: 1.16,
                6: 1.14,
                7: 1.11,
                8: 1.08,
                9: 1.05,
                10: 1.02,
                11: 1.00,
                12: 0.98,
                13: 0.96,
                14: 0.94,
            }

        lower_rank_pool = [
            candidate
            for candidate in pool
            if candidate["rank"] >= 2
        ]

        # If the normal safety/evaluation band leaves fewer than three
        # candidates, do NOT let the fuzzy draw collapse back to #1/#2.
        # Build a slightly wider lower-rank-only pool using the same engine
        # candidates and the same rank cap. The extra band is small and is
        # used only to create human-like choice diversity.
        diversity_floor_cp = max(
            5,
            int(
                best_cp
                * (
                    1.0
                    - min(
                        0.28,
                        max_drop
                        + HUMAN_SELECTION_DIVERSITY_EXTRA_DROP
                    )
                )
            )
        )

        diversity_lower_pool = [
            candidate
            for candidate in candidates
            if (
                candidate["rank"] >= 2
                and candidate["rank"] <= effective_rank_cap
                and candidate["cp"] >= diversity_floor_cp
                and candidate["cp"] > 0
                and (
                    not winning_guard
                    or not candidate_hangs_own_piece(
                        board,
                        candidate["move"]
                    )
                )
            )
        ]

        if (
            diversity_lower_pool
            and (
                len(pool) <= 1
                or not lower_rank_pool
            )
        ):
            selection_pool = diversity_lower_pool
        else:
            selection_pool = (
                lower_rank_pool
                if (
                    lower_rank_pool
                    and random.random() > HUMAN_SELECTION_TOP12_MAX_CHANCE
                )
                else pool
            )

        weighted = []

        for candidate in selection_pool:
            distance = abs(
                candidate["cp"]
                - desired_cp
            )

            weight = (
                1.0
                / (
                    1.0
                    + distance / (
                        HUMAN_SELECTION_DISTANCE_CP
                        * (
                            1.35
                            if advantage_mode
                            else 1.0
                        )
                    )
                )
            )

            weight *= (
                rank_factors.get(
                    candidate["rank"],
                    0.45
                )
            )

            weighted.append(
                (
                    candidate,
                    max(
                        0.01,
                        weight
                    )
                )
            )

        total = sum(
            weight
            for _, weight
            in weighted
        )

        pick = random.uniform(
            0,
            total
        )

        running = 0.0
        selected = weighted[0][0]

        for candidate, weight in weighted:
            running += weight

            if pick <= running:
                selected = candidate
                break

        return (
            selected["move"],
            selected["info"],
            {
                "rank": selected["rank"],
                "current_cp": best_cp,
                "selected_cp": selected["cp"],
                "reason": (
                    (
                        "advantage growth"
                        if advantage_mode and advantage_growth
                        else "advantage maintain"
                        if advantage_mode
                        else "controlled shuffle"
                    )
                    + " | "
                    + f"BEST={best_cp/100:+.2f} "
                    + f"SELECTED={selected['cp']/100:+.2f} "
                    + f"RANK=#{selected['rank'] + 1} "
                    + f"FLOOR={floor_cp/100:+.2f} "
                    + f"MAX_DROP={max_drop*100:.1f}% "
                    + f"OPP="
                    f"{profile['opponent_accuracy'] if profile['opponent_accuracy'] is not None else 0.0:.1f}% "
                    f"TARGET="
                    f"{profile['target_accuracy']:.1f}% "
                    f"RANKCAP=#"
                    f"{adaptive_max_rank + 1}"
                )
            }
        )

    near_equal_floor = (
        best_cp - 20
    )

    safe = [
        c
        for c in candidates
        if (
            c["cp"] >= near_equal_floor
            and c["rank"] <= adaptive_max_rank
        )
    ]

    if not safe:
        safe = [best]

    rank_weights = {
        0: 0.85,
        1: 0.90,
        2: 0.98,
        3: 1.04,
        4: 1.08,
        5: 1.10,
        6: 1.08,
        7: 1.06,
        8: 1.04,
        9: 1.02,
        10: 1.00,
        11: 0.98,
        12: 0.96,
        13: 0.94,
        14: 0.92,
    }

    lower_rank_safe = [
        candidate
        for candidate in safe
        if candidate["rank"] >= 2
    ]

    selection_safe = (
        lower_rank_safe
        if (
            lower_rank_safe
            and random.random() > HUMAN_SELECTION_TOP12_MAX_CHANCE
        )
        else safe
    )

    weighted = []

    for candidate in selection_safe:
        weighted.append(
            (
                candidate,
                rank_weights.get(
                    candidate["rank"],
                    0.3
                )
            )
        )

    total = sum(
        weight
        for _, weight
        in weighted
    )

    pick = random.uniform(
        0,
        total
    )

    running = 0.0
    selected = weighted[0][0]

    for candidate, weight in weighted:
        running += weight

        if pick <= running:
            selected = candidate
            break

    return (
        selected["move"],
        selected["info"],
        {
            "rank": selected["rank"],
            "current_cp": best_cp,
            "selected_cp": selected["cp"],
            "reason": (
                f"near-equal shuffle | "
                f"BEST="
                f"{best_cp/100:+.2f} "
                f"SELECTED="
                f"{selected['cp']/100:+.2f} "
                f"RANK=#"
                f"{selected['rank'] + 1}"
            )
        }
    )
