#!/usr/bin/env python3
"""Rank incoming FPL players and the best affordable one-, two- and three-transfer plans.

Run: python3 find_transfers.py --json-output transfer-results.json
     python3 find_transfers.py --bank 0.5 --free-transfers 2 --horizon 3

Bank and free transfers default to the "Bank:" and "Free transfers:" lines in
the team file; the flags override them.
     python3 find_transfers.py --selling-prices selling-prices.json

Only the standard library and the checked-out FPL-Core-Insights CSVs are used.
All future weeks share one training cutoff: later fixtures never introduce later
results, prices or injury news. This is a deterministic screening approximation
to simulate_points.py, not a second Monte Carlo forecast or an FPL API client.
Read the comments and emitted assumptions before acting on a small point gain.
"""
from __future__ import annotations

import argparse
import bisect
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


def team_settings(path):
    """Read "Bank: £0.7m" and "Free transfers: 3" lines from the team Markdown."""
    settings = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*(Bank|Free transfers)\s*:\s*(.+?)\s*$", line, flags=re.IGNORECASE)
        if not match:
            continue
        key, value = match.group(1).lower(), match.group(2)
        if key == "bank":
            settings["bank"] = money_units(value) / 10
        elif re.fullmatch(r"\d+", value):
            settings["free_transfers"] = int(value)
        else:
            raise ValueError(f"Free transfers must be a whole number in {path}: {value!r}")
    return settings


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


SIZE_KEYS = {1: "single", 2: "pair", 3: "triple"}
RANK = lambda m: (-m["net_gain"], len(m["in"]), m["cost"], m["in_ids"])
# Branches are cut only when their bound misses the cut-off by this many points,
# covering bench-cover effects the stand-in does not bound strictly.
BOUND_SLACK = 0.5


def undominated(pool, max_transfers):
    """Drop players who can always be swapped for one no dearer and no worse.

    Y dominates X when Y costs no more and projects at least as many points in
    every horizon week. Swapping X for Y then keeps the package affordable and
    cannot lower the best XI. The swap is blocked only if Y's club is full or Y
    is another incoming player. The other 14 players fill at most four clubs, so
    X is dropped only when its dominators outside the four largest-club groups
    outnumber the other incoming slots. Dominators from X's own club are never
    blocked by the club limit. Bench cover and captain fallback depend on
    appearance probabilities as well, so this is exact for the XI, not the bench.
    """
    keep = []
    for x in pool:
        px = money_units(x["price"])
        clubs = Counter()
        for y in pool:
            if y is x or money_units(y["price"]) > px:
                continue
            if any(a < b for a, b in zip(y["weekly_points"], x["weekly_points"])):
                continue
            if money_units(y["price"]) == px and y["weekly_points"] == x["weekly_points"] and y["pid"] > x["pid"]:
                continue  # Identical players: keep exactly one.
            clubs[y["club"]] += 1
        own = clubs.pop(x["club"], 0)
        if own + sum(sorted(clubs.values(), reverse=True)[4:]) < max_transfers:
            keep.append(x)
    return keep


def price_ladder(pool):
    """Prefix bests by price: the optimistic stand-in for any affordable player.

    Each week takes the highest projection among affordable players, with that
    player's appearance probability. Pairing it with the pool's lowest
    appearance would be a strict bound, but bench cover for near-certain
    non-starters makes it far too loose to prune anything.
    """
    pool = sorted(pool, key=lambda p: money_units(p["price"]))
    prices, points, appearance = [], [], []
    for p in pool:
        if points:
            pairs = [max(old, new) for old, new in zip(zip(points[-1], appearance[-1]),
                                                       zip(p["weekly_points"], p["weekly_appearance_probability"]))]
        else:
            pairs = list(zip(p["weekly_points"], p["weekly_appearance_probability"]))
        prices.append(money_units(p["price"]))
        points.append([pts for pts, _ in pairs])
        appearance.append([play for _, play in pairs])
    return prices, points, appearance


def search_moves(owned, candidates, sales, bank, free_transfers, baseline, gameweek, horizon,
                 top=10, max_transfers=3):
    """Search every 1..max_transfers package, returning the best `top` per size.

    Singles are evaluated exhaustively. Larger packages are a branch-and-bound
    over undominated players: unfilled slots become one optimistic stand-in with
    the best weekly points of any affordable player (see price_ladder). A
    branch is cut once that bound, after hits and
    BOUND_SLACK, cannot beat the current `top`-th package of its size. The bound
    ignores club limits, so a club limit never causes a cut.
    """
    pools = {pos: [p for p in candidates if p["position"] == pos] for pos in SQUAD_COUNTS}
    best = {k: [] for k in range(1, max_transfers + 1)}
    counts = Counter()

    def threshold(k):
        return best[k][-1]["net_gain"] if len(best[k]) >= top else 1e-9

    def consider(outgoing, incoming):
        move = evaluate_move(owned, outgoing, incoming, sales, bank, free_transfers, baseline, gameweek, horizon)
        if move is None:
            return
        k = len(outgoing)
        counts[f"{SIZE_KEYS[k]}_moves_evaluated"] += 1
        if move["net_gain"] > threshold(k):
            best[k].append(move)
            best[k].sort(key=RANK)
            del best[k][top:]

    for outgoing in owned:
        for incoming in pools[outgoing["position"]]:
            consider([outgoing], [incoming])

    frontier = {pos: sorted(undominated(pool, max_transfers), key=lambda p: (-p["total_points"], money_units(p["price"]), p["pid"]))
                for pos, pool in pools.items()}
    ladders = {pos: price_ladder(pool) for pos, pool in frontier.items()}
    cheapest = {pos: ladder[0][0] if ladder[0] else None for pos, ladder in ladders.items()}
    counts.update({f"{pos}_undominated": len(pool) for pos, pool in frontier.items()})

    def bound(kept, chosen, slots, budget):
        """Net gain if each unfilled slot held its optimistic affordable stand-in."""
        stand_ins = []
        for j, pos in enumerate(slots):
            prices, points, appearance = ladders[pos]
            ceiling = budget - sum(cheapest[p] for p in slots[j + 1:])
            i = bisect.bisect_right(prices, ceiling) - 1
            if i < 0:
                return None
            stand_ins.append({"pid": f"~{j}", "name": "bound", "club": f"~{j}", "position": pos,
                              "weekly_points": points[i], "weekly_appearance_probability": appearance[i]})
            budget -= cheapest[pos]
        counts["bounds_evaluated"] += 1
        projected = project_squad(kept + chosen + stand_ins, gameweek, horizon)["total_points"]
        return projected - baseline["total_points"] - 4 * max(0, len(chosen) + len(slots) - free_transfers)

    for k in range(2, max_transfers + 1):
        roots = []
        for outgoing in itertools.combinations(owned, k):
            # Same-position slots are adjacent, so unordered incoming sets can be
            # enumerated once by requiring increasing frontier indices.
            outgoing = sorted(outgoing, key=lambda p: list(SQUAD_COUNTS).index(p["position"]))
            slots = [p["position"] for p in outgoing]
            if any(cheapest[pos] is None for pos in slots):
                continue
            out_ids = {p["pid"] for p in outgoing}
            kept = [p for p in owned if p["pid"] not in out_ids]
            budget = bank + sum(sales[p["pid"]] for p in outgoing)
            root = bound(kept, [], slots, budget)
            if root is not None:
                roots.append((root, outgoing, slots, kept, budget))
        # Most promising outgoing sets first; once a root bound fails, all later ones do.
        roots.sort(key=lambda r: -r[0])

        def extend(outgoing, slots, kept, chosen, budget, start):
            j = len(chosen)
            if j == len(slots):
                consider(outgoing, chosen)
                return
            pos = slots[j]
            reserve = sum(cheapest[p] for p in slots[j + 1:])
            clubs = Counter(p["club"] for p in kept + chosen)
            for i in range(start, len(frontier[pos])):
                player = frontier[pos][i]
                price = money_units(player["price"])
                if price > budget - reserve or clubs[player["club"]] >= 3:
                    continue
                rest = slots[j + 1:]
                if rest:
                    upper = bound(kept, chosen + [player], rest, budget - price)
                    if upper is None or upper + BOUND_SLACK <= threshold(len(slots)):
                        counts[f"{SIZE_KEYS[len(slots)]}_branches_pruned"] += 1
                        continue
                next_start = i + 1 if rest and rest[0] == pos else 0
                extend(outgoing, slots, kept, chosen + [player], budget - price, next_start)

        for root, outgoing, slots, kept, budget in roots:
            if root + BOUND_SLACK <= threshold(k):
                counts[f"{SIZE_KEYS[k]}_outgoing_sets_pruned"] += 1
                continue
            extend(outgoing, slots, kept, [], budget, 0)

    by_size = {k: moves for k, moves in best.items()}
    overall = sorted((m for moves in best.values() for m in moves), key=RANK)[:top]
    counts["max_transfers"] = max_transfers
    return overall, dict(counts), by_size


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
    moves, search, by_size = search_moves(owned, candidates, sales, money_units(args.bank), args.free_transfers,
                                          baseline_projection, gw, args.horizon, args.top, args.max_transfers)
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
            "moves": moves, **{f"{SIZE_KEYS[k]}_moves": by_size.get(k, []) for k in SIZE_KEYS}, "recommended_action": "transfer" if moves else "hold", "search": search,
            "warnings": warnings, "candidate_availability_notes": candidate_warnings,
            "assumptions": [
                f"Bank is £{money_units(args.bank) / 10:.1f}m and {args.free_transfers} free transfer(s), from {args.settings_source}.",
                "Markdown prices are provisional selling proceeds unless overridden with --selling-prices JSON. FPL selling prices can differ from market prices because only half of a price rise is retained.",
                "Incoming prices are from the latest eligible local playerstats snapshot, already in £m. No price-change forecasts are made.",
                "All moves happen before the first gameweek; the resulting squad is held for the entire horizon. Hits cost four points per transfer beyond the supplied free transfers, paid once. No chips or later transfers are modelled.",
                "All candidate fitting uses one pre-target training cutoff. Later weeks change fixtures only; availability and minute distributions remain fixed.",
                "Deterministic screening integrates the same fitted rates and minute distributions as the simulator, but omits squad-dependent goal/assist share normalisation. Compare every transfer gain with the analytic hold baseline, not the Monte Carlo total.",
                "Each week selects the highest projected legal XI and its highest projected captain/vice; bench effects are evaluated after XI selection. Captain fallback and legal autosubs use independent appearance probabilities.",
                "Bonus is an independent historical mean, xA approximates FPL assists, and Poisson assumptions approximate defensive contributions, saves and rare events. Current availability may double-count historical injury absence.",
                f"Every eligible single swap is examined. Packages of two to {args.max_transfers} transfers are searched exhaustively by branch and bound: a player is skipped only when enough others in the same position cost no more and project at least as many points every week, and a branch is cut only when an optimistic bound cannot reach the best {args.top} of its size. Dominance ignores appearance-driven bench cover, so near-ties may differ slightly from a brute-force search.",
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
    parser.add_argument("--bank", type=float, help="Money in bank, £m (default: team file's Bank line, else 0)")
    parser.add_argument("--free-transfers", type=int, help="Default: team file's Free transfers line, else 1")
    parser.add_argument("--selling-prices", type=Path, help='JSON object such as {"Haaland": 15.2}, in £m')
    parser.add_argument("--max-transfers", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    if min(args.horizon, args.lookback, args.top) < 1:
        parser.error("--horizon, --lookback and --top must be positive")
    try:
        settings = team_settings(args.team)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    sources = []
    for key, default, label in (("bank", 0.0, "bank"), ("free_transfers", 1, "free transfers")):
        if getattr(args, key) is not None:
            sources.append(f"{label} from arguments")
        elif key in settings:
            setattr(args, key, settings[key])
            sources.append(f"{label} from {args.team.name}")
        else:
            setattr(args, key, default)
            sources.append(f"{label} defaulted")
    args.settings_source = ", ".join(sources)
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
    for k, label in SIZE_KEYS.items():
        if k > args.max_transfers:
            continue
        print(f"\nBest {k}-transfer options:")
        options = report[f"{label}_moves"]
        if not options:
            print("None with a positive net gain.")
        for i, move in enumerate(options, 1):
            print(f"{i:2}. {', '.join(move['out'])} -> {', '.join(move['in'])}: +{move['net_gain']:.2f} pts, hit {move['hit']}, bank £{move['bank_after']:.1f}m")
    search = report["search"]
    print("\nEvaluated " + ", ".join(f"{search.get(f'{label}_moves_evaluated', 0):,} {label}s" for k, label in SIZE_KEYS.items() if k <= args.max_transfers)
          + f" ({search.get('bounds_evaluated', 0):,} bounds).")
    print("\nAssumptions:")
    for note in report["assumptions"] + report["warnings"]:
        print("- " + note)
    if args.json_output:
        print(f"\nJSON report: {args.json_output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
