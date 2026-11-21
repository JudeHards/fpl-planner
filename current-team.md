# Current FPL team

Last updated: 21 November 2026

Snapshot read from the signed-in FPL Transfers page for Gameweek 11 (deadline Sat 21 Nov, 11:00). Prices are the single price shown per player on that page; the page does not separate current price from selling price, so treat them as provisional selling values. Fixtures are as shown on the page.

Changes since Gameweek 10: Thomas (Coventry City) → Van Hecke (Spurs); Emersonn (Ipswich Town) → Barry (Everton).

Gameweek: 11  
Bank: £2.3m  
Free transfers: 1  
Chips: Wildcard played GW4; Triple Captain played GW3; Free Hit and Bench Boost available

| Player | Club | Price | Opponent | Venue | Selection |
| --- | --- | --- | --- | --- | --- |
| Raya | Arsenal | £6.1m | Newcastle | Away | GK |
| Scherpen | Ipswich Town | £4.4m | Spurs | Away | GK |
| Gabriel | Arsenal | £8.0m | Newcastle | Away | DEF |
| Konsa | Arsenal | £4.7m | Newcastle | Away | DEF |
| Ajer | Brentford | £4.5m | Everton | Home | DEF |
| Van Hecke | Spurs | £5.0m | Ipswich Town | Home | DEF |
| De Cuyper | Brighton | £5.0m | Hull City | Away | DEF |
| Rogers | Chelsea | £7.9m | Leeds | Home | MID |
| Tavernier | Bournemouth | £6.5m | Nott'm Forest | Home | MID |
| Lewis-Potter | Brentford | £5.5m | Everton | Home | MID |
| Groß | Brighton | £6.2m | Hull City | Away | MID |
| Mbeumo | Man Utd | £7.8m | Liverpool | Away | MID |
| Evanilson | Bournemouth | £6.3m | Nott'm Forest | Home | FWD |
| Barry | Everton | £5.6m | Brentford | Away | FWD |
| Haaland | Man City | £15.6m | Fulham | Home | FWD |

Availability flags come from the local data and may be stale. The Transfers screen does not show the starting XI, bench order or captaincy, so the simulator chooses its own.

## Simulate this squad

```sh
python3 simulate_points.py --gameweek 11
python3 build_report.py
```

To evaluate a specific selection, pass `--starting-xi`, `--bench-order`, `--captain` and `--vice-captain` to `simulate_points.py`.
