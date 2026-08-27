# MT5-Trader

Automated trading tooling for MetaTrader 5.

## Status

Early setup — project scaffolding only. No trading logic yet.

## Requirements

- Python 3.10+
- A MetaTrader 5 terminal installed and logged in
- `MetaTrader5` Python package (Windows-only for the official package)

## Getting started

```bash
git clone https://github.com/ajoxf/MT5-Trader.git
cd MT5-Trader

python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

## Configuration

Credentials and account settings are read from environment variables. Copy the
example file and fill in your own values:

```bash
cp .env.example .env
```

**Never commit `.env` or any file containing account numbers, passwords, or API
keys.** `.gitignore` already excludes them.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the branching and pull request
workflow.

## Disclaimer

This software is provided for educational purposes. Automated trading carries
substantial risk of financial loss. Test on a demo account first. Use at your
own risk.
