# Running MT5-Trader on the EC2 box

Written for whoever sits in front of it, not for whoever built it.

## Getting the software onto the machine

Once:

```
cd C:\
git clone https://github.com/ajoxf/MT5-Trader.git
cd MT5-Trader
git checkout claude/monitoring-positions-market-grid-17nyys
powershell -ExecutionPolicy Bypass -File deploy\bootstrap.ps1
```

The bootstrap installs Python only if the machine has none: if you
already have one — a conda environment, a venv, anything with `python`
on PATH — it uses that and says which. If you would rather do it by
hand, the bootstrap is only these four lines:

```
python -m pip install -r requirements.txt
copy config.example.json config.json
copy .env.example .env
python -m pytest tests -q
```

To update it later:

```
cd C:\MT5-Trader
git pull
```

`git pull` never touches `config.json`, `.env` or `mt5trader.db` —
those are yours and are not in the repository.

**Restart after a pull.** The engine reads its code when it starts, so
a pull into a running instance leaves the old one working. Close the
black window and double-click START TRADING again (or Ctrl-C and
`python start.py`). The browser needs nothing: the page stamps its own
CSS and JS, and the HTML is re-read per request.

## Every day

1. Connect to the machine (RDP, or your usual shortcut).
2. Check **both MetaTrader 5 terminals** are open and logged in, and
   that the **Algo Trading** button in each is green. If a terminal is
   closed, open it — it logs itself in.
3. Double-click **START TRADING** on the desktop. (Or, in a terminal:
   `python start.py` — same thing, one file, no arguments. Where the
   python.org launcher is installed, `py -3.11 start.py` does the same;
   in a conda prompt there is usually no launcher, and `python` is the
   one that answers. `python -3.11` is not a thing — the `-3.11` is the
   LAUNCHER's argument, and `python` exits with `Unknown option: -3`.)
4. Wait for the browser to open by itself. At the top of the Exchanges
   page it says either **CONNECTED — you can trade** or **NOT READY**
   with the one thing to fix.
5. Leave the black window open. Closing it stops trading.

At the end of the day, close the black window. It asks before closing
any open positions, and an unanswered question means **no** — positions
stay at the broker and come back when you start again.

## When something is wrong

The Exchanges page has three buttons on each account. Use them in this
order; each one says what to do next, in words.

| Button | Asks | Typical answer |
|---|---|---|
| **Connect** | Is the leg runner there, and is the terminal logged in? | "Start it: python run_leg.py …" or "log the terminal in" |
| **Test** | Can this account trade? | "Algo Trading is OFF — press the button in that terminal (it turns green)" |
| **Diagnose** | Everything, including whether the two legs fit each other | a symbol that has been renamed, a contract that has expired, a hedge ratio left behind by another instrument |

Nothing here sends you to a log file. If an order is refused, the
refusal appears on the ladder in the broker's own words.

## The rules the software will not let you break

- **A market click goes immediately.** There is no "are you sure" on
  the ladder, by design. The mode badge, the tinted columns and the
  crosshair cursor tell you the ladder is armed.
- **Flatten and KILL ALL ask once.** They cannot be undone.
- **The engine refuses clicks when it is not running.** A dead engine
  never looks like a quiet market.
- **Positions survive a restart.** They are written to the database as
  they happen and recovered when the engine starts. Until it has
  checked both accounts, it will not close anything by itself.
- **Anything at the broker it cannot explain is left alone** and listed
  on a red banner for you to adopt or close by hand.

## The ladder, in one paragraph

The **Asks** column buys the spread (buy leg B, sell leg A); the
**Bids** column sells it. Clicking at the touch crosses immediately in
MARKET mode; clicking away from the touch leaves a **working order** at
that price, and the message says which it did. The ladder keeps the mid
in the middle and re-centres every few seconds, except while you are
scrolling it by hand — and **Lock** on the rail stops it moving at all.
The taskbar has **Sound** and **Keys** switches, and one badge that is
green only when both accounts answered and every ladder is quoting.

## "QUOTES STALE"

The badge turns amber when one leg's quote has not CHANGED for longer
than `MAX_QUOTE_AGE_SEC` (15 seconds by default — 5 was tuned on a
futures feed that ticks constantly, and fired all day on a CFD account
that is simply slow between trades). It is not about the
connection — a leg that is being re-sent unchanged still reads as
stale, which is the point: the spread is a difference, so one frozen
leg makes the whole number fictitious while the other ticks perfectly.

Which leg it is, is on the ladder's footer: each leg's bid, ask, width
and the age of its last change, with the stale one in red.

The three ordinary causes, in order of likelihood:

1. **That instrument is out of session** — a future overnight, a
   holiday, the daily break. Nothing to fix; it clears when the market
   opens.
2. **That terminal has lost its feed.** Look at the symbol in that
   MT5's Market Watch: if the numbers are frozen there too, it is the
   terminal, not this software. Re-log that terminal in.
3. **The instrument genuinely ticks slowly.** A CFD or an illiquid
   future can be quiet for a good deal longer than fifteen seconds
   between trades. Raise `MAX_QUOTE_AGE_SEC` in Settings → Trading if
   that is what you are trading — deliberately, because the guard
   exists to stop an order being priced off a book that has stopped
   moving. The badge carries the number it tripped at, so you can set
   the limit from what you actually see rather than from a guess.

While it is stale, ORDERS are withheld and say so. Closing is never
withheld: a guard may hold back an order, and must never prevent a
close.

## More than one spread

Each pair is its own ladder and they run side by side. Add one on
**Exchanges → Pairs → New pair**, pick each leg's account and symbol,
press **Read both legs from MT5**, then Save. The **+** button lists
every ladder to open.

## Margin

The **Accounts** tab of the Positions window shows each account's
equity, margin used and free, and its margin level against the broker's
own margin-call and stop-out levels. Two brokers means two separate
margin pools: the pair can only be carried by the **weaker** of the
two, so that account is named at the top. A level under the level you
set is marked in red.

Equity, not balance, is what the broker actually holds of yours —
brokers often fund a demo with credit, so a balance of 0.00 against
real equity is normal.

## Slippage

The **Slippage** tab of the Positions window reports the session you are
in — cut at the 16:55 cutoff on the **broker's** clock, so it lines up
with the day MT5 stamps its deals in.

Every number there was measured against the price that was clicked:
entries against the touch the click was taken at, exits against the
touch the close was sent at, in spread points and in money. Positive is
a **cost**, at both ends.

Three things to read it for:

- **MARKET against LIMIT**, side by side. That is what the peg has to
  justify itself with: a LIMIT that is not beating a market click on
  the same ladder is costing time for nothing.
- **The worst entries**, ranked in money rather than points — two
  ladders with different clip sizes are not comparable in points.
- **The unmeasured column.** A fill that could not be priced is counted
  there and never averaged in as zero. If that column is not empty, the
  average beside it is over fewer trades than the session had.

`Export CSV` gives one row per position, both ends. Empty cells, not
zeros, where nothing was measured.

## Times

The session cutoff (`16:55` by default) runs on the **broker's clock**,
not the machine's. The Exchanges page shows the broker's time and how
far it is from this machine, so the number you configure is the number
you see on the terminal.

## What to keep

If the machine has to be rebuilt, these are the files that matter, in
the folder the software runs from:

- `config.json` — the accounts and pairs
- `.env` — the passwords (nothing else has them)
- `mt5trader.db` — open positions and the Fills journal
- `*.log` — what happened

Snapshot the disk, or copy those four. The database is what makes a
restart safe: without it the engine cannot tell a live position from an
orphan, and it will refuse to clean up rather than guess.

## Reaching the screen from your own laptop

The web page has **no password** and it can place orders, so it is
never exposed to the internet. Reach it through an AWS Session Manager
port forward:

```
aws ssm start-session --target i-XXXXXXXX ^
  --document-name AWS-StartPortForwardingSession ^
  --parameters "portNumber=8000,localPortNumber=8000"
```

then open `http://localhost:8000` in your browser. Nothing inbound has
to be open on the instance for this to work.
