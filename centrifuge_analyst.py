"""
analyst.py — Daily analyst agent for JAAA and JTRSY pools.

Reads vault_state.json for SLA data, queries the Centrifuge GraphQL API for
NAV, AUM, yield, pending orders, epoch state, and 24hr activity for both pools,
calls Claude API to write the Flags & Exceptions narrative, and outputs a single
structured JSON file (analyst_output.json) for reporter.py.

Usage:
    python3 analyst.py
    python3 analyst.py --stdout   # also print JSON to stdout

Dependencies:
    pip3 install requests anthropic python-dotenv
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_here = Path(__file__).parent
load_dotenv(_here / ".env", override=True)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Original CONTEXT.md specified claude-sonnet-4-20250514 but that model ID is no longer
# available in the API (404). Updated to claude-sonnet-4-6 (current Sonnet 4.x).
CLAUDE_MODEL      = "claude-sonnet-4-6"

API_URL    = "https://api.centrifuge.io"
STATE_FILE = Path("vault_state.json")
OUTPUT_FILE = Path("analyst_output.json")

PRICE_DECIMALS    = 10 ** 18
TOKEN_DECIMALS    = 10 ** 6    # default USDC decimals (Ethereum, Avalanche, Monad, Pharos)
RAY               = 10 ** 27   # Centrifuge yield values are stored in ray format
# Sub-minimum sentinel threshold for redeem filtering. No real redemption can be below the
# $500k fund minimum; at current NAV ~$1.10 even 1000 shares is only ~$1,100. The 1-share
# test records seen in live data are filtered by this constant — applied consistently to both
# pending redeem orders (so they don't count as open requests) and epoch redeem display.
MIN_REDEEM_SHARES = 1000

# Chain-specific USDC decimal overrides — verified against token contracts:
# Ethereum:  6  (default) — etherscan.io/token/0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48
# Binance:  18  (override) — bscscan.com/token/0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d
# Avalanche: 6  (default) — snowtrace.io/token/0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6
# Monad:     6  (default) — confirmed via explorer
# Pharos:    6  (default) — confirmed via explorer
CURRENCY_DECIMALS_BY_CHAIN = {
    "binance": 10 ** 18,
}

POOLS = {
    "JAAA": {
        "pool_id":               "281474976710663",
        "token_id":              "0x00010000000000070000000000000001",
        "currency":              "USDC",
        "settlement_days":       3,
        "mgmt_fee_bps":          40,
        "sla_alert_hours":       6,
        "sla_escalation_hours":  24,
        "claimable_soft_hours":  48,
        "investment_min_usd":    500_000,
    },
    "JTRSY": {
        "pool_id":               "281474976710662",
        "token_id":              "0x00010000000000060000000000000001",
        "currency":              "USDC",
        "settlement_days":       1,
        "mgmt_fee_bps":          15,
        "sla_alert_hours":       6,
        "sla_escalation_hours":  24,
        "claimable_soft_hours":  48,
        "investment_min_usd":    500_000,
    },
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("analyst")


# ---------------------------------------------------------------------------
# Decimal helpers
# ---------------------------------------------------------------------------

def currency_decimals(chain: str) -> int:
    return CURRENCY_DECIMALS_BY_CHAIN.get(chain.lower() if chain else "", TOKEN_DECIMALS)


def to_usd(raw, chain: str = "") -> float:
    if not raw:
        return 0.0
    return int(raw) / currency_decimals(chain)


def to_shares(raw) -> float:
    if not raw:
        return 0.0
    return int(raw) / TOKEN_DECIMALS


def to_price(raw) -> float:
    if not raw:
        return 0.0
    return int(raw) / PRICE_DECIMALS


def to_epoch_redeem_usd(raw) -> float:
    """revokedAssetsAmount from epochRedeemOrders is inconsistently stored:
    some epochs use 6-decimal USDC precision, others use 18-decimal token precision.
    Heuristic: if the 6-decimal result exceeds $1B it's implausible — use 18 decimals instead.
    """
    if not raw:
        return 0.0
    val_6d = int(raw) / TOKEN_DECIMALS
    if val_6d > 1_000_000_000:
        return int(raw) / PRICE_DECIMALS
    return val_6d


def annualized_pct(raw, days: int) -> float:
    if not raw:
        return 0.0
    return (int(raw) / RAY) / days * 365 * 100


# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

def gql(query: str, variables: dict = None) -> dict | None:
    try:
        resp = requests.post(
            API_URL,
            json={"query": query, "variables": variables or {}},
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
# API queries
# ---------------------------------------------------------------------------

TOKEN_QUERY = """
query Tokens($poolId: BigInt) {
  tokens(where: { poolId: $poolId }) {
    items {
      id
      name
      symbol
      totalIssuance
      tokenPrice
      decimals
    }
  }
}
"""

TOKEN_SNAPSHOT_QUERY = """
query TokenSnapshots($tokenId: String) {
  tokenSnapshots(
    where: { id_starts_with: $tokenId }
    orderBy: "timestamp"
    orderDirection: "desc"
    limit: 10
  ) {
    items {
      timestamp
      tokenPrice
      totalIssuance
      yield7d
      yield30d
      yieldYtd
      yieldSinceInception
    }
  }
}
"""

PENDING_INVEST_QUERY = """
query PendingInvest($poolId: BigInt) {
  pendingInvestOrders(where: { poolId: $poolId }) {
    items {
      account
      pendingAssetsAmount
      queuedAssetsAmount
      createdAt
      updatedAt
    }
  }
}
"""

PENDING_REDEEM_QUERY = """
query PendingRedeem($poolId: BigInt) {
  pendingRedeemOrders(where: { poolId: $poolId }) {
    items {
      account
      pendingSharesAmount
      queuedSharesAmount
      createdAt
      updatedAt
    }
  }
}
"""

EPOCH_INVEST_QUERY = """
query EpochInvest($poolId: BigInt) {
  epochInvestOrders(
    where: { poolId: $poolId, issuedAt_not: null }
    orderBy: "issuedAt"
    orderDirection: "desc"
    limit: 1
  ) {
    items {
      index
      approvedAt
      issuedAt
      approvedAssetsAmount
      issuedSharesAmount
      issuedWithNavPoolPerShare
    }
  }
}
"""
# Note: ordered by issuedAt not index — Centrifuge can have duplicate index values
# (confirmed in JTRSY where two records share index 9). Timestamp ordering is more
# robust and always returns the most recently settled epoch.
# issuedAt_not: null excludes in-progress epochs (approved but not yet settled) that
# would otherwise sort first when ordering by timestamp desc and revokedAt is null.

EPOCH_REDEEM_QUERY = """
query EpochRedeem($poolId: BigInt) {
  epochRedeemOrders(
    where: { poolId: $poolId, revokedAt_not: null }
    orderBy: "revokedAt"
    orderDirection: "desc"
    limit: 1
  ) {
    items {
      index
      approvedAt
      revokedAt
      approvedSharesAmount
      revokedAssetsAmount
      revokedWithNavPoolPerShare
    }
  }
}
"""
# Note: ordered by revokedAt not index — Centrifuge can have duplicate index values
# (confirmed in JTRSY where two records share index 9). Timestamp ordering is more
# robust and always returns the most recently settled epoch.
# revokedAt_not: null excludes in-progress epochs (shares queued but not yet settled)
# that would otherwise sort first when ordering by timestamp desc and revokedAt is null.

RECENT_TRANSACTIONS_QUERY = """
query RecentTransactions($poolId: BigInt) {
  investorTransactions(
    where: { poolId: $poolId }
    orderBy: "createdAt"
    orderDirection: "desc"
    limit: 100
  ) {
    items {
      txHash
      account
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


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def fetch_token_data(pool_id: str) -> dict:
    data = gql(TOKEN_QUERY, {"poolId": pool_id})
    if not data:
        return {}
    items = data.get("tokens", {}).get("items", [])
    return items[0] if items else {}


def fetch_snapshots(token_id: str) -> list:
    data = gql(TOKEN_SNAPSHOT_QUERY, {"tokenId": token_id})
    if not data:
        return []
    return data.get("tokenSnapshots", {}).get("items", [])


def fetch_pending_invest(pool_id: str) -> list:
    data = gql(PENDING_INVEST_QUERY, {"poolId": pool_id})
    if not data:
        return []
    return data.get("pendingInvestOrders", {}).get("items", [])


def fetch_pending_redeem(pool_id: str) -> list:
    data = gql(PENDING_REDEEM_QUERY, {"poolId": pool_id})
    if not data:
        return []
    return data.get("pendingRedeemOrders", {}).get("items", [])


def fetch_epoch_invest(pool_id: str) -> dict:
    data = gql(EPOCH_INVEST_QUERY, {"poolId": pool_id})
    if not data:
        return {}
    items = data.get("epochInvestOrders", {}).get("items", [])
    return items[0] if items else {}


def fetch_epoch_redeem(pool_id: str) -> dict:
    data = gql(EPOCH_REDEEM_QUERY, {"poolId": pool_id})
    if not data:
        return {}
    items = data.get("epochRedeemOrders", {}).get("items", [])
    return items[0] if items else {}


def fetch_recent_transactions(pool_id: str) -> list:
    data = gql(RECENT_TRANSACTIONS_QUERY, {"poolId": pool_id})
    if not data:
        return []
    return data.get("investorTransactions", {}).get("items", [])


# ---------------------------------------------------------------------------
# State reader
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log.warning("Could not read vault_state.json — SLA data unavailable.")
    return {}


# ---------------------------------------------------------------------------
# SLA analysis (read-only from vault_state.json)
# ---------------------------------------------------------------------------

def analyse_sla(state: dict, pool: str, cfg: dict, now: datetime) -> dict:
    records = state.get(pool, {}).get("requests", {})

    open_requests = []      # submitted, not yet executed
    claimable_stale = []    # claimable but not claimed for 48h+

    for tx, rec in records.items():
        submitted = rec.get("submitted_at")
        executed  = rec.get("executed_at")
        cancelled = rec.get("cancelled_at")
        claimable = rec.get("claimable_at")
        claimed   = rec.get("claimed_at")

        if cancelled:
            continue

        if submitted and not executed:
            submitted_dt = datetime.fromisoformat(submitted)
            age_hours = (now - submitted_dt).total_seconds() / 3600
            open_requests.append({
                "tx": tx,
                "type": rec.get("type"),
                "submitted_at": submitted,
                "age_hours": round(age_hours, 1),
                "sla_6hr_alerted": rec.get("sla_6hr_alerted", False),
                "sla_24hr_alerted": rec.get("sla_24hr_alerted", False),
            })

        if claimable and not claimed:
            claimable_dt = datetime.fromisoformat(claimable)
            age_hours = (now - claimable_dt).total_seconds() / 3600
            if age_hours >= cfg["claimable_soft_hours"]:
                claimable_stale.append({
                    "tx": tx,
                    "type": rec.get("type"),
                    "claimable_at": claimable,
                    "age_hours": round(age_hours, 1),
                })

    sla_breaches_6hr  = [r for r in open_requests if r["age_hours"] >= cfg["sla_alert_hours"]]
    sla_breaches_24hr = [r for r in open_requests if r["age_hours"] >= cfg["sla_escalation_hours"]]
    oldest_hours      = max((r["age_hours"] for r in open_requests), default=None)

    return {
        "open_requests":              open_requests,
        "oldest_unactioned_hours":    oldest_hours,
        "sla_6hr_breach_count":       len(sla_breaches_6hr),
        "sla_24hr_escalation_count":  len(sla_breaches_24hr),
        "sla_6hr_breaches":           sla_breaches_6hr,
        "sla_24hr_escalations":       sla_breaches_24hr,
        "claimable_stale_48hr_plus":  claimable_stale,
        "lifecycle_gap_note": (
            "Lifecycle tracking started this session. "
            "Pre-existing JAAA transaction hashes have no lifecycle records."
        ) if not records else None,
    }


# ---------------------------------------------------------------------------
# 24hr activity
# ---------------------------------------------------------------------------

OPERATIONAL_TYPES = {
    "DEPOSIT_REQUEST_UPDATED",
    "REDEEM_REQUEST_UPDATED",
    "DEPOSIT_CLAIMABLE",    # operator action signal — EXECUTED never appears in live data
    "REDEEM_CLAIMABLE",
    "DEPOSIT_CLAIMED",
    "REDEEM_CLAIMED",
    "DEPOSIT_REQUEST_CANCELLED",
    "REDEEM_REQUEST_CANCELLED",
    # TRANSFER_IN / TRANSFER_OUT excluded: secondary market transfers, not operational events
}


def analyse_24hr_activity(transactions: list, now: datetime) -> dict:
    cutoff_ms = int((now - timedelta(hours=24)).timestamp() * 1000)
    # Guard against null/missing createdAt before int() conversion
    recent = [
        t for t in transactions
        if t.get("createdAt") and int(t["createdAt"]) >= cutoff_ms
        and t["type"] in OPERATIONAL_TYPES
    ]

    deposit_requests = [t for t in recent if t["type"] == "DEPOSIT_REQUEST_UPDATED"]
    redeem_requests  = [t for t in recent if t["type"] == "REDEEM_REQUEST_UPDATED"]
    # DEPOSIT_CLAIMABLE / REDEEM_CLAIMABLE are the operator action signals —
    # DEPOSIT_REQUEST_EXECUTED / REDEEM_REQUEST_EXECUTED never appear in live data
    actioned         = [t for t in recent if t["type"] in ("DEPOSIT_CLAIMABLE", "REDEEM_CLAIMABLE")]
    claims           = [t for t in recent if t["type"] in ("DEPOSIT_CLAIMED", "REDEEM_CLAIMED")]

    def sum_currency(items):
        total = 0.0
        for t in items:
            chain = (t.get("blockchain") or {}).get("name", "")
            total += to_usd(t.get("currencyAmount"), chain)
        return total

    deposit_usd = sum_currency(deposit_requests)
    redeem_usd  = sum_currency([t for t in recent if t["type"] == "REDEEM_CLAIMED"])

    # Flag sub-minimum transactions
    sub_minimum = []
    min_usd = 500_000
    for t in deposit_requests + redeem_requests:
        chain = (t.get("blockchain") or {}).get("name", "")
        amt = to_usd(t.get("currencyAmount"), chain)
        if 0 < amt < min_usd:
            sub_minimum.append({
                "tx": t["txHash"],
                "type": t["type"],
                "amount_usd": round(amt, 2),
                "chain": chain,
            })

    return {
        "new_deposit_requests_count":  len(deposit_requests),
        "new_deposit_requests_usd":    round(deposit_usd, 2),
        "new_redeem_requests_count":   len(redeem_requests),
        "requests_actioned_count":     len(actioned),
        "claims_made_count":           len(claims),
        "net_flow_usd":                round(deposit_usd - redeem_usd, 2),
        "sub_minimum_transactions":    sub_minimum,
        "raw_events":                  [
            {
                "type":       t["type"],
                "amount_usd": round(to_usd(t.get("currencyAmount"), (t.get("blockchain") or {}).get("name", "")), 2),
                "chain":      (t.get("blockchain") or {}).get("name", ""),
                "time":       datetime.fromtimestamp(int(t["createdAt"]) / 1000, tz=timezone.utc).isoformat(),
            }
            for t in recent
        ],
    }


# ---------------------------------------------------------------------------
# NAV snapshot with 24hr delta
# ---------------------------------------------------------------------------

def build_nav_section(token: dict, snapshots: list) -> dict:
    nav          = to_price(token.get("tokenPrice"))
    total_shares = to_shares(token.get("totalIssuance"))
    aum          = nav * total_shares

    # Find today's and yesterday's distinct snapshots for AUM delta
    seen_ts   = {}
    for s in snapshots:
        ts = s["timestamp"]
        if ts not in seen_ts:
            seen_ts[ts] = s
    distinct = sorted(seen_ts.values(), key=lambda x: int(x["timestamp"]), reverse=True)

    aum_24hr_change = None
    if len(distinct) >= 2:
        prev = distinct[1]
        prev_nav    = to_price(prev.get("tokenPrice"))
        prev_shares = to_shares(prev.get("totalIssuance"))
        prev_aum    = prev_nav * prev_shares
        aum_24hr_change = round(aum - prev_aum, 2)

    latest = distinct[0] if distinct else {}
    return {
        "nav_per_share":             round(nav, 6),
        "total_shares_outstanding":  round(total_shares, 2),
        "aum_usd":                   round(aum, 2),
        "aum_24hr_change_usd":       aum_24hr_change,
        "yield_7d_annualized_pct":   round(annualized_pct(latest.get("yield7d"), 7), 2),
        "yield_30d_annualized_pct":  round(annualized_pct(latest.get("yield30d"), 30), 2),
        "yield_ytd_pct":             round(int(latest.get("yieldYtd") or 0) / RAY * 100, 4),
        "yield_since_inception_pct": round(int(latest.get("yieldSinceInception") or 0) / RAY * 100, 4),
    }


# ---------------------------------------------------------------------------
# Epoch section
# ---------------------------------------------------------------------------

def build_epoch_section(epoch_invest: dict, epoch_redeem: dict, now: datetime) -> dict:
    def ts_to_iso(ms_str):
        if not ms_str:
            return None
        return datetime.fromtimestamp(int(ms_str) / 1000, tz=timezone.utc).isoformat()

    def hours_since(ms_str):
        if not ms_str:
            return None
        dt = datetime.fromtimestamp(int(ms_str) / 1000, tz=timezone.utc)
        return round((now - dt).total_seconds() / 3600, 1)

    # EpochInvestOrder uses issuedAt; EpochRedeemOrder uses revokedAt.
    # Index comparison is authoritative — the two tables index independently.
    # Redeem index only increments when there are actual redemptions to process,
    # so invest index will almost always be higher and is the correct epoch label.
    # revokedAt can be null on the most recent redeem epoch (still open/unsettled) —
    # only use redeem close timestamp if revokedAt is present AND redeem index wins.
    invest_closed_at = epoch_invest.get("issuedAt")
    redeem_closed_at = epoch_redeem.get("revokedAt")  # may be null if epoch still open

    invest_index = epoch_invest.get("index")
    redeem_index = epoch_redeem.get("index")

    if invest_index is not None and redeem_index is not None:
        invest_wins = int(invest_index) >= int(redeem_index)
    else:
        invest_wins = invest_index is not None

    if invest_wins:
        latest_epoch_index = invest_index
        last_closed_at = invest_closed_at
    else:
        latest_epoch_index = redeem_index
        # Only use revokedAt if present — fall back to invest close if redeem epoch still open
        last_closed_at = redeem_closed_at if redeem_closed_at else invest_closed_at

    # Sentinel guard: epochRedeemOrders occasionally contains test/initialisation records
    # where revokedWithNavPoolPerShare is exactly $1.00 (10^18) and both amount fields are
    # identical integers. These are not real investor redemptions — JAAA and JTRSY always
    # trade above $1.00 NAV after inception. Zero out rather than surface misleading data.
    # Note: if this architecture is extended to a new pool that processes redemptions at
    # inception before NAV has moved above $1.00, this guard would incorrectly suppress them.
    redeem_nav_raw     = epoch_redeem.get("revokedWithNavPoolPerShare")
    redeem_shares_raw  = epoch_redeem.get("approvedSharesAmount")
    redeem_assets_raw  = epoch_redeem.get("revokedAssetsAmount")
    is_sentinel = (
        redeem_nav_raw == "1000000000000000000"   # NAV pinned at exactly $1.00
        and redeem_shares_raw == redeem_assets_raw  # amounts identical — implied by NAV=1, belt-and-suspenders
    )
    raw_shares_redeemed = 0.0 if is_sentinel else round(to_shares(redeem_shares_raw), 2)
    # Apply the same sub-minimum threshold used for pending orders — epochs with fewer than
    # 1000 shares redeemed are test/sentinel records, not real investor redemptions.
    is_sub_minimum = raw_shares_redeemed < MIN_REDEEM_SHARES
    last_epoch_redeem_usd      = 0.0 if (is_sentinel or is_sub_minimum) else round(to_epoch_redeem_usd(redeem_assets_raw), 2)
    last_epoch_shares_redeemed = 0.0 if (is_sentinel or is_sub_minimum) else raw_shares_redeemed

    return {
        "latest_epoch_index":           latest_epoch_index,
        "last_epoch_approved_at":       ts_to_iso(epoch_invest.get("approvedAt")),
        "last_epoch_issued_at":         ts_to_iso(last_closed_at),
        "hours_since_last_close":       hours_since(last_closed_at),
        "last_epoch_deposit_usd":       round(to_usd(epoch_invest.get("approvedAssetsAmount")), 2),
        "last_epoch_shares_issued":     round(to_shares(epoch_invest.get("issuedSharesAmount")), 2),
        "last_epoch_nav_at_issue":      round(to_price(epoch_invest.get("issuedWithNavPoolPerShare")), 6),
        "last_epoch_redeem_usd":        last_epoch_redeem_usd,
        "last_epoch_shares_redeemed":   last_epoch_shares_redeemed,
    }


# ---------------------------------------------------------------------------
# Pending section
# ---------------------------------------------------------------------------

def build_pending_section(pending_invest: list, pending_redeem: list, now: datetime) -> dict:
    # Filter out sub-minimum redeem records before counting or measuring age
    pending_redeem = [p for p in pending_redeem if to_shares(p.get("pendingSharesAmount")) >= MIN_REDEEM_SHARES]

    total_pending_invest_usd   = sum(to_usd(p.get("pendingAssetsAmount")) for p in pending_invest)
    total_pending_redeem_shares = sum(to_shares(p.get("pendingSharesAmount")) for p in pending_redeem)

    oldest_invest_hours = None
    if pending_invest:
        oldest_ms = min(int(p["createdAt"]) for p in pending_invest)
        oldest_invest_hours = round((now.timestamp() * 1000 - oldest_ms) / 3_600_000, 1)

    oldest_redeem_hours = None
    if pending_redeem:
        oldest_ms = min(int(p["createdAt"]) for p in pending_redeem)
        oldest_redeem_hours = round((now.timestamp() * 1000 - oldest_ms) / 3_600_000, 1)

    oldest_hours = None
    if oldest_invest_hours is not None and oldest_redeem_hours is not None:
        oldest_hours = max(oldest_invest_hours, oldest_redeem_hours)
    elif oldest_invest_hours is not None:
        oldest_hours = oldest_invest_hours
    elif oldest_redeem_hours is not None:
        oldest_hours = oldest_redeem_hours

    return {
        "open_deposit_requests_count":  len(pending_invest),
        "pending_deposit_usd":          round(total_pending_invest_usd, 2),
        "open_redeem_requests_count":   len(pending_redeem),
        "pending_redeem_shares":        round(total_pending_redeem_shares, 2),
        "oldest_unactioned_hours":      oldest_hours,
    }


# ---------------------------------------------------------------------------
# Claude API — Flags & Exceptions narrative
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a senior investment operations analyst at an institutional digital asset fund manager.
You write precise, direct daily operational briefings. Your audience is fund operations staff
and, for pool-specific reports, the fund administrator (Trident Trust for JAAA, TBC for JTRSY).

When writing Flags & Exceptions:
- Lead with the most urgent item. If nothing is urgent, say so plainly.
- Distinguish between operator-action items (SLA breaches, pending requests) and
  investor-behaviour items (unclaimed positions, sub-minimum transactions).
- Use plain English. Short paragraphs or a numbered list if there are multiple items.
  Numbers in plain dollar amounts (e.g. "$15.0M", not "$15,000,000.00").
- Be specific: name the pool, the amount, the time elapsed.
- If everything is clean, write one sentence confirming it. Do not pad.
- Maximum 150 words per pool section, 200 words for combined operator section.

FORMATTING — strictly enforced:
- Plain text only. Absolutely no markdown.
- No asterisks or double asterisks (no bold or italic markup).
- No hyphens, equals signs, or dashes used as horizontal dividers or rules.
- No ## or # headings of any kind.
- No numbered section headings such as "1.", "## 1.", or "1. JAAA FLAGS".
- No bullet points using - or *.
- Paragraphs separated by a single blank line only.
- The three section labels below are required for parsing — write each on its own line
  as plain text with no decoration before or after:
    JAAA FLAGS & EXCEPTIONS
    JTRSY FLAGS & EXCEPTIONS
    OPERATOR COMBINED FLAGS & EXCEPTIONS
"""


def call_claude(pool_data: dict) -> tuple[str, str, str]:
    """Call Claude API. Returns (jaaa_narrative, jtrsy_narrative, combined_narrative)."""
    if not ANTHROPIC_API_KEY:
        placeholder = "[ANTHROPIC_API_KEY not set — narrative unavailable]"
        return placeholder, placeholder, placeholder

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    user_prompt = f"""
Write the Flags & Exceptions section for today's daily operations report.

Today: {pool_data['report_timestamp']}

=== JAAA — Janus Henderson Anemoy AAA CLO Fund ===
NAV: ${pool_data['JAAA']['nav']['nav_per_share']:.6f} | AUM: ${pool_data['JAAA']['nav']['aum_usd']/1e6:.1f}M
24hr AUM change: {f"${pool_data['JAAA']['nav']['aum_24hr_change_usd']/1e6:+.1f}M" if pool_data['JAAA']['nav']['aum_24hr_change_usd'] is not None else "N/A"}
30d yield (annualized): {pool_data['JAAA']['nav']['yield_30d_annualized_pct']}%
Last epoch: #{pool_data['JAAA']['epoch']['latest_epoch_index']}, closed {pool_data['JAAA']['epoch']['hours_since_last_close']}h ago
Pending deposits: {pool_data['JAAA']['pending']['open_deposit_requests_count']} (${pool_data['JAAA']['pending']['pending_deposit_usd']/1e6:.2f}M)
Pending redeems: {pool_data['JAAA']['pending']['open_redeem_requests_count']} ({pool_data['JAAA']['pending']['pending_redeem_shares']:,.0f} shares)
Oldest unactioned request: {pool_data['JAAA']['sla']['oldest_unactioned_hours'] or 'none'}h
SLA 6hr breaches: {pool_data['JAAA']['sla']['sla_6hr_breach_count']}
SLA 24hr escalations: {pool_data['JAAA']['sla']['sla_24hr_escalation_count']}
Claimable but unclaimed 48h+: {len(pool_data['JAAA']['sla']['claimable_stale_48hr_plus'])}
24hr activity: {pool_data['JAAA']['activity_24hr']['new_deposit_requests_count']} new deposits, {pool_data['JAAA']['activity_24hr']['new_redeem_requests_count']} new redeems, {pool_data['JAAA']['activity_24hr']['claims_made_count']} claims
Sub-minimum transactions: {len(pool_data['JAAA']['activity_24hr']['sub_minimum_transactions'])}

=== JTRSY — Janus Henderson Anemoy Treasury Fund ===
NAV: ${pool_data['JTRSY']['nav']['nav_per_share']:.6f} | AUM: ${pool_data['JTRSY']['nav']['aum_usd']/1e6:.1f}M
24hr AUM change: {f"${pool_data['JTRSY']['nav']['aum_24hr_change_usd']/1e6:+.1f}M" if pool_data['JTRSY']['nav']['aum_24hr_change_usd'] is not None else "N/A"}
30d yield (annualized): {pool_data['JTRSY']['nav']['yield_30d_annualized_pct']}%
Last epoch: #{pool_data['JTRSY']['epoch']['latest_epoch_index']}, closed {pool_data['JTRSY']['epoch']['hours_since_last_close']}h ago
Pending deposits: {pool_data['JTRSY']['pending']['open_deposit_requests_count']} (${pool_data['JTRSY']['pending']['pending_deposit_usd']/1e6:.2f}M)
Pending redeems: {pool_data['JTRSY']['pending']['open_redeem_requests_count']} ({pool_data['JTRSY']['pending']['pending_redeem_shares']:,.0f} shares)
Oldest unactioned request: {pool_data['JTRSY']['sla']['oldest_unactioned_hours'] or 'none'}h
SLA 6hr breaches: {pool_data['JTRSY']['sla']['sla_6hr_breach_count']}
SLA 24hr escalations: {pool_data['JTRSY']['sla']['sla_24hr_escalation_count']}
Claimable but unclaimed 48h+: {len(pool_data['JTRSY']['sla']['claimable_stale_48hr_plus'])}
24hr activity: {pool_data['JTRSY']['activity_24hr']['new_deposit_requests_count']} new deposits, {pool_data['JTRSY']['activity_24hr']['new_redeem_requests_count']} new redeems, {pool_data['JTRSY']['activity_24hr']['claims_made_count']} claims
Sub-minimum transactions: {len(pool_data['JTRSY']['activity_24hr']['sub_minimum_transactions'])}
Sub-minimum detail: {pool_data['JTRSY']['activity_24hr']['sub_minimum_transactions'] if pool_data['JTRSY']['activity_24hr']['sub_minimum_transactions'] else 'none'}

Recent JTRSY 24hr events:
{chr(10).join(f"  {e['time'][11:16]} UTC | {e['type']} | ${e['amount_usd']:,.0f} | {e['chain']}" for e in pool_data['JTRSY']['activity_24hr']['raw_events'][:15])}

Produce three sections:
1. JAAA FLAGS & EXCEPTIONS (for fund admin report — max 150 words)
2. JTRSY FLAGS & EXCEPTIONS (for fund admin report — max 150 words)
3. OPERATOR COMBINED FLAGS & EXCEPTIONS (for internal digest — max 200 words, highest priority items first)

Label each section clearly.
"""

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_prompt}],
    )

    full_text = response.content[0].text

    def extract_section(text: str, label: str, next_label: str = None) -> str:
        import re
        start = text.find(label)
        if start == -1:
            return full_text  # fallback: return everything
        content_start = text.find("\n", start) + 1
        if next_label:
            end = text.find(next_label, content_start)
            raw = text[content_start:end].strip() if end != -1 else text[content_start:].strip()
        else:
            raw = text[content_start:].strip()
        # Strip any markdown artefacts Claude may have appended despite instructions
        raw = re.sub(r'\n[-=]{2,}\s*$', '', raw)           # trailing --- or === lines
        raw = re.sub(r'\n#{1,3}\s+\d+\..*$', '', raw)      # trailing ## 2. headers
        raw = re.sub(r'(\*\*|__)(.*?)(\*\*|__)', r'\2', raw)  # **bold** → plain
        raw = re.sub(r'\*([^*]+)\*', r'\1', raw)            # *italic* → plain
        raw = re.sub(r'^#{1,3}\s+', '', raw, flags=re.MULTILINE)  # ## headers → plain
        raw = re.sub(r'^[-*]\s+', '', raw, flags=re.MULTILINE)    # bullet points → plain
        return raw.strip()

    jaaa_text     = extract_section(full_text, "JAAA FLAGS",     "JTRSY FLAGS")
    jtrsy_text    = extract_section(full_text, "JTRSY FLAGS",    "OPERATOR COMBINED")
    combined_text = extract_section(full_text, "OPERATOR COMBINED")

    return jaaa_text, jtrsy_text, combined_text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> dict:
    now = datetime.now(timezone.utc)
    log.info("Analyst run started: %s", now.isoformat())

    state = load_state()
    output = {
        "report_timestamp": now.isoformat(),
        "schema_version":   "1.0",
    }

    # Collect per-pool data
    for pool_name, cfg in POOLS.items():
        log.info("Fetching data for %s (pool_id=%s)...", pool_name, cfg["pool_id"])

        token         = fetch_token_data(cfg["pool_id"])
        snapshots     = fetch_snapshots(cfg["token_id"])
        pending_inv   = fetch_pending_invest(cfg["pool_id"])
        pending_rdm   = fetch_pending_redeem(cfg["pool_id"])
        epoch_inv     = fetch_epoch_invest(cfg["pool_id"])
        epoch_rdm     = fetch_epoch_redeem(cfg["pool_id"])
        transactions  = fetch_recent_transactions(cfg["pool_id"])

        nav_section     = build_nav_section(token, snapshots)
        epoch_section   = build_epoch_section(epoch_inv, epoch_rdm, now)
        pending_section = build_pending_section(pending_inv, pending_rdm, now)
        sla_section     = analyse_sla(state, pool_name, cfg, now)
        activity_section = analyse_24hr_activity(transactions, now)

        output[pool_name] = {
            "pool_id":        cfg["pool_id"],
            "pool_name":      token.get("name", pool_name),
            "nav":            nav_section,
            "epoch":          epoch_section,
            "pending":        pending_section,
            "sla":            sla_section,
            "activity_24hr":  activity_section,
        }

        log.info(
            "%s: NAV=%.4f AUM=$%.1fM pending_deposits=%d pending_redeems=%d",
            pool_name,
            nav_section["nav_per_share"],
            nav_section["aum_usd"] / 1e6,
            pending_section["open_deposit_requests_count"],
            pending_section["open_redeem_requests_count"],
        )

    # Claude API — Flags & Exceptions narrative
    log.info("Calling Claude API for narrative...")
    jaaa_flags, jtrsy_flags, combined_flags = call_claude(output)

    output["JAAA"]["flags_and_exceptions"]  = jaaa_flags
    output["JTRSY"]["flags_and_exceptions"] = jtrsy_flags
    output["operator_combined_flags"]       = combined_flags

    return output


def main():
    parser = argparse.ArgumentParser(description="Centrifuge daily analyst agent")
    parser.add_argument("--stdout", action="store_true", help="Also print JSON to stdout")
    args = parser.parse_args()

    result = run()

    OUTPUT_FILE.write_text(json.dumps(result, indent=2))
    log.info("Output written to %s", OUTPUT_FILE)

    if args.stdout:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
