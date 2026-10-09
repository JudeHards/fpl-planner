"""Budget, scoring and substitution checks for deterministic transfer screening."""
from __future__ import annotations

import itertools
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import find_transfers as transfer
import simulate_points as sim


def squad():
    positions = ["GKP"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
    return [{"pid": str(i), "name": f"Player {i}", "club": f"Club {i // 3}",
             "position": position, "price": 5.0, "weekly_points": [2.0],
             "total_points": 2.0, "weekly_appearance_probability": [1.0]}
            for i, position in enumerate(positions)]


def candidate(pid, position, price, points, club="Incoming FC"):
    return {"pid": pid, "name": pid, "club": club, "position": position, "price": price,
            "weekly_points": [points], "total_points": points, "weekly_appearance_probability": [1.0]}


class MarginalScoringTests(unittest.TestCase):
    def player(self, position="MID"):
        return sim.Player(name="Player", pid="1", team="home", club="Home", position=position,
                          minutes=[(90, 0)], minute_weights=[1], rates=dict.fromkeys(sim.METRICS, 0.0),
                          bonus=[0], bonus_weights=[1], availability=1.0, history_games=1, news="")

    def test_goal_assist_and_clean_sheet_expectations(self):
        player = self.player()
        player.rates["expected_goals"] = .6
        player.rates["expected_assists"] = .2
        result = transfer.project_player(player, [sim.Fixture("x", "home", "away", 1, 1)], {"home": 1})
        self.assertAlmostEqual(result["points"], 2 + .6 * 5 + .2 * 3 + math.exp(-1))

    def test_thresholds_integrate_events_instead_of_rounding_means(self):
        player = self.player("DEF")
        player.rates["defensive_contribution"] = 10
        result = transfer.project_player(player, [sim.Fixture("x", "home", "away", 0, 0)], {"home": 1})
        self.assertAlmostEqual(result["points"], 6 + 2 * transfer.poisson_tail(10, 10))
        self.assertGreater(transfer.poisson_floor(2.9, 3), 0)
        self.assertAlmostEqual(transfer.poisson_floor(1.3, 2), sum((i//2)*math.exp(-1.3)*1.3**i/math.factorial(i) for i in range(40)))

    def test_double_gameweek_availability_shared_and_blank_zero(self):
        player = self.player("FWD")
        player.availability = .5
        player.minutes = [(90, 0), (0, 0)]
        player.minute_weights = [1, 1]
        fixtures = [sim.Fixture(str(i), "home", "away", 0, 0) for i in range(2)]
        result = transfer.project_player(player, fixtures, {"home": 1})
        self.assertAlmostEqual(result["points"], 1.0)
        self.assertAlmostEqual(result["minutes"], 45)
        self.assertAlmostEqual(result["appearance"], .375)
        self.assertEqual(transfer.project_player(player, [], {"home": 1}), {"points": 0, "minutes": 0, "appearance": 0.0})


class SquadExpectationTests(unittest.TestCase):
    def test_best_legal_xi_matches_brute_force(self):
        players = squad()
        for i, player in enumerate(players):
            player["weekly_points"] = [float((i * 7) % 13)]
        xi, bench, captain, vice = transfer.choose_lineup(players, 0)
        objects = [SimpleNamespace(position=p["position"]) for p in players]
        best = max(sum(players[i]["weekly_points"][0] for i in combo)
                   for combo in itertools.combinations(range(15), 11) if sim.valid_xi(combo, objects))
        self.assertEqual(sum(players[i]["weekly_points"][0] for i in xi), best)
        self.assertEqual(players[captain]["weekly_points"][0], max(players[i]["weekly_points"][0] for i in xi))
        self.assertNotEqual(captain, vice)
        self.assertEqual(len(bench), 4)

    def test_autosubs_and_vice_match_exact_independent_enumeration(self):
        players = squad()
        uncertain = [0, 2, 3, 7, 11, 12]
        play = [.55 if i in uncertain else 1.0 for i in range(15)]
        conditional = [3 + i / 10 for i in range(15)]
        for i, player in enumerate(players):
            player["weekly_appearance_probability"] = [play[i]]
            player["weekly_points"] = [play[i] * conditional[i]]
        xi, bench, captain, vice = transfer.choose_lineup(players, 0)
        objects = [SimpleNamespace(position=p["position"]) for p in players]
        expected = 0
        for mask in range(1 << len(uncertain)):
            played = [True] * 15
            probability = 1.0
            for j, i in enumerate(uncertain):
                played[i] = bool(mask & (1 << j))
                probability *= play[i] if played[i] else 1 - play[i]
            incoming = sim.substitutes(xi, bench, played, objects)
            active = xi + incoming
            score = sum(conditional[i] for i in active if played[i])
            score += conditional[captain] if played[captain] else conditional[vice] if played[vice] else 0
            expected += probability * score
        self.assertAlmostEqual(transfer.expected_week(players, 0, 6)["expected_points"], expected)


class TransferRulesTests(unittest.TestCase):
    def setUp(self):
        self.owned = squad()
        self.sales = {p["pid"]: 50 for p in self.owned}
        self.baseline = transfer.project_squad(self.owned, 6, 1)

    def evaluate(self, outgoing, incoming, bank=0, free=1):
        return transfer.evaluate_move(self.owned, outgoing, incoming, self.sales, bank, free, self.baseline, 6, 1)

    def test_budget_is_exact_and_prices_are_millions(self):
        self.assertEqual(transfer.money_units("£5.9m"), 59)
        incoming = candidate("new", "MID", 5.1, 7)
        self.assertIsNone(self.evaluate([self.owned[7]], [incoming]))
        self.assertEqual(self.evaluate([self.owned[7]], [incoming], bank=1)["bank_after"], 0)
        for bad in ["NaN", "-1", "5.11"]:
            with self.assertRaises(ValueError):
                transfer.money_units(bad)

    def test_funding_pair_can_buy_player_unaffordable_as_single(self):
        dear = candidate("dear", "MID", 6, 8)
        cheap = candidate("cheap", "DEF", 4, 3)
        self.assertIsNone(self.evaluate([self.owned[7]], [dear]))
        move = self.evaluate([self.owned[7], self.owned[2]], [dear, cheap])
        self.assertEqual(move["bank_after"], 0)
        self.assertEqual(move["hit"], 4)
        self.assertAlmostEqual(move["net_gain"], move["gross_gain"] - 4)
        self.assertEqual(self.evaluate([self.owned[7], self.owned[2]], [dear, cheap], free=2)["hit"], 0)
        self.assertEqual(self.evaluate([self.owned[2]], [cheap], free=0)["hit"], 4)

    def test_club_and_position_limits_and_duplicates(self):
        fourth = candidate("fourth", "MID", 5, 8, club="Club 0")
        self.assertIsNone(self.evaluate([self.owned[7]], [fourth]))
        # Removing one player from that club in the same package frees its slot.
        replacement = candidate("replacement", "DEF", 5, 3)
        self.assertIsNotNone(self.evaluate([self.owned[7], self.owned[2]], [fourth, replacement]))
        self.assertIsNone(self.evaluate([self.owned[7]], [candidate("wrong", "DEF", 5, 8)]))
        self.assertIsNone(self.evaluate([self.owned[7]], [self.owned[8]]))
        self.assertIsNone(self.evaluate([self.owned[7], self.owned[8]], [fourth, fourth]))

    def test_search_keeps_funding_pair_and_hides_negative_gains(self):
        dear = candidate("dear", "MID", 6, 8)
        cheap = candidate("cheap", "DEF", 4, 3)
        moves, counts, singles, pairs = transfer.search_moves(self.owned, [dear, cheap], self.sales, 0, 1,
                                                             self.baseline, 6, 1, top=100)
        self.assertTrue(any(set(m["in"]) == {"dear", "cheap"} for m in pairs))
        self.assertTrue(all(m["net_gain"] > 0 for m in moves))
        self.assertGreater(counts["pair_moves_evaluated"], 0)
        terrible = candidate("bad", "MID", 5, -10)
        moves, _, _, _ = transfer.search_moves(self.owned, [terrible], self.sales, 0, 0, self.baseline, 6, 1)
        self.assertEqual(moves, [])

    def test_selling_price_override_affects_budget_and_unknown_keys_fail(self):
        rows = [{"Price": "£5.0m"} for p in self.owned]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prices.json"
            path.write_text(json.dumps({"Player 7": 4.5, "8": "£4.7m"}))
            prices = transfer.selling_prices(rows, self.owned, path)
            self.assertEqual(prices["7"], 45)
            self.assertEqual(prices["8"], 47)
            self.assertEqual(prices["6"], 50)
            for overrides in [{"unknown": 5}, {"7": 5, "Player 7": 5}]:
                path.write_text(json.dumps(overrides))
                with self.assertRaises(ValueError):
                    transfer.selling_prices(rows, self.owned, path)

    def test_selling_price_override_can_fill_missing_markdown_price(self):
        rows = [{"Price": "£5.0m"} for p in self.owned]
        rows[7] = {"Price": "unknown"}
        rows[8] = {}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prices.json"
            path.write_text(json.dumps({"Player 7": 4.5, "8": 4.7}))
            prices = transfer.selling_prices(rows, self.owned, path)
            self.assertEqual((prices["7"], prices["8"]), (45, 47))
            with self.assertRaises(ValueError):
                transfer.selling_prices(rows, self.owned, None)


if __name__ == "__main__":
    unittest.main()
