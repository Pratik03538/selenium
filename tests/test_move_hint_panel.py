"""Unit tests for legal move and red-premove hint descriptions."""
import unittest

import chess

from move_hint_panel import (
    build_hint, build_premove_hint, build_coach_variations,
    classify_variation_risk, square_to_display, toggle_output_mode,
    toggle_hint_visibility,
)


class TacticalCoachVariationTests(unittest.TestCase):
    def test_multipv_lines_become_legal_step_by_step_variations(self):
        board = chess.Board()
        variations = build_coach_variations(board.fen(), [
            {"pv": [
                chess.Move.from_uci("e2e4"),
                chess.Move.from_uci("e7e5"),
                chess.Move.from_uci("g1f3"),
            ]},
            {"pv": [
                chess.Move.from_uci("d2d4"),
                chess.Move.from_uci("d7d5"),
                chess.Move.from_uci("c2c4"),
            ]},
        ])
        self.assertEqual(len(variations), 2)
        self.assertEqual(variations[0]["sequence"], "e4 → e5 → Nf3")
        self.assertEqual(variations[1]["sequence"], "d4 → d5 → c4")
        self.assertIn("central space", variations[0]["moves"][0]["explanation"])
        self.assertTrue(variations[0]["explanation"])
        self.assertEqual(variations[0]["moves"][0]["side"], "white")
        self.assertEqual(variations[0]["moves"][1]["side"], "black")
        self.assertEqual(variations[0]["moves"][2]["uci"], "g1f3")

    def test_top_three_opponent_lines_are_capped_at_three(self):
        board = chess.Board()
        infos = [
            {"pv": [chess.Move.from_uci(move)]}
            for move in ("e2e4", "d2d4", "c2c4", "g1f3")
        ]
        variations = build_coach_variations(board.fen(), infos, max_lines=3)
        self.assertEqual(len(variations), 3)
        self.assertEqual(
            [line["moves"][0]["uci"] for line in variations],
            ["e2e4", "d2d4", "c2c4"],
        )

    def test_each_opponent_option_shows_only_its_immediate_reply(self):
        board = chess.Board()
        infos = [
            {"pv": [
                chess.Move.from_uci("e2e4"),
                chess.Move.from_uci("e7e5"),
                chess.Move.from_uci("g1f3"),
            ]},
            {"pv": [
                chess.Move.from_uci("d2d4"),
                chess.Move.from_uci("d7d5"),
                chess.Move.from_uci("c2c4"),
            ]},
            {"pv": [
                chess.Move.from_uci("c2c4"),
                chess.Move.from_uci("e7e5"),
                chess.Move.from_uci("g1f3"),
            ]},
        ]
        variations = build_coach_variations(
            board.fen(), infos, max_lines=3, max_plies=2,
        )
        self.assertEqual(len(variations), 3)
        self.assertEqual(
            [line["sequence"] for line in variations],
            ["e4 → e5", "d4 → d5", "c4 → e5"],
        )
        self.assertTrue(all(len(line["moves"]) == 2 for line in variations))

    def test_piece_moving_out_of_attack_is_explained(self):
        board = chess.Board("r6k/8/8/8/8/8/8/R3K3 w - - 0 1")
        variations = build_coach_variations(
            board.fen(), [{"pv": [chess.Move.from_uci("a1b1")]}],
            max_lines=1, max_plies=1,
        )
        self.assertEqual(len(variations), 1)
        self.assertIn(
            "out of attack", variations[0]["moves"][0]["explanation"]
        )

    def test_bad_candidate_is_flagged_when_eval_drops_by_threshold(self):
        lines = [
            {"score_cp": 35, "mate_distance": None, "moves": []},
            {"score_cp": -5, "mate_distance": None, "moves": []},
            {"score_cp": -210, "mate_distance": None, "moves": []},
        ]
        best, bad, best_cp = classify_variation_risk(lines, threshold_cp=150)
        self.assertIs(best, lines[0])
        self.assertEqual(best_cp, 35)
        self.assertEqual(bad["score_cp"], -210)
        self.assertEqual(bad["drop_cp"], 245)

    def test_forced_mate_against_side_to_move_is_marked_dangerous(self):
        lines = [
            {"score_cp": 25, "mate_distance": None, "moves": []},
            {"score_cp": -99997, "mate_distance": -3, "moves": []},
        ]
        _best, bad, _best_cp = classify_variation_risk(lines)
        self.assertIsNotNone(bad)
        self.assertEqual(bad["mate_distance"], -3)

    def test_capture_is_explained_in_coaching_steps(self):
        board = chess.Board()
        board.push_uci("e2e4")
        board.push_uci("d7d5")
        variations = build_coach_variations(board.fen(), [{
            "pv": [chess.Move.from_uci("e4d5")]
        }])
        self.assertEqual(len(variations), 1)
        self.assertIn("captures pawn", variations[0]["moves"][0]["notes"])

    def test_empty_or_missing_engine_lines_returns_no_variations(self):
        board = chess.Board()
        self.assertEqual(build_coach_variations(board.fen(), []), [])
        self.assertEqual(build_coach_variations(board.fen(), [{"pv": []}]), [])

    def test_illegal_move_truncates_a_principal_variation(self):
        board = chess.Board()
        variations = build_coach_variations(board.fen(), [{
            "pv": [
                chess.Move.from_uci("e2e4"),
                chess.Move.from_uci("e2e5"),
                chess.Move.from_uci("e7e5"),
            ]
        }])
        self.assertEqual(len(variations), 1)
        self.assertEqual(variations[0]["sequence"], "e4")

class HintVisibilityToggleTests(unittest.TestCase):
    def test_enabled_overlay_switches_off(self):
        self.assertFalse(toggle_hint_visibility(True))

    def test_disabled_overlay_switches_on(self):
        self.assertTrue(toggle_hint_visibility(False))


class OutputModeToggleTests(unittest.TestCase):
    def test_hint_mode_switches_to_existing_autoplay_path(self):
        self.assertEqual(toggle_output_mode("hint"), "execute")

    def test_autoplay_mode_switches_back_to_hint(self):
        self.assertEqual(toggle_output_mode("execute"), "hint")

    def test_unknown_mode_fails_safe_to_hint(self):
        self.assertEqual(toggle_output_mode("unexpected"), "hint")


class MoveHintDescriptionTests(unittest.TestCase):
    def test_regular_suggestion_is_validated_against_current_position(self):
        board = chess.Board()
        hint = build_hint(board.fen(), "e2e4")
        self.assertEqual(hint["uci"], "e2e4")
        self.assertEqual(hint["san"], "e4")

    def test_regular_illegal_move_is_rejected(self):
        board = chess.Board()
        with self.assertRaises(ValueError):
            build_hint(board.fen(), "e2e5")

    def test_premove_is_validated_after_predicted_opponent_reply(self):
        board = chess.Board()
        hint = build_premove_hint(board.fen(), "g8f6", "e2e4")
        self.assertEqual(hint["predicted_reply_san"], "e4")
        self.assertEqual(hint["san"], "Nf6")
        self.assertEqual(hint["uci"], "g8f6")

    def test_premove_illegal_after_predicted_reply_is_rejected(self):
        board = chess.Board()
        with self.assertRaises(ValueError):
            build_premove_hint(board.fen(), "e2e3", "e2e4")

    def test_illegal_predicted_reply_is_rejected(self):
        board = chess.Board()
        with self.assertRaises(ValueError):
            build_premove_hint(board.fen(), "g8f6", "e2e5")

    def test_promotion_premove_is_recognized(self):
        board = chess.Board("4k3/P7/8/8/8/8/8/4K3 b - - 0 1")
        hint = build_premove_hint(board.fen(), "a7a8q", "e8d7")
        self.assertEqual(hint["promotion"], chess.QUEEN)
        self.assertEqual(hint["san"], "a8=Q")

    def test_white_orientation_square_coordinates(self):
        self.assertEqual(square_to_display(chess.A1, "white"), (0, 7))
        self.assertEqual(square_to_display(chess.H8, "white"), (7, 0))

    def test_black_orientation_square_coordinates(self):
        self.assertEqual(square_to_display(chess.A1, "black"), (7, 0))
        self.assertEqual(square_to_display(chess.H8, "black"), (0, 7))


if __name__ == "__main__":
    unittest.main()
