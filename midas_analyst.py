"""
analyst.py — Midas Analyst Agent

Runs once daily or on demand. Reads vault_state.json, queries oracles and
Etherscan for holder data, calls Claude API for narrative, writes analyst_output.json.

Steps (each confirmed against live data before proceeding):
  Step 1: NAV and AUM               ← current
  Step 2: 24hr activity breakdown
  Step 3: SLA status
  Step 4: Holder intelligence
  Step 5: Claude API narrative
  Step 6: Output — analyst_output.json

Decimal handling (confirmed Session 1):
  Oracle:              8 decimals  — divide answer by 1e8
  Token / mToken:     18 decimals  — divide by 1e18
  Vault event USD:    18-decimal normalised — divide by 1e18 (NOT 1e6)
  RedeemRequest USD:  amount_mtoken_raw / 1e18 * NAV  (no USD field)
  RedeemInstant USD:  amount_token_out_raw / 1e18
  vault_state.json:   all large ints stored as strings — use int() throughout
"""

import os, json, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
from web3 import Web3
import requests as http
from dotenv import load_dotenv

load_dotenv(override=True)

# ── Config ─────────────────────────────────────────────────────────────────────
ALCHEMY_ETH   = os.environ["ALCHEMY_ETH_URL"]
ALCHEMY_BASE  = os.environ["ALCHEMY_BASE_URL"]
ETHERSCAN_KEY = os.environ["ETHERSCAN_API_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

ETHERSCAN_API     = "https://api.etherscan.io/v2/api"
CLAUDE_MODEL = "claude-sonnet-4-6"

ROOT              = Path(__file__).parent
STATE_FILE        = ROOT / "data" / "vault_state.json"
NAV_HISTORY_FILE  = ROOT / "data" / "nav_history.json"
HOLDER_SNAP_FILE  = ROOT / "data" / "holder_snapshot.json"
OUTPUT_FILE       = ROOT / "data" / "analyst_output.json"
LOG_FILE          = ROOT / "logs" / "analyst.log"
(ROOT / "data").mkdir(exist_ok=True)
(ROOT / "logs").mkdir(exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Web3 connections ───────────────────────────────────────────────────────────
W3 = {
    "ethereum": Web3(Web3.HTTPProvider(ALCHEMY_ETH)),
    "base":     Web3(Web3.HTTPProvider(ALCHEMY_BASE)),
}

# ── ABIs ───────────────────────────────────────────────────────────────────────
ORACLE_ABI = [{
    "name": "latestRoundData", "type": "function", "stateMutability": "view",
    "inputs": [],
    "outputs": [
        {"name": "roundId",         "type": "uint80"},
        {"name": "answer",          "type": "int256"},
        {"name": "startedAt",       "type": "uint256"},
        {"name": "updatedAt",       "type": "uint256"},
        {"name": "answeredInRound", "type": "uint80"},
    ],
}]

ERC20_ABI = [{
    "name": "totalSupply", "type": "function", "stateMutability": "view",
    "inputs": [], "outputs": [{"name": "", "type": "uint256"}],
}]

# ── Product config ─────────────────────────────────────────────────────────────
# Keys match vault_state.json product names exactly (mTBILL, mfONE).
PRODUCTS = {
    "mTBILL": {
        "display_name": "mTBILL",
        "oracle":  {"chain": "ethereum", "address": "0x056339C044055819E8Db84E71f5f2E1F536b2E5b"},
        "tokens": [
            {"chain": "ethereum", "address": "0xdd629e5241cbc5919847783e6c96b2de4754e438"},
            {"chain": "base",     "address": "0xDD629E5241CbC5919847783e6C96B2De4754e438"},
        ],
        "etherscan_token": "0xdd629e5241cbc5919847783e6c96b2de4754e438",
        "has_reject":       False,
        "standard_sla_h":   48,
        "escalation_sla_h": 96,
    },
    "mfONE": {
        "display_name": "mF-ONE",
        "oracle":  {"chain": "ethereum", "address": "0x8D51DBC85cEef637c97D02bdaAbb5E274850e68C"},
        "tokens": [
            {"chain": "ethereum", "address": "0x238a700eD6165261Cf8b2e544ba797BC11e466Ba"},
        ],
        "etherscan_token": "0x238a700eD6165261Cf8b2e544ba797BC11e466Ba",
        "has_reject":       True,
        "standard_sla_h":   48,
        "escalation_sla_h": 96,
    },
}

# ── Shared helpers ─────────────────────────────────────────────────────────────
def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def ts_to_dt(ts_str: str) -> datetime:
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


# ── Step 1: NAV and AUM ────────────────────────────────────────────────────────

def load_nav_history() -> dict:
    if NAV_HISTORY_FILE.exists():
        with NAV_HISTORY_FILE.open() as f:
            return json.load(f)
    return {}

def save_nav_history(nav_history: dict) -> None:
    nav_history["schema_version"] = "1"
    nav_history["last_updated"]   = utcnow()
    with NAV_HISTORY_FILE.open("w") as f:
        json.dump(nav_history, f, indent=2)

def fetch_nav(product: str) -> tuple[float | None, str | None]:
    """Return (nav, oracle_updated_at_iso) or (None, None) on failure."""
    cfg = PRODUCTS[product]["oracle"]
    try:
        c = W3[cfg["chain"]].eth.contract(
            address=Web3.to_checksum_address(cfg["address"]), abi=ORACLE_ABI
        )
        _, answer, _, updated_at, _ = c.functions.latestRoundData().call()
        nav      = answer / 1e8
        oracle_ts = datetime.fromtimestamp(updated_at, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return nav, oracle_ts
    except Exception as e:
        log.error(f"Oracle read failed for {product}: {e}")
        return None, None

def fetch_total_supply(chain: str, address: str) -> float | None:
    """Return total supply in token units (18 decimals). None on failure."""
    try:
        c = W3[chain].eth.contract(
            address=Web3.to_checksum_address(address), abi=ERC20_ABI
        )
        return c.functions.totalSupply().call() / 1e18
    except Exception as e:
        log.error(f"totalSupply failed {chain} {address}: {e}")
        return None

def compute_nav_and_aum(nav_history: dict) -> dict:
    """
    Query oracles and token contracts for both products.
    Returns structured nav/aum data keyed by product name.
    nav_history is read-only here — writing happens at end of run in main().
    """
    result = {}

    for product, cfg in PRODUCTS.items():
        nav, oracle_ts = fetch_nav(product)
        if nav is None:
            log.error(f"{product}: oracle unavailable — skipping")
            result[product] = None
            continue

        prev_entry = nav_history.get(product, {})
        prev_nav   = prev_entry.get("nav") if prev_entry else None
        # Discard same-day history — only cross-day comparison is meaningful
        if prev_nav is not None and prev_entry.get("recorded_at", "")[:10] == utcnow()[:10]:
            prev_nav = None
        change_24h     = round(nav - prev_nav, 8)                if prev_nav is not None else None
        change_24h_pct = round((change_24h / prev_nav) * 100, 6) if prev_nav else None

        supply_by_chain: dict[str, float] = {}
        total_supply = 0.0
        for tok in cfg["tokens"]:
            s = fetch_total_supply(tok["chain"], tok["address"])
            if s is not None:
                supply_by_chain[tok["chain"]] = s
                total_supply += s

        aum_usd = round(total_supply * nav, 2)

        result[product] = {
            "nav": {
                "current":           round(nav, 8),
                "previous":          round(prev_nav, 8) if prev_nav is not None else None,
                "change_24h":        change_24h,
                "change_24h_pct":    change_24h_pct,
                "oracle_updated_at": oracle_ts,
            },
            "aum": {
                "total_supply_tokens":   round(total_supply, 6),
                "total_supply_ethereum": round(supply_by_chain.get("ethereum", 0.0), 6),
                "total_supply_base":     round(supply_by_chain.get("base", 0.0), 6)
                                         if "base" in supply_by_chain else None,
                "aum_usd":               aum_usd,
            },
        }

        prev_str = f"prev=${prev_nav:.6f}" if prev_nav is not None else "prev=none (first run)"
        log.info(
            f"{product}: NAV=${nav:.6f} ({prev_str}) | "
            f"supply={total_supply:,.2f} tokens | AUM=${aum_usd:,.0f}"
        )

    return result


# ── Step 2: 24hr activity breakdown ───────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        with STATE_FILE.open() as f:
            return json.load(f)
    return {"events": [], "redemption_requests": {}, "issuance_requests": {}}

def _in_window(ts_str: str, window_start: datetime, window_end: datetime) -> bool:
    try:
        return window_start <= ts_to_dt(ts_str) <= window_end
    except Exception:
        return False

def _usd(raw: str | int) -> float:
    """Convert a raw 18-decimal normalised vault event amount to USD float."""
    return int(raw) / 1e18

def compute_activity(
    state: dict,
    nav_aum: dict,
    window_start: datetime,
    window_end: datetime,
) -> dict:
    """
    Compute 24hr activity per product from the rolling events list.

    Amount rules (confirmed Session 1):
      DepositInstant:   amount_usd_raw / 1e18
      DepositRequest:   amount_usd_raw / 1e18
      RedeemInstant:    amount_token_out_raw / 1e18  (actual USDC received)
      RedeemRequest:    amount_mtoken_raw / 1e18 * NAV  (no USD field)
      ApproveRequest /
      SafeApproveRequest on redemption vault:
                        look up original RedeemRequest in redemption_requests for amount.
                        If not found (predates monitoring window) — count the event, USD=0.
    """
    result: dict[str, dict] = {}

    for product, pcfg in PRODUCTS.items():
        nav = nav_aum[product]["nav"]["current"] if nav_aum.get(product) else None

        acc: dict[str, dict] = {
            "instant_issuance":              {"count": 0, "usd": 0.0},
            "standard_issuance_submitted":   {"count": 0, "usd": 0.0},
            "standard_issuance_processed":   {"count": 0, "usd": 0.0},
            "instant_redemption":            {"count": 0, "usd": 0.0},
            "standard_redemption_submitted": {"count": 0, "usd": 0.0},
            "standard_redemption_processed": {"count": 0, "usd": 0.0},
            "standard_redemption_rejected":  {"count": 0, "usd": 0.0} if pcfg["has_reject"] else None,
        }

        redemption_vaults = {
            vk for vk in [
                f"{product}_ethereum_standard_redemption",
                f"{product}_ethereum_instant_redemption",
                f"{product}_ethereum_redemption",
            ]
        }
        issuance_vaults = {f"{product}_ethereum_issuance", f"{product}_base_issuance"}

        for ev in state.get("events", []):
            if ev.get("product") != product:
                continue
            if not _in_window(ev.get("timestamp", ""), window_start, window_end):
                continue

            etype  = ev.get("event_type", "")
            vk     = ev.get("vault_key", "")

            if etype in ("DepositInstant", "DepositInstantWithCustomRecipient"):
                acc["instant_issuance"]["count"] += 1
                acc["instant_issuance"]["usd"]   += _usd(ev.get("amount_usd_raw", 0))

            elif etype in ("DepositRequest", "DepositRequestWithCustomRecipient"):
                acc["standard_issuance_submitted"]["count"] += 1
                acc["standard_issuance_submitted"]["usd"]   += _usd(ev.get("amount_usd_raw", 0))

            elif etype == "SafeApproveRequest" and vk in issuance_vaults:
                # Issuance approval — look up original DepositRequest for USD amount
                req_key = f"{ev.get('vault_address', '')}_{ev.get('request_id', '')}"
                req     = state["issuance_requests"].get(req_key, {})
                usd     = _usd(req.get("amount_usd_raw", 0))
                acc["standard_issuance_processed"]["count"] += 1
                acc["standard_issuance_processed"]["usd"]   += usd

            elif etype in ("RedeemInstant", "RedeemInstantWithCustomRecipient"):
                acc["instant_redemption"]["count"] += 1
                acc["instant_redemption"]["usd"]   += _usd(ev.get("amount_token_out_raw", 0))

            elif etype in ("RedeemRequest", "RedeemRequestWithCustomRecipient") and vk in redemption_vaults:
                usd = (_usd(ev.get("amount_mtoken_raw", 0)) * nav) if nav else 0.0
                acc["standard_redemption_submitted"]["count"] += 1
                acc["standard_redemption_submitted"]["usd"]   += usd

            elif etype in ("ApproveRequest", "SafeApproveRequest") and vk in redemption_vaults:
                req_key = f"{ev.get('vault_address', '')}_{ev.get('request_id', '')}"
                req     = state["redemption_requests"].get(req_key, {})
                mtoken  = int(req.get("amount_mtoken_raw", 0))
                usd     = (mtoken / 1e18 * nav) if (nav and mtoken) else 0.0
                acc["standard_redemption_processed"]["count"] += 1
                acc["standard_redemption_processed"]["usd"]   += usd

            elif etype == "RejectRequest" and pcfg["has_reject"]:
                req_key = f"{ev.get('vault_address', '')}_{ev.get('request_id', '')}"
                req     = state["redemption_requests"].get(req_key, {})
                mtoken  = int(req.get("amount_mtoken_raw", 0))
                usd     = (mtoken / 1e18 * nav) if (nav and mtoken) else 0.0
                acc["standard_redemption_rejected"]["count"] += 1
                acc["standard_redemption_rejected"]["usd"]   += usd

        total_issuance   = (acc["instant_issuance"]["usd"]
                            + acc["standard_issuance_submitted"]["usd"])
        total_redemption = (acc["instant_redemption"]["usd"]
                            + acc["standard_redemption_submitted"]["usd"])
        acc["net_flow_usd"] = round(total_issuance - total_redemption, 2)

        # Round all USD values
        for k, v in acc.items():
            if isinstance(v, dict) and "usd" in v:
                v["usd"] = round(v["usd"], 2)

        result[product] = acc

    return result


# ── Step 3: SLA status ────────────────────────────────────────────────────────

def compute_sla_status(state: dict, nav_aum: dict, now: datetime) -> dict:
    """
    Identify all pending redemption requests, compute elapsed time, flag at
    48h, escalate at 96h. Aggregate per product.

    mTBILL has two redemption vaults (standard_redemption + instant_redemption
    MSL fallback) — both are included. Request keys are vault-scoped so there
    is no cross-vault ambiguity.
    """
    result: dict[str, dict] = {}

    for product in PRODUCTS:
        nav = nav_aum[product]["nav"]["current"] if nav_aum.get(product) else None
        cfg = PRODUCTS[product]

        open_requests = []
        for key, req in state.get("redemption_requests", {}).items():
            if req.get("product") != product:
                continue
            if req.get("status") != "pending":
                continue

            submitted_at = ts_to_dt(req["submitted_at"])
            age          = now - submitted_at
            age_hours    = age.total_seconds() / 3600

            mtoken_raw = int(req.get("amount_mtoken_raw", 0))
            usd        = round(mtoken_raw / 1e18 * nav, 2) if (nav and mtoken_raw) else 0.0

            open_requests.append({
                "request_key":    key,
                "investor":       req.get("investor", "unknown"),
                "vault_key":      req.get("vault_key", ""),
                "submitted_at":   req["submitted_at"],
                "age_hours":      round(age_hours, 2),
                "usd":            usd,
                "flag_48h":       age_hours >= cfg["standard_sla_h"],
                "escalation_96h": age_hours >= cfg["escalation_sla_h"],
            })

        open_requests.sort(key=lambda r: r["age_hours"], reverse=True)

        result[product] = {
            "open_count":           len(open_requests),
            "total_usd_pending":    round(sum(r["usd"] for r in open_requests), 2),
            "oldest_age_hours":     open_requests[0]["age_hours"] if open_requests else None,
            "flag_48h_count":       sum(1 for r in open_requests if r["flag_48h"]),
            "escalation_96h_count": sum(1 for r in open_requests if r["escalation_96h"]),
            "open_requests":        open_requests,
        }

    return result


# ── Step 4: Holder intelligence (Ethereum only) ────────────────────────────────

# Large-reduction thresholds (confirmed in session brief)
REDUCTION_MIN_PCT = 10.0    # ≥10% of previous balance
REDUCTION_MIN_USD = 50_000  # AND ≥$50,000 USD

def load_holder_snapshot() -> dict:
    if HOLDER_SNAP_FILE.exists():
        with HOLDER_SNAP_FILE.open() as f:
            return json.load(f)
    return {}

def save_holder_snapshot(snapshot: dict) -> None:
    snapshot["schema_version"] = "1"
    snapshot["last_updated"]   = utcnow()
    with HOLDER_SNAP_FILE.open("w") as f:
        json.dump(snapshot, f, indent=2)

ETHPLORER_API = "https://api.ethplorer.io"

def fetch_holders(token_address: str, limit: int = 100) -> list[dict]:
    """
    Fetch top holders via Ethplorer API (free public key).
    Etherscan tokenholderlist requires API Pro — confirmed Session 2.
    Returns list of {"address": str, "balance_tokens": float} sorted descending.
    """
    try:
        r = http.get(
            f"{ETHPLORER_API}/getTopTokenHolders/{token_address}",
            params={"apiKey": "freekey", "limit": limit},
            timeout=30,
        )
        data = r.json()
    except Exception as e:
        log.error(f"Ethplorer getTopTokenHolders failed for {token_address}: {e}")
        return []

    if "error" in data:
        log.warning(f"Ethplorer error for {token_address}: {data['error']}")
        return []

    # Ethplorer returns balance as raw units (18-decimal), not token units.
    return [
        {
            "address":        h["address"].lower(),
            "balance_tokens": float(h["balance"]) / 1e18,
        }
        for h in data.get("holders", [])
    ]

def compute_holder_intelligence(
    product: str,
    nav_aum: dict,
    prev_snapshot: dict,
) -> tuple[dict, list[dict]]:
    """
    Fetch current holder list via Ethplorer, compute top-10, new wallets,
    large reductions, and concentration.
    Returns (intelligence_dict, current_holders_list).
    current_holders_list is persisted to holder_snapshot.json at end of run.
    """
    cfg        = PRODUCTS[product]
    nav        = nav_aum[product]["nav"]["current"]
    eth_supply = nav_aum[product]["aum"]["total_supply_ethereum"]

    holders = fetch_holders(cfg["etherscan_token"], limit=100)
    if not holders:
        log.warning(f"{product}: no holder data returned")
        return {
            "top_10": [], "top5_concentration_pct": None,
            "new_wallets": [], "large_reductions": [],
        }, []

    # Top 10
    top_10 = []
    for i, h in enumerate(holders[:10]):
        bal    = h["balance_tokens"]
        share  = (bal / eth_supply * 100) if eth_supply else 0.0
        top_10.append({
            "rank":           i + 1,
            "address":        h["address"],
            "balance_tokens": round(bal, 4),
            "usd":            round(bal * nav, 2),
            "share_pct":      round(share, 4),
        })

    # Top-5 concentration
    top5_bal = sum(h["balance_tokens"] for h in holders[:5])
    top5_pct = round(top5_bal / eth_supply * 100, 2) if eth_supply else None

    # Compare to previous snapshot — keyed by address, value is balance_tokens float
    prev_holders: dict[str, float] = {
        h["address"]: float(h["balance_tokens"])
        for h in prev_snapshot.get(product, {}).get("holders", [])
    }
    curr_holders: dict[str, float] = {h["address"]: h["balance_tokens"] for h in holders}

    new_wallets: list[dict] = []
    if prev_holders:
        for addr, bal in curr_holders.items():
            if addr not in prev_holders and bal > 0:
                new_wallets.append({
                    "address":        addr,
                    "balance_tokens": round(bal, 4),
                    "usd":            round(bal * nav, 2),
                })
        new_wallets.sort(key=lambda x: x["usd"], reverse=True)

    large_reductions: list[dict] = []
    if prev_holders:
        for addr, prev_bal in prev_holders.items():
            curr_bal = curr_holders.get(addr, 0.0)
            if curr_bal >= prev_bal:
                continue
            reduction     = prev_bal - curr_bal
            reduction_pct = (reduction / prev_bal * 100) if prev_bal else 0.0
            reduction_usd = reduction * nav
            if reduction_pct >= REDUCTION_MIN_PCT and reduction_usd >= REDUCTION_MIN_USD:
                large_reductions.append({
                    "address":             addr,
                    "prev_balance_tokens": round(prev_bal, 4),
                    "curr_balance_tokens": round(curr_bal, 4),
                    "reduction_usd":       round(reduction_usd, 2),
                    "reduction_pct":       round(reduction_pct, 2),
                })
        large_reductions.sort(key=lambda x: x["reduction_usd"], reverse=True)

    intelligence = {
        "top_10":                 top_10,
        "top5_concentration_pct": top5_pct,
        "new_wallets":            new_wallets,
        "large_reductions":       large_reductions,
    }
    return intelligence, holders


# ── Step 5: Claude API narrative ──────────────────────────────────────────────

CLAUDE_SYSTEM = (
    "You are an investment operations analyst writing the Flags & Exceptions section "
    "of a daily operations report. Your audience is the Midas investment operations team. "
    "Write in the voice of a sharp ops professional: concise, prioritised, no padding. "
    "Two to four sentences unless there is a genuine escalation or rejection to flag. "
    "Operator action items first. Investor behaviour observations second. "
    "Do not use bullet points or headers — plain prose only. "
    "Do not repeat numbers already in the report — reference them by significance, not by restating them. "
    "Do not mention first run limitations, data sourcing instructions, or system setup notes — these are internal concerns and must not appear in the report. "
    "Do not flag high holder concentration as a risk unless it has changed materially since the last report — for newly launched institutional tokenised funds, high top-5 concentration is structural and expected. Only flag concentration if a large holder is actively reducing, a new large wallet has entered, or an unusual pattern is present. "
    "Do not include observations about fund growth, wallet growth momentum, or organic adoption — these are business observations, not operational flags. The narrative must only contain items that are factually relevant to operations or require attention or action."
)

def build_claude_prompt(
    product: str,
    report_date: str,
    nav_data: dict,
    aum_data: dict,
    activity: dict,
    sla: dict,
    holder_intel: dict,
) -> str:
    n   = nav_data
    a   = activity
    s   = sla
    h   = holder_intel
    cfg = PRODUCTS[product]

    change_str = (
        f"{'+' if n['change_24h'] >= 0 else ''}{n['change_24h_pct']:+.4f}% vs yesterday"
        if n["change_24h"] is not None else "first run — no prior NAV on file"
    )

    rejection_line = ""
    if cfg["has_reject"] and a["standard_redemption_rejected"] and a["standard_redemption_rejected"]["count"] > 0:
        r = a["standard_redemption_rejected"]
        rejection_line = f"\n- Rejected requests: {r['count']}, ${r['usd']:,.0f}"

    oldest_str = f"{s['oldest_age_hours']:.1f}h" if s["oldest_age_hours"] is not None else "none"

    reduction_detail = ""
    if h["large_reductions"]:
        top_r = h["large_reductions"][0]
        reduction_detail = f" Largest single reduction: ${top_r['reduction_usd']:,.0f} ({top_r['reduction_pct']:.1f}% of prior position)."

    new_wallet_str = str(len(h["new_wallets"])) if h["new_wallets"] is not None else "n/a (first run)"
    reduction_str  = str(len(h["large_reductions"])) if h["large_reductions"] is not None else "n/a (first run)"
    top5_str       = f"{h['top5_concentration_pct']:.1f}%" if h["top5_concentration_pct"] is not None else "unavailable"

    return f"""Daily ops summary for {cfg['display_name']} — {report_date}

NAV: ${n['current']:.6f} ({change_str})
AUM: ${aum_data['aum_usd']:,.0f}

24hr activity:
- Instant issuance: {a['instant_issuance']['count']} transactions, ${a['instant_issuance']['usd']:,.0f}
- Standard issuance submitted: {a['standard_issuance_submitted']['count']} | processed: {a['standard_issuance_processed']['count']}
- Instant redemptions: {a['instant_redemption']['count']} transactions, ${a['instant_redemption']['usd']:,.0f}
- Standard redemptions submitted: {a['standard_redemption_submitted']['count']} | processed: {a['standard_redemption_processed']['count']}{rejection_line}

Open standard redemption requests: {s['open_count']}
Total USD pending: ${s['total_usd_pending']:,.0f}
Oldest request age: {oldest_str}
Requests approaching 48hr SLA: {s['flag_48h_count']}
Requests exceeding 96hr escalation threshold: {s['escalation_96h_count']}

Holder intelligence (Ethereum):
- Top 5 concentration: {top5_str}
- New wallets entering: {new_wallet_str}
- Large position reductions: {reduction_str}{reduction_detail}

Flag anything operationally significant. If nothing requires attention, say so briefly."""


_anthropic_client: anthropic.Anthropic | None = None

def _get_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        # SDK reads ANTHROPIC_API_KEY from environment automatically.
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client

def call_claude(prompt: str) -> str:
    try:
        msg = _get_client().messages.create(
            model      = CLAUDE_MODEL,
            max_tokens = 300,
            system     = CLAUDE_SYSTEM,
            messages   = [{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log.error(f"Claude API call failed: {e}")
        return "Narrative unavailable — API call failed."


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("Analyst starting")
    now          = datetime.now(timezone.utc)
    window_end   = now
    window_start = now - timedelta(hours=24)

    # ── Step 1: NAV and AUM ───────────────────────────────────────────────────
    log.info("── Step 1: NAV and AUM ──────────────────────────────────────────")
    nav_history = load_nav_history()
    nav_aum     = compute_nav_and_aum(nav_history)

    for product, data in nav_aum.items():
        if data is None:
            log.error(f"{product}: no NAV data — cannot proceed")
            return
        n = data["nav"]
        a = data["aum"]
        change_str = (
            f"{'+' if n['change_24h'] >= 0 else ''}{n['change_24h']:.6f} "
            f"({'+' if n['change_24h_pct'] >= 0 else ''}{n['change_24h_pct']:.4f}%)"
            if n["change_24h"] is not None else "N/A (first run)"
        )
        log.info(
            f"  {product}: NAV=${n['current']:.6f} | 24h change={change_str} | "
            f"AUM=${a['aum_usd']:,.0f}"
        )

    log.info("Step 1 complete")

    # ── Step 2: 24hr activity breakdown ──────────────────────────────────────
    log.info("── Step 2: 24hr activity breakdown ─────────────────────────────")
    state    = load_state()
    activity = compute_activity(state, nav_aum, window_start, window_end)

    for product, act in activity.items():
        log.info(f"  {product}:")
        log.info(f"    instant_issuance         : {act['instant_issuance']['count']:>3} tx  ${act['instant_issuance']['usd']:>12,.0f}")
        log.info(f"    std_issuance_submitted   : {act['standard_issuance_submitted']['count']:>3} tx  ${act['standard_issuance_submitted']['usd']:>12,.0f}")
        log.info(f"    std_issuance_processed   : {act['standard_issuance_processed']['count']:>3} tx  ${act['standard_issuance_processed']['usd']:>12,.0f}")
        log.info(f"    instant_redemption       : {act['instant_redemption']['count']:>3} tx  ${act['instant_redemption']['usd']:>12,.0f}")
        log.info(f"    std_redemption_submitted : {act['standard_redemption_submitted']['count']:>3} tx  ${act['standard_redemption_submitted']['usd']:>12,.0f}")
        log.info(f"    std_redemption_processed : {act['standard_redemption_processed']['count']:>3} tx  ${act['standard_redemption_processed']['usd']:>12,.0f}")
        if act["standard_redemption_rejected"] is not None:
            log.info(f"    std_redemption_rejected  : {act['standard_redemption_rejected']['count']:>3} tx  ${act['standard_redemption_rejected']['usd']:>12,.0f}")
        log.info(f"    net_flow_usd             : ${act['net_flow_usd']:>12,.0f}")

    log.info("Step 2 complete")

    # ── Step 3: SLA status ────────────────────────────────────────────────────
    log.info("── Step 3: SLA status ───────────────────────────────────────────")
    sla = compute_sla_status(state, nav_aum, now)

    for product, s in sla.items():
        log.info(
            f"  {product}: {s['open_count']} open | "
            f"${s['total_usd_pending']:,.0f} pending | "
            f"oldest={s['oldest_age_hours']}h | "
            f"48h_flag={s['flag_48h_count']} | "
            f"96h_escalation={s['escalation_96h_count']}"
        )
        for req in s["open_requests"]:
            flag = " ⚠ 48H" if req["flag_48h"] else ""
            esc  = " 🔴 96H ESCALATION" if req["escalation_96h"] else ""
            log.info(
                f"    {req['vault_key']} | "
                f"{req['investor'][:10]}... | "
                f"${req['usd']:,.0f} | "
                f"{req['age_hours']:.1f}h{flag}{esc}"
            )

    log.info("Step 3 complete")

    # ── Step 4: Holder intelligence ───────────────────────────────────────────
    log.info("── Step 4: Holder intelligence ──────────────────────────────────")
    prev_snapshot    = load_holder_snapshot()
    holder_intel     = {}
    current_holders  = {}   # used to build new snapshot at end of run

    for product in PRODUCTS:
        intel, holders = compute_holder_intelligence(product, nav_aum, prev_snapshot)
        holder_intel[product]    = intel
        current_holders[product] = holders

        log.info(f"  {product}: {len(intel['top_10'])} top holders fetched | top5={intel['top5_concentration_pct']}%")
        for h in intel["top_10"]:
            log.info(f"    #{h['rank']:>2}  {h['address'][:10]}...  {h['balance_tokens']:>14,.2f} tokens  ${h['usd']:>12,.0f}  ({h['share_pct']:.2f}%)")
        if intel["new_wallets"]:
            log.info(f"    New wallets: {len(intel['new_wallets'])}")
        if intel["large_reductions"]:
            log.info(f"    Large reductions: {len(intel['large_reductions'])}")
            for r in intel["large_reductions"]:
                log.info(f"      {r['address'][:10]}... reduced by ${r['reduction_usd']:,.0f} ({r['reduction_pct']:.1f}%)")

    log.info("Step 4 complete")

    # ── Step 5: Claude API narrative ──────────────────────────────────────────
    log.info("── Step 5: Claude API narrative ─────────────────────────────────")
    report_date = now.strftime("%Y-%m-%d")
    narratives: dict[str, str] = {}

    for product in PRODUCTS:
        if nav_aum.get(product) is None:
            narratives[product] = "Narrative unavailable — NAV data missing."
            continue
        prompt = build_claude_prompt(
            product     = product,
            report_date = report_date,
            nav_data    = nav_aum[product]["nav"],
            aum_data    = nav_aum[product]["aum"],
            activity    = activity[product],
            sla         = sla[product],
            holder_intel= holder_intel[product],
        )
        log.info(f"  {product}: calling Claude API...")
        narrative = call_claude(prompt)
        narratives[product] = narrative
        log.info(f"  {product} narrative:\n    {narrative}")

    log.info("Step 5 complete")

    # ── Step 6: Assemble output and write all files ───────────────────────────
    log.info("── Step 6: Output ───────────────────────────────────────────────")

    output = {
        "meta": {
            "schema_version": "1",
            "generated_at":   utcnow(),
            "report_date":    report_date,
            "window_start":   window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_end":     window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }

    for product in PRODUCTS:
        output[product] = {
            **nav_aum[product],
            "activity_24h":        activity[product],
            "sla_status":          sla[product],
            "holder_intelligence": holder_intel[product],
            "flags_and_exceptions": narratives[product],
        }

    with OUTPUT_FILE.open("w") as f:
        json.dump(output, f, indent=2)
    log.info(f"analyst_output.json written → {OUTPUT_FILE}")

    # nav_history.json — written at end of successful run only
    for product in PRODUCTS:
        if nav_aum.get(product):
            nav_history[product] = {
                "nav":         nav_aum[product]["nav"]["current"],
                "recorded_at": utcnow(),
            }
    save_nav_history(nav_history)
    log.info(f"nav_history.json written → {NAV_HISTORY_FILE}")

    # holder_snapshot.json — written at end of successful run only
    new_snapshot: dict = {}
    for product, holders in current_holders.items():
        new_snapshot[product] = {
            "snapshot_at": utcnow(),
            "holders": [
                {"address": h["address"], "balance_tokens": h["balance_tokens"]}
                for h in holders
            ],
        }
    save_holder_snapshot(new_snapshot)
    log.info(f"holder_snapshot.json written → {HOLDER_SNAP_FILE}")

    log.info("Analyst complete")


if __name__ == "__main__":
    main()
