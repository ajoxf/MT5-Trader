# Running MT5-Trader on the EC2 box

Written for whoever sits in front of it, not for whoever built it.

## Every day

1. Connect to the machine (RDP, or your usual shortcut).
2. Check **both MetaTrader 5 terminals** are open and logged in, and
   that the **Algo Trading** button in each is green. If a terminal is
   closed, open it — it logs itself in.
3. Double-click **START-TRADING** on the desktop.
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
