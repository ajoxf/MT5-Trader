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

## Reference screen

The UI target is a TT inter-product ladder screen supplied by the operator.
Drop it at `docs/reference/tt_ladder.png` — §3.3 transcribes it, and the
Playwright layout test checks against it.
