"""Rule and sampling checks, using synthetic data rather than changing CSV exports.

Run from the project directory with:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import simulate_points as simulation


def squad():
    """Return a legal squad with certain 90-minute appearances and no events."""
    positions = ["GKP"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
    return [
        simulation.Player(
            name=f"Player {index}", pid=str(index), team="unused", club="Unused",
            position=position, minutes=[(90, 0)], minute_weights=[1],
            rates=dict.fromkeys(simulation.METRICS, 0.0), bonus=[0],
            bonus_weights=[1], availability=1.0, history_games=1, news="",
        )
        for index, position in enumerate(positions)
    ]


def lineup_arguments(**overrides):
    values = dict(starting_xi=None, bench_order=None, captain=None, vice_captain=None)
    values.update(overrides)
    return SimpleNamespace(**values)


class ScoringRulesTests(unittest.TestCase):
    def test_appearance_and_clean_sheet_require_sixty_minutes(self):
        for position, full_match_points in {"GKP": 6, "DEF": 6, "MID": 3, "FWD": 2}.items():
            with self.subTest(position=position):
                self.assertEqual(simulation.score_points(position, 59), 1)
                self.assertEqual(simulation.score_points(position, 60), full_match_points)

    def test_goal_value_depends_on_position(self):
        for position, goal_value in {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4}.items():
            with self.subTest(position=position):
                self.assertEqual(simulation.score_points(position, 45, goals=1), 1 + goal_value)

    def test_conceding_removes_clean_sheet_points(self):
        for position in ("GKP", "DEF", "MID", "FWD"):
            with self.subTest(position=position):
                self.assertEqual(simulation.score_points(position, 90, conceded=1), 2)

    def test_conceded_deductions_use_complete_pairs_and_only_defensive_positions(self):
        for conceded, deduction in ((1, 0), (2, 1), (3, 1), (4, 2)):
            for position in ("GKP", "DEF", "MID", "FWD"):
                with self.subTest(conceded=conceded, position=position):
                    expected = 2 - deduction if position in ("GKP", "DEF") else 2
                    self.assertEqual(simulation.score_points(position, 90, conceded=conceded), expected)

    def test_saves_use_complete_groups_of_three_and_only_goalkeepers(self):
        for saves, save_points in ((2, 0), (3, 1), (5, 1), (6, 2), (8, 2)):
            with self.subTest(saves=saves):
                self.assertEqual(simulation.score_points("GKP", 90, conceded=1, saves=saves), 2 + save_points)
                self.assertEqual(simulation.score_points("DEF", 90, conceded=1, saves=saves), 2)

    def test_defensive_contributions_are_one_threshold_award_per_match(self):
        for position, threshold in (("DEF", 10), ("MID", 12), ("FWD", 12)):
            with self.subTest(position=position):
                # Under 60 minutes deliberately isolates this award from clean sheets.
                self.assertEqual(simulation.score_points(position, 45, contributions=threshold - 1), 1)
                self.assertEqual(simulation.score_points(position, 45, contributions=threshold), 3)
                self.assertEqual(simulation.score_points(position, 45, contributions=threshold * 3), 3)
        self.assertEqual(simulation.score_points("GKP", 45, contributions=30), 1)

    def test_other_events_and_deductions_combine(self):
        self.assertEqual(simulation.score_points(
            "FWD", 90, goals=1, assists=1, conceded=1, bonus=3, yellow=1,
            red=1, own_goals=1, penalties_saved=1, penalties_missed=1,
        ), 9)

    def test_nonappearance_scores_zero_despite_supplied_events(self):
        self.assertEqual(simulation.score_points("GKP", 0, goals=2, saves=6, bonus=3), 0)


class LineupTests(unittest.TestCase):
    def setUp(self):
        self.players = squad()
        # A 3-4-3 leaves two defenders, a midfielder, and a goalkeeper on the bench.
        self.xi = [0, 2, 3, 4, 7, 8, 9, 10, 12, 13, 14]

    def test_valid_formation_and_duplicate_rejection(self):
        self.assertTrue(simulation.valid_xi(self.xi, self.players))
        self.assertFalse(simulation.valid_xi(self.xi[:-1] + [self.xi[0]], self.players))
        self.assertFalse(simulation.valid_xi(self.xi[:-1], self.players))
        self.assertFalse(simulation.valid_xi([1 if i == 2 else i for i in self.xi], self.players))
        self.assertFalse(simulation.valid_xi([11 if i == 2 else i for i in self.xi], self.players))

    def test_default_chooses_highest_projected_legal_xi_and_captains(self):
        means = [6, 5, 9, 8, 7, 6, 5, 14, 13, 12, 11, 10, 4, 3, 2]
        xi, bench, captain, vice = simulation.choose_lineup(self.players, means, lineup_arguments())
        self.assertEqual(set(xi), {0, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12})
        self.assertEqual(bench, [6, 13, 14, 1])
        self.assertEqual((captain, vice), (7, 8))

    def test_manual_lineup_captain_and_bench_order_are_respected(self):
        args = lineup_arguments(
            starting_xi=",".join(self.players[i].name for i in self.xi),
            bench_order="Player 11, Player 5, Player 6, Player 1",
            captain="Player 12", vice_captain="Player 7",
        )
        xi, bench, captain, vice = simulation.choose_lineup(self.players, list(range(15)), args)
        self.assertEqual(xi, self.xi)
        self.assertEqual(bench, [11, 5, 6, 1])
        self.assertEqual((captain, vice), (12, 7))

    def test_captain_must_start_and_vice_must_differ(self):
        starting = ",".join(self.players[i].name for i in self.xi)
        for captain, vice in (("Player 5", "Player 7"), ("Player 7", "Player 7")):
            with self.subTest(captain=captain, vice=vice), self.assertRaises(ValueError):
                simulation.choose_lineup(self.players, [1] * 15, lineup_arguments(
                    starting_xi=starting, captain=captain, vice_captain=vice,
                ))

    def test_names_are_accent_insensitive_but_ambiguity_and_duplicates_fail(self):
        self.players[0].name = "Dúbravka"
        self.assertEqual(simulation.resolve_names(" dubravka ", self.players), [0])
        with self.assertRaises(ValueError):
            simulation.resolve_names("Dúbravka,Dubravka", self.players)
        self.players[1].name = "Dubravka"
        with self.assertRaises(ValueError):
            simulation.resolve_names("Dubravka", self.players)


class AutomaticSubstitutionTests(unittest.TestCase):
    def setUp(self):
        self.players = squad()
        self.xi = [0, 2, 3, 4, 7, 8, 9, 10, 12, 13, 14]
        self.bench = [11, 5, 6, 1]

    def incoming(self, absent, xi=None, bench=None):
        played = [i not in absent for i in range(15)]
        return simulation.substitutes(xi or self.xi, bench or self.bench, played, self.players)

    def test_all_starters_appearing_means_no_substitutions(self):
        # Substitutions depend on appearances, never whether a player scored well.
        self.assertEqual(self.incoming(set()), [])

    def test_goalkeepers_replace_only_goalkeepers(self):
        self.assertEqual(self.incoming({0}), [1])
        self.assertEqual(self.incoming({7, 11, 5, 6}), [])
        self.assertEqual(self.incoming({0, 1}), [])

    def test_skip_first_bench_player_when_minimum_three_defenders_requires_it(self):
        self.assertEqual(self.incoming({2}), [5])

    def test_first_eligible_bench_player_takes_priority(self):
        self.assertEqual(self.incoming({7}), [11])
        self.assertEqual(self.incoming({7, 11}), [5])

    def test_two_absences_can_use_first_midfielder_and_next_defender(self):
        self.assertEqual(self.incoming({2, 7}), [11, 5])

    def test_missing_defenders_do_not_block_an_unrelated_like_for_like_sub(self):
        # Both benched defenders are absent, but the midfielder can still replace
        # an absent midfielder even though two starting defenders cannot be filled.
        self.assertEqual(self.incoming({2, 3, 7, 5, 6}), [11])

    def test_only_forward_cannot_be_replaced_by_a_defender(self):
        xi = [0, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]  # 4-5-1
        self.assertEqual(self.incoming({12}, xi=xi, bench=[6, 13, 14, 1]), [13])


class SimulationTests(unittest.TestCase):
    def test_goal_and_assist_marginals_preserve_expected_player_points(self):
        players = squad()
        players[12].team = "home"
        players[12].rates["expected_goals"] = 0.6
        players[12].rates["expected_assists"] = 0.2
        fixture = simulation.Fixture("match", "home", "away", 1, 0)
        points, _, _ = simulation.simulate(players, [fixture], {"home": 1, "away": 1}, 20_000, 23)
        # Expected points = appearance (2) + goals (.6 * 4) + assists (.2 * 3).
        # Excluding a scorer from assisting must not also shrink their marginal
        # assist rate. A fixed seed and generous tolerance avoid a flaky test.
        mean_points = sum(row[12] for row in points) / len(points)
        self.assertAlmostEqual(mean_points, 5.0, delta=0.12)

    def test_seed_reproduces_all_samples(self):
        players = squad()
        for player in players[:5]:
            player.team = "home"
        fixture = simulation.Fixture("match", "home", "away", 1.4, 1.1)
        first = simulation.simulate(players, [fixture], {"home": 1.4, "away": 1.1}, 100, 42)
        self.assertEqual(first, simulation.simulate(players, [fixture], {"home": 1.4, "away": 1.1}, 100, 42))
        self.assertNotEqual(first, simulation.simulate(players, [fixture], {"home": 1.4, "away": 1.1}, 100, 43))

    def test_teammates_share_clean_sheets_and_goals_conceded(self):
        players = squad()
        for index in (2, 3, 4):
            players[index].team = "home"
        fixture = simulation.Fixture("match", "home", "away", 0, 1.5)
        points, played, minutes = simulation.simulate(players, [fixture], {"home": 1, "away": 1}, 200, 17)
        self.assertGreater(len({row[2] for row in points}), 1)
        for scores, appearances, duration in zip(points, played, minutes):
            self.assertEqual(scores[2], scores[3])
            self.assertEqual(scores[3], scores[4])
            self.assertEqual((appearances[2], duration[2]), (True, 90))
            self.assertEqual((scores[12], appearances[12], duration[12]), (0, False, 0))

    def test_allocated_goal_removes_opponents_clean_sheet_in_same_trial(self):
        players = squad()
        players[12].team = "home"
        # An overwhelming share gives this sole home player every sampled goal.
        players[12].rates["expected_goals"] = 100
        players[2].team = "away"
        fixture = simulation.Fixture("match", "home", "away", 1.5, 0)
        points, _, _ = simulation.simulate(players, [fixture], {"home": 1, "away": 1}, 200, 19)
        self.assertTrue(any(row[12] == 2 for row in points))
        self.assertTrue(any(row[12] > 2 for row in points))
        for scores in points:
            self.assertEqual(scores[2] == 6, scores[12] == 2)

    def test_double_gameweek_sums_matches_and_shares_availability(self):
        players = squad()
        players[12].team = "home"
        players[12].availability = 0.5
        fixtures = [simulation.Fixture("first", "home", "away", 0, 0),
                    simulation.Fixture("second", "home", "other", 0, 0)]
        points, played, minutes = simulation.simulate(
            players, fixtures, {"home": 1, "away": 1, "other": 1}, 100, 41,
        )
        self.assertEqual({row[12] for row in minutes}, {0, 180})
        for scores, appearances, duration in zip(points, played, minutes):
            self.assertEqual(scores[12], 4 if duration[12] else 0)
            self.assertEqual(appearances[12], duration[12] > 0)


if __name__ == "__main__":
    unittest.main()
