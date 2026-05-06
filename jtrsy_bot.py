"""
jtrsy_bot.py — Monitors the JTRSY pool on Centrifuge and sends Telegram alerts.

Alerts:
  🔵 DEPOSIT_REQUEST_UPDATED  — new deposit request submitted
  ✅ DEPOSIT_CLAIMABLE        — deposit processed, shares ready to claim
  🔵 REDEEM_REQUEST_UPDATED   — new redemption request submitted
  ✅ REDEEM_CLAIMABLE         — redemption processed, funds ready to claim

Setup:
  pip3 install requests python-dotenv "python-telegram-bot==20.7"
  python3 jtrsy_bot.py
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
POOL_ID            = os.getenv("POOL_ID", "281474976710662")

API_URL           = "https://api.centrifuge.io"
STATE_FILE        = Path("vault_state.json")
POLL_INTERVAL     = 60
DECIMALS          = 10 ** 18
MIN_AMOUNT_USDC   = 10_000  # ignore transactions below this value

EXPLORER_MAP = {
    "ethereum":  "https://etherscan.io/tx/",
    "arbitrum":  "https://arbiscan.io/tx/",
    "avalanche": "https://snowtrace.io/tx/",
    "bnb":       "https://bscscan.com/tx/",
    "base":      "https://basescan.org/tx/",
}
DEFAULT_EXPLORER = "https://etherscan.io/tx/"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("jtrsy_bot")

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            # Ensure lifecycle section exists for both pools
            data.setdefault("JAAA", {}).setdefault("requests", {})
            data.setdefault("JTRSY", {}).setdefault("requests", {})
            data.setdefault("seen_jtrsy_deposit_requests", [])
            data.setdefault("seen_jtrsy_deposit_claimable", [])
            data.setdefault("seen_jtrsy_redeem_requests", [])
            data.setdefault("seen_jtrsy_redeem_claimable", [])
            return data
        except Exception:
            log.warning("Could not read state file — starting fresh.")
    return {
        "seen_deposit_requests": [],
        "seen_deposit_claimable": [],
        "seen_redeem_requests": [],
        "seen_redeem_claimable": [],
        "seen_jtrsy_deposit_requests": [],
        "seen_jtrsy_deposit_claimable": [],
        "seen_jtrsy_redeem_requests": [],
        "seen_jtrsy_redeem_claimable": [],
        "JAAA": {"requests": {}},
        "JTRSY": {"requests": {}},
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def write_lifecycle_record(state: dict, pool: str, node: dict, event_type: str) -> None:
    """Create or update a lifecycle record for a transaction in vault_state.json."""
    tx = node["txHash"]
    now = datetime.now(timezone.utc).isoformat()
    records = state[pool]["requests"]

    if tx not in records:
        records[tx] = {
            "type": event_type,
            "investor": node.get("account", ""),
            "chain": node.get("blockchain", {}).get("name", ""),
            "currency_amount": node.get("currencyAmount", "0"),
            "token_amount": node.get("tokenAmount", "0"),
            "submitted_at": None,
            "executed_at": None,
            "claimable_at": None,
            "claimed_at": None,
            "cancelled_at": None,
            "sla_6hr_alerted": False,
            "sla_24hr_alerted": False,
        }

    record = records[tx]

    if event_type in ("DEPOSIT_REQUEST_UPDATED", "REDEEM_REQUEST_UPDATED"):
        if record["submitted_at"] is None:
            record["submitted_at"] = now
    elif event_type in ("DEPOSIT_REQUEST_EXECUTED", "REDEEM_REQUEST_EXECUTED"):
        record["executed_at"] = now
    elif event_type in ("DEPOSIT_CLAIMABLE", "REDEEM_CLAIMABLE"):
        record["claimable_at"] = now
        if record["executed_at"] is None:
            record["executed_at"] = now
    elif event_type in ("DEPOSIT_CLAIMED", "REDEEM_CLAIMED"):
        record["claimed_at"] = now
    elif event_type in ("DEPOSIT_REQUEST_CANCELLED", "REDEEM_REQUEST_CANCELLED"):
        record["cancelled_at"] = now


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_amount(raw) -> str:
    value = int(raw) / DECIMALS
    if value >= 1_000_000:
        return f"{value:,.0f}"
    if value >= 1_000:
        return f"{value:,.2f}"
    return f"{value:.4f}"


def fmt_ts(ms) -> str:
    dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def fmt_address(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


def explorer_url(node: dict) -> str:
    """Return the correct block explorer URL based on the transaction's chain."""
    chain = ""
    if node.get("blockchain") and node["blockchain"].get("name"):
        chain = node["blockchain"]["name"].lower()
    base_url = EXPLORER_MAP.get(chain, DEFAULT_EXPLORER)
    return f"{base_url}{node['txHash']}"


def is_above_minimum(node: dict) -> bool:
    """Return True if the currency amount meets the minimum threshold."""
    try:
        value = int(node["currencyAmount"]) / DECIMALS
        return value >= MIN_AMOUNT_USDC
    except Exception:
        return True  # if we can't parse it, let it through


# ---------------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------------

DEPOSIT_REQUEST_QUERY = """
query DepositRequests($poolId: BigInt) {
  investorTransactions(
    where: { poolId: $poolId, type: DEPOSIT_REQUEST_UPDATED }
    orderBy: "createdAt"
    orderDirection: "desc"
    limit: 50
  ) {
    items {
      txHash
      account
      poolId
      type
      currencyAmount
      tokenAmount
      tokenPrice
      createdAt
      blockchain {
        name
      }
    }
  }
}
"""

DEPOSIT_CLAIMABLE_QUERY = """
query DepositClaimable($poolId: BigInt) {
  investorTransactions(
    where: { poolId: $poolId, type: DEPOSIT_CLAIMABLE }
    orderBy: "createdAt"
    orderDirection: "desc"
    limit: 50
  ) {
    items {
      txHash
      account
      poolId
      type
      currencyAmount
      tokenAmount
      tokenPrice
      createdAt
      blockchain {
        name
      }
    }
  }
}
"""

REDEEM_REQUEST_QUERY = """
query RedeemRequests($poolId: BigInt) {
  investorTransactions(
    where: { poolId: $poolId, type: REDEEM_REQUEST_UPDATED }
    orderBy: "createdAt"
    orderDirection: "desc"
    limit: 50
  ) {
    items {
      txHash
      account
      poolId
      type
      currencyAmount
      tokenAmount
      tokenPrice
      createdAt
      blockchain {
        name
      }
    }
  }
}
"""

REDEEM_CLAIMABLE_QUERY = """
query RedeemClaimable($poolId: BigInt) {
  investorTransactions(
    where: { poolId: $poolId, type: REDEEM_CLAIMABLE }
    orderBy: "createdAt"
    orderDirection: "desc"
    limit: 50
  ) {
    items {
      txHash
      account
      poolId
      type
      currencyAmount
      tokenAmount
      tokenPrice
      createdAt
      blockchain {
        name
      }
    }
  }
}
"""


def gql(query: str, variables: dict) -> dict | None:
    try:
        resp = requests.post(
            API_URL,
            json={"query": query, "variables": variables},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            log.error("GraphQL errors: %s", data["errors"])
            return None
        return data.get("data")
    except requests.RequestException as exc:
        log.error("API request failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def build_deposit_request_msg(node: dict) -> str:
    account = fmt_address(node["account"])
    amount  = fmt_amount(node["currencyAmount"])
    chain   = node.get("blockchain", {}).get("name", "Unknown chain")
    url     = explorer_url(node)
    return (
        f"🔵 *DEPOSIT REQUEST SUBMITTED*\n"
        f"Pool: JTRSY\n"
        f"Investor: `{account}`\n"
        f"Amount: *{amount} USDC*\n"
        f"Chain: {chain.capitalize()}\n"
        f"Time: {fmt_ts(node['createdAt'])}\n"
        f"[View on Explorer]({url})"
    )


def build_deposit_claimable_msg(node: dict) -> str:
    account = fmt_address(node["account"])
    shares  = fmt_amount(node["tokenAmount"])
    price   = fmt_amount(node["tokenPrice"])
    chain   = node.get("blockchain", {}).get("name", "Unknown chain")
    url     = explorer_url(node)
    return (
        f"✅ *DEPOSIT CLAIMABLE*\n"
        f"Pool: JTRSY\n"
        f"Investor: `{account}`\n"
        f"Shares ready: *{shares} JTRSY*\n"
        f"Token price: {price}\n"
        f"Chain: {chain.capitalize()}\n"
        f"Time: {fmt_ts(node['createdAt'])}\n"
        f"[View on Explorer]({url})"
    )


def build_redeem_request_msg(node: dict) -> str:
    account = fmt_address(node["account"])
    shares  = fmt_amount(node["tokenAmount"])
    chain   = node.get("blockchain", {}).get("name", "Unknown chain")
    url     = explorer_url(node)
    return (
        f"🔵 *REDEEM REQUEST SUBMITTED*\n"
        f"Pool: JTRSY\n"
        f"Investor: `{account}`\n"
        f"Shares: *{shares} JTRSY*\n"
        f"Chain: {chain.capitalize()}\n"
        f"Time: {fmt_ts(node['createdAt'])}\n"
        f"[View on Explorer]({url})"
    )


def build_redeem_claimable_msg(node: dict) -> str:
    account = fmt_address(node["account"])
    amount  = fmt_amount(node["currencyAmount"])
    price   = fmt_amount(node["tokenPrice"])
    chain   = node.get("blockchain", {}).get("name", "Unknown chain")
    url     = explorer_url(node)
    return (
        f"✅ *REDEEM CLAIMABLE*\n"
        f"Pool: JTRSY\n"
        f"Investor: `{account}`\n"
        f"USDC ready: *{amount} USDC*\n"
        f"Token price: {price}\n"
        f"Chain: {chain.capitalize()}\n"
        f"Time: {fmt_ts(node['createdAt'])}\n"
        f"[View on Explorer]({url})"
    )


# ---------------------------------------------------------------------------
# Telegram sender
# ---------------------------------------------------------------------------

def send_telegram(bot: Bot, message: str) -> None:
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        ))
        loop.close()
        log.info("Telegram message sent.")
    except TelegramError as exc:
        log.error("Telegram send failed: %s", exc)


# ---------------------------------------------------------------------------
# Poll logic
# ---------------------------------------------------------------------------

def check_deposit_requests(bot: Bot, state: dict) -> None:
    data = gql(DEPOSIT_REQUEST_QUERY, {"poolId": POOL_ID})
    if not data:
        return
    nodes = data.get("investorTransactions", {}).get("items", [])
    seen  = set(state["seen_jtrsy_deposit_requests"])
    for node in reversed(nodes):
        tx = node["txHash"]
        if tx not in seen:
            seen.add(tx)
            if not is_above_minimum(node):
                log.info("Skipping dust deposit request: %s", tx)
                continue
            log.info("New deposit request: %s", tx)
            send_telegram(bot, build_deposit_request_msg(node))
            write_lifecycle_record(state, "JTRSY", node, node["type"])
    state["seen_jtrsy_deposit_requests"] = list(seen)


def check_deposit_claimable(bot: Bot, state: dict) -> None:
    data = gql(DEPOSIT_CLAIMABLE_QUERY, {"poolId": POOL_ID})
    if not data:
        return
    nodes = data.get("investorTransactions", {}).get("items", [])
    seen  = set(state["seen_jtrsy_deposit_claimable"])
    for node in reversed(nodes):
        tx = node["txHash"]
        if tx not in seen:
            seen.add(tx)
            if not is_above_minimum(node):
                log.info("Skipping dust deposit claimable: %s", tx)
                continue
            log.info("New deposit claimable: %s", tx)
            send_telegram(bot, build_deposit_claimable_msg(node))
            write_lifecycle_record(state, "JTRSY", node, node["type"])
    state["seen_jtrsy_deposit_claimable"] = list(seen)


def check_redeem_requests(bot: Bot, state: dict) -> None:
    data = gql(REDEEM_REQUEST_QUERY, {"poolId": POOL_ID})
    if not data:
        return
    nodes = data.get("investorTransactions", {}).get("items", [])
    seen  = set(state["seen_jtrsy_redeem_requests"])
    for node in reversed(nodes):
        tx = node["txHash"]
        if tx not in seen:
            seen.add(tx)
            if not is_above_minimum(node):
                log.info("Skipping dust redeem request: %s", tx)
                continue
            log.info("New redeem request: %s", tx)
            send_telegram(bot, build_redeem_request_msg(node))
            write_lifecycle_record(state, "JTRSY", node, node["type"])
    state["seen_jtrsy_redeem_requests"] = list(seen)


def check_redeem_claimable(bot: Bot, state: dict) -> None:
    data = gql(REDEEM_CLAIMABLE_QUERY, {"poolId": POOL_ID})
    if not data:
        return
    nodes = data.get("investorTransactions", {}).get("items", [])
    seen  = set(state["seen_jtrsy_redeem_claimable"])
    for node in reversed(nodes):
        tx = node["txHash"]
        if tx not in seen:
            seen.add(tx)
            if not is_above_minimum(node):
                log.info("Skipping dust redeem claimable: %s", tx)
                continue
            log.info("New redeem claimable: %s", tx)
            send_telegram(bot, build_redeem_claimable_msg(node))
            write_lifecycle_record(state, "JTRSY", node, node["type"])
    state["seen_jtrsy_redeem_claimable"] = list(seen)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env")
    if not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_CHAT_ID is not set in .env")

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    send_telegram(bot, "🟢 *JTRSY bot is online*\nMonitoring pool `281474976710662` — polling every 60s")
    log.info("JTRSY bot started. Pool ID: %s — polling every %ds", POOL_ID, POLL_INTERVAL)

    while True:
        state = load_state()
        try:
            check_deposit_requests(bot, state)
            check_deposit_claimable(bot, state)
            check_redeem_requests(bot, state)
            check_redeem_claimable(bot, state)
            save_state(state)
        except Exception as exc:
            log.exception("Unexpected error in poll loop: %s", exc)
            send_telegram(bot, f"🔴 *JTRSY bot error*\n```{exc}```")

        log.info("Sleeping %ds until next poll…", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
