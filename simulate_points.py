#!/usr/bin/env python3
"""Estimate one gameweek's FPL points from a Markdown squad and local CSVs.

Run: python3 simulate_points.py
     python3 simulate_points.py --iterations 50000 --json-output projection.json
     python3 simulate_points.py --captain Haaland --vice-captain Rogers

Only Python's standard library is required; nothing is downloaded or modified.
Default paths are relative to this script, so it also works from another folder.
The default XI maximises the sum of individual projected points subject to FPL
formation rules. Captain/vice and bench priority follow those same projections.
A separate pilot simulation chooses the lineup before the reported simulation,
so the reported total does not select players using its own lucky outcomes.

MODEL: recent FPL minutes + regularised per-90 rates + fixture-level Poisson
scores. Goal times are shared by teammates and opponents, preserving clean-sheet
correlation. Goals/assists are allocated to squad players or to unowned players.
Bonus is an empirical approximation, not a reconstruction of the full match BPS.
This is an exploratory estimate, not a calibrated prediction or live injury feed.

Scoring reference (checked 9 October 2026):
https://www.premierleague.com/en/news/2174909
Data source: local FPL-Core-Insights/ (see that repository's README.md).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import random
import re
import statistics
import subprocess
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POSITIONS = {"Goalkeeper": "GKP", "Defender": "DEF", "Midfielder": "MID", "Forward": "FWD"}
GOAL_POINTS = {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4}
CS_POINTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
METRICS = ("expected_goals", "expected_assists", "saves", "defensive_contribution",
           "yellow_cards", "red_cards", "own_goals", "penalties_saved", "penalties_missed")
# Three full matches of positional evidence prevent tiny samples from dominating.
PRIOR_MINUTES = 270.0
TEAM_PRIOR_MATCHES = 4.0


def number(value, default=0.0):
    """CSV blanks mean missing data; an explicit zero is still a real observation."""
    if value is None or str(value).strip().lower() in ("", "nan", "none"):
        return default
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Non-finite number in data: {value!r}")
    return result


def identifier(value):
    # These exports encode some integer team codes as strings such as '43.0'.
    return str(int(number(value)))


def normalise(value):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def read_csv(path, required=True):
    if not path.exists():
        if required:
            raise ValueError(f"Required data file does not exist: {path}")
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def timestamp(value):
    if not value:
        return None
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # Current exports sometimes omit the offset; their fixture timestamps are UTC.
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def read_team(path):
    """Read the named Markdown columns, ignoring prices and surrounding prose."""
    header, squad = None, []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if "Player" in cells and "Club" in cells:
            header = cells
            continue
        if header is None or all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        if len(cells) != len(header):
            raise ValueError(f"Malformed team table row: {line}")
        squad.append(dict(zip(header, cells)))
    if len(squad) != 15:
        raise ValueError(f"Expected a 15-player Markdown squad, found {len(squad)}")
    return squad


@dataclass
class Player:
    name: str
    pid: str
    team: str
    club: str
    position: str
    minutes: list[tuple[float, float]]  # (minutes, start minute)
    minute_weights: list[float]
    rates: dict[str, float]
    bonus: list[float]
    bonus_weights: list[float]
    availability: float
    history_games: int
    news: str


@dataclass
class Fixture:
    key: str
    home: str
    away: str
    home_xg: float
    away_xg: float


def load_data(args):
    seasons = sorted(p.name for p in args.data.iterdir()
                     if p.is_dir() and re.fullmatch(r"\d{4}-\d{4}", p.name))
    season = args.season or (seasons[-1] if seasons else "")
    base = args.data / season
    summaries = read_csv(base / "gameweek_summaries.csv")
    next_gws = [int(r["id"]) for r in summaries if r.get("is_next", "").lower() == "true"]
    if args.gameweek is None and len(next_gws) != 1:
        raise ValueError("Cannot identify one next gameweek; supply --gameweek")
    gw = args.gameweek if args.gameweek is not None else next_gws[0]
    summary = next((r for r in summaries if int(r["id"]) == gw), None)
    if summary is None:
        raise ValueError(f"No gameweek {gw} in {base}")
    cutoff = timestamp(summary.get("deadline_time"))
    pl = base / "By Tournament" / "Premier League"
    teams = {identifier(r["code"]): r for r in read_csv(base / "teams.csv")}
    # For a historical target use its roster snapshot, not today's club/position.
    roster_path = pl / f"GW{gw}" / "players.csv"
    roster = read_csv(roster_path, required=False) or read_csv(base / "players.csv")
    roster = {identifier(r["player_id"]): r for r in roster}
    warnings = []
    matches, history = {}, defaultdict(list)
    last_gw = 0
    # Use ONE tournament view. Reading both By Gameweek and By Tournament would
    # count the same match twice; friendlies/cups must not influence FPL rates.
    for folder in sorted(pl.glob("GW*"), key=lambda p: int(p.name[2:])):
        hist_gw = int(folder.name[2:])
        if not max(1, gw - args.lookback) <= hist_gw < gw:
            continue
        completed = {}
        for row in read_csv(folder / "matches.csv", required=False):
            if row.get("finished", "").lower() != "true":
                continue
            kickoff = timestamp(row.get("kickoff_time"))
            if cutoff and (kickoff is None or kickoff >= cutoff):
                continue  # Avoid using outcomes that occurred after the deadline.
            completed[row["match_id"]] = row
        matches.update(completed)
        match_counts = Counter(identifier(r[k]) for r in completed.values()
                               for k in ("home_team", "away_team"))
        historical_roster = {identifier(r["player_id"]): r for r in
                             read_csv(folder / "players.csv", required=False)}
        seen = set()
        for row in read_csv(folder / "player_gameweek_stats.csv", required=False):
            pid = identifier(row["id"])
            if pid in seen:
                raise ValueError(f"Duplicate player {pid} in {folder}/player_gameweek_stats.csv")
            seen.add(pid)
            identity = historical_roster.get(pid, roster.get(pid))
            if identity is None:
                continue
            minutes = number(row.get("minutes"), None)
            starts = number(row.get("starts"), None)
            # Missing participation is not evidence that someone stayed on the
            # bench. Fail explicitly instead of biasing their playing-time model.
            if minutes is None or starts is None:
                raise ValueError(f"Missing minutes/starts for player {pid}, GW{hist_gw}")
            if minutes < 0 or starts < 0:
                raise ValueError(f"Negative minutes/starts for player {pid}, GW{hist_gw}")
            # A GW aggregate cannot distinguish two matches or a postponed match.
            # Omit those training rows rather than treating 180 minutes as one game.
            club = identifier(identity["team_code"])
            if match_counts[club] != 1 or minutes > 90:
                continue
            row = dict(row, _gw=hist_gw, _position=POSITIONS[identity["position"]])
            history[pid].append(row)
            last_gw = max(last_gw, hist_gw)
    if not matches or not history:
        raise ValueError("No completed Premier League history before the target; increase --lookback")
    # playerstats is CUMULATIVE and has repeated snapshots. We use only the newest
    # eligible row's availability/news, never sum its season totals as match data.
    snapshots = {}
    for row in read_csv(base / "playerstats.csv"):
        snapshot_gw = int(number(row.get("gw")))
        pid = identifier(row["id"])
        if snapshot_gw < gw and snapshot_gw <= last_gw:
            if pid not in snapshots or snapshot_gw > int(number(snapshots[pid]["gw"])):
                snapshots[pid] = row
    target_rows = read_csv(pl / f"GW{gw}" / "fixtures.csv")
    target_rows = list({r["match_id"]: r for r in target_rows}.values())
    if any(r.get("finished", "").lower() == "true" for r in target_rows):
        warnings.append("Target fixtures contain finished matches: this is a retrospective estimate; scores are ignored.")
    if last_gw < gw - 1:
        warnings.append(f"Latest usable history is GW{last_gw}; target is GW{gw}.")
    return season, gw, summary, teams, roster, history, snapshots, list(matches.values()), target_rows, warnings


def weight(row, latest):
    # A three-gameweek half-life reacts to changing roles without ignoring history.
    return 0.5 ** ((latest - row["_gw"]) / 3.0)


def fit_team_strength(matches, target_rows):
    """Fit attack/defence from match xG, regularising toward the league average."""
    by_team = defaultdict(list)
    home_values, away_values = [], []
    fallbacks = 0
    for row in matches:
        values = []
        for side in ("home", "away"):
            value = number(row.get(f"{side}_expected_goals_xg"), None)
            if value is None:
                value = number(row.get(f"{side}_score"), None)
                fallbacks += 1
            if value is None or value < 0:
                raise ValueError(f"Missing/negative team xG and score: {row['match_id']}")
            values.append(value)
        h, a = values
        home_values.append(h)
        away_values.append(a)
        by_team[identifier(row["home_team"])].append((h, a, True))
        by_team[identifier(row["away_team"])].append((a, h, False))
    home_mean, away_mean = statistics.mean(home_values), statistics.mean(away_values)
    if min(home_mean, away_mean) <= 0:
        raise ValueError("Too little scoring data to estimate team strength")
    league_mean = (home_mean + away_mean) / 2
    attack, defence, baseline = {}, {}, {}
    all_teams = set(by_team) | {identifier(r[k]) for r in target_rows for k in ("home_team", "away_team")}
    for club in all_teams:
        games = by_team[club]
        # Divide by the appropriate league venue baseline first. This avoids
        # calling a team stronger just because it has had more home fixtures.
        attack[club] = (TEAM_PRIOR_MATCHES + sum(gf / (home_mean if home else away_mean)
                                              for gf, ga, home in games)) / (TEAM_PRIOR_MATCHES + len(games))
        defence[club] = (TEAM_PRIOR_MATCHES + sum(ga / (away_mean if home else home_mean)
                                               for gf, ga, home in games)) / (TEAM_PRIOR_MATCHES + len(games))
        baseline[club] = league_mean * attack[club]
    fixtures = []
    for row in target_rows:
        h, a = identifier(row["home_team"]), identifier(row["away_team"])
        fixtures.append(Fixture(row["match_id"], h, a,
                                home_mean * attack[h] * defence[a],
                                away_mean * attack[a] * defence[h]))
    return fixtures, baseline, fallbacks


def fit_players(team_rows, roster, teams, history, snapshots, warnings, validate_squad=True):
    """Fit requested players; transfer screening can pass a league-wide list.

    The simulator keeps squad validation on by default. Reusing the same fitting
    routine for candidates avoids giving incoming players a different model.
    """
    latest = max(r["_gw"] for rows in history.values() for r in rows)
    priors = defaultdict(lambda: defaultdict(float))
    prior_exposure = defaultdict(lambda: defaultdict(float))
    bonus_prior = defaultdict(Counter)
    for rows in history.values():
        for r in rows:
            pos, w, mins = r["_position"], weight(r, latest), number(r.get("minutes"))
            if mins <= 0:
                continue
            for metric in METRICS:
                value = number(r.get(metric), None)
                if value is not None and value >= 0:
                    priors[pos][metric] += w * value
                    prior_exposure[pos][metric] += w * mins / 90
            b = number(r.get("bonus"), None)
            if b is not None:
                bonus_prior[pos][min(3, max(0, int(b)))] += w
    players = []
    for requested in team_rows:
        club_matches = [code for code, r in teams.items()
                        if normalise(requested["Club"]) in (normalise(r["name"]), normalise(r["short_name"]))]
        if len(club_matches) != 1:
            raise ValueError(f"Club does not resolve uniquely: {requested['Club']}")
        club = club_matches[0]
        candidates = []
        for pid, r in roster.items():
            names = [r["web_name"], r["first_name"] + " " + r["second_name"]]
            if identifier(r["team_code"]) == club and normalise(requested["Player"]) in map(normalise, names):
                candidates.append((pid, r))
        if len(candidates) != 1:
            raise ValueError(f"Player does not resolve uniquely: {requested['Player']} ({requested['Club']})")
        pid, identity = candidates[0]
        pos = POSITIONS[identity["position"]]
        rows = history.get(pid, [])
        if not rows:
            raise ValueError(f"No minute history for {requested['Player']}; cannot estimate selection reliably")
        rates = {}
        for metric in METRICS:
            total = exposure = 0.0
            for r in rows:
                value = number(r.get(metric), None)
                if value is not None and value >= 0:
                    w = weight(r, latest)
                    total += w * value
                    exposure += w * number(r.get("minutes")) / 90
            prior_minutes = prior_exposure[pos][metric]
            if prior_minutes == 0:
                raise ValueError(f"No {metric} coverage for position {pos}")
            prior_rate = priors[pos][metric] / prior_minutes
            rates[metric] = (total + PRIOR_MINUTES / 90 * prior_rate) / (exposure + PRIOR_MINUTES / 90)
        minutes, minute_weights, bonuses, bonus_weights = [], [], [], []
        for r in rows:
            mins = number(r.get("minutes"))
            # When only GW totals exist, model starters from minute zero and
            # substitute appearances at the end. Timing is an approximation.
            start = 0.0 if number(r.get("starts")) > 0 else 90.0 - mins
            minutes.append((mins, start))
            minute_weights.append(weight(r, latest))
            if mins > 0 and number(r.get("bonus"), None) is not None:
                bonuses.append(min(3, max(0, number(r["bonus"]))))
                bonus_weights.append(weight(r, latest))
        prior_bonus_total = sum(bonus_prior[pos].values())
        if not prior_bonus_total:
            raise ValueError(f"No bonus coverage for position {pos}")
        for b, count in sorted(bonus_prior[pos].items()):
            bonuses.append(b)
            bonus_weights.append(3 * count / prior_bonus_total)
        snapshot = snapshots.get(pid, {})
        chance = number(snapshot.get("chance_of_playing_next_round"), None)
        status = snapshot.get("status", "")
        availability = max(0, min(1, chance / 100)) if chance is not None else (0.0 if status in ("i", "s", "u", "n") else 1.0)
        if not snapshot:
            warnings.append(f"No availability snapshot for {requested['Player']}; assuming available.")
        elif status == "d" and chance is None:
            availability = 0.75
            warnings.append(f"{requested['Player']}: doubtful without a percentage; assuming 75% availability.")
        if availability < 1:
            warnings.append(f"{requested['Player']}: {availability:.0%} availability in local GW{snapshot.get('gw')} snapshot. {snapshot.get('news', '')}".strip())
        players.append(Player(requested["Player"], pid, club, requested["Club"], pos,
                              minutes, minute_weights, rates, bonuses, bonus_weights,
                              availability, len(rows), snapshot.get("news", "")))
    if len({p.pid for p in players}) != len(players):
        raise ValueError("The squad contains a duplicate player")
    if validate_squad and Counter(p.position for p in players) != Counter({"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}):
        raise ValueError("Squad must contain 2 goalkeepers, 5 defenders, 5 midfielders and 3 forwards")
    return players


def poisson(rng, mean):
    """Knuth sampler: sufficient for the small football event rates used here."""
    if mean <= 0:
        return 0
    limit, product, count = math.exp(-mean), 1.0, 0
    while product > limit:
        product *= rng.random()
        count += 1
    return count - 1


def choose_event_player(rng, candidates):
    # The leftover probability represents goals/assists by players outside our
    # 15-player squad. If estimates exceed 100%, normalise them before allocating.
    total = sum(chance for _, chance in candidates)
    draw = rng.random() * max(1.0, total)
    for index, chance in candidates:
        draw -= chance
        if draw < 0:
            return index
    return None


def score_points(position, minutes, goals=0, assists=0, conceded=0, saves=0,
                 contributions=0, bonus=0, yellow=0, red=0, own_goals=0,
                 penalties_saved=0, penalties_missed=0):
    """Convert sampled events to points; thresholds are applied BEFORE averaging."""
    if minutes <= 0:
        return 0
    points = (2 if minutes >= 60 else 1) + goals * GOAL_POINTS[position] + assists * 3
    if minutes >= 60 and conceded == 0:
        points += CS_POINTS[position]
    if position in ("GKP", "DEF"):
        points -= conceded // 2
    if position == "GKP":
        points += saves // 3
    elif contributions >= (10 if position == "DEF" else 12):
        points += 2  # Awarded once per match, not once for every ten/twelve actions.
    return points + bonus - yellow - 3 * red - 2 * own_goals + 5 * penalties_saved - 2 * penalties_missed


def simulate(players, fixtures, baseline, iterations, seed):
    rng = random.Random(seed)
    samples, played_samples, minute_samples = [], [], []
    team_indices = defaultdict(list)
    for i, p in enumerate(players):
        team_indices[p.team].append(i)
    for _ in range(iterations):
        points, played, total_minutes = [0.0] * 15, [False] * 15, [0.0] * 15
        # Fitness is shared across a double gameweek; rotation is sampled per match.
        available = [rng.random() < p.availability for p in players]
        for f in fixtures:
            indices = team_indices[f.home] + team_indices[f.away]
            if not indices:
                continue
            windows = {}
            for i in indices:
                p = players[i]
                mins, start = rng.choices(p.minutes, weights=p.minute_weights, k=1)[0] if available[i] else (0, 0)
                windows[i] = (start, start + mins, mins)
                played[i] |= mins > 0
                total_minutes[i] += mins
            # Shared scores/times preserve teammate clean-sheet correlations and
            # mean an opponent's goal removes the same clean sheet in this trial.
            goal_times = {f.home: [rng.random() * 90 for _ in range(poisson(rng, f.home_xg))],
                          f.away: [rng.random() * 90 for _ in range(poisson(rng, f.away_xg))]}
            goals, assists = Counter(), Counter()
            for club in (f.home, f.away):
                for minute in goal_times[club]:
                    eligible = [i for i in team_indices[club] if windows[i][0] <= minute < windows[i][1]]
                    scorer_weights = [(i, players[i].rates["expected_goals"] / baseline[club]) for i in eligible]
                    scorer_total = max(1.0, sum(chance for _, chance in scorer_weights))
                    scorer_probabilities = {i: chance / scorer_total for i, chance in scorer_weights}
                    scorer = choose_event_player(rng, scorer_weights)
                    if scorer is not None:
                        goals[scorer] += 1
                    # A player cannot assist their own goal. Compensate for the
                    # chance of their being the scorer so that excluding them
                    # does not systematically depress their marginal xA rate.
                    # Oversubscribed probabilities are still normalised below.
                    assister = choose_event_player(rng, [(i, players[i].rates["expected_assists"] / baseline[club]
                                                         / max(1e-9, 1 - scorer_probabilities[i]))
                                                        for i in eligible if i != scorer])
                    if assister is not None:
                        assists[assister] += 1
            for i in indices:
                start, end, mins = windows[i]
                if not mins:
                    continue
                p = players[i]
                opponent = f.away if p.team == f.home else f.home
                conceded = sum(start <= t < end for t in goal_times[opponent])
                def draw(metric):
                    return poisson(rng, p.rates[metric] * mins / 90)
                # Empirical bonus avoids pretending xG alone reproduces BPS. It
                # is independent of simulated goals and other players' bonus.
                bonus = rng.choices(p.bonus, weights=p.bonus_weights, k=1)[0]
                # Rare penalties/cards are independent marginal approximations;
                # we do not reconstruct penalty sequences or dismissal timing.
                points[i] += score_points(p.position, mins, goals[i], assists[i], conceded,
                                          draw("saves") if p.position == "GKP" else 0,
                                          draw("defensive_contribution"), bonus,
                                          min(1, draw("yellow_cards")), min(1, draw("red_cards")),
                                          draw("own_goals"), draw("penalties_saved"), draw("penalties_missed"))
        samples.append(points)
        played_samples.append(played)
        minute_samples.append(total_minutes)
    return samples, played_samples, minute_samples


def valid_xi(indices, players):
    counts = Counter(players[i].position for i in indices)
    return (len(indices) == 11 and len(set(indices)) == 11 and counts["GKP"] == 1
            and 3 <= counts["DEF"] <= 5 and 2 <= counts["MID"] <= 5 and 1 <= counts["FWD"] <= 3)


def resolve_names(value, players):
    result = []
    for name in value.split(","):
        found = [i for i, p in enumerate(players) if normalise(p.name) == normalise(name)]
        if len(found) != 1:
            raise ValueError(f"Squad name does not resolve uniquely: {name}")
        result.append(found[0])
    if len(set(result)) != len(result):
        raise ValueError("Repeated name in lineup/captain arguments")
    return result


def choose_lineup(players, means, args):
    xi = resolve_names(args.starting_xi, players) if args.starting_xi else list(max(
        (combo for combo in itertools.combinations(range(15), 11) if valid_xi(combo, players)),
        key=lambda combo: sum(means[i] for i in combo)))
    if not valid_xi(xi, players):
        raise ValueError("Starting XI must be a legal 11-player formation")
    # The reserve goalkeeper has a separate substitution rule; display them last
    # so the first three entries clearly show outfield bench priority.
    bench = sorted((i for i in range(15) if i not in xi),
                   key=lambda i: (players[i].position == "GKP", -means[i]))
    if args.bench_order:
        bench = resolve_names(args.bench_order, players)
        if len(bench) != 4 or set(bench) != set(range(15)) - set(xi):
            raise ValueError("--bench-order must name all four players outside the starting XI")
    ranked = sorted(xi, key=lambda i: means[i], reverse=True)
    def captain_arg(value, fallback):
        indices = resolve_names(value, players) if value else [fallback]
        if len(indices) != 1 or indices[0] not in xi:
            raise ValueError("Captain and vice-captain must each name one player in the starting XI")
        return indices[0]
    captain = captain_arg(args.captain, ranked[0])
    vice = captain_arg(args.vice_captain, next(i for i in ranked if i != captain))
    if captain == vice:
        raise ValueError("Captain and vice-captain must differ")
    return xi, bench, captain, vice


def substitutes(xi, bench, played, players):
    """Find legal replacements, respecting bench priority and goalkeeper isolation.

    Keep absent, unreplaced slots when checking the formation: FPL can leave you
    with fewer than eleven actual appearances. An unavailable defender must not
    make a perfectly legal like-for-like substitution elsewhere impossible.
    """
    absent = [i for i in xi if not played[i]]
    available = [i for i in bench if played[i]]
    choices = []
    for size in range(min(len(absent), len(available)) + 1):
        for incoming in itertools.combinations(available, size):
            for outgoing in itertools.combinations(absent, size):
                if sum(players[i].position == "GKP" for i in incoming) != sum(players[i].position == "GKP" for i in outgoing):
                    continue
                if valid_xi([i for i in xi if i not in outgoing] + list(incoming), players):
                    choices.append(incoming)
                    break
    # Lexicographic inclusion prioritises the first eligible bench player, then
    # the second; maximise the number of legal replacements before that ordering.
    return list(max(choices, key=lambda c: (len(c), tuple(i in c for i in bench))))


def percentile(values, fraction):
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def build_report(args):
    season, gw, summary, teams, roster, history, snapshots, matches, target_rows, warnings = load_data(args)
    team_rows = read_team(args.team)
    fixtures, baseline, fallback_count = fit_team_strength(matches, target_rows)
    players = fit_players(team_rows, roster, teams, history, snapshots, warnings)
    if fallback_count:
        warnings.append(f"Used actual goals for {fallback_count} team-match xG blanks.")
    for p, requested in zip(players, team_rows):
        actual = [(teams[f.away if p.team == f.home else f.home]["name"], "Home" if p.team == f.home else "Away")
                  for f in fixtures if p.team in (f.home, f.away)]
        supplied = (requested.get("Opponent", ""), requested.get("Venue", ""))
        if supplied[0] and not any(tuple(map(normalise, supplied)) == tuple(map(normalise, fixture)) for fixture in actual):
            warnings.append(f"{p.name}: Markdown fixture {supplied} differs from GW{gw} CSV fixtures {actual}; using CSV schedule.")
        if not actual:
            warnings.append(f"{p.name}: blank gameweek, projected zero points.")
    # Selection is fixed using a separate pilot, never re-optimised after seeing a
    # simulated gameweek's results (which would amount to giving us hindsight).
    pilot_count = max(2000, min(10000, args.iterations))
    pilot, _, _ = simulate(players, fixtures, baseline, pilot_count, args.seed + 1)
    means = [statistics.mean(row[i] for row in pilot) for i in range(15)]
    xi, bench, captain, vice = choose_lineup(players, means, args)
    samples, appearances, minute_samples = simulate(players, fixtures, baseline, args.iterations, args.seed)
    totals, contributions, sub_counts, cache = [], [0.0] * 15, [0] * 15, {}
    for points, played in zip(samples, appearances):
        mask = tuple(played)
        if mask not in cache:
            cache[mask] = substitutes(xi, bench, played, players)
        incoming = cache[mask]
        active = xi + incoming
        scoring_captain = captain if played[captain] else vice if played[vice] else None
        total = sum(points[i] for i in active)
        if scoring_captain is not None:
            total += points[scoring_captain]
        totals.append(total)
        for i in active:
            contributions[i] += points[i]
        if scoring_captain is not None:
            contributions[scoring_captain] += points[scoring_captain]
        for i in incoming:
            sub_counts[i] += 1
    output_players = []
    for i, p in enumerate(players):
        outfield_bench = [j for j in bench if players[j].position != "GKP"]
        role = "XI" if i in xi else "Reserve GK" if p.position == "GKP" else f"Bench {outfield_bench.index(i) + 1}"
        if i == captain:
            role += " (C)"
        if i == vice:
            role += " (VC)"
        output_players.append({"name": p.name, "club": p.club, "position": p.position, "role": role,
                               "expected_points": statistics.mean(r[i] for r in samples),
                               "selection_expected_points": means[i],
                               "expected_team_contribution": contributions[i] / args.iterations,
                               "expected_minutes": statistics.mean(r[i] for r in minute_samples),
                               "appearance_probability": statistics.mean(r[i] for r in appearances),
                               "autosub_probability": sub_counts[i] / args.iterations,
                               "availability": p.availability, "history_games": p.history_games,
                               "rates_per_90": p.rates})
    # Pin the inputs in saved results so a report can reject a stale team snapshot.
    try:
        revision = subprocess.run(["git", "-C", str(args.data.parent), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, check=False)
        data_revision = revision.stdout.strip() if revision.returncode == 0 else None
    except FileNotFoundError:
        data_revision = None  # Also allow a downloaded dataset without Git installed.
    return {"season": season, "gameweek": gw, "deadline": summary.get("deadline_time"),
            "team_sha256": hashlib.sha256(args.team.read_bytes()).hexdigest(),
            "data_revision": data_revision,
            "team_file": str(args.team.resolve()), "data_directory": str(args.data.resolve()),
            "training_gameweeks": sorted({r["_gw"] for rows in history.values() for r in rows}),
            "training_matches": len(matches), "iterations": args.iterations, "pilot_iterations": pilot_count,
            "seed": args.seed, "expected_points": statistics.mean(totals),
            "median": statistics.median(totals), "p10": percentile(totals, .1), "p90": percentile(totals, .9),
            "monte_carlo_standard_error": statistics.stdev(totals) / math.sqrt(args.iterations),
            "captain": players[captain].name, "vice_captain": players[vice].name,
            "starting_xi": [players[i].name for i in xi], "bench_order": [players[i].name for i in bench],
            "players": output_players, "fixtures": [dict(home=teams[f.home]["name"], away=teams[f.away]["name"],
                                                          home_xg=f.home_xg, away_xg=f.away_xg) for f in fixtures],
            "warnings": warnings,
            "assumptions": ["No chips or transfer-hit deductions. One target gameweek, including all its fixtures.",
                "Automatic XI maximises individual pilot projections within legal formations; bench effects are evaluated afterwards.",
                "Minute samples include historical nonappearances; applying current availability can double-count injury absence.",
                "Player rates shrink toward position means; team strength shrinks toward league venue means. Current season only.",
                "xA approximates FPL assists; unowned teammates absorb unused goal/assist probability.",
                "Bonus is drawn independently from historical appearance distributions, not a match-wide BPS ranking.",
                "Defensive actions and rare events use independent Poisson rates. Dismissal timing and bench cards are not modelled.",
                "Percentiles describe simulated match variation, not uncertainty in estimated rates or future team news.",
                "Training excludes multi-fixture player-gameweeks; future doubles are simulated match by match."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--team", type=Path, default=ROOT / "current-team.md", help="Markdown squad file")
    parser.add_argument("--data", type=Path, default=ROOT / "FPL-Core-Insights" / "data", help="Directory containing season folders")
    parser.add_argument("--season", help="Default: latest local season")
    parser.add_argument("--gameweek", type=int, help="Default: local is_next gameweek")
    parser.add_argument("--lookback", type=int, default=8, help="Maximum prior gameweeks to fit (default: 8)")
    parser.add_argument("--iterations", type=int, default=20000, help="Reported Monte Carlo trials (default: 20000)")
    parser.add_argument("--seed", type=int, default=42, help="Fixed seed makes results reproducible")
    parser.add_argument("--starting-xi", help="Optional comma-separated 11 squad names")
    parser.add_argument("--bench-order", help="Optional comma-separated four remaining names, highest priority first")
    parser.add_argument("--captain", help="Optional squad name; must be in the XI")
    parser.add_argument("--vice-captain", help="Optional squad name; must be in the XI")
    parser.add_argument("--json-output", type=Path, help="Optional machine-readable report path")
    args = parser.parse_args(argv)
    if args.iterations < 2 or args.lookback < 1:
        parser.error("--iterations must be at least 2 and --lookback must be positive")
    try:
        report = build_report(args)
        if args.json_output:
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))
    print(f"{report['season']} GW{report['gameweek']} | {report['iterations']:,} simulations | seed {args.seed}")
    print(f"History: GW{report['training_gameweeks'][0]}–GW{report['training_gameweeks'][-1]}, {report['training_matches']} completed PL matches")
    print(f"Expected team points: {report['expected_points']:.2f} (captain + autosubs included)")
    print(f"Median: {report['median']:.1f} | 10th–90th percentile: {report['p10']:.1f}–{report['p90']:.1f}")
    print(f"Monte Carlo standard error: {report['monte_carlo_standard_error']:.2f} points (sampling error only)")
    print(f"Captain: {report['captain']} | Vice-captain: {report['vice_captain']}")
    print(f"\n{'Player':<16} {'Pos':<4} {'Role':<10} {'E[min]':>7} {'P(play)':>8} {'xPts':>7} {'Team xPts':>10}")
    for p in report["players"]:
        print(f"{p['name']:<16} {p['position']:<4} {p['role']:<10} {p['expected_minutes']:7.1f} {p['appearance_probability']:8.1%} {p['expected_points']:7.2f} {p['expected_team_contribution']:10.2f}")
    print("\nxPts = individual projection; Team xPts = contribution after bench/captain rules.")
    print("Bench priority: " + ", ".join(report["bench_order"]))
    if report["warnings"]:
        print("\nData notes:")
        for note in report["warnings"]:
            print("- " + note)
    print("\nModel assumptions:")
    for note in report["assumptions"]:
        print("- " + note)
    if args.json_output:
        print(f"\nJSON report: {args.json_output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
