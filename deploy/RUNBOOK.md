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
