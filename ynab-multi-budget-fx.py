#!/usr/bin/env python3

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import questionary
import ynab
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from ynab.rest import ApiException

console = Console()
CONFIG_PATH = Path.home() / ".local/share/ynab-multi-budget-fx/config.json"
RATES_CACHE_PATH = Path.home() / ".local/share/ynab-multi-budget-fx/rates_cache.json"
CURRENCY_DATA_PATH = Path.home() / ".local/share/ynab-multi-budget-fx/currency_data.json"
CURRENCY_DATA_URL = "https://raw.githubusercontent.com/tammoippen/iso4217parse/refs/heads/master/iso4217parse/data.json"


def milliunits_to_amount(milliunits: int, decimal_digits: int) -> float:
    return milliunits / (10 ** (decimal_digits + 1))


def load_currency_data() -> dict:
    """Load currency data from cache or fetch from URL."""
    if CURRENCY_DATA_PATH.exists():
        return json.loads(CURRENCY_DATA_PATH.read_text())
    try:
        resp = httpx.get(CURRENCY_DATA_URL, timeout=10, follow_redirects=True)
        if resp.status_code == 200:
            data = resp.json()
            CURRENCY_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
            CURRENCY_DATA_PATH.write_text(json.dumps(data))
            return data
    except Exception:
        pass
    return {}


_currency_data: dict | None = None


def get_currency_symbol(iso_code: str) -> str | None:
    """Get currency symbol from ISO code, or return the code if no symbol available."""
    global _currency_data
    if _currency_data is None:
        _currency_data = load_currency_data()
    currency = _currency_data.get(iso_code.upper(), {})
    if symbols := currency.get("symbols"):
        return symbols[0]
    return None


def load_config() -> dict | None:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return None


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def load_rates_cache() -> dict[str, float]:
    if RATES_CACHE_PATH.exists():
        return json.loads(RATES_CACHE_PATH.read_text())
    return {}


def save_rates_cache(cache: dict[str, float]) -> None:
    RATES_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RATES_CACHE_PATH.write_text(json.dumps(cache, indent=2))


def get_ynab_client(api_key: str) -> ynab.ApiClient:
    config = ynab.Configuration(access_token=api_key)
    return ynab.ApiClient(config)


def load_budgets(client: ynab.ApiClient) -> list:
    api = ynab.BudgetsApi(client)
    response = api.get_budgets()
    budgets = response.data.budgets
    return sorted(budgets, key=lambda b: b.last_modified_on or "", reverse=True)


def load_categories(client: ynab.ApiClient, budget_id: str) -> list[dict]:
    api = ynab.CategoriesApi(client)
    response = api.get_categories(budget_id)
    categories = []
    for group in response.data.category_groups:
        if group.name == "Internal Master Category":
            for cat in group.categories:
                if cat.name == "Inflow: Ready to Assign":
                    categories.append({"id": cat.id, "name": cat.name, "group": group.name})
            continue
        if group.name == "Credit Card Payments":
            continue
        for cat in group.categories:
            categories.append({"id": cat.id, "name": cat.name, "group": group.name})
    return categories


def load_accounts(client: ynab.ApiClient, budget_id: str) -> list[dict]:
    api = ynab.AccountsApi(client)
    response = api.get_accounts(budget_id)
    return [
        {"id": a.id, "name": a.name, "balance": a.balance, "closed": a.closed}
        for a in response.data.accounts
        if not a.deleted
    ]


def build_category_map(dest_cats: list[dict], src_cats: list[dict]) -> tuple[dict, list[str]]:
    dest_by_name = {c["name"]: c["id"] for c in dest_cats}
    cat_map = {}
    errors = []
    for cat in src_cats:
        if cat["name"] in dest_by_name:
            cat_map[cat["id"]] = dest_by_name[cat["name"]]
        else:
            errors.append(f"Category not found in destination: '{cat['name']}' (group: {cat['group']})")
    return cat_map, errors


def build_account_map(dest_accs: list[dict], src_accs: list[dict]) -> tuple[dict, list[str]]:
    dest_by_name = {a["name"]: a["id"] for a in dest_accs if not a["closed"]}
    acc_map = {}
    errors = []
    for acc in src_accs:
        if acc["closed"]:
            continue
        if acc["name"] in dest_by_name:
            acc_map[acc["id"]] = dest_by_name[acc["name"]]
        else:
            errors.append(f"Account not found in destination: '{acc['name']}'")
    return acc_map, errors


def fetch_fx_rates(base: str, target: str, dates: set[date]) -> dict[date, float]:
    rates = {}
    base_lower = base.lower()
    target_lower = target.lower()

    cache = load_rates_cache()
    dates_to_fetch = set()

    for d in dates:
        cache_key = f"{base_lower}:{target_lower}:{d.isoformat()}"
        if cache_key in cache:
            rates[d] = cache[cache_key]
        else:
            dates_to_fetch.add(d)

    if not dates_to_fetch:
        console.print("[dim]All rates loaded from cache[/dim]")
        return rates

    if len(dates) > len(dates_to_fetch):
        console.print(f"[dim]{len(dates) - len(dates_to_fetch)} rates loaded from cache[/dim]")

    def fetch_rate(d: date, is_fallback: bool = False) -> tuple[date, float | None, Exception | None]:
        date_str = d.isoformat()
        urls = [
            f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{date_str}/v1/currencies/{base_lower}.min.json",
            f"https://{date_str}.currency-api.pages.dev/v1/currencies/{base_lower}.min.json"
            f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{date_str}/v1/currencies/{base_lower}.json",
            f"https://{date_str}.currency-api.pages.dev/v1/currencies/{base_lower}.json",
        ]

        last_exc = None
        for url in urls:
            try:
                resp = httpx.get(url, timeout=10, follow_redirects=True)
                if resp.status_code == 200:
                    data = resp.json()
                    rate = data[base_lower][target_lower]
                    return d, rate, None
                last_exc = Exception(f"HTTP {resp.status_code} from {url}")
            except Exception as e:
                last_exc = e

        if not is_fallback:
            # Sometimes published rates are not available for the date, fallback to previous day
            console.print(
                f"[yellow]Warning: No rate available for {d.isoformat()}, falling back to previous day[/yellow]"
            )
            _, rate, exc = fetch_rate(d - timedelta(days=1), True)
            # However, return as of the requested date
            return d, rate, exc

        return d, None, last_exc

    total = len(dates_to_fetch)
    fetch_errors: dict[date, Exception] = {}
    newly_fetched: dict[date, float] = {}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"Fetching FX rates (0/{total})...", total=total)
        completed = 0

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_rate, d): d for d in dates_to_fetch}

            for future in as_completed(futures):
                d, rate, exc = future.result()
                if rate is not None:
                    rates[d] = rate
                    newly_fetched[d] = rate
                elif exc:
                    fetch_errors[d] = exc
                completed += 1
                progress.update(task, description=f"Fetching FX rates ({completed}/{total})...")
                progress.advance(task)

    if fetch_errors:
        for d, exc in sorted(fetch_errors.items()):
            error_msg = str(exc)
            console.print(f"[red]Failed to fetch rate for {d.isoformat()}: {error_msg}[/red]")
        sys.exit(1)

    if newly_fetched:
        for d, rate in newly_fetched.items():
            cache_key = f"{base_lower}:{target_lower}:{d.isoformat()}"
            cache[cache_key] = rate
        save_rates_cache(cache)

    return rates


def load_transactions(client: ynab.ApiClient, budget_id: str, since_date: str):
    api = ynab.TransactionsApi(client)
    response = api.get_transactions(budget_id, since_date=since_date)
    return response.data.transactions


def get_import_id(tx: Any) -> str:  #
    """
    We have little room here, as import_id is limited to 36 characters.
    Using original transaction ID (uuid4, so without dashes it's 32 characters) plus some prefix.
    Hope there won't be any collisions if you sync from multiple sources.
    """
    return f"MB:{tx.id.replace('-', '')}"


def convert_transaction(
    tx: Any,
    rate: float,
    cat_map: dict,
    acc_map: dict,
    base_currency: str,
    decimal_digits: int,
) -> dict | None:
    if tx.payee_name == "<IGNORE>":
        return None

    if tx.account_id not in acc_map:
        return None

    orig_amount = milliunits_to_amount(tx.amount, decimal_digits)
    new_amount = int(tx.amount * rate)

    fx_info = f"{abs(orig_amount):,.{decimal_digits}f} {base_currency} @{1 / rate:.2f}"
    if symbol := get_currency_symbol(base_currency):
        fx_info = f"{abs(orig_amount):,.{decimal_digits}f}{symbol} @{1 / rate:.2f}"

    memo = tx.memo or ""
    if memo:
        memo += f" / {fx_info}"
    else:
        memo = fx_info

    if len(memo) > 200:
        memo = memo[: 200 - 1] + "…"

    subtransactions = None
    category_id = None

    if tx.subtransactions:
        subtransactions = []
        for sub in tx.subtransactions:
            sub_amount = int(sub.amount * rate)
            sub_cat_id = cat_map.get(sub.category_id) if sub.category_id else None
            subtransactions.append(
                {
                    "amount": sub_amount,
                    "category_id": sub_cat_id,
                    "memo": sub.memo,
                }
            )
    else:
        category_id = cat_map.get(tx.category_id) if tx.category_id else None

    return {
        "account_id": acc_map[tx.account_id],
        "var_date": tx.var_date.isoformat(),
        "amount": new_amount,
        "payee_name": tx.payee_name,
        "category_id": category_id,
        "memo": memo,
        "approved": False,
        "import_id": get_import_id(tx),
        "subtransactions": subtransactions,
    }


def sync_batch(client: ynab.ApiClient, budget_id: str, transactions: list[dict]) -> int:
    if not transactions:
        return 0
    api = ynab.TransactionsApi(client)
    wrapper = ynab.PostTransactionsWrapper(transactions=transactions)
    response = api.create_transaction(budget_id, wrapper)

    if dups := response.data.duplicate_import_ids:
        console.print("[yellow]Warning: Following transactions were not created due to duplicate import IDs:[/yellow]")
        for id in dups:
            for tx in transactions:
                if tx["import_id"] == id:
                    console.print(f"  - {tx['var_date']} {tx['payee_name']}")

    return len(response.data.transaction_ids)


def update_batch(client: ynab.ApiClient, budget_id: str, transactions: list[dict]) -> int:
    if not transactions:
        return 0
    api = ynab.TransactionsApi(client)
    wrapper = ynab.PatchTransactionsWrapper(transactions=transactions)
    response = api.update_transactions(budget_id, wrapper)
    if not response:
        # for some reason, API sometimes retuns None instead of correct object, warn about it and assume it's successful
        console.print("[yellow]Warning: API returned None on update_transactions, assuming success.[/yellow]")
        return len(transactions)
    return len(response.data.transactions)


def prompt_config(client: ynab.ApiClient | None = None, api_key: str | None = None) -> dict:
    if not api_key:
        api_key = questionary.password("YNAB API Key:").ask()
        if not api_key:
            sys.exit(1)

    if not client:
        client = get_ynab_client(api_key)

    with console.status("Loading budgets..."):
        budgets = load_budgets(client)

    budget_choices = [{"name": f"{b.name} ({b.id[:8]}...)", "value": b} for b in budgets]

    dest_budget = questionary.select(
        "Select DESTINATION budget (target for synced transactions):",
        choices=budget_choices,
    ).ask()
    if not dest_budget:
        sys.exit(1)

    src_budget = questionary.select(
        "Select SOURCE budget (transactions to sync from):",
        choices=[c for c in budget_choices if c["value"].id != dest_budget.id],
    ).ask()
    if not src_budget:
        sys.exit(1)

    default_cutoff = (date.today() - timedelta(days=30)).isoformat()
    cutoff_date = questionary.text(
        "Cutoff date YYYY-MM-DD (sync transactions since):",
        default=default_cutoff,
    ).ask()
    if not cutoff_date:
        sys.exit(1)

    return {
        "api_key": api_key,
        "dest_budget_id": dest_budget.id,
        "dest_budget_name": dest_budget.name,
        "dest_currency": dest_budget.currency_format.iso_code,
        "dest_decimal_digits": dest_budget.currency_format.decimal_digits,
        "src_budget_id": src_budget.id,
        "src_budget_name": src_budget.name,
        "src_currency": src_budget.currency_format.iso_code,
        "src_decimal_digits": src_budget.currency_format.decimal_digits,
        "cutoff_date": cutoff_date,
    }


def display_config(cfg: dict) -> None:
    table = Table(title="Current Configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Destination Budget", f"{cfg['dest_budget_name']} ({cfg['dest_budget_id'][:8]}...)")
    table.add_row("Source Budget", f"{cfg['src_budget_name']} ({cfg['src_budget_id'][:8]}...)")
    table.add_row("Cutoff Date", cfg["cutoff_date"])
    table.add_row("Currency Conversion", f"{cfg['src_currency']} → {cfg['dest_currency']}")
    console.print(table)


def main():
    console.print("[bold]YNAB Multi-Budget FX Sync Tool[/bold]\n")

    cfg = load_config()
    client = None

    if cfg:
        display_config(cfg)
        choice = questionary.select(
            "Use cached configuration?",
            choices=[
                {"name": "Yes, use cached config", "value": "yes"},
                {"name": "No, update cutoff date only", "value": "date"},
                {"name": "No, pick new budgets", "value": "new"},
            ],
        ).ask()

        if choice == "date":
            default_cutoff = (date.today() - timedelta(days=30)).isoformat()
            cutoff_date = questionary.text(
                "Cutoff date YYYY-MM-DD (sync transactions since):",
                default=default_cutoff,
            ).ask()
            if cutoff_date:
                cfg["cutoff_date"] = cutoff_date
                save_config(cfg)
        elif choice == "new":
            cfg = None

    if not cfg:
        api_key = questionary.password("YNAB API Key:").ask()
        if not api_key:
            sys.exit(1)
        client = get_ynab_client(api_key)
        cfg = prompt_config(client, api_key)
        save_config(cfg)
        console.print("[green]Configuration saved![/green]\n")

    if not client:
        client = get_ynab_client(cfg["api_key"])

    with console.status("Loading categories..."):
        dest_cats = load_categories(client, cfg["dest_budget_id"])
        src_cats = load_categories(client, cfg["src_budget_id"])
        cat_map, cat_errors = build_category_map(dest_cats, src_cats)

    with console.status("Loading accounts..."):
        dest_accs = load_accounts(client, cfg["dest_budget_id"])
        src_accs = load_accounts(client, cfg["src_budget_id"])
        acc_map, acc_errors = build_account_map(dest_accs, src_accs)

    if cat_errors or acc_errors:
        console.print("[red bold]Validation errors:[/red bold]")
        for err in cat_errors + acc_errors:
            console.print(f"  [red]• {err}[/red]")
        sys.exit(1)

    console.print(f"[green]✓ Mapped {len(cat_map)} categories and {len(acc_map)} accounts[/green]\n")

    with console.status("Loading transactions..."):
        src_txs = load_transactions(client, cfg["src_budget_id"], cfg["cutoff_date"])
        dest_txs = load_transactions(client, cfg["dest_budget_id"], cfg["cutoff_date"])
        existing_by_import_id = {tx.import_id: tx for tx in dest_txs if tx.import_id}

    console.print(f"Found {len(src_txs)} transactions in source budget since {cfg['cutoff_date']}")

    to_sync = []
    to_update = []
    unique_dates = set()

    base_currency = cfg["src_currency"]
    target_currency = cfg["dest_currency"]
    src_decimals = cfg["src_decimal_digits"]
    dest_decimals = cfg["dest_decimal_digits"]

    for tx in src_txs:
        if tx.payee_name == "<IGNORE>":
            continue
        if tx.account_id not in acc_map:
            continue
        import_id = get_import_id(tx)
        if import_id in existing_by_import_id:
            to_update.append((tx, existing_by_import_id[import_id]))
        else:
            to_sync.append(tx)
        tx_date = tx.var_date
        unique_dates.add(tx_date)

    if not to_sync and not to_update:
        console.print("[yellow]No new transactions to sync or update.[/yellow]")
    else:
        if unique_dates:
            rates = fetch_fx_rates(base_currency, target_currency, unique_dates)
        else:
            rates = {}

        # Handle new transactions
        if to_sync:
            console.print(f"[cyan]{len(to_sync)} new transactions to sync[/cyan]\n")

            converted = []
            for tx in to_sync:
                tx_date = tx.var_date
                rate = rates[tx_date]
                conv = convert_transaction(
                    tx,
                    rate,
                    cat_map,
                    acc_map,
                    base_currency,
                    src_decimals,
                )
                if conv:
                    converted.append(conv)

            table = Table(title="Transactions to Sync")
            table.add_column("Date", style="cyan")
            table.add_column("Payee")
            table.add_column(f"Original ({base_currency})", justify="right")
            table.add_column(f"Converted ({target_currency})", justify="right", style="green")

            for i, (tx, conv) in enumerate(zip(to_sync, converted)):
                if i >= 20:
                    table.add_row("...", f"({len(to_sync) - 20} more)", "", "")
                    break
                orig_amt = milliunits_to_amount(tx.amount, src_decimals)
                conv_amt = milliunits_to_amount(conv["amount"], dest_decimals)
                table.add_row(
                    str(tx.var_date),
                    tx.payee_name or "(no payee)",
                    f"{orig_amt:.{src_decimals}f}",
                    f"{conv_amt:.{dest_decimals}f}",
                )
            console.print(table)

            if questionary.confirm("Proceed with sync?", default=True).ask():
                with console.status("Syncing transactions..."):
                    count = sync_batch(client, cfg["dest_budget_id"], converted)
                console.print(f"[green]✓ Synced {count} transactions[/green]\n")
            else:
                console.print("[yellow]Sync cancelled.[/yellow]")
        else:
            console.print("[yellow]No new transactions to sync.[/yellow]")

        # Handle updates to existing transactions
        if to_update:
            updates_needed = []
            for src_tx, dest_tx in to_update:
                tx_date = src_tx.var_date
                rate = rates[tx_date]
                conv = convert_transaction(
                    src_tx,
                    rate,
                    cat_map,
                    acc_map,
                    base_currency,
                    src_decimals,
                )
                if not conv:
                    continue

                # Check if amount or memo changed
                amount_changed = conv["amount"] != dest_tx.amount
                memo_changed = conv["memo"] != (dest_tx.memo or "")
                # Skip category updates for split transactions
                category_changed = (
                    not conv["subtransactions"]
                    and conv["category_id"] != dest_tx.category_id
                )

                if amount_changed or memo_changed or category_changed:
                    update_data = {"id": dest_tx.id}
                    if amount_changed:
                        update_data["amount"] = conv["amount"]
                    if memo_changed:
                        update_data["memo"] = conv["memo"]
                    if category_changed:
                        update_data["category_id"] = conv["category_id"]
                    updates_needed.append((src_tx, dest_tx, conv, update_data, amount_changed, memo_changed, category_changed))

            if updates_needed:
                console.print(f"\n[cyan]{len(updates_needed)} transactions need updates[/cyan]\n")

                table = Table(title="Transactions to Update")
                table.add_column("Date", style="cyan")
                table.add_column("Payee")
                table.add_column(f"Amount ({target_currency})", justify="right")
                table.add_column("Changes", style="yellow")

                for i, (src_tx, dest_tx, conv, update_data, amount_changed, memo_changed, category_changed) in enumerate(updates_needed):
                    if i >= 20:
                        table.add_row("...", f"({len(updates_needed) - 20} more)", "", "")
                        break
                    changes = []
                    if amount_changed:
                        old_amt = milliunits_to_amount(dest_tx.amount, dest_decimals)
                        new_amt = milliunits_to_amount(conv["amount"], dest_decimals)
                        changes.append(f"amount: {old_amt:.{dest_decimals}f} → {new_amt:.{dest_decimals}f}")
                    if memo_changed:
                        changes.append("memo updated")
                    if category_changed:
                        changes.append("category updated")
                    conv_amt = milliunits_to_amount(conv["amount"], dest_decimals)
                    table.add_row(
                        str(src_tx.var_date),
                        src_tx.payee_name or "(no payee)",
                        f"{conv_amt:.{dest_decimals}f}",
                        ", ".join(changes),
                    )
                console.print(table)

                if questionary.confirm("Proceed with updates?", default=True).ask():
                    with console.status("Updating transactions..."):
                        update_list = [u[3] for u in updates_needed]
                        count = update_batch(client, cfg["dest_budget_id"], update_list)
                    console.print(f"[green]✓ Updated {count} transactions[/green]\n")
                else:
                    console.print("[yellow]Updates cancelled.[/yellow]")
            else:
                console.print("[dim]No existing transactions need updates.[/dim]")

    # this api doesn't support today's
    yesterday = date.today() - timedelta(days=1)
    yesterday_rate = fetch_fx_rates(base_currency, target_currency, {yesterday})[yesterday]

    with console.status("Refreshing account balances..."):
        dest_accs = load_accounts(client, cfg["dest_budget_id"])
        src_accs = load_accounts(client, cfg["src_budget_id"])

    dest_balances = {a["name"]: a["balance"] for a in dest_accs}
    adjustment_category_id = cfg.get("adjustment_category_id")

    for acc in src_accs:
        if acc["closed"] or acc["id"] not in acc_map:
            continue

        src_balance = acc["balance"]
        converted_balance = int(src_balance * yesterday_rate)
        dest_balance = dest_balances.get(acc["name"], 0)
        diff = converted_balance - dest_balance

        diff_display = milliunits_to_amount(diff, dest_decimals)
        console.print(f"\n[cyan]{acc['name']}:[/cyan]")
        console.print(
            f"  Source balance: {milliunits_to_amount(src_balance, src_decimals):.{src_decimals}f} {base_currency}"
        )
        console.print(
            f"  Converted:      {milliunits_to_amount(converted_balance, dest_decimals):.{dest_decimals}f} {target_currency}"
        )
        console.print(
            f"  Dest balance:   {milliunits_to_amount(dest_balance, dest_decimals):.{dest_decimals}f} {target_currency}"
        )
        console.print(f"  Difference:     {diff_display:+.{dest_decimals}f} {target_currency}")

        if abs(diff_display) < 1:
            console.print("[yellow]Balance diff is less than 1 {target_currency}, skipping adjustment.[/yellow]")
            continue

        if questionary.confirm(
            f"Create adjustment of {diff_display:+.{dest_decimals}f} {target_currency}?",
            default=True,
        ).ask():
            if not adjustment_category_id:
                with console.status("Loading categories..."):
                    dest_cats = load_categories(client, cfg["dest_budget_id"])
                cat_choices = [{"name": f"{c['group']}: {c['name']}", "value": c["id"]} for c in dest_cats]
                adjustment_category_id = questionary.select(
                    "Select category for adjustments:",
                    choices=cat_choices,
                ).ask()
                if adjustment_category_id:
                    cfg["adjustment_category_id"] = adjustment_category_id
                    save_config(cfg)

            if adjustment_category_id:
                adj_tx = {
                    "account_id": acc_map[acc["id"]],
                    "var_date": date.today().isoformat(),
                    "amount": diff,
                    "payee_name": "Currency Rate Adjustment",
                    "category_id": adjustment_category_id,
                    "memo": f"FX sync adjustment @{1 / yesterday_rate:.2f}",
                    "approved": False,
                }
                with console.status("Creating adjustment..."):
                    api = ynab.TransactionsApi(client)
                    wrapper = ynab.PostTransactionsWrapper(transaction=adj_tx)
                    api.create_transaction(cfg["dest_budget_id"], wrapper)
                console.print(f"[green]✓ Created adjustment for {acc['name']}[/green]")

    console.print("\n[bold green]Done![/bold green]")


if __name__ == "__main__":
    try:
        main()
    except ApiException as e:
        console.print(f"[red]YNAB API Error: {e.reason} {e.data}[/red]")
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        sys.exit(0)
