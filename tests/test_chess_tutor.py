"""Regression tests for engine-grounded chess tutoring commentary."""
import unittest

import chess
import chess.engine

from chess_tutor import (
    build_coach_commentary,
    build_opponent_move_commentary,
    voice_summary,
)


def scored_info(pv, cp=None, mate=None):
    score = chess.engine.Mate(mate) if mate is not None else chess.engine.Cp(cp or 0)
    return {
        "pv": pv,
        "score": chess.engine.PovScore(score, chess.WHITE),
        "depth": 12,
    }


class ChessTutorCommentaryTests(unittest.TestCase):
    def setUp(self):
        self.board = chess.Board()
        self.e4 = chess.Move.from_uci("e2e4")
        self.d4 = chess.Move.from_uci("d2d4")
        self.infos = [
            scored_info([
                self.e4,
                chess.Move.from_uci("e7e5"),
                chess.Move.from_uci("g1f3"),
            ], cp=120),
            scored_info([
                self.d4,
                chess.Move.from_uci("e7e5"),
                chess.Move.from_uci("g1f3"),
            ], cp=-180),
        ]

    def test_selected_move_and_predicted_reply_are_explained(self):
        lines = build_coach_commentary(
            self.board, self.infos, self.e4, self.infos[0],
            {"rank": 0, "source": "STOCKFISH"},
        )
        combined = "\n".join(lines)
        self.assertIn("Hamne e4", combined)
        self.assertIn("engine line", combined)
        self.assertIn("opponent ka likely jawab", combined)

    def test_worse_second_candidate_is_flagged_with_eval_drop(self):
        lines = build_coach_commentary(
            self.board, self.infos, self.e4, self.infos[0],
            {"rank": 0, "source": "STOCKFISH"},
        )
        combined = "\n".join(lines)
        self.assertIn("Engine #2", combined)
        self.assertIn("evaluation", combined)
        self.assertIn("3.0 pawn", combined)

    def test_forced_mate_for_opponent_is_called_out(self):
        bad_info = scored_info(
            [self.d4, chess.Move.from_uci("e7e5")],
            mate=-3,
        )
        lines = build_coach_commentary(
            self.board,
            [self.infos[0], bad_info],
            self.e4,
            self.infos[0],
            {"rank": 0, "source": "STOCKFISH"},
        )
        self.assertTrue(
            any("forced mate in 3" in line for line in lines),
            "\n".join(lines),
        )
        self.assertFalse(
            any("1000.0 pawn" in line for line in lines),
            "Mate scores must not be reported as huge pawn differences.",
        )

    def test_invalid_selected_move_returns_no_commentary(self):
        illegal = chess.Move.from_uci("e2e5")
        self.assertEqual(
            build_coach_commentary(self.board, self.infos, illegal), []
        )

    def test_opponent_capture_is_described_from_history(self):
        board = chess.Board()
        for san in ("e4", "d5", "exd5"):
            board.push_san(san)
        lines = build_opponent_move_commentary(board, 15)
        combined = "\n".join(lines)
        self.assertIn("Opponent ne exd5", combined)
        self.assertIn("hamara pawn capture kiya", combined)
        self.assertIn("kaafi kareeb", combined)

    def test_opponent_check_is_flagged(self):
        board = chess.Board()
        for san in ("e4", "e5", "Qh5", "Nc6", "Bc4", "Nf6", "Qxf7+"):
            board.push_san(san)
        lines = build_opponent_move_commentary(board, 12)
        self.assertTrue(
            any("Yeh check hai" in line for line in lines),
            "\n".join(lines),
        )

    def test_voice_summary_is_shorter_than_detailed_commentary(self):
        lines = build_coach_commentary(
            self.board, self.infos, self.e4, self.infos[0],
            {"rank": 0, "source": "STOCKFISH"},
        )
        spoken = voice_summary(lines, max_chars=100)
        self.assertLessEqual(len(spoken), 100)
        self.assertTrue(spoken)


if __name__ == "__main__":
    unittest.main()
