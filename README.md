# MT5-Trader

A **spread price-ladder trading terminal for MetaTrader 5** — a manual tool. No
strategy, no signals, no automatic entries or exits. A human looks at a ladder
of spread prices, clicks a price, and an order exists at that price.

Each ladder trades one pair of instruments across **two MT5 accounts** (Leg A on
account 1, Leg B on account 2) and displays the spread between them as a single
instrument, the way a CQG or TT inter-product ladder does.

## The specification

**[`MT5_TRADER_LADDER_PROMPT.md`](MT5_TRADER_LADDER_PROMPT.md)** is the full
build specification. Read it before writing code. Sections most likely to be
skimmed and shouldn't be:

| Section | Why |
|---|---|
| §2 | The spread and sizing arithmetic — it inverts the hedge if guessed |
| §3.3 | Fidelity to the reference screen; it overrides the tables above it |
| §4 | A resting spread order is synthetic — the central engineering problem |
| §5 | Marks at the closing touch; the bid-ask is charged once |
| §16 | Monitoring positions — the positions / working-orders / fills monitor |
| §17 | The Market Grid — quote and trade per row, and the ladder launcher |
| Hard rules | Repeat them back before starting |

## What is built so far

The build order in §14 of the spec, in order. Steps 1-4 are in:

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
| `mt5trader/shutdown.py` | An unanswered prompt means NO |

Still to come: the synthetic LIMIT path (quote one leg, cross the other,
re-peg by MODIFY), reconciliation, the ladder UI, the positions monitor
(§16) and the Market Grid (§17).

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

They must pass before any commit, and LIVE mode must never be run
without them. Everything is faked — `FakeBroker` keeps a real
hedging-mode book, so an opposite market order opens a SECOND position
exactly as it does on the real accounts. No MT5, no network, no clock.

## Reference screen

The UI target is a TT inter-product ladder screen supplied by the operator.
Drop it at `docs/reference/tt_ladder.png` — §3.3 transcribes it, and the
Playwright layout test checks against it.
