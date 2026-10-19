"""Regression tests for the live tracker safety and history helpers."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import chess
from seleniumbase_live_game_tracker import (
    PREMOVE_LOW_CLOCK_MAX_EVAL_DROP_CP,
    PREMOVE_OPENING_MAX_EVAL_DROP_CP,
    PREMOVE_MAX_EVAL_DROP_CP,
    PREMOVE_OPENING_MIN_BOT_EVAL_CP,
    PREMOVE_OPENING_FULLMOVE_MIN,
    PREMOVE_OPENING_FULLMOVE_MAX,
    JS_INSTALL_OBSERVER,
    PlayerSideResolver,
    MoveAttemptRetryGate,
    normalize_api_bot_color,
    is_low_clock_mode,
    calculate_engine_search_limit,
    _DeadlineBoundEngine,
    _PremoveDeadlineExceeded,
    choose_mate_phase_premove,
    infer_move_between_fens,
    calculate_top_continuation_uci_moves,
    build_move_coaching_plan,
    build_current_position_danger_plan,
    should_render_post_move_coaching,
    auto_draw_suppression_reason,
    filter_new_coaching_arrows,
    _replace_native_coaching_annotations,
    premove_phase_allowed,
    reconstruct_history_from_dom_moves,
    sync_dashboard_history,
    read_analyzer_move_reference,
    format_move_reference_label,
    uci_move_reaches_fen,
)


class PlayerSideResolverTests(unittest.TestCase):
    def test_name_only_side_requires_two_consistent_snapshots(self):
        resolver = PlayerSideResolver(player_name_enabled=True)
        self.assertIsNone(resolver.resolve(None, "black"))
        self.assertEqual(resolver.resolve(None, "black"), "black")

    def test_conflicting_api_and_name_signals_do_not_resolve(self):
        resolver = PlayerSideResolver(player_name_enabled=True)
        self.assertIsNone(resolver.resolve("black", "white"))
        self.assertEqual(resolver.resolve("black", "black"), "black")

    def test_api_side_does_not_flip_after_a_transient_conflicting_snapshot(self):
        resolver = PlayerSideResolver(player_name_enabled=True)
        self.assertEqual(resolver.resolve("black", None), "black")
        self.assertIsNone(resolver.resolve("white", None))
        self.assertIsNone(resolver.resolve("white", None))
        self.assertEqual(resolver.resolve("black", None), "black")

    def test_name_side_does_not_flip_until_new_game_reset(self):
        resolver = PlayerSideResolver(player_name_enabled=True)
        self.assertIsNone(resolver.resolve(None, "black"))
        self.assertEqual(resolver.resolve(None, "black"), "black")
        self.assertIsNone(resolver.resolve(None, "white"))
        self.assertIsNone(resolver.resolve(None, "white"))
        self.assertEqual(resolver.resolve(None, "black"), "black")
        resolver.reset_for_new_game()
        self.assertIsNone(resolver.resolve(None, "white"))
        self.assertEqual(resolver.resolve(None, "white"), "white")

    def test_fixed_color_mismatch_pauses_instead_of_overriding_api(self):
        resolver = PlayerSideResolver(fixed_color="white", player_name_enabled=True)
        self.assertIsNone(resolver.resolve("black", "white"))
        self.assertEqual(resolver.resolve("white", "white"), "white")

    def test_reset_requires_fresh_name_evidence(self):
        resolver = PlayerSideResolver(player_name_enabled=True)
        resolver.resolve(None, "black")
        self.assertEqual(resolver.resolve(None, "black"), "black")
        resolver.reset_for_new_game()
        self.assertIsNone(resolver.resolve(None, "black"))

    def test_api_color_fields_must_agree_when_both_are_present(self):
        self.assertEqual(normalize_api_bot_color("white", "w"), ("white", False))
        self.assertEqual(normalize_api_bot_color("black", "white"), (None, True))
        self.assertEqual(normalize_api_bot_color(None, "black"), ("black", False))


class BoardInputSourceGuardTests(unittest.TestCase):
    def test_unreadable_dom_class_falls_back_to_authoritative_fen_after_side_check(self):
        self.assertIn("sourceColor !== wantedColor", JS_INSTALL_OBSERVER)
        self.assertIn(
            "if (renderedColor && renderedColor !== sourceColor)",
            JS_INSTALL_OBSERVER,
        )
        self.assertIn("unreadable-fen-verified", JS_INSTALL_OBSERVER)


class TopContinuationArrowTests(unittest.TestCase):
    class FakeEngine:
        def __init__(self, pv):
            self.pv = [chess.Move.from_uci(uci) for uci in pv]
            self.called = False

        def analyse(self, board, limit, multipv=1):
            self.called = True
            self.board_fen_at_search = board.fen()
            return {"pv": list(self.pv)}

    def test_draw_line_is_limited_to_the_next_two_pv_moves(self):
        board = chess.Board()
        original_fen = board.fen()
        engine = self.FakeEngine(["e2e4", "e7e5", "g1f3"])

        self.assertEqual(
            calculate_top_continuation_uci_moves(
                engine, board, max_plies=2, search_seconds=0.05
            ),
            ["e2e4", "e7e5"],
        )
        self.assertTrue(engine.called)
        self.assertEqual(board.fen(), original_fen)

    def test_stops_if_stockfish_pv_contains_an_illegal_followup(self):
        board = chess.Board()
        engine = self.FakeEngine(["e2e4", "e2e3"])

        self.assertEqual(
            calculate_top_continuation_uci_moves(engine, board, max_plies=2),
            ["e2e4"],
        )

    def test_never_returns_more_than_two_arrows(self):
        board = chess.Board()
        engine = self.FakeEngine(["e2e4", "e7e5", "g1f3", "b8c6"])

        self.assertEqual(
            calculate_top_continuation_uci_moves(engine, board, max_plies=5),
            ["e2e4", "e7e5"],
        )

    def test_no_analysis_arrows_after_checkmate(self):
        board = chess.Board()
        for uci in ("f2f3", "e7e5", "g2g4", "d8h4"):
            board.push_uci(uci)
        engine = self.FakeEngine([])

        self.assertEqual(
            calculate_top_continuation_uci_moves(engine, board, max_plies=2),
            [],
        )
        self.assertFalse(engine.called)




class MoveCoachingPlanTests(unittest.TestCase):
    class FakeEngine:
        def __init__(self, pv=(), score=None):
            self.pv = [chess.Move.from_uci(uci) for uci in pv]
            self.score = score

        def analyse(self, board, limit, multipv=1):
            result = {"pv": list(self.pv)}
            if self.score is not None:
                result["score"] = self.score
            return result

    def test_quiet_position_does_not_show_generic_opponent_pv(self):
        before = chess.Board()
        after = before.copy(stack=True)
        move = chess.Move.from_uci("e2e4")
        after.push(move)
        engine = self.FakeEngine(["e7e5", "g1f3", "b8c6", "f1b5", "a7a6"])

        plan = build_move_coaching_plan(
            engine, before.fen(), after.fen(), "e2e4", "white",
            max_plies=5, search_seconds=0.08,
        )
        self.assertEqual(plan["mode"], "none")
        self.assertEqual(plan["arrows"], [])
        self.assertIsNone(plan["highlight_square"])

    def test_capture_without_recapture_uses_single_target_square_highlight(self):
        before = chess.Board("r3k3/8/4p3/3B4/8/8/8/4K3 w - - 0 1")
        move = chess.Move.from_uci("d5e6")
        self.assertIn(move, before.legal_moves)
        after = before.copy(stack=True)
        after.push(move)
        engine = self.FakeEngine([])

        plan = build_move_coaching_plan(
            engine, before.fen(), after.fen(), "d5e6", "white"
        )
        self.assertEqual(plan["mode"], "none")
        self.assertIsNone(plan["highlight_square"])
        self.assertEqual(plan["arrows"], [])

    def test_routine_check_does_not_automatically_start_tactical_queue(self):
        before = chess.Board("4k3/8/8/8/8/8/4R3/4K3 w - - 0 1")
        move = chess.Move.from_uci("e2e7")
        self.assertIn(move, before.legal_moves)
        after = before.copy(stack=True)
        after.push(move)
        self.assertTrue(after.is_check())
        engine = self.FakeEngine(["e8f8", "e7e2", "f8g8"])

        plan = build_move_coaching_plan(
            engine, before.fen(), after.fen(), "e2e7", "white",
            max_plies=5, search_seconds=0.08,
        )
        self.assertNotEqual(plan["mode"], "tactical-queue")
        self.assertEqual(plan["tactical_queue"], [])

    def test_forced_mate_queue_is_capped_at_three_plies(self):
        before = chess.Board("4k3/8/8/8/8/8/4R3/4K3 w - - 0 1")
        move = chess.Move.from_uci("e2e7")
        self.assertIn(move, before.legal_moves)
        after = before.copy(stack=True)
        after.push(move)
        self.assertTrue(after.is_check())
        mate_score = chess.engine.PovScore(chess.engine.Mate(3), chess.WHITE)
        engine = self.FakeEngine(
            ["e8f8", "e7e2", "f8g8", "e2e8", "g8h7"],
            score=mate_score,
        )

        plan = build_move_coaching_plan(
            engine, before.fen(), after.fen(), "e2e7", "white",
            max_plies=5, search_seconds=0.08,
        )
        self.assertEqual(plan["mode"], "tactical-queue")
        self.assertLessEqual(len(plan["tactical_queue"]), 3)
        self.assertLessEqual(len(plan["arrows"]), 5)

    def test_valuable_capture_alone_does_not_start_tactical_queue(self):
        before = chess.Board("k7/8/8/3Rn3/8/8/8/4K3 w - - 0 1")
        move = chess.Move.from_uci("d5e5")
        self.assertIn(move, before.legal_moves)
        after = before.copy(stack=True)
        after.push(move)
        self.assertFalse(after.is_check())
        plan = build_move_coaching_plan(
            self.FakeEngine([]), before.fen(), after.fen(), "d5e5", "white"
        )
        self.assertNotEqual(plan["mode"], "tactical-queue")
        self.assertEqual(plan["tactical_queue"], [])

    def test_normal_move_draws_arrow_to_newly_threatened_enemy_piece(self):
        before = chess.Board("3qk3/8/8/8/R7/8/8/4K3 w - - 0 1")
        move = chess.Move.from_uci("a4d4")
        self.assertIn(move, before.legal_moves)
        after = before.copy(stack=True)
        after.push(move)
        engine = self.FakeEngine(["e8f8", "d8c7", "d4d8"])

        plan = build_move_coaching_plan(
            engine, before.fen(), after.fen(), "a4d4", "white"
        )
        self.assertIn(plan["mode"], {"threat-arrows", "tactical-queue"})
        self.assertIn("d4d8", plan["danger_arrows"])
        self.assertLessEqual(len(plan["danger_arrows"]), 2)

    def test_multiple_opponent_danger_pieces_are_prioritized_over_our_risk(self):
        before = chess.Board("k2q4/8/8/8/3R3r/8/8/4K3 w - - 0 1")
        move = chess.Move.from_uci("e1f1")
        self.assertIn(move, before.legal_moves)
        after = before.copy(stack=True)
        after.push(move)
        plan = build_move_coaching_plan(
            self.FakeEngine([]), before.fen(), after.fen(), "e1f1", "white"
        )
        self.assertEqual(plan["mode"], "threat-arrows")
        self.assertGreaterEqual(len(plan["danger_arrows"]), 2)
        self.assertIn("d4d8", plan["danger_arrows"])
        self.assertIn("d4h4", plan["danger_arrows"][:3])
        self.assertLessEqual(len(plan["arrows"]), 4)

    def test_valuable_bot_piece_that_can_be_captured_gets_a_danger_arrow(self):
        before = chess.Board("k3r3/8/8/8/8/8/P3Q3/7K w - - 0 1")
        move = chess.Move.from_uci("a2a3")
        self.assertIn(move, before.legal_moves)
        after = before.copy(stack=True)
        after.push(move)

        plan = build_move_coaching_plan(
            self.FakeEngine([]), before.fen(), after.fen(), "a2a3", "white"
        )
        self.assertEqual(plan["mode"], "threat-arrows")
        self.assertIn("e8e2", plan["danger_arrows"])
        self.assertIn("our queen", plan["reason"])

    def test_normal_move_that_threatens_nothing_shows_no_arrows(self):
        before = chess.Board()
        move = chess.Move.from_uci("e2e4")
        after = before.copy(stack=True)
        after.push(move)
        engine = self.FakeEngine(["e7e5", "g1f3", "b8c6"])

        plan = build_move_coaching_plan(
            engine, before.fen(), after.fen(), "e2e4", "white"
        )
        self.assertEqual(plan["mode"], "none")
        self.assertEqual(plan["arrows"], [])
        self.assertIsNone(plan["highlight_square"])

    def test_invalid_position_transition_shows_nothing(self):
        before = chess.Board()
        after = chess.Board()
        after.push_uci("d2d4")
        plan = build_move_coaching_plan(
            None, before.fen(), after.fen(), "e2e4", "white"
        )
        self.assertEqual(plan["mode"], "none")
        self.assertEqual(plan["arrows"], [])




class NativeAnnotationRedrawTests(unittest.TestCase):
    def test_previous_arrow_is_not_toggled_after_board_move_clears_it(self):
        fen = chess.Board().fen()

        class FakeDriver:
            def execute_script(self, script, *_args):
                if "current?.()?.fen" in script:
                    return fen
                return None

        with (
            patch("seleniumbase_live_game_tracker._direct_cdp_page_for_driver", return_value=object()),
            patch(
                "seleniumbase_live_game_tracker._read_board_annotation_geometry",
                return_value={
                    "left": 0.0, "top": 0.0, "width": 800.0, "height": 800.0,
                    "bottom_side": "white", "fen": chess.STARTING_FEN,
                    "screen_left": 100.0, "screen_top": 200.0,
                    "screen_width": 800.0, "screen_height": 800.0,
                    "window_title": "Owned Chess Site - Browser",
                },
            ),
            patch("seleniumbase_live_game_tracker._same_chess_position", return_value=True),
            patch("seleniumbase_live_game_tracker._toggle_native_annotation_arrow") as draw_arrow,
            patch("seleniumbase_live_game_tracker._toggle_native_annotation_square") as draw_square,
        ):
            arrows, highlight = _replace_native_coaching_annotations(
                FakeDriver(), "board",
                previous_arrows=["d4d8"],
                previous_highlight="e4",
                next_arrows=[],
                next_highlight=None,
                expected_fen=fen,
            )

        # The board already cleared prior annotations on move; clicking the old
        # route would recreate it. So old arrows/highlights must never be toggled.
        draw_arrow.assert_not_called()
        draw_square.assert_not_called()
        self.assertEqual(arrows, [])
        self.assertIsNone(highlight)



class AutoDrawFastModeTests(unittest.TestCase):
    @staticmethod
    def fen_at_fullmove(fullmove_number):
        fields = chess.STARTING_FEN.split()
        fields[5] = str(fullmove_number)
        return " ".join(fields)

    def test_dashboard_switch_off_disables_drawing(self):
        fen = self.fen_at_fullmove(15)
        self.assertEqual(
            auto_draw_suppression_reason(
                False, "white", fen, fen, 60.0, 60.0, 9.0, False, 5
            ),
            "Auto-Draw switch is OFF",
        )

    def test_short_time_control_fast_mode_disables_drawing(self):
        fen = self.fen_at_fullmove(15)
        self.assertEqual(
            auto_draw_suppression_reason(
                True, "white", fen, fen, 60.0, 60.0, 9.0, True, 5
            ),
            "short-time-control fast mode is active",
        )

    def test_opening_window_disables_drawing_across_black_move_counter_increment(self):
        previous_fen = self.fen_at_fullmove(10)
        current_fen = self.fen_at_fullmove(11)
        self.assertEqual(
            auto_draw_suppression_reason(
                True, "black", current_fen, previous_fen, 60.0, 60.0, 9.0, False, 10
            ),
            "opening fast/premove window is active",
        )

    def test_our_low_clock_disables_drawing_but_opponents_low_clock_does_not(self):
        fen = self.fen_at_fullmove(15)
        own_clock_reason = auto_draw_suppression_reason(
            True, "white", fen, fen, 8.5, 45.0, 9.0, False, 5
        )
        self.assertEqual(own_clock_reason, "bot low-clock fast mode is active (8.50s)")
        opponent_clock_reason = auto_draw_suppression_reason(
            True, "white", fen, fen, 45.0, 8.5, 9.0, False, 5
        )
        self.assertEqual(opponent_clock_reason, "")

    def test_normal_mode_allows_auto_draw_when_bot_has_clock_reserve(self):
        fen = self.fen_at_fullmove(15)
        self.assertEqual(
            auto_draw_suppression_reason(
                True, "white", fen, fen, 45.0, 8.5, 9.0, False, 5
            ),
            "",
        )


class VisibleCursorAnnotationIntegrationTests(unittest.TestCase):
    def test_visible_mode_uses_desktop_cursor_for_arrows_and_highlights(self):
        fen = chess.STARTING_FEN
        from seleniumbase_live_game_tracker import _replace_native_coaching_annotations

        class FakeDriver:
            title = "Owned Chess Site - Browser"

            def execute_script(self, script):
                if "__liveGameApiReader" in script:
                    return fen
                return None

        class FakeBackend:
            def foreground_title_contains(self, fragment):
                return fragment == "Owned Chess Site - Browser"

        controller = SimpleNamespace(
            backend=FakeBackend(),
            draw_arrow=Mock(return_value=SimpleNamespace(ok=True)),
            draw_highlight=Mock(return_value=SimpleNamespace(ok=True)),
            preview_legal_moves=Mock(return_value=SimpleNamespace(ok=True, duration_s=0.005, waypoints=4)),
        )

        with (
            patch("seleniumbase_live_game_tracker._direct_cdp_page_for_driver") as cdp,
            patch(
                "seleniumbase_live_game_tracker._read_board_annotation_geometry",
                return_value={
                    "left": 0.0, "top": 0.0, "width": 800.0, "height": 800.0,
                    "bottom_side": "white", "fen": chess.STARTING_FEN,
                    "screen_left": 100.0, "screen_top": 200.0,
                    "screen_width": 800.0, "screen_height": 800.0,
                    "window_title": "Owned Chess Site - Browser",
                },
            ),
            patch(
                "seleniumbase_live_game_tracker._get_visible_cursor_controller",
                return_value=controller,
            ),
            patch(
                "seleniumbase_live_game_tracker.read_selenium_board_geometry",
                return_value=object(),
            ) as fallback_geometry,
            patch("seleniumbase_live_game_tracker._same_chess_position", return_value=True),
            patch("seleniumbase_live_game_tracker.time.sleep") as gap_sleep,
            patch("seleniumbase_live_game_tracker.random.uniform", return_value=0.015) as random_gap,
        ):
            arrows, highlight = _replace_native_coaching_annotations(
                FakeDriver(), "board", [], None, ["d4d8", "a4a8"], "e4", fen,
                use_visible_cursor=True,
                game_id=7,
                auto_draw_enabled=True,
                bot_color="white",
                white_clock_seconds=60.0,
                black_clock_seconds=40.0,
                low_clock_trigger_seconds=9.0,
                opening_fast_mode=False,
                fast_mode=False,
            )

        cdp.assert_called_once()
        cdp.return_value.evaluate.assert_any_call(
            "(() => { if (window.__variationLineAnalyzer) window.__variationLineAnalyzer.suppress = true; return true; })()"
        )
        self.assertEqual(arrows, ["d4d8", "a4a8"])
        self.assertEqual(highlight, "e4")
        self.assertEqual(controller.draw_arrow.call_count, 2)
        controller.draw_highlight.assert_called_once()
        self.assertEqual(controller.preview_legal_moves.call_count, 3)
        self.assertEqual(
            [round(item.args[0], 3) for item in gap_sleep.call_args_list],
            [0.015, 0.010, 0.010],
        )
        self.assertEqual(random_gap.call_count, 3)
        for call_item in random_gap.call_args_list:
            self.assertEqual(call_item.args, (0.010, 0.030))
        fallback_geometry.assert_not_called()


class PostMoveCoachingGateTests(unittest.TestCase):
    def test_coaching_is_allowed_after_the_bot_completes_its_move(self):
        self.assertTrue(
            should_render_post_move_coaching(
                True, True, "white", "white", "position-after", "position-before"
            )
        )

    def test_opponent_move_never_triggers_coaching_or_draw_input(self):
        self.assertFalse(
            should_render_post_move_coaching(
                True, True, "white", "black", "position-after", "position-before"
            )
        )

    def test_coaching_is_skipped_if_autoplay_engine_or_prior_position_is_missing(self):
        cases = [
            (False, True, "white", "white", "after", "before"),
            (True, False, "white", "white", "after", "before"),
            (True, True, "white", "white", "after", ""),
            (True, True, "white", "white", "", "before"),
        ]
        for args in cases:
            with self.subTest(args=args):
                self.assertFalse(should_render_post_move_coaching(*args))


class CoachingArrowDeduplicationTests(unittest.TestCase):
    def test_same_arrow_is_not_repeated_after_opponent_reply_while_threat_persists(self):
        shown_arrows = set()

        first_plan = filter_new_coaching_arrows(["d4d8"], shown_arrows)
        self.assertEqual(first_plan, ["d4d8"])
        # Production records an arrow only after the board renderer drew it.
        shown_arrows.update(first_plan)

        second_plan = filter_new_coaching_arrows(["d4d8"], shown_arrows)
        self.assertEqual(second_plan, [])
        self.assertIn("d4d8", shown_arrows)

    def test_new_unseen_arrows_are_allowed_but_old_arrows_remain_suppressed(self):
        shown_arrows = set()
        first_plan = filter_new_coaching_arrows(["d4d8"], shown_arrows)
        shown_arrows.update(first_plan)
        self.assertEqual(first_plan, ["d4d8"])

        new_plan = filter_new_coaching_arrows(["d4d8", "a4a8", "a4a8"], shown_arrows)
        self.assertEqual(new_plan, ["a4a8"])
        shown_arrows.update(new_plan)
        self.assertEqual(shown_arrows, {"d4d8", "a4a8"})

    def test_same_arrow_stays_suppressed_even_after_absent_from_multiple_plans(self):
        shown_arrows = set()
        first_plan = filter_new_coaching_arrows(["d4d8"], shown_arrows)
        shown_arrows.update(first_plan)
        self.assertEqual(first_plan, ["d4d8"])

        self.assertEqual(filter_new_coaching_arrows([], shown_arrows), [])
        self.assertEqual(filter_new_coaching_arrows(["a4a8"], shown_arrows), ["a4a8"])
        self.assertEqual(filter_new_coaching_arrows(["d4d8"], shown_arrows), [])
        self.assertIn("d4d8", shown_arrows)


class CurrentPositionDangerPlanTests(unittest.TestCase):
    class FakeEngine:
        def __init__(self):
            self.calls = []

        def analyse(self, board, limit, multipv=1):
            self.calls.append(board.fen())
            return {"pv": []}

    def test_opponent_reply_gets_reanalysed_from_the_new_fen(self):
        engine = self.FakeEngine()
        attacked_queen = chess.Board("3qk3/8/8/8/3R4/8/8/4K3 w - - 0 1")
        moved_queen = chess.Board("4k3/2q5/8/8/3R4/8/8/4K3 w - - 0 1")

        first_plan = build_current_position_danger_plan(
            attacked_queen.fen(), "white", engine=engine
        )
        second_plan = build_current_position_danger_plan(
            moved_queen.fen(), "white", engine=engine
        )

        self.assertIn("d4d8", first_plan["danger_arrows"])
        self.assertNotIn("d4d8", second_plan["danger_arrows"])
        self.assertEqual(len(engine.calls), 2)
        self.assertEqual(engine.calls[0], attacked_queen.fen())
        self.assertEqual(engine.calls[1], moved_queen.fen())

    def test_current_position_plan_prioritizes_multiple_opponent_pieces(self):
        board = chess.Board("3qk3/8/8/8/3R2r1/8/8/4K3 w - - 0 1")
        plan = build_current_position_danger_plan(
            board.fen(), "white", engine=self.FakeEngine()
        )
        self.assertEqual(plan["mode"], "threat-arrows")
        self.assertIn("d4d8", plan["danger_arrows"])
        self.assertLessEqual(len(plan["danger_arrows"]), 4)
        self.assertEqual(plan["tactical_queue"], [])



class MoveRetryGateTests(unittest.TestCase):
    def test_retry_same_fen_only_after_backoff_and_with_bounded_attempts(self):
        gate = MoveAttemptRetryGate()
        self.assertTrue(gate.should_attempt("fen-A", now=0.0))
        self.assertTrue(gate.begin("fen-A", now=0.0))
        gate.finish({"ok": False, "reason": "temporary-submit-failure"})
        self.assertFalse(gate.should_attempt("fen-A", now=0.19))
        self.assertTrue(gate.should_attempt("fen-A", now=0.20))
        self.assertTrue(gate.begin("fen-A", now=0.20))
        gate.finish({"ok": False, "reason": "temporary-submit-failure"})
        self.assertFalse(gate.should_attempt("fen-A", now=0.69))
        self.assertTrue(gate.should_attempt("fen-A", now=0.71))
        self.assertTrue(gate.begin("fen-A", now=0.71))
        gate.finish({"ok": False, "reason": "temporary-submit-failure"})
        self.assertFalse(gate.should_attempt("fen-A", now=10.0))

    def test_accepted_but_unconfirmed_move_is_not_blindly_resubmitted(self):
        gate = MoveAttemptRetryGate()
        self.assertTrue(gate.begin("fen-A", now=0.0))
        gate.finish({"ok": False, "api_accepted": True, "reason": "api-move-not-confirmed"})
        self.assertFalse(gate.should_attempt("fen-A", now=100.0))

    def test_confirmed_success_does_not_retry_same_fen(self):
        gate = MoveAttemptRetryGate()
        self.assertTrue(gate.begin("fen-A", now=0.0))
        gate.finish({"ok": True, "confirmed": True})
        self.assertFalse(gate.should_attempt("fen-A", now=100.0))

    def test_new_fen_resets_retry_budget(self):
        gate = MoveAttemptRetryGate()
        self.assertTrue(gate.begin("fen-A", now=0.0))
        gate.finish({"ok": False})
        self.assertTrue(gate.begin("fen-B", now=0.1))
        self.assertEqual(gate.attempts, 1)


class LateClockAndPremoveSafetyTests(unittest.TestCase):
    def test_clock_mode_uses_the_same_sampled_boundary_everywhere(self):
        self.assertFalse(is_low_clock_mode(8.5, 7.5))
        self.assertTrue(is_low_clock_mode(7.5, 7.5))
        self.assertTrue(is_low_clock_mode(9.1, 9.79))
        self.assertFalse(is_low_clock_mode(10.1, 9.79))
        self.assertTrue(is_low_clock_mode(8.5, 7.5, force_fast_mode=True))
        self.assertFalse(is_low_clock_mode(None, 7.5, force_fast_mode=True))

    def test_missing_clock_uses_conservative_engine_budget(self):
        limit = calculate_engine_search_limit(
            base_seconds=0.75, remaining_seconds=None, initial_seconds=None,
            increment_seconds=0.0, board=chess.Board(),
        )
        self.assertEqual(limit, 0.05)

    def test_engine_search_uses_sampled_clock_boundary_not_fixed_ten(self):
        board = chess.Board()
        common = dict(
            base_seconds=5.0, remaining_seconds=8.5, initial_seconds=60.0,
            increment_seconds=0.0, board=board,
        )
        with patch("seleniumbase_live_game_tracker.random.uniform", return_value=0.28):
            normal_path = calculate_engine_search_limit(
                **common, low_clock_trigger_seconds=7.5
            )
            low_clock_path = calculate_engine_search_limit(
                **common, low_clock_trigger_seconds=9.0
            )
            forced_short_control = calculate_engine_search_limit(
                **{**common, "remaining_seconds": 11.0},
                low_clock_trigger_seconds=7.5, force_fast_mode=True,
            )
        self.assertGreater(normal_path, 0.035)
        self.assertLessEqual(low_clock_path, 0.035)
        self.assertLessEqual(forced_short_control, 0.035)

    def test_engine_search_is_capped_at_35_ms_with_7_to_10_seconds_left(self):
        board = chess.Board()
        for remaining in (7.0, 8.0, 9.0, 10.0):
            with self.subTest(remaining=remaining):
                limit = calculate_engine_search_limit(
                    base_seconds=5.0,
                    remaining_seconds=remaining,
                    initial_seconds=60.0,
                    increment_seconds=0.0,
                    board=board,
                )
                self.assertGreater(limit, 0.0)
                self.assertLessEqual(limit, 0.035)
                self.assertAlmostEqual(limit, min(0.035, remaining * 0.004))

    def test_opening_engine_search_is_bounded_in_one_minute_controls(self):
        limit = calculate_engine_search_limit(
            base_seconds=0.75,
            remaining_seconds=55.0,
            initial_seconds=60.0,
            increment_seconds=0.0,
            board=chess.Board(),
        )
        self.assertGreater(limit, 0.0)
        self.assertLessEqual(limit, 0.18)

    def test_opening_engine_search_has_a_slightly_larger_three_minute_cap(self):
        limit = calculate_engine_search_limit(
            base_seconds=0.75,
            remaining_seconds=170.0,
            initial_seconds=180.0,
            increment_seconds=0.0,
            board=chess.Board(),
        )
        self.assertGreater(limit, 0.0)
        self.assertLessEqual(limit, 0.30)

    def test_relative_clock_deficit_tightens_normal_search_budget(self):
        board = chess.Board()
        common = dict(
            base_seconds=2.0,
            remaining_seconds=45.0,
            initial_seconds=120.0,
            increment_seconds=0.0,
            board=board,
        )
        with patch("seleniumbase_live_game_tracker.random.uniform", return_value=0.10):
            behind = calculate_engine_search_limit(**common, opponent_seconds=60.0)
        with patch("seleniumbase_live_game_tracker.random.uniform", return_value=0.28):
            level = calculate_engine_search_limit(**common, opponent_seconds=45.0)
        self.assertLess(behind, level)

    def test_premove_deadline_reserves_cleanup_window_before_engine_call(self):
        class FakeEngine:
            called = False
            def analyse(self, *_args, **_kwargs):
                self.called = True
                return {}

        engine = FakeEngine()
        proxy = _DeadlineBoundEngine(engine, time.monotonic() + 0.005)
        with self.assertRaises(_PremoveDeadlineExceeded):
            proxy.analyse(chess.Board(), chess.engine.Limit(time=0.1, depth=8))
        self.assertFalse(engine.called)

    def test_expired_premove_deadline_never_calls_engine(self):
        class FakeEngine:
            called = False
            def analyse(self, *_args, **_kwargs):
                self.called = True
                return {}

        engine = FakeEngine()
        proxy = _DeadlineBoundEngine(engine, time.monotonic() - 1.0)
        with self.assertRaises(_PremoveDeadlineExceeded):
            proxy.analyse(chess.Board(), chess.engine.Limit(time=0.1, depth=8))
        self.assertFalse(engine.called)

    def test_premove_evaluation_guards_reject_large_drops(self):
        self.assertEqual(PREMOVE_LOW_CLOCK_MAX_EVAL_DROP_CP, 50)
        self.assertEqual(PREMOVE_MAX_EVAL_DROP_CP, 50)
        self.assertEqual(PREMOVE_OPENING_MAX_EVAL_DROP_CP, 60)
        self.assertEqual(PREMOVE_OPENING_MIN_BOT_EVAL_CP, -50)


class PremovePhaseTests(unittest.TestCase):
    def test_opening_premove_window_is_sampled_between_five_and_ten_fullmoves(self):
        self.assertEqual(PREMOVE_OPENING_FULLMOVE_MIN, 5)
        self.assertEqual(PREMOVE_OPENING_FULLMOVE_MAX, 10)
        for opening_limit in range(5, 11):
            with self.subTest(opening_limit=opening_limit):
                inside = chess.Board(
                    f"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 {opening_limit}"
                )
                outside = chess.Board(
                    f"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 {opening_limit + 1}"
                )
                self.assertEqual(
                    premove_phase_allowed(inside, opening_limit), (True, "opening")
                )
                self.assertEqual(
                    premove_phase_allowed(outside, opening_limit),
                    (False, "outside-opening-endgame"),
                )

    def test_mate_phase_premove_uses_forced_mate_continuation(self):
        board = chess.Board()
        board.push_san("e4")
        reply = chess.Move.from_uci("e7e5")
        mate_move = chess.Move.from_uci("d1h5")

        class FakeEngine:
            def __init__(self):
                self.calls = 0

            def analyse(self, _board, _limit, multipv=1, root_moves=None):
                self.calls += 1
                if self.calls == 1:
                    return [{
                        "pv": [reply],
                        "score": chess.engine.PovScore(
                            chess.engine.Mate(5), chess.WHITE
                        ),
                    }]
                return [{
                    "pv": [mate_move],
                    "score": chess.engine.PovScore(
                        chess.engine.Mate(3), chess.WHITE
                    ),
                }]

        class Args:
            engine_time = 0.02
            move_input = "api"

        selected, predicted_reply, reason = choose_mate_phase_premove(
            FakeEngine(), board, "white", Args()
        )
        self.assertEqual(predicted_reply, reply)
        self.assertEqual(selected, mate_move)
        self.assertIn("mate-phase-premove", reason)


class MoveConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.before = chess.Board()
        self.before_fen = self.before.fen()

    def test_confirms_selected_move(self):
        after = self.before.copy()
        after.push_uci("e2e4")
        self.assertTrue(uci_move_reaches_fen(self.before_fen, after.fen(), "e2e4"))

    def test_reconstructs_double_pawn_push_when_fen_omits_inert_ep_square(self):
        after = self.before.copy()
        after.push_uci("e2e4")
        self.assertEqual(infer_move_between_fens(self.before_fen, after.fen())["san"], "e4")

    def test_confirms_move_after_fast_opponent_reply(self):
        after = self.before.copy()
        after.push_uci("e2e4")
        after.push_uci("e7e5")
        self.assertTrue(uci_move_reaches_fen(self.before_fen, after.fen(), "e2e4"))

    def test_does_not_confirm_unrelated_position(self):
        after = self.before.copy()
        after.push_uci("d2d4")
        self.assertFalse(uci_move_reaches_fen(self.before_fen, after.fen(), "e2e4"))


class DomHistoryRecoveryTests(unittest.TestCase):
    def test_rebuilds_only_when_full_san_list_matches_fen(self):
        board = chess.Board()
        sans = ["e4", "e5", "Nf3", "Nc6", "Bb5"]
        dom_moves = []
        for index, san in enumerate(sans, start=1):
            move = board.parse_san(san)
            dom_moves.append({"ply": str(index), "text": san})
            board.push(move)

        history = reconstruct_history_from_dom_moves(dom_moves, board.fen())
        self.assertIsNotNone(history)
        self.assertEqual(len(history), len(sans))
        self.assertEqual(history[-1]["san"], "Bb5")
        self.assertEqual(history[-1]["game_ply"], str(len(sans)))
        self.assertIsNone(reconstruct_history_from_dom_moves(dom_moves[:-1], board.fen()))



class DashboardMoveReferenceTests(unittest.TestCase):
    def test_reads_move_reference_and_formats_source_rank(self):
        reference_payload = {
            "autoplay_enabled": False,
            "move_reference": {
                "event_id": "test-event-1",
                "fen": chess.Board().fen(),
                "uci": "e2e4",
                "san": "e4",
                "ply": 1,
                "side": "White",
                "rank": 1,
                "source": "CHESSKIDA",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mode.json"
            path.write_text(json.dumps(reference_payload), encoding="utf-8")
            reference = read_analyzer_move_reference(path)

        self.assertIsNotNone(reference)
        self.assertEqual(reference["uci"], "e2e4")
        self.assertEqual(format_move_reference_label(reference), "CHESSKIDA #2")

    def test_book_reference_uses_book_label(self):
        self.assertEqual(
            format_move_reference_label({"source": "GM_BOOK", "rank": 0}),
            "BOOK #1",
        )

    def test_rejects_invalid_or_missing_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mode.json"
            path.write_text(json.dumps({"autoplay_enabled": False}), encoding="utf-8")
            self.assertIsNone(read_analyzer_move_reference(path))
            path.write_text(
                json.dumps({
                    "move_reference": {
                        "event_id": "bad-event",
                        "fen": chess.Board().fen(),
                        "uci": "not-a-uci",
                    }
                }),
                encoding="utf-8",
            )
            self.assertIsNone(read_analyzer_move_reference(path))


class DashboardHistorySyncTests(unittest.TestCase):
    class FakeDashboard:
        def __init__(self):
            self.rows = {}

        def record_observed_move(self, game_number, ply, move, eval_white_cp=None):
            self.rows[(game_number, ply)] = {
                "san": move.get("san"),
                "uci": move.get("uci"),
                "side": move.get("side"),
            }

    def test_syncs_every_observed_ply_without_requiring_bot_mode(self):
        dashboard = self.FakeDashboard()
        history = [
            {"game_ply": "1", "san": "e4", "uci": "e2e4", "side": "white"},
            {"game_ply": "2", "san": "e5", "uci": "e7e5", "side": "black"},
            {"game_ply": "3", "san": "Nf3", "uci": "g1f3", "side": "white"},
        ]

        synced = sync_dashboard_history(dashboard, 2, history)

        self.assertEqual(synced, 3)
        self.assertEqual(len(dashboard.rows), 3)
        self.assertEqual(dashboard.rows[(2, 1)]["san"], "e4")
        self.assertEqual(dashboard.rows[(2, 2)]["san"], "e5")
        self.assertEqual(dashboard.rows[(2, 3)]["san"], "Nf3")

    def test_uses_history_index_when_ply_metadata_is_missing(self):
        dashboard = self.FakeDashboard()
        history = [
            {"san": "d4", "uci": "d2d4", "side": "white"},
            {"san": "d5", "uci": "d7d5", "side": "black"},
        ]

        self.assertEqual(sync_dashboard_history(dashboard, 1, history), 2)
        self.assertIn((1, 1), dashboard.rows)
        self.assertIn((1, 2), dashboard.rows)

    def test_empty_history_is_safe(self):
        self.assertEqual(sync_dashboard_history(self.FakeDashboard(), 1, []), 0)


if __name__ == "__main__":
    unittest.main()
