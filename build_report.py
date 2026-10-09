#!/usr/bin/env python3
"""Regenerate the current-team forecast, transfer search, and one readable report.

    python3 build_report.py
    python3 build_report.py --bank 1.5 --free-transfers 2
    python3 build_report.py --from-json

The default recomputes both analyses, avoiding a report assembled from stale JSON.
HTML is self-contained and printable; Markdown is the same report for GitHub.
Neither this script nor its analysis subprocesses makes transfers or calls FPL.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent


def money(value):
    return f"{'−' if value < 0 else ''}£{abs(value):.1f}m"


def signed(value):
    return f"{value:+.2f}"


def text_block(text):
    return {"kind": "paragraph", "text": text}


def table(headers, rows):
    return {"kind": "table", "headers": headers, "rows": rows}


def section(title):
    return {"kind": "heading", "text": title}


def fixture_text(player):
    return "; ".join(f"GW{f['gameweek']}: {', '.join(f['opponents']) or 'Blank'}" for f in player.get("fixtures", []))


def validate_inputs(simulation, transfers, team_path, data_path=None):
    # The two methods have different expected totals, but must refer to the same
    # season, target, squad and data revision. Never silently splice old outputs.
    for key in ("season", "gameweek", "training_gameweeks", "training_matches"):
        if key not in simulation and key not in transfers:
            continue
        if simulation.get(key) != transfers.get(key):
            raise ValueError(f"Analysis outputs disagree on {key}; rerun without --from-json")
    digest = hashlib.sha256(team_path.read_bytes()).hexdigest()
    for label, report in (("Simulation", simulation), ("Transfers", transfers)):
        if report.get("team_sha256") != digest:
            raise ValueError(f"{label} JSON does not match the current team file; rerun without --from-json")
    if simulation.get("data_revision") != transfers.get("data_revision"):
        raise ValueError("Analyses use different data revisions; rerun without --from-json")
    if data_path is not None:
        try:
            current = subprocess.run(["git", "-C", str(data_path.parent), "rev-parse", "HEAD"],
                                     capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return  # A downloaded ZIP can still be analysed without a Git binary.
        if current.returncode == 0 and current.stdout.strip() != transfers.get("data_revision"):
            raise ValueError("Saved analyses predate the checked-out data revision; rerun without --from-json")


def make_document(sim, transfers, created):
    gw, horizon = transfers["gameweek"], transfers["horizon"]
    weeks = list(range(gw, gw + horizon))
    span = f"GW{gw}" if horizon == 1 else f"GW{gw}–{weeks[-1]}"
    moves = transfers["moves"]
    best = moves[0] if moves else None
    squad_lookup = {p["name"]: p for p in transfers["current_team"]["players"]}
    source_rev = transfers.get("data_revision") or "Unavailable"
    deadline = datetime.fromisoformat(sim["deadline"].replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Dublin"))
    blocks = [text_block(f"{transfers['season']} • {span} • Generated {created} • Deadline {deadline.strftime('%d %B %Y, %H:%M %Z')}"),
              {"kind": "metrics", "items": [
                  ("Current team · next GW", f"{sim['expected_points']:.1f} pts"),
                  ("Simulated middle 80%", f"{sim['p10']:.0f}–{sim['p90']:.0f} pts"),
                  ("Best move · net horizon gain", f"{signed(best['net_gain'])} pts" if best else "Hold")]}]
    blocks.append(section("Decision to consider"))
    if best:
        blocks.append(text_block(f"The highest-ranked package in this search is **{', '.join(best['out'])} → {', '.join(best['in'])}**. "
                                 f"It adds an estimated **{best['net_gain']:.2f} team points over {span} after a {best['hit']:.0f}-point hit**, "
                                 f"leaving {money(best['bank_after'])} in the bank under the assumptions below."))
        blocks.append(text_block("Treat this as a shortlist to check before the deadline. The ranking depends on provisional selling prices, "
                                 "local injury flags and a small sample of matches; it is not a calibrated forecast."))
        if best["net_gain"] < 2:
            blocks.append(text_block("The leading edge is small. Holding the transfer is a reasonable alternative, especially if availability news changes."))
    else:
        blocks.append(text_block("**Hold the current squad.** No tested package improves the projected team score after points hits under the supplied budget assumptions."))
    blocks.append(text_block(f"Budget assumed: **{money(transfers['bank'])}**. Free transfers assumed: **{transfers['free_transfers']}**. "
                             "Listed squad prices are provisional sale proceeds unless explicitly overridden. "
                             "Check the selling values shown in FPL before acting. All proposed packages are made now and then held through the horizon; future transfers are not planned."))

    blocks.append(section("Current team and selection"))
    blocks.append(text_block(f"The Monte Carlo forecast for GW{gw} is **{sim['expected_points']:.2f} points**, including captaincy and automatic substitutions. "
                             f"Median {sim['median']:.0f}; 10th–90th percentile {sim['p10']:.0f}–{sim['p90']:.0f}. "
                             f"Captain **{sim['captain']}**, vice-captain **{sim['vice_captain']}**."))
    blocks.append(text_block("Starting XI: " + ", ".join(sim["starting_xi"]) + ". Bench order (reserve goalkeeper separate in use): " + ", ".join(sim["bench_order"]) + "."))
    rows = []
    for p in sim["players"]:
        target = squad_lookup[p["name"]]
        rows.append([p["name"], p["position"], p["role"], money(target["selling_price"]),
                     f"{p['expected_points']:.2f}", f"{target['total_points']:.2f}",
                     f"{p['expected_minutes']:.0f}", f"{p['availability']:.0%}"])
    blocks.append(table(["Player", "Pos", "Selection", "Assumed sale", f"GW{gw} MC xPts", f"{span} screen xPts", "Next GW minutes", "Availability"], rows))
    blocks.append(text_block("Player xPts above are before captain doubling and bench selection. Availability is the local fitness flag; "
                             "expected minutes also reflect recent starts and nonappearances. The multiweek screen is an analytic approximation, so its values differ slightly from the simulation."))
    current = transfers["current_team"]
    blocks.append(table(["Scenario"] + [f"GW{w}" for w in weeks] + ["Total before hits"],
                        [["Hold — comparison baseline"] + [f"{v:.2f}" for v in current["weekly_points"]] + [f"{current['total_points']:.2f}"]]))

    blocks.append(section("Possible transfers"))
    blocks.append(text_block("Every gain below compares the updated squad with the **same analytic holding baseline**, including its best legal XI, captaincy and bench cover. "
                             "It is not simply incoming-player points minus outgoing-player points. Costs and club limits apply to the complete package."))
    displayed_incoming = set()
    for count, label in ((1, "One-player moves"), (2, "Two-player packages")):
        by_size = transfers.get("single_moves" if count == 1 else "pair_moves", moves)
        chosen = [m for m in by_size if len(m["out"]) == count][:6]
        blocks.append({"kind": "subheading", "text": label})
        if not chosen:
            blocks.append(text_block("No positive-net package of this size appears in the returned shortlist."))
            continue
        rows = []
        for m in chosen:
            displayed_incoming.update(m["in_ids"])
            rows.append([", ".join(m["out"]), ", ".join(m["in"]), money(m["cost"]),
                         money(m["bank_after"]), f"{m['gross_gain']:+.2f}", f"{m['hit']:.0f}", f"{m['net_gain']:+.2f}"])
        blocks.append(table(["Out", "In", "Net spend", "Bank left", "Gross gain", "Hit", "Net gain"], rows))
    blocks.append(text_block("A negative net spend releases money. Only positive net gains are shown; holding is always available. "
                             "A saved free transfer has future option value that the model does not price."))
    if best:
        blocks.append({"kind": "subheading", "text": "Leading package by gameweek"})
        blocks.append(table(["Scenario"] + [f"GW{w}" for w in weeks] + ["After-hit total"], [
            ["Hold"] + [f"{v:.2f}" for v in current["weekly_points"]] + [f"{current['total_points']:.2f}"],
            ["Leading package (GW values before hit)"] + [f"{v:.2f}" for v in best["weekly_points"]] + [f"{best['projected_total'] - best['hit']:.2f}"]]))
        lineup = best["weekly_lineups"][0]
        blocks.append(text_block(f"After this package in GW{gw}: captain **{lineup['captain']}**, vice **{lineup['vice_captain']}**. "
                                 "XI: " + ", ".join(lineup["starting_xi"]) + ". Bench: " + ", ".join(lineup["bench_order"]) + "."))
        if best.get("reason"):
            blocks.append(text_block(best["reason"]))

    blocks.append(section("Players to watch by position"))
    blocks.append(text_block("These are the highest projected unowned players, regardless of whether you can afford them in a direct swap. "
                             "Use the package tables above for budget-checked options. All prices are the local snapshot's buying prices."))
    for pos, label in (("GKP", "Goalkeepers"), ("DEF", "Defenders"), ("MID", "Midfielders"), ("FWD", "Forwards")):
        candidates = sorted((p for p in transfers["players"] if not p["owned"] and p["position"] == pos),
                            key=lambda p: (-p["total_points"], p["name"]))[:5]
        blocks.append({"kind": "subheading", "text": label})
        blocks.append(table(["Player", "Club", "Buy", f"{span} xPts", "Availability", "Fixtures"],
                            [[p["name"], p["club"], money(p["price"]), f"{p['total_points']:.2f}",
                              f"{p['availability']:.0%}", fixture_text(p)] for p in candidates]))

    blocks.append(section("Availability and checks before acting"))
    notes = [note for note in dict.fromkeys(sim.get("warnings", []) + transfers.get("warnings", []))
             if "candidate_availability_notes" not in note]
    for p in transfers["players"]:
        if p["pid"] in displayed_incoming and (p["availability"] < 1 or p.get("news")):
            notes.append(f"Suggested incoming player {p['name']} ({p['club']}): {p['availability']:.0%} availability. {p.get('news') or 'No further news supplied.'}")
    blocks.append({"kind": "list", "items": notes or ["No warnings were reported by the input checks."]})
    blocks.append(text_block("Before making a move, confirm your bank balance, free transfers, selling prices and the latest team news in FPL. "
                             "No transfer has been submitted by these scripts."))

    blocks.append(section("Method and limitations"))
    blocks.append(text_block(f"Training uses {transfers['training_matches']} completed Premier League matches from gameweeks "
                             f"{', '.join(map(str, transfers['training_gameweeks']))}. The current-team result uses {sim['iterations']:,} simulations "
                             f"with seed {sim['seed']}; its Monte Carlo standard error is {sim['monte_carlo_standard_error']:.2f} points. "
                             "This measures simulation noise only, not model accuracy."))
    search = transfers["search"]
    blocks.append(text_block(f"Transfer search: {search['single_moves_evaluated']:,} single moves and {search['pair_moves_evaluated']:,} two-player packages evaluated. "
                             f"Two-player search uses a bounded shortlist ({search['shortlist_per_outgoing']} candidates per outgoing player); "
                             "the leading package is the best found in this search, not a proof of the global optimum."))
    blocks.append({"kind": "subheading", "text": "Transfer-screen assumptions"})
    blocks.append({"kind": "list", "items": transfers.get("assumptions", [])})
    blocks.append({"kind": "subheading", "text": "Current-team simulation assumptions"})
    blocks.append({"kind": "list", "items": sim.get("assumptions", [])})
    blocks.append(text_block("The Monte Carlo model shares goal events within a match; the faster screening model uses marginal expectations and independent "
                             "player appearance probabilities. Neither method has been backtested or calibrated. Future injury recovery, transfers, price changes and "
                             "unobserved rotation risk are not forecast."))

    blocks.append(section("Sources and reproduction"))
    blocks.append(text_block("Data: [FPL-Core-Insights](https://github.com/olbauday/FPL-Core-Insights), pinned as a Git submodule. "
                             f"Data revision: `{source_rev}`. Team input SHA-256: `{transfers['team_sha256']}`."))
    blocks.append(text_block("Rules: [official scoring guide](https://www.premierleague.com/en/news/2174909) and "
                             "[official transfer guide](https://www.premierleague.com/en/news/2174907), checked 9 October 2026. "
                             "Four points are deducted for each transfer beyond the free allowance; selling proceeds can differ from the current buying price."))
    blocks.append({"kind": "code", "text": "python3 build_report.py\n# Change assumptions when your actual values are known:\npython3 build_report.py --bank 1.5 --free-transfers 2\n# Pull upstream data, then regenerate:\ngit submodule update --remote --merge FPL-Core-Insights\npython3 build_report.py"})
    return {"title": "FPL squad and transfer report", "blocks": blocks}


def markdown_text(value):
    # Escape table separators and line breaks from names/news in external CSVs.
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(document):
    output = ["# " + document["title"]]
    for b in document["blocks"]:
        kind = b["kind"]
        if kind == "heading":
            output.append("## " + b["text"])
        elif kind == "subheading":
            output.append("### " + b["text"])
        elif kind == "paragraph":
            output.append(b["text"])
        elif kind == "list":
            output.append("\n".join("- " + str(i).replace("\n", " ") for i in b["items"]))
        elif kind == "code":
            output.append("```sh\n" + b["text"] + "\n```")
        elif kind == "metrics":
            output.append(" · ".join(f"**{label}: {value}**" for label, value in b["items"]))
        elif kind == "table":
            rows = [b["headers"], ["---"] * len(b["headers"])] + b["rows"]
            output.append("\n".join("| " + " | ".join(markdown_text(c) for c in row) + " |" for row in rows))
    return "\n\n".join(output) + "\n"


def inline(value):
    # Escape raw HTML first. Only generated emphasis/code and HTTPS links render.
    value = html.escape(str(value))
    value = re.sub(r"`([^`]+)`", r"<code>\1</code>", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", value)
    return re.sub(r"\[([^\]]+)\]\((https://[^\s)]+)\)", r'<a href="\2">\1</a>', value)


def render_html(document):
    content = []
    for b in document["blocks"]:
        kind = b["kind"]
        if kind in ("heading", "subheading"):
            tag = "h2" if kind == "heading" else "h3"
            content.append(f"<{tag}>{inline(b['text'])}</{tag}>")
        elif kind == "paragraph":
            content.append(f"<p>{inline(b['text'])}</p>")
        elif kind == "list":
            content.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in b["items"]) + "</ul>")
        elif kind == "code":
            content.append("<pre><code>" + html.escape(b["text"]) + "</code></pre>")
        elif kind == "metrics":
            content.append('<div class="metrics">' + "".join(f'<div><span>{inline(label)}</span><strong>{inline(value)}</strong></div>' for label, value in b["items"]) + "</div>")
        elif kind == "table":
            content.append('<div class="table-wrap"><table><thead><tr>' + "".join(f"<th>{inline(c)}</th>" for c in b["headers"]) + "</tr></thead><tbody>" +
                           "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>" for row in b["rows"]) + "</tbody></table></div>")
    return '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>FPL squad and transfer report</title>
<style>
:root{--ink:#183329;--muted:#57665e;--accent:#187b59;--line:#d9e2db;--paper:#fffefa;--tint:#edf4ee}
*{box-sizing:border-box}body{margin:0;background:#e9eeea;color:var(--ink);font:16px/1.65 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1180px;margin:36px auto;padding:52px 58px;background:var(--paper);box-shadow:0 10px 45px #18332912;border-top:8px solid var(--accent)}
.eyebrow{font-size:12px;letter-spacing:.18em;font-weight:750;color:var(--accent);text-transform:uppercase}h1{font-size:42px;line-height:1.15;margin:12px 0 20px;letter-spacing:-.04em}
h2{font-size:25px;line-height:1.3;margin:40px 0 15px;padding-top:22px;border-top:1px solid var(--line);letter-spacing:-.025em}h3{font-size:18px;margin:26px 0 10px}p{margin:12px 0}a{color:var(--accent);text-underline-offset:3px}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:15px;margin:28px 0}.metrics>div{background:var(--tint);border-radius:7px;padding:20px}.metrics span{display:block;font-size:12px;color:var(--muted);font-weight:600}.metrics strong{display:block;font-size:31px;line-height:1.25;margin-top:8px;letter-spacing:-.035em}
.table-wrap{width:100%;overflow:auto;margin:18px 0}table{width:100%;border-collapse:collapse;font-size:13px;line-height:1.45}th{background:var(--ink);color:#fff;text-align:left;padding:11px 10px;font-weight:650}td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top}tbody tr:nth-child(even){background:#f4f7f1}td:first-child{font-weight:600}li{margin:8px 0}code{font-size:.85em;overflow-wrap:anywhere}pre{background:var(--tint);padding:20px;border-radius:5px;overflow:auto;line-height:1.55}footer{margin-top:36px;color:var(--muted);font-size:12px}
@media(max-width:760px){main{margin:0;padding:30px 20px;box-shadow:none}h1{font-size:32px}.metrics{grid-template-columns:1fr}.metrics>div{padding:15px}.metrics strong{font-size:27px}table{min-width:620px}}
@media print{@page{size:A4 landscape;margin:13mm}body{background:white;font-size:10pt}main{padding:0;margin:0;max-width:none;box-shadow:none;border:0}h1{font-size:25pt}h2{font-size:17pt;break-after:avoid}h3{break-after:avoid}.metrics{margin:14px 0}.metrics strong{font-size:23pt}.table-wrap{overflow:visible}table{font-size:8pt;min-width:0}thead{display:table-header-group}tr{break-inside:avoid}th,td{padding:6px}pre{white-space:pre-wrap}a{color:inherit}footer{margin-top:20px}}
</style></head><body><main><div class="eyebrow">FPL planner · squad intelligence</div><h1>''' + html.escape(document["title"]) + "</h1>" + "\n".join(content) + '<footer>Generated locally from versioned inputs. No account changes or transfers have been made.</footer></main></body></html>\n'


def run_analysis(command):
    print("Running " + Path(command[1]).name + "…", flush=True)
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--team", type=Path, default=ROOT / "current-team.md")
    parser.add_argument("--data", type=Path, default=ROOT / "FPL-Core-Insights" / "data")
    parser.add_argument("--season")
    parser.add_argument("--gameweek", type=int)
    parser.add_argument("--horizon", type=int, default=3)
    parser.add_argument("--bank", type=float, default=0)
    parser.add_argument("--free-transfers", type=int, default=1)
    parser.add_argument("--selling-prices", type=Path, help="JSON mapping player names/IDs to actual sale values")
    parser.add_argument("--iterations", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--lookback", type=int, default=8)
    parser.add_argument("--top", type=int, default=30, help="Number of transfer packages retained")
    parser.add_argument("--from-json", action="store_true", help="Use existing matching analysis JSONs")
    parser.add_argument("--simulation-json", type=Path, default=ROOT / "simulation-results.json")
    parser.add_argument("--transfers-json", type=Path, default=ROOT / "transfer-results.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    args = parser.parse_args(argv)
    try:
        if not args.from_json:
            common = ["--team", str(args.team), "--data", str(args.data), "--lookback", str(args.lookback)]
            if args.season:
                common += ["--season", args.season]
            if args.gameweek is not None:
                common += ["--gameweek", str(args.gameweek)]
            run_analysis([sys.executable, str(ROOT / "simulate_points.py")] + common +
                         ["--iterations", str(args.iterations), "--seed", str(args.seed), "--json-output", str(args.simulation_json)])
            command = [sys.executable, str(ROOT / "find_transfers.py")] + common + [
                "--horizon", str(args.horizon), "--bank", str(args.bank), "--free-transfers", str(args.free_transfers),
                "--top", str(args.top), "--json-output", str(args.transfers_json)]
            if args.selling_prices:
                command += ["--selling-prices", str(args.selling_prices)]
            run_analysis(command)
        sim = json.loads(args.simulation_json.read_text(encoding="utf-8"))
        transfers = json.loads(args.transfers_json.read_text(encoding="utf-8"))
        validate_inputs(sim, transfers, args.team, args.data)
        created = datetime.now(ZoneInfo("Europe/Dublin")).strftime("%d %B %Y, %H:%M %Z")
        document = make_document(sim, transfers, created)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for extension, render in (("html", render_html), ("md", render_markdown)):
            path = args.output_dir / f"fpl-report.{extension}"
            path.write_text(render(document), encoding="utf-8")
            print(f"Report: {path}")
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
