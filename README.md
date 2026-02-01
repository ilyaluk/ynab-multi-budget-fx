# YNAB Multi-Budget FX Sync Tool

> Note: this is mostly vibe-coded, but was reviewed and works for me. Use at your own risk.

Sync transactions between two YNAB budgets with automatic currency conversion.

## Use Case

- You have a **destination** YNAB budget where you manage most of your finances
- You have a **source** budget in a different currency with fewer transactions
- You want to sync transactions from the source budget to your destination one, converting amounts while preserving payees and categories

## Installation

Requires Python 3.10+.

```bash
# Using uv
uv run ynab-multi-budget-fx.py

# Or install dependencies manually
pip install ynab rich questionary httpx
python ynab-multi-budget-fx.py
```

## Configuration

On first run, you'll be prompted for:

1. **YNAB API Key** - Get one from [YNAB Developer Settings](https://app.ynab.com/settings/developer)
2. **Destination budget** - Target budget (receives synced transactions)
3. **Source budget** - Budget to sync transactions from
4. **Cutoff date** - Only sync transactions since this date

Configuration is cached in `~/.local/share/ynab-multi-budget-fx/config.json`.

## How It Works

1. **Validation** - Maps categories and accounts between budgets by name. Source budget must have a subset of the categories and accounts of the destination budget.

2. **Transaction sync** - Fetches transactions from the source budget since the cutoff date. For each new transaction:
   - Converts the amount using daily FX rates
   - Maps category and account IDs to the destination budget
   - Appends FX info to memo: `(original_amount currency @rate)`
   - Sets `import_id` to `MB:<orig_tx_id>` to implement matching

3. **Balance adjustments** - After syncing, compares account balances and offers to create adjustment transactions for any discrepancies.

## Special Cases

- Transactions with payee `<IGNORE>` are skipped
- Split transactions are fully supported
- Synced transactions are created as unapproved for review

## FX Data

Uses the [Exchange API](https://github.com/fawazahmed0/exchange-api) for historical exchange rates.

## Dependencies

- `ynab` - Official YNAB API client
- `rich` - Terminal formatting
- `questionary` - Interactive prompts
- `httpx` - HTTP client for FX API
