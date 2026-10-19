"""Regression tests for the visible desktop cursor controller."""
import unittest

import chess

from visible_cursor_controller import (
    BoardGeometry,
    CursorMotionConfig,
    VisibleCursorController,
    positions_match,
    read_selenium_board_geometry,
)


class FakeMouseBackend:
    def __init__(self):
        self.events = []
        self.last_position = (0, 0)
        self.fail_next_position = False

    def get_position(self):
        return self.last_position

    def set_position(self, x, y):
        if self.fail_next_position:
            self.fail_next_position = False
            return False
        self.last_position = (x, y)
        self.events.append(("position", x, y))
        return True

    def press(self, button):
        self.events.append(("press", button))

    def release(self, button):
        self.events.append(("release", button))

    def release_all(self):
        self.events.append(("release_all",))

    def foreground_title_contains(self, fragment):
        return True


def make_controller():
    backend = FakeMouseBackend()
    motion = CursorMotionConfig(
        approach_duration_s=0.01,
        move_drag_duration_s=0.01,
        arrow_drag_duration_s=0.01,
        settle_after_press_s=0.0,
        settle_after_release_s=0.0,
        min_steps=4,
        max_steps=8,
        curve_pixels=0.0,
    )
    return VisibleCursorController(backend=backend, motion=motion), backend


class BoardGeometryTests(unittest.TestCase):
    def test_white_bottom_orientation(self):
        board = BoardGeometry(100, 200, 800, 800, "white")
        self.assertEqual(board.square_center("a8"), (150, 250))
        self.assertEqual(board.square_center("h1"), (850, 950))
        self.assertEqual(board.square_center("e2"), (550, 850))
        self.assertEqual(board.square_center("e4"), (550, 650))

    def test_black_bottom_orientation_flips_both_axes(self):
        board = BoardGeometry(100, 200, 800, 800, "black")
        self.assertEqual(board.square_center("a8"), (850, 950))
        self.assertEqual(board.square_center("h1"), (150, 250))

    def test_invalid_square_and_non_square_board_are_rejected(self):
        board = BoardGeometry(0, 0, 800, 800)
        with self.assertRaises(ValueError):
            board.square_center("i9")
        with self.assertRaises(ValueError):
            BoardGeometry(0, 0, 800, 400)

    def test_selenium_rect_is_converted_to_screen_pixels(self):
        class FakeDriver:
            def execute_script(self, script, selector):
                self.selector = selector
                return {
                    "left": 120.0,
                    "top": 230.0,
                    "width": 640.0,
                    "height": 640.0,
                    "dpr": 1.0,
                }

        driver = FakeDriver()
        geometry = read_selenium_board_geometry(driver, "my-board", "black")
        self.assertEqual(driver.selector, "my-board")
        self.assertEqual(
            (geometry.left, geometry.top, geometry.width, geometry.height, geometry.bottom_side),
            (120.0, 230.0, 640.0, 640.0, "black"),
        )


class PositionGuardTests(unittest.TestCase):
    def test_position_match_ignores_counters_but_not_turn_or_castling(self):
        start = chess.STARTING_FEN.split()
        changed_counters = list(start)
        changed_counters[4] = "9"
        changed_counters[5] = "8"
        self.assertTrue(positions_match(chess.STARTING_FEN, " ".join(changed_counters)))

        changed_turn = list(start)
        changed_turn[1] = "b"
        self.assertFalse(positions_match(chess.STARTING_FEN, " ".join(changed_turn)))

        changed_castling = list(start)
        changed_castling[2] = "KQ"
        self.assertFalse(positions_match(chess.STARTING_FEN, " ".join(changed_castling)))

    def test_move_is_rejected_if_fen_changed_before_action(self):
        controller, backend = make_controller()
        changed_fen = chess.Board().copy()
        changed_fen.push_uci("e2e4")
        result = controller.move_uci(
            "e2e4",
            expected_fen=chess.STARTING_FEN,
            current_fen_reader=lambda: changed_fen.fen(),
            geometry=BoardGeometry(0, 0, 800, 800),
            bot_color="white",
            focus_guard=lambda: True,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "board-position-changed")
        self.assertEqual(backend.events, [])


class VisibleMoveTests(unittest.TestCase):
    def test_legal_move_uses_left_button_and_releases_mouse(self):
        controller, backend = make_controller()
        fen = chess.STARTING_FEN
        result = controller.move_uci(
            "e2e4",
            expected_fen=fen,
            current_fen_reader=lambda: fen,
            geometry=BoardGeometry(0, 0, 800, 800),
            bot_color="white",
            focus_guard=lambda: True,
        )
        self.assertTrue(result.ok, result.reason)
        self.assertIn(("press", "left"), backend.events)
        self.assertIn(("release", "left"), backend.events)
        self.assertIn(("release_all",), backend.events)
        self.assertNotIn(("press", "right"), backend.events)

    def test_illegal_move_is_rejected_without_mouse_input(self):
        controller, backend = make_controller()
        fen = chess.STARTING_FEN
        result = controller.move_uci(
            "e2e5",
            expected_fen=fen,
            current_fen_reader=lambda: fen,
            geometry=BoardGeometry(0, 0, 800, 800),
            bot_color="white",
            focus_guard=lambda: True,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "illegal-or-invalid-uci")
        self.assertEqual(backend.events, [])

    def test_wrong_bot_turn_is_rejected(self):
        controller, backend = make_controller()
        fen = chess.STARTING_FEN
        result = controller.move_uci(
            "e2e4",
            expected_fen=fen,
            current_fen_reader=lambda: fen,
            geometry=BoardGeometry(0, 0, 800, 800),
            bot_color="black",
            focus_guard=lambda: True,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "not-bot-turn")
        self.assertEqual(backend.events, [])

    def test_premove_requires_site_validation_and_can_run_during_opponents_turn(self):
        controller, backend = make_controller()
        board = chess.Board()
        board.push_uci("e2e4")
        fen = board.fen()

        blocked = controller.move_uci(
            "g1f3",
            expected_fen=fen,
            current_fen_reader=lambda: fen,
            geometry=BoardGeometry(0, 0, 800, 800),
            bot_color="white",
            focus_guard=lambda: True,
            allow_premove=True,
            premove_prevalidated=False,
        )
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.reason, "premove-validation-required")
        self.assertEqual(backend.events, [])

        accepted = controller.move_uci(
            "g1f3",
            expected_fen=fen,
            current_fen_reader=lambda: fen,
            geometry=BoardGeometry(0, 0, 800, 800),
            bot_color="white",
            focus_guard=lambda: True,
            allow_premove=True,
            premove_prevalidated=True,
        )
        self.assertTrue(accepted.ok, accepted.reason)
        self.assertIn(("press", "left"), backend.events)

    def test_foreground_guard_is_required(self):
        controller, backend = make_controller()
        fen = chess.STARTING_FEN
        result = controller.move_uci(
            "e2e4",
            expected_fen=fen,
            current_fen_reader=lambda: fen,
            geometry=BoardGeometry(0, 0, 800, 800),
            bot_color="white",
            focus_guard=lambda: False,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "foreground-window-guard-failed")
        self.assertEqual(backend.events, [])


class VisibleArrowTests(unittest.TestCase):
    def draw(self, controller, fen, **overrides):
        args = {
            "expected_fen": fen,
            "current_fen_reader": lambda: fen,
            "geometry": BoardGeometry(0, 0, 800, 800),
            "game_id": "game-1",
            "focus_guard": lambda: True,
            "auto_draw_enabled": True,
            "opening_fast_mode": False,
            "fast_mode": False,
            "bot_clock_seconds": 60.0,
            "low_clock_trigger_seconds": 9.0,
        }
        args.update(overrides)
        return controller.draw_arrow("d4", "d8", **args)

    def test_native_arrow_uses_right_button_and_route_is_drawn_once_per_game(self):
        controller, backend = make_controller()
        fen = chess.STARTING_FEN
        first = self.draw(controller, fen)
        self.assertTrue(first.ok, first.reason)
        self.assertIn(("press", "right"), backend.events)
        self.assertIn(("release", "right"), backend.events)
        event_count = len(backend.events)

        second = self.draw(controller, fen)
        self.assertFalse(second.ok)
        self.assertEqual(second.reason, "route-already-drawn-this-game")
        self.assertEqual(len(backend.events), event_count)

        third = self.draw(controller, fen, game_id="game-2")
        self.assertTrue(third.ok, third.reason)

    def test_auto_draw_and_fast_modes_suppress_input(self):
        controller, backend = make_controller()
        fen = chess.STARTING_FEN
        for kwargs, expected_reason in (
            ({"auto_draw_enabled": False}, "auto-draw-disabled"),
            ({"opening_fast_mode": True}, "opening-fast-mode"),
            ({"fast_mode": True}, "fast-mode"),
            ({"bot_clock_seconds": 8.5}, "bot-low-clock"),
            ({"bot_clock_seconds": None}, "bot-clock-unavailable"),
        ):
            with self.subTest(expected_reason=expected_reason):
                result = self.draw(controller, fen, **kwargs)
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, expected_reason)
        self.assertEqual(backend.events, [])

    def test_native_highlight_uses_right_click_and_respects_fast_mode(self):
        controller, backend = make_controller()
        fen = chess.STARTING_FEN
        blocked = controller.draw_highlight(
            "e4",
            expected_fen=fen,
            current_fen_reader=lambda: fen,
            geometry=BoardGeometry(0, 0, 800, 800),
            focus_guard=lambda: True,
            auto_draw_enabled=True,
            opening_fast_mode=True,
            fast_mode=False,
            bot_clock_seconds=60.0,
            low_clock_trigger_seconds=9.0,
        )
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.reason, "opening-fast-mode")
        self.assertEqual(backend.events, [])

        result = controller.draw_highlight(
            "e4",
            expected_fen=fen,
            current_fen_reader=lambda: fen,
            geometry=BoardGeometry(0, 0, 800, 800),
            focus_guard=lambda: True,
            auto_draw_enabled=True,
            opening_fast_mode=False,
            fast_mode=False,
            bot_clock_seconds=60.0,
            low_clock_trigger_seconds=9.0,
        )
        self.assertTrue(result.ok, result.reason)
        self.assertIn(("press", "right"), backend.events)
        self.assertIn(("release", "right"), backend.events)

    def test_arrow_rejected_for_stale_fen_or_missing_focus(self):
        controller, backend = make_controller()
        fen = chess.STARTING_FEN
        changed = chess.Board()
        changed.push_uci("e2e4")
        stale = self.draw(
            controller, fen,
            current_fen_reader=lambda: changed.fen(),
        )
        self.assertFalse(stale.ok)
        self.assertEqual(stale.reason, "board-position-changed")
        unfocused = self.draw(controller, fen, focus_guard=lambda: False)
        self.assertFalse(unfocused.ok)
        self.assertEqual(unfocused.reason, "foreground-window-guard-failed")
        self.assertEqual(backend.events, [])


if __name__ == "__main__":
    unittest.main()
