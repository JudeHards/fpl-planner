# FPL Planner

Project repository: [JudeHards/fpl-planner](https://github.com/JudeHards/fpl-planner).

Estimate your current squad, screen transfer targets, and regenerate a single
report with affordable one- and two-player packages. Everything runs locally with
Python 3.10+ and the standard library. The scripts never submit FPL transfers.

```sh
python3 build_report.py
```

Open [the combined HTML report](reports/fpl-report.html) in a browser, or read
[the Markdown report](reports/fpl-report.md) directly on GitHub. This command
refreshes both analysis JSONs before compiling the report.

```sh
# Screen players and transfers separately:
python3 find_transfers.py --json-output transfer-results.json

# Replace the default £0.0m bank / one free transfer with actual values:
python3 build_report.py --bank 1.5 --free-transfers 2

# Compile saved outputs without rerunning analyses (uses their saved assumptions):
python3 build_report.py --from-json
```

`find_transfers.py` defaults to a three-gameweek horizon and compares keeping the
squad with immediate one- and two-player changes, held for that horizon. It uses
the same fitted player and team rates as the simulation, with a faster analytic
screening approximation. All gains are relative to that method's own holding
baseline, after legal XI selection, captaincy, bench cover and transfer hits.

The script checks position quotas, player uniqueness, the three-per-club limit
and combined affordability. Two-player packages can fund an upgrade by selling a
more expensive player elsewhere. Their search uses bounded shortlists, so the
best result is the best found rather than a guaranteed global optimum. Holding
is preferable when no tested move has a positive net gain. The model does not
price the future value of saving a free transfer.

Prices in `current-team.md` are **provisional selling values**, not verified FPL
sale proceeds. To supply actual values, create a JSON object such as
`{"Palmer": 9.6, "João Pedro": 7.4}` and use
`--selling-prices selling-prices.json` with either transfer or report command.
Prices and bank values are in millions of pounds. All future projections use the
same pre-deadline training cutoff and the same local availability information;
the model does not predict injury recovery.

## GitHub and upstream data

`FPL-Core-Insights/` is a Git submodule of the original upstream repository. Your
scripts and reports live in your repository; the data is pinned to a reproducible
upstream commit. Pulling data does not overwrite the analysis scripts.

```sh
# Clone your project with its pinned data:
git clone --recurse-submodules https://github.com/JudeHards/fpl-planner.git
cd fpl-planner

# If already cloned without data:
git submodule update --init --recursive

# Refresh from upstream main, regenerate findings, and record the new pin:
git submodule update --remote --merge FPL-Core-Insights
python3 build_report.py
git add FPL-Core-Insights simulation-results.json transfer-results.json reports/
git commit -m "Refresh FPL data and findings"
git push
```

Saved analyses include the team file's SHA-256 and data commit. Report compilation
rejects mismatched outputs or a team/data checkout that has changed since they
were generated. Keep custom analysis outside the submodule to make upstream
updates straightforward.

## Current-team simulation

`simulate_points.py` estimates one gameweek's points for the 15 players in
`current-team.md`, using the local `FPL-Core-Insights/data` CSVs. Python 3.10+
is sufficient; there are no packages to install or network requests at runtime.

```sh
python3 simulate_points.py
python3 simulate_points.py --iterations 50000 --json-output simulation-results.json
python3 simulate_points.py --captain Haaland --vice-captain Mbeumo
python3 simulate_points.py --help
```

The default is the latest local season and its `is_next` gameweek. In the supplied
data this is **2026/27 GW6**, trained on **50 completed GW1–5 league matches**.
The Markdown file's clubs disambiguate names such as Thomas and Palmer. The
CSV schedule supplies the fixtures; differences from the Markdown are reported.
An explicit `--gameweek` selects another week, including blanks or double weeks.

The script selects the highest projected legal XI, then the highest projected
captain and vice-captain. Bench priority follows individual projections. A separate
pilot simulation makes these choices before the reported trials are run. It
maximises starting-player projections, rather than jointly optimising bench cover.
The final total includes captain doubling, vice-captain fallback and legal
substitutions. No chips or transfer hits are included.

To evaluate your actual selection, pass eleven comma-separated names:

```sh
python3 simulate_points.py \
  --starting-xi 'Raya,Calafiori,Ajer,De Cuyper,Thomas,Rogers,Lewis-Potter,Szoboszlai,Mbeumo,Emersonn,Haaland' \
  --bench-order 'Palmer,Konsa,João Pedro,Scherpen' \
  --captain Haaland --vice-captain Mbeumo
```

## How the estimate works

1. **Read discrete FPL gameweek statistics.** Use only the Premier League view to
   avoid counting duplicated exports and cup matches. Historical outcomes must
   precede the target gameweek and its deadline. Cumulative `playerstats.csv`
   snapshots supply availability/news only; their totals are never added together.
2. **Estimate playing time and event rates.** Bootstrap recent minutes, weighting
   each week with a three-week half-life. Fit per-90 xG, xA, saves, defensive actions
   and negative events, adding three full matches of positional-average evidence
   to stabilise small samples. Apply the latest local availability percentage.
3. **Estimate fixture strength.** Infer attack and defence from completed match xG,
   accounting for league home/away averages and adding four league-average matches
   per team. Missing match xG falls back to actual goals and triggers a note.
   Unpopulated Elo/strength columns are not treated as valid ratings.
4. **Simulate match events.** Draw team goals from Poisson distributions, then goal
   times and scorers/assisters. Unowned teammates absorb the remaining probability.
   Sharing goal events preserves teammate and opponent clean-sheet dependence.
   A substituted defender can keep a clean sheet when a later goal is conceded.
5. **Score and aggregate.** Apply appearance, positional goals, assists, clean
   sheets, save thresholds, defensive contribution thresholds, bonus and deductions
   before averaging. Pick the XI in a separate pilot, then score that fixed lineup
   across the main trials with captaincy and substitutions.

The script contains comments explaining these steps and the implementation choices.
Scoring constants follow the [official Premier League scoring guide](https://www.premierleague.com/en/news/2174909),
checked on 9 October 2026.

## Reading the output

- **Expected team points** is the mean after captaincy and substitutions.
- **10th–90th percentile** describes the middle 80% of simulated outcomes.
- **xPts** is each player's projection before captaincy or benching.
- **Team xPts** is their contribution after those rules; these sum to the team total.
- **Monte Carlo standard error** measures numerical sampling noise, not the accuracy
  of the underlying football model. More trials reduce this noise only.

The optional JSON report also includes fitted player rates, fixture xG, the chosen
lineup, availability, history coverage, model assumptions and warnings. A fixed
seed makes reruns with identical inputs and arguments reproducible.

## Practical limitations

There are only five completed weeks in the current dataset. This is an exploratory
model with no backtesting or calibration yet. It does not fetch live news.
Historical minute samples can repeat injury absences already reflected in current
availability, making some projections conservative. Conversely, a player who
played every observed week has no unobserved rotation risk in their minute sample.

Bonus is sampled from historical appearances with positional shrinkage; it is not
recomputed from match-wide BPS and is independent of simulated attacking returns.
xA approximates FPL assists. Rare cards/penalties and defensive actions use simple
independent distributions; dismissal timing and cards to unused substitutes are
not modelled. The forecast range does not include uncertainty in fitted rates.

Training excludes multi-fixture player-gameweek rows because their aggregated
minutes cannot be treated as one match. Future double weeks are simulated fixture
by fixture, with a shared availability draw. Historical CLI runs use the supplied
squad and available local snapshots; they are not a full historical backtest.

```sh
python3 -m unittest discover -s tests -v
```
