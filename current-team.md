# Current FPL team

Last updated: 6 November 2026

Snapshot read from the signed-in FPL Transfers page for Gameweek 10 (team: Old Havertz Kai Hard). Prices are the single price shown per player on that page; the page does not separate current price from selling price, so treat them as provisional selling values. Fixtures are as shown on the page.

Gameweek: 10  
Bank: £3.4m  
Free transfers: 2  
Chips: Wildcard played GW4; Triple Captain played GW3; Free Hit and Bench Boost available

| Player | Club | Price | Opponent | Venue | Selection |
| --- | --- | --- | --- | --- | --- |
| Raya | Arsenal | £6.1m | Hull City | Home | GK |
| Scherpen | Ipswich Town | £4.4m | Bournemouth | Home | GK |
| Gabriel | Arsenal | £7.9m | Hull City | Home | DEF |
| Konsa | Arsenal | £4.6m | Hull City | Home | DEF |
| Ajer | Brentford | £4.5m | Brighton | Away | DEF |
| De Cuyper | Brighton | £5.0m | Brentford | Home | DEF |
| Thomas | Coventry City | £4.0m | Everton | Away | DEF |
| Rogers | Chelsea | £7.9m | Sunderland | Away | MID |
| Lewis-Potter | Brentford | £5.5m | Brighton | Away | MID |
| Groß | Brighton | £6.1m | Brentford | Home | MID |
| Tavernier | Bournemouth | £6.4m | Ipswich Town | Away | MID |
| Mbeumo | Man Utd | £7.9m | Aston Villa | Home | MID |
| Evanilson | Bournemouth | £6.3m | Ipswich Town | Away | FWD |
| Emersonn | Ipswich Town | £5.5m | Bournemouth | Home | FWD |
| Haaland | Man City | £15.6m | Nott'm Forest | Away | FWD |

Availability flags come from the local data and may be stale. The Transfers screen does not show the starting XI, bench order or captaincy, so the simulator chooses its own.

## Simulate this squad

```sh
python3 simulate_points.py --gameweek 10
python3 build_report.py
```

To evaluate a specific selection, pass `--starting-xi`, `--bench-order`, `--captain` and `--vice-captain` to `simulate_points.py`.
