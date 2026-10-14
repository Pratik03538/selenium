"""Regression tests for the live tracker safety and history helpers."""
import unittest

import chess
from seleniumbase_live_game_tracker import (
    PlayerSideResolver,
    infer_move_between_fens,
    reconstruct_history_from_dom_moves,
    uci_move_reaches_fen,
)


class PlayerSideResolverTests(unittest.TestCase):
    def test_name_only_side_requires_two_consistent_snapshots(self):
        resolver = PlayerSideResolver(player_name_enabled=True)
        self.assertIsNone(resolver.resolve(None, "black"))
        self.assertEqual(resolver.resolve(None, "black"), "black")

    def test_api_side_takes_precedence_over_name_match(self):
        resolver = PlayerSideResolver(player_name_enabled=True)
        self.assertEqual(resolver.resolve("black", "white"), "black")

    def test_opposite_name_match_pauses_until_confirmed(self):
        resolver = PlayerSideResolver(player_name_enabled=True)
        self.assertIsNone(resolver.resolve(None, "black"))
        self.assertEqual(resolver.resolve(None, "black"), "black")
        self.assertIsNone(resolver.resolve(None, "white"))
        self.assertEqual(resolver.resolve(None, "white"), "white")

    def test_reset_requires_fresh_name_evidence(self):
        resolver = PlayerSideResolver(player_name_enabled=True)
        resolver.resolve(None, "black")
        self.assertEqual(resolver.resolve(None, "black"), "black")
        resolver.reset_for_new_game()
        self.assertIsNone(resolver.resolve(None, "black"))


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


if __name__ == "__main__":
    unittest.main()
