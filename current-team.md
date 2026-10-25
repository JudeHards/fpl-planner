# Current FPL team

Last updated: 25 October 2026

Snapshot transcribed from the supplied Gameweek 9 Transfers screenshot. Prices are the selling values shown on the Transfers pitch. Clubs are inferred from kits and checked against the local players.csv; fixtures are preserved as shown.

Gameweek: 9  
Bank: £3.4m  
Free transfers: 1  
Chips: Wildcard played GW4; Free Hit available

GW9 transfers (3 of 4 free transfers used): Palmer (£9.7m) → Tavernier (£6.3m) and João Pedro (£7.7m) → Evanilson (£6.2m) on 24 October 2026; Calafiori (£5.7m) → Gabriel (£7.9m) on 25 October 2026. Bank is the GW8 snapshot's £0.7m plus those sales less those purchases; the GW9 screenshot did not show it.

| Player | Club | Price | Opponent | Venue | Selection |
| --- | --- | --- | --- | --- | --- |
| Raya | Arsenal | £6.1m | Liverpool | Away | GK |
| Scherpen | Ipswich Town | £4.4m | Hull City | Away | GK |
| Gabriel | Arsenal | £7.9m | Liverpool | Away | DEF |
| Konsa | Arsenal | £4.5m | Liverpool | Away | DEF |
| Ajer | Brentford | £4.5m | Nott'm Forest | Home | DEF |
| De Cuyper | Brighton | £4.9m | Man City | Away | DEF |
| Thomas | Coventry City | £4.0m | Sunderland | Home | DEF |
| Rogers | Chelsea | £7.8m | Man Utd | Home | MID |
| Lewis-Potter | Brentford | £5.5m | Nott'm Forest | Home | MID |
| Groß | Brighton | £6.1m | Man City | Away | MID |
| Tavernier | Bournemouth | £6.3m | Leeds | Home | MID |
| Mbeumo | Man Utd | £7.9m | Chelsea | Away | MID |
| Evanilson | Bournemouth | £6.2m | Leeds | Home | FWD |
| Emersonn | Ipswich Town | £5.5m | Hull City | Away | FWD |
| Haaland | Man City | £15.6m | Brighton | Home | FWD |

Availability flags: Thomas (unspecified injury, 75%), per the live FPL API on 25 October 2026. The Transfers screen does not show the starting XI, bench order or captaincy, so the simulator chooses its own.

## Simulate this squad

```sh
python3 simulate_points.py --gameweek 9
python3 build_report.py
```

To evaluate a specific selection, pass `--starting-xi`, `--bench-order`, `--captain` and `--vice-captain` to `simulate_points.py`.
