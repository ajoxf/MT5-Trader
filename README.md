# MT5-Trader

A **spread price-ladder trading terminal for MetaTrader 5** — a manual tool. No
strategy, no signals, no automatic entries or exits. A human looks at a ladder
of spread prices, clicks a price, and an order exists at that price.

Each ladder trades one pair of instruments across **two MT5 accounts** (Leg A on
account 1, Leg B on account 2) and displays the spread between them as a single
instrument, the way a CQG or TT inter-product ladder does.

## The specification

The full build specification is held privately, outside this repository.
Ask the repository owner for a copy — it is required reading before
writing any code.

## What is built so far

The spec's build order, in order. Steps 1-8 are in:

| Module | What it does |
|---|---|
| `mt5trader/config.py` | Accounts, pairs, settings; the three clash refusals; atomic saves; `.env` secrets |
| `mt5trader/broker.py` | The ONLY module that imports MetaTrader5 — every MT5 quirk lives here |
| `mt5trader/leg_runner.py`, `ipc.py`, `legs.py` | One process per account, JSON-lines over localhost, `LocalLeg`/`RemoteLeg` behind one interface |
| `mt5trader/spread.py` | The spread, the executable touches, the staleness and jump guards |
| `mt5trader/sizing.py` | `L_B = L_A x C_A / (beta x C_B)` and the one `k` |
| `mt5trader/hedgeratio.py` | Beta stamped with the pair it was computed for |
| `mt5trader/costs.py` | The round trip, split into crossing (already in the prices) and commission (not) |
| `mt5trader/executor.py` | MARKET entry both legs, the 2.0s escalation, unwind by ticket, ticket-based closes |
| `mt5trader/book.py` | Synthetic orders one-per-click, positions, net and average |
| `mt5trader/coordinator.py` | The poll loop, the guards, the sweeps, one status snapshot for every panel |
| `mt5trader/quoter.py` | The synthetic LIMIT path: quote one leg, re-peg off the OTHER leg by MODIFY, cross on fill |
| `mt5trader/reconcile.py` | Orphans and ghosts, three strikes, contract-size-correct P&L |
| `mt5trader/session.py` | The one cutoff: DAY orders die, each ladder's overnight rule decides |
| `mt5trader/shutdown.py` | An unanswered prompt means NO |
| `mt5trader/commands.py` | The web↔coordinator bridge, primed at startup so a restart never replays a command |
| `mt5trader/webapp.py` | The Flask process: it renders and it asks; it never trades |
| `mt5trader/static/`, `templates/` | The ladder, the Market Grid, the positions monitor and the settings page — self-hosted, no CDN |

The UI is the reference TT screen: window chrome and a bottom taskbar,
the quote strip, the left control rail, and the five-column grid
(`Work | Bids | Price | Asks | LTQ`) with the black inside-market rule,
bid blue and ask red. MARKET mode re-tints the click columns and
confirms through the one shared modal — there are no native dialogs
anywhere, and a test fails the build if they come back.

Settings (accounts and pairs) is a page in the browser: it saves each
account with the clash refusals on the row, tests a terminal (naming
Algo Trading being off, or a netting account, in words), searches each
broker's symbols, and derives β, the increment, the matched-minimum
clip and the pair's minimum notional from what MT5 says — each shown
with its derivation and offered as a one-click correction, never
applied silently. It talks to the leg runners directly, so it works
with the coordinator down.

Still to come: restart recovery of open positions, per-account margin on
the monitor, and the slippage report over a real session.

## Running it

The terminals and the leg runners live on the Windows box; everything
else runs anywhere.

```
cp config.example.json config.json     # then edit accounts and pairs
cp .env.example .env                   # passwords go here, nowhere else
python start.py --config config.json   # spawns the runners + coordinator
```

Or run the pieces separately while setting up:

```
python run_leg.py --config config.json --account leg_a
python run_leg.py --config config.json --account leg_b
python run_coordinator.py --config config.json
```

## Tests

```
pytest tests/ -q
```

155 of them, including nine that drive Chromium under Playwright and
read `pageerror` — Python tests cannot see a temporal-dead-zone
`ReferenceError` that aborts a script block and silently unregisters a
handler, and that is exactly what happened in the system this is ported
from. They skip cleanly where no browser is installed.

They must pass before any commit, and LIVE mode must never be run
without them. Everything is faked — `FakeBroker` keeps a real
hedging-mode book, so an opposite market order opens a SECOND position
exactly as it does on the real accounts. No MT5, no network, no clock.

## Reference screen

The UI target is a TT inter-product ladder screen supplied by the operator.
Drop it at `docs/reference/tt_ladder.png` — §3.3 transcribes it, and the
Playwright layout test checks against it.
