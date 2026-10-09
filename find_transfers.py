#!/usr/bin/env python3
"""Rank incoming FPL players and affordable one/two-transfer plans.

Run: python3 find_transfers.py --json-output transfer-results.json
     python3 find_transfers.py --bank 0.5 --free-transfers 2 --horizon 3
     python3 find_transfers.py --selling-prices selling-prices.json

Only the standard library and the checked-out FPL-Core-Insights CSVs are used.
All future weeks share one training cutoff: later fixtures never introduce later
results, prices or injury news. This is a deterministic screening approximation
to simulate_points.py, not a second Monte Carlo forecast or an FPL API client.
Read the comments and emitted assumptions before acting on a small point gain.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import re
import subprocess
import sys
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

import simulate_points as sim

ROOT = Path(__file__).resolve().parent
OUTFIELD = ("DEF", "MID", "FWD")
SQUAD_COUNTS = Counter({"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3})


def money_units(value):
    """Use integer tenths of £1m for exact budget checks; CSV prices are millions."""
    cleaned = re.sub(r"[£m\s]", "", str(value), flags=re.IGNORECASE)
    try:
        amount = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"Invalid price in millions: {value!r}") from exc
    if not math.isfinite(amount) or amount < 0 or abs(amount * 10 - round(amount * 10)) > 1e-7:
        raise ValueError(f"Price must be a nonnegative multiple of £0.1m: {value!r}")
    return round(amount * 10)


def poisson_tail(mean, threshold):
    """P(N >= threshold), so a threshold award is never applied to a mean count."""
    if threshold <= 0:
        return 1.0
    term = math.exp(-mean)
    below = term
    for count in range(1, threshold):
        term *= mean / count
        below += term
    return min(1.0, max(0.0, 1.0 - below))


def poisson_floor(mean, divisor):
    """E[floor(N/divisor)]; floor(E[N]/divisor) would systematically misprice saves."""
    if divisor == 2:
        # An exact parity identity is faster for goals-conceded deductions.
        return (mean - (1.0 - math.exp(-2 * mean)) / 2) / 2
    term, result = math.exp(-mean), 0.0
    # The finite tail is far below reporting precision for football event rates.
    for count in range(1, math.ceil(mean + 15 * math.sqrt(mean + 1) + 50)):
        term *= mean / count
        result += (count // divisor) * term
    return result


def project_player(player, fixtures, baseline):
    """Integrate the fitted minute mixture and Poisson event expectations.

    The marginal goal/xA allocation follows rate / team baseline, capped at one.
    Unlike a squad Monte Carlo trial we do not renormalise shares among whichever
    teammates happen to be selected. That makes each candidate's screening score
    stable across alternative squads, at the cost of a documented approximation.
    """
    weights = [w / sum(player.minute_weights) for w in player.minute_weights]
    bonus = sum(v * w for v, w in zip(player.bonus, player.bonus_weights)) / sum(player.bonus_weights)
    points = minutes = 0.0
    applicable = [f for f in fixtures if player.team in (f.home, f.away)]
    minute_play = sum(w for (duration, _), w in zip(player.minutes, weights) if duration > 0)
    # Availability is shared across fixtures within a double GW, as in simulation.
    appearance = player.availability * (1 - (1 - minute_play) ** len(applicable))
    for fixture in applicable:
        goals_for = fixture.home_xg if player.team == fixture.home else fixture.away_xg
        goals_against = fixture.away_xg if player.team == fixture.home else fixture.home_xg
        for (duration, _), probability in zip(player.minutes, weights):
            if duration <= 0:
                continue
            exposure = duration / 90
            conceded = goals_against * exposure
            score = 2.0 if duration >= 60 else 1.0
            score += sim.GOAL_POINTS[player.position] * goals_for * exposure * min(1.0, player.rates["expected_goals"] / baseline[player.team])
            score += 3 * goals_for * exposure * min(1.0, player.rates["expected_assists"] / baseline[player.team])
            if duration >= 60:
                score += sim.CS_POINTS[player.position] * math.exp(-conceded)
            if player.position in ("GKP", "DEF"):
                score -= poisson_floor(conceded, 2)
            if player.position == "GKP":
                score += poisson_floor(player.rates["saves"] * exposure, 3)
            else:
                score += 2 * poisson_tail(player.rates["defensive_contribution"] * exposure,
                                           10 if player.position == "DEF" else 12)
            # MC caps each card draw at one; its expectation is P(N >= 1).
            score += bonus - poisson_tail(player.rates["yellow_cards"] * exposure, 1)
            score -= 3 * poisson_tail(player.rates["red_cards"] * exposure, 1)
            score += exposure * (-2 * player.rates["own_goals"] + 5 * player.rates["penalties_saved"]
                                 - 2 * player.rates["penalties_missed"])
            points += player.availability * probability * score
            minutes += player.availability * probability * duration
    return {"points": points, "minutes": minutes, "appearance": appearance}


def valid_squad(players):
    return (len(players) == 15 and len({p["pid"] for p in players}) == 15
            and Counter(p["position"] for p in players) == SQUAD_COUNTS
            and max(Counter(p["club"] for p in players).values(), default=0) <= 3)


def choose_lineup(players, week):
    """Find the highest projected valid XI by trying each legal formation.

    For a fixed formation the best players in each position must be selected;
    this avoids enumerating all 1,365 eleven-player subsets for every proposal.
    """
    ranked = {pos: sorted((i for i, p in enumerate(players) if p["position"] == pos),
                         key=lambda i: (-players[i]["weekly_points"][week], players[i]["pid"]))
              for pos in SQUAD_COUNTS}
    shapes = [(d, m, 10 - d - m) for d in range(3, 6) for m in range(2, 6) if 1 <= 10 - d - m <= 3]
    options = [ranked["GKP"][:1] + ranked["DEF"][:d] + ranked["MID"][:m] + ranked["FWD"][:f]
               for d, m, f in shapes]
    xi = max(options, key=lambda ids: sum(players[i]["weekly_points"][week] for i in ids))
    bench = sorted((i for i in range(15) if i not in xi),
                   key=lambda i: (players[i]["position"] == "GKP", -players[i]["weekly_points"][week], players[i]["pid"]))
    captains = sorted(xi, key=lambda i: (-players[i]["weekly_points"][week], players[i]["pid"]))
    return xi, bench, captains[0], captains[1]


def absence_distribution(probabilities):
    """Poisson-binomial probabilities for how many starters miss out by position."""
    distribution = [1.0]
    for play in probabilities:
        updated = [0.0] * (len(distribution) + 1)
        for absent, probability in enumerate(distribution):
            updated[absent] += probability * play
            updated[absent + 1] += probability * (1 - play)
        distribution = updated
    return distribution


@lru_cache(maxsize=None)
def substitution_choices(shape, bench_positions, absent):
    """For each bench appearance mask, return the legal priority-respecting mask.

    Absent starting slots remain in the formation unless replaced, just as in
    simulate_points.substitutes. This permits another valid replacement even
    when an unavailable defender has no eligible substitute of their own.
    Cache only rules, not projections, so every proposed squad uses the same law.
    """
    feasible = []
    for mask in range(8):
        incoming = Counter(bench_positions[i] for i in range(3) if mask & (1 << i))
        size = mask.bit_count()
        for outgoing in itertools.product(*(range(min(n, size) + 1) for n in absent)):
            if sum(outgoing) != size:
                continue
            final = [shape[i] - outgoing[i] + incoming[OUTFIELD[i]] for i in range(3)]
            if 3 <= final[0] <= 5 and 2 <= final[1] <= 5 and 1 <= final[2] <= 3:
                feasible.append(mask)
                break
    priority = lambda mask: (mask.bit_count(), tuple(bool(mask & (1 << i)) for i in range(3)))
    return tuple(max((mask for mask in feasible if mask & present == mask), key=priority) for present in range(8))


def expected_week(players, week, gameweek):
    """Captain and automatic-substitution expectation under independent appearances.

    Means already include nonappearances, so starters contribute their raw mean.
    Bench players contribute their conditional-on-playing mean multiplied by the
    probability of being legally substituted. Independence is an approximation:
    correlated rotation/injuries would change these team totals.
    """
    xi, bench, captain, vice = choose_lineup(players, week)
    means = [p["weekly_points"][week] for p in players]
    play = [p["weekly_appearance_probability"][week] for p in players]
    total = sum(means[i] for i in xi) + means[captain] + (1 - play[captain]) * means[vice]
    reserve = next(i for i in bench if players[i]["position"] == "GKP")
    keeper = next(i for i in xi if players[i]["position"] == "GKP")
    total += (1 - play[keeper]) * means[reserve]
    outbench = [i for i in bench if players[i]["position"] != "GKP"]
    shape = tuple(sum(players[i]["position"] == pos for i in xi) for pos in OUTFIELD)
    bench_positions = tuple(players[i]["position"] for i in outbench)
    distributions = [absence_distribution([play[i] for i in xi if players[i]["position"] == pos]) for pos in OUTFIELD]
    # Eight states are enough for the three outfield bench players. Scores given
    # a state are conditional means, because the appearance probability is here.
    bench_states = []
    for mask in range(8):
        probability = math.prod(play[i] if mask & (1 << j) else 1 - play[i] for j, i in enumerate(outbench))
        if probability:
            bench_states.append((mask, probability))
    scores = [sum(means[i] / play[i] for j, i in enumerate(outbench) if mask & (1 << j) and play[i]) for mask in range(8)]
    for absent in itertools.product(*(range(len(d)) for d in distributions)):
        probability = math.prod(distributions[j][count] for j, count in enumerate(absent))
        if probability == 0:
            continue
        choices = substitution_choices(shape, bench_positions, absent)
        total += probability * sum(state_probability * scores[choices[mask]] for mask, state_probability in bench_states)
    return {"gameweek": gameweek, "expected_points": total,
            "starting_xi": [players[i]["name"] for i in xi], "bench_order": [players[i]["name"] for i in bench],
            "captain": players[captain]["name"], "vice_captain": players[vice]["name"]}


def project_squad(players, gameweek, horizon):
    weeks = [expected_week(players, week, gameweek + week) for week in range(horizon)]
    return {"weekly_points": [r["expected_points"] for r in weeks],
            "total_points": sum(r["expected_points"] for r in weeks), "weekly_lineups": weeks}


def selling_prices(team_rows, owned, path):
    """Absent purchase prices, Markdown prices are explicit provisional proceeds.

    FPL retains half of price rises: market value is NOT necessarily selling
    value. Overrides keyed by player name or ID let a user supply the real value.
    """
    # Apply overrides before parsing, so an explicit value can repair a missing
    # or unknown Markdown price as well as replace an outdated numeric value.
    prices = {p["pid"]: row.get("Price", "") for p, row in zip(owned, team_rows)}
    if path:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("--selling-prices must be a JSON object keyed by player name or ID")
        seen = set()
        for name, price in data.items():
            matches = [p for p in owned if p["pid"] == str(name) or sim.normalise(p["name"]) == sim.normalise(str(name))]
            if len(matches) != 1 or matches[0]["pid"] in seen:
                raise ValueError(f"Selling-price key is unknown, ambiguous or repeated: {name}")
            pid = matches[0]["pid"]
            seen.add(pid)
            prices[pid] = price
    return {pid: money_units(price) for pid, price in prices.items()}


def evaluate_move(owned, outgoing, incoming, sales, bank, free_transfers, baseline, gameweek, horizon):
    """Validate a simultaneous transfer package, then compare it with holding.

    Budget and club limits are checked AFTER both legs, allowing a cheaper sale
    to fund a dearer purchase and allowing a departing club player to free a slot.
    The hit is paid once now; this is a fixed-squad horizon, not sequential moves.
    """
    outgoing_ids = {p["pid"] for p in outgoing}
    if len(outgoing_ids) != len(outgoing) or len(incoming) != len(outgoing):
        return None
    if not outgoing_ids <= {p["pid"] for p in owned}:
        return None
    if any(p["pid"] in {q["pid"] for q in owned} for p in incoming):
        return None
    cost = sum(money_units(p["price"]) for p in incoming) - sum(sales[p["pid"]] for p in outgoing)
    if cost > bank:
        return None
    squad = [p for p in owned if p["pid"] not in outgoing_ids] + list(incoming)
    if not valid_squad(squad):
        return None
    projection = project_squad(squad, gameweek, horizon)
    hit = 4 * max(0, len(outgoing) - free_transfers)
    gross_gain = projection["total_points"] - baseline["total_points"]
    return {"out": [p["name"] for p in outgoing], "in": [p["name"] for p in incoming],
            "out_ids": [p["pid"] for p in outgoing], "in_ids": [p["pid"] for p in incoming],
            "cost": cost / 10, "bank_after": (bank - cost) / 10, "hit": hit,
            "projected_total": projection["total_points"], "gross_gain": gross_gain, "net_gain": gross_gain - hit,
            "weekly_points": projection["weekly_points"], "weekly_lineups": projection["weekly_lineups"],
            "reason": f"Reoptimising the XI and captain over {horizon} gameweeks gains {gross_gain:.2f} points before a {hit}-point transfer hit."}


def shortlist(pool, size, affordable_ceiling):
    """Keep high projections plus cheap funding options; pairs remain bounded.

    A full two-transfer search grows quadratically. Half this list is the highest
    horizon projection, and half is the best available at low price brackets.
    Singles still examine EVERY eligible player, without this shortlist filter.
    """
    pool = [p for p in pool if money_units(p["price"]) <= affordable_ceiling]
    ranked = sorted(pool, key=lambda p: (-p["total_points"], money_units(p["price"]), p["pid"]))
    chosen = ranked[: (size + 1) // 2]
    # At each price choose its best player, then sweep upward from the cheapest.
    # This keeps bench-budget releases that would disappear from a points-only list.
    best_at_price = {}
    for p in ranked:
        best_at_price.setdefault(money_units(p["price"]), p)
    for p in [best_at_price[cost] for cost in sorted(best_at_price)] + ranked:
        if p not in chosen:
            chosen.append(p)
        if len(chosen) >= size:
            break
    return chosen[:size]


def search_moves(owned, candidates, sales, bank, free_transfers, baseline, gameweek, horizon,
                 top=10, max_transfers=2, shortlist_size=8):
    pools = {pos: [p for p in candidates if p["position"] == pos] for pos in SQUAD_COUNTS}
    results, counts = [], {"single_moves_evaluated": 0, "pair_moves_evaluated": 0}
    def consider(outgoing, incoming, key):
        move = evaluate_move(owned, outgoing, incoming, sales, bank, free_transfers, baseline, gameweek, horizon)
        if move is not None:
            counts[key] += 1
            if move["net_gain"] > 1e-9:
                results.append(move)
    for outgoing in owned:
        for incoming in pools[outgoing["position"]]:
            consider([outgoing], [incoming], "single_moves_evaluated")
    if max_transfers >= 2:
        for outgoing in itertools.combinations(owned, 2):
            available = bank + sum(sales[p["pid"]] for p in outgoing)
            # A partner's cheapest eligible incoming player defines the upper
            # affordability bound. Do not require each individual leg to be funded.
            pair_pools = [pools[p["position"]] for p in outgoing]
            if any(not pool for pool in pair_pools):
                continue
            minimum_prices = [min(money_units(p["price"]) for p in pool) for pool in pair_pools]
            lists = [shortlist(pair_pools[i], shortlist_size, available - minimum_prices[1-i]) for i in range(2)]
            seen_pairs = set()
            for incoming in itertools.product(*lists):
                key = tuple(sorted(p["pid"] for p in incoming))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                consider(outgoing, incoming, "pair_moves_evaluated")
    results.sort(key=lambda m: (-m["net_gain"], len(m["in"]), m["cost"], m["in_ids"]))
    counts.update(shortlist_per_outgoing=shortlist_size, max_transfers=max_transfers)
    return results[:top], counts, [m for m in results if len(m["in"]) == 1][:top], [m for m in results if len(m["in"]) == 2][:top]


def data_revision(data):
    try:
        return subprocess.check_output(["git", "-C", str(data), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def build_report(args):
    season, gw, summary, teams, roster, history, snapshots, matches, first_rows, warnings = sim.load_data(args)
    team_rows = sim.read_team(args.team)
    owned_fitted = sim.fit_players(team_rows, roster, teams, history, snapshots, warnings)
    owned_ids = {p.pid for p in owned_fitted}
    # Exclude never-used players: positional priors alone are not enough evidence
    # to advise a transfer. Keep any owned zero-minute player so it can be sold.
    eligible = [pid for pid in roster if pid in owned_ids or any(sim.number(r.get("minutes")) > 0 for r in history.get(pid, []))]
    requests = [{"Player": roster[pid]["first_name"] + " " + roster[pid]["second_name"],
                 "Club": teams[sim.identifier(roster[pid]["team_code"])]["name"]} for pid in eligible]
    candidate_warnings = []
    fitted = sim.fit_players(requests, roster, teams, history, snapshots, candidate_warnings, validate_squad=False)
    # All weeks use the SAME matches/history/snapshot dictionaries loaded above.
    # Only the schedule changes; calling load_data for later weeks would leak data.
    weeks, raw_weeks, baseline = [], [], None
    base = args.data / season / "By Tournament" / "Premier League"
    summaries = {int(row["id"]): row for row in sim.read_csv(args.data / season / "gameweek_summaries.csv")}
    for week in range(gw, gw + args.horizon):
        if week not in summaries:
            raise ValueError(f"Horizon reaches GW{week}, outside this season; shorten --horizon")
        rows = first_rows if week == gw else sim.read_csv(base / f"GW{week}" / "fixtures.csv")
        rows = list({row["match_id"]: row for row in rows}.values())
        fixtures, baseline, fallbacks = sim.fit_team_strength(matches, rows)
        weeks.append(fixtures)
        raw_weeks.extend(rows)
    # Fit baseline once against all horizon clubs so a club with no recent match
    # still has the same regularised baseline in every projected gameweek.
    _, baseline, fallbacks = sim.fit_team_strength(matches, raw_weeks)
    if fallbacks:
        warnings.append(f"Used actual goals for {fallbacks} team-match xG blanks.")
    owned_by_id = {p.pid: p for p in owned_fitted}
    players, excluded_prices = [], 0
    for player in fitted:
        price_raw = snapshots.get(player.pid, {}).get("now_cost")
        if price_raw is None or str(price_raw).strip() == "":
            if player.pid not in owned_ids:
                excluded_prices += 1
                continue
            price = None
        else:
            price = money_units(price_raw) / 10
            if price <= 0:
                if player.pid not in owned_ids:
                    excluded_prices += 1
                    continue
                price = None
        projections = [project_player(player, fixtures, baseline) for fixtures in weeks]
        players.append({"pid": player.pid, "name": owned_by_id[player.pid].name if player.pid in owned_by_id else roster[player.pid]["web_name"],
                        "club": teams[player.team]["name"], "position": player.position, "price": price,
                        "weekly_points": [p["points"] for p in projections],
                        "total_points": sum(p["points"] for p in projections),
                        "weekly_minutes": [p["minutes"] for p in projections],
                        "weekly_appearance_probability": [p["appearance"] for p in projections],
                        "availability": player.availability, "history_games": player.history_games,
                        "news": player.news, "owned": player.pid in owned_ids,
                        "fixtures": [{"gameweek": gw + j, "opponents": [teams[f.away if player.team == f.home else f.home]["name"] + (" (H)" if player.team == f.home else " (A)")
                                                                              for f in fixtures if player.team in (f.home, f.away)]}
                                     for j, fixtures in enumerate(weeks)]})
    by_id = {p["pid"]: p for p in players}
    owned = [by_id[p.pid] for p in owned_fitted]
    if not valid_squad(owned):
        raise ValueError("Current squad must have the legal position counts, unique players, and at most three per club")
    sales = selling_prices(team_rows, owned, args.selling_prices)
    for p in owned:
        p["selling_price"] = sales[p["pid"]] / 10
    baseline_projection = project_squad(owned, gw, args.horizon)
    baseline_projection["players"] = owned
    candidates = sorted((p for p in players if not p["owned"]), key=lambda p: (-p["total_points"], p["pid"]))
    moves, search, single_moves, pair_moves = search_moves(owned, candidates, sales, money_units(args.bank), args.free_transfers,
                                 baseline_projection, gw, args.horizon, args.top, args.max_transfers, args.shortlist)
    if excluded_prices:
        warnings.append(f"Excluded {excluded_prices} candidates with no usable snapshot price.")
    warnings.append(f"No live data was fetched. All projections use local pre-GW{gw} evidence and availability held fixed throughout the horizon.")
    if candidate_warnings:
        warnings.append(f"{len(candidate_warnings)} candidate availability notices are recorded in candidate_availability_notes; inspect player availability/news before buying.")
    return {"season": season, "gameweek": gw, "horizon": args.horizon,
            "deadline": summary.get("deadline_time"), "bank": money_units(args.bank) / 10,
            "free_transfers": args.free_transfers, "team_sha256": hashlib.sha256(args.team.read_bytes()).hexdigest(),
            "data_revision": data_revision(args.data),
            "training_gameweeks": sorted({r["_gw"] for rows in history.values() for r in rows}), "training_matches": len(matches),
            "players": sorted(players, key=lambda p: (-p["total_points"], p["pid"])),
            "candidates": candidates[:args.top], "current_team": baseline_projection,
            "moves": moves, "single_moves": single_moves, "pair_moves": pair_moves, "recommended_action": "transfer" if moves else "hold", "search": search,
            "warnings": warnings, "candidate_availability_notes": candidate_warnings,
            "assumptions": [
                f"Bank is £{money_units(args.bank) / 10:.1f}m and {args.free_transfers} free transfer(s), supplied by arguments (defaults: £0.0m, one free transfer).",
                "Markdown prices are provisional selling proceeds unless overridden with --selling-prices JSON. FPL selling prices can differ from market prices because only half of a price rise is retained.",
                "Incoming prices are from the latest eligible local playerstats snapshot, already in £m. No price-change forecasts are made.",
                "All moves happen before the first gameweek; the resulting squad is held for the entire horizon. Hits cost four points per transfer beyond the supplied free transfers, paid once. No chips or later transfers are modelled.",
                "All candidate fitting uses one pre-target training cutoff. Later weeks change fixtures only; availability and minute distributions remain fixed.",
                "Deterministic screening integrates the same fitted rates and minute distributions as the simulator, but omits squad-dependent goal/assist share normalisation. Compare every transfer gain with the analytic hold baseline, not the Monte Carlo total.",
                "Each week selects the highest projected legal XI and its highest projected captain/vice; bench effects are evaluated after XI selection. Captain fallback and legal autosubs use independent appearance probabilities.",
                "Bonus is an independent historical mean, xA approximates FPL assists, and Poisson assumptions approximate defensive contributions, saves and rare events. Current availability may double-count historical injury absence.",
                f"Every eligible single swap is examined. Two-transfer plans use at most {args.shortlist} candidates per departing player, combining high projections and cheap funding options: this is not an exhaustive global two-transfer optimum.",
                "Candidates need at least one positive-minute history row and a usable price. Unknown new arrivals are omitted; positive gains are estimates rather than guaranteed improvements.",
                "Holding saves a transfer for future flexibility, which has no explicit points value in this fixed-horizon model. Very small positive gains should be treated cautiously.",
                "Rules reference: https://www.premierleague.com/en/news/2174907 (checked 9 October 2026)."]}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--team", type=Path, default=ROOT / "current-team.md")
    parser.add_argument("--data", type=Path, default=ROOT / "FPL-Core-Insights" / "data")
    parser.add_argument("--season")
    parser.add_argument("--gameweek", type=int)
    parser.add_argument("--lookback", type=int, default=8)
    parser.add_argument("--horizon", type=int, default=3)
    parser.add_argument("--bank", type=float, default=0.0, help="Money in bank, £m (default: 0)")
    parser.add_argument("--free-transfers", type=int, default=1)
    parser.add_argument("--selling-prices", type=Path, help='JSON object such as {"Haaland": 15.2}, in £m')
    parser.add_argument("--max-transfers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--shortlist", type=int, default=8, help="Candidates per departing player for pair search (default: 8)")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    if min(args.horizon, args.lookback, args.top, args.shortlist) < 1:
        parser.error("--horizon, --lookback, --top and --shortlist must be positive")
    if not 0 <= args.free_transfers <= 5:
        parser.error("--free-transfers must be between zero and five")
    try:
        money_units(args.bank)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        report = build_report(args)
        if args.json_output:
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ValueError, OSError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"{report['season']} GW{report['gameweek']}–GW{report['gameweek'] + report['horizon'] - 1} transfer screening")
    print(f"Assumed bank £{report['bank']:.1f}m | Free transfers {report['free_transfers']} | Hold projection {report['current_team']['total_points']:.2f} points")
    print("Selling proceeds use Markdown prices unless overridden. This is an analytic estimate.")
    print("\nHighest projected unowned players (affordability depends on outgoing players):")
    for p in report["candidates"]:
        print(f"{p['name']:<18} {p['position']:<3} {p['club']:<16} £{p['price']:.1f}m  {p['total_points']:.2f} pts  available {p['availability']:.0%}")
    print("\nAffordable transfer options after hits:")
    if not report["moves"]:
        print("Hold: no searched transfer plan has a positive net gain.")
    for i, move in enumerate(report["moves"], 1):
        print(f"{i:2}. {', '.join(move['out'])} -> {', '.join(move['in'])}: +{move['net_gain']:.2f} pts, hit {move['hit']}, bank £{move['bank_after']:.1f}m")
    print(f"\nEvaluated {report['search']['single_moves_evaluated']} legal affordable singles and {report['search']['pair_moves_evaluated']} shortlisted pairs.")
    print("\nAssumptions:")
    for note in report["assumptions"] + report["warnings"]:
        print("- " + note)
    if args.json_output:
        print(f"\nJSON report: {args.json_output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
