"""
midas_bot.py — Midas Watcher Agent

Polls all vault contracts every POLL_INTERVAL_SECONDS.
Writes lifecycle records to data/vault_state.json.
Sends Telegram alerts for operational events.

Vaults covered:
  mTBILL  — Ethereum: issuance, standard_redemption, instant_redemption
           — Base:     issuance, instant_redemption
  mF-ONE  — Ethereum: issuance, redemption

Event set confirmed against live contracts in Session 1 exploration.
Key findings carried into this implementation:
  - mTBILL instant_redemption vault also emits RedeemRequest/ApproveRequest
    when MSL pool is depleted (standard queue fallback) — tracked per-vault.
  - mF-ONE redemption vault uses both ApproveRequest and SafeApproveRequest
    to close standard redemptions — both stop the SLA clock.
  - SafeApproveRequest appears on issuance vaults (closes DepositRequest)
    and redemption vaults (closes RedeemRequest) — vault role disambiguates.
  - Oracle: latestRoundData(), answer field, 8 decimals on both products.
"""

import os, json, time, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from web3 import Web3
import requests as http
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
POLL_INTERVAL  = int(os.environ["POLL_INTERVAL_SECONDS"])
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT  = os.environ["TELEGRAM_CHAT_ID"]
ALCHEMY_ETH    = os.environ["ALCHEMY_ETH_URL"]
ALCHEMY_BASE   = os.environ["ALCHEMY_BASE_URL"]
ETHERSCAN_KEY  = os.environ["ETHERSCAN_API_KEY"]

# getLogs data sources (confirmed in Session 1 against free-tier limits):
#   Ethereum: Etherscan API V2 — arbitrary block range, paginated
#   Base:     public Base RPC  — no auth required, supports up to ~10k blocks
#   Alchemy:  oracle / block number / timestamps only (10-block getLogs limit on free tier)
ETHERSCAN_API = "https://api.etherscan.io/v2/api"
BASE_PUBLIC_RPC = "https://mainnet.base.org"

ROOT       = Path(__file__).parent
STATE_FILE = ROOT / "data" / "vault_state.json"

DISPLAY_NAME = {"mTBILL": "mTBILL", "mfONE": "mF-ONE"}
LOG_FILE   = ROOT / "logs" / "midas_bot.log"
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

# ── Oracle config (Chainlink-compatible, 8 decimals, confirmed Session 1) ──────
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

ORACLE_ADDRESS = {
    "mTBILL": "0x056339C044055819E8Db84E71f5f2E1F536b2E5b",
    "mfONE":  "0x8D51DBC85cEef637c97D02bdaAbb5E274850e68C",
}

def get_nav(product: str) -> float | None:
    """Return current NAV per token in USD. Returns None on failure."""
    try:
        addr = ORACLE_ADDRESS[product]
        c = W3["ethereum"].eth.contract(
            address=Web3.to_checksum_address(addr), abi=ORACLE_ABI
        )
        _, answer, _, _, _ = c.functions.latestRoundData().call()
        return answer / 1e8
    except Exception as e:
        log.warning(f"Oracle lookup failed for {product}: {e}")
        return None

# ── Event topic constants (confirmed from ABI exploration, Session 1) ──────────
TOPIC = {
    "DepositInstant":                    "0xdd6865ec496cf9bdd5cb1661ab84cf4e86edc877208a54cbf642f69d744530c5",
    "DepositInstantWithCustomRecipient": "0xe8bfe7b6cdaff26f82915adfad787fe8cc232bf312d39f4eab839d013e65da5a",
    "DepositRequest":                    "0x3704c9b13a68ac43d7f8a85f2700f0b4f89a11ed9e2bcac5324f0d228d409009",
    "ApproveRequest":                    "0xf7d1fde87f32720fc30ce6847e0aae77e640b59bfac41b11b270358ccfa7a0ac",
    "SafeApproveRequest":                "0x03ea09e71742c9c754c9746b3e671ecb27fc372e3d29c31bac0192458ffd9d4b",
    "RejectRequest":                     "0x00ce63cc55966b103e4f4cb39f3426cb91718ad4f8eb4ad08c14a7ee749d8157",
    "RedeemRequest":                     "0x55ba94d231fa70a45e82b0a1c6a60ef72e41bb2455385128ee5cf8d98c0c1c0e",
    "RedeemRequestWithCustomRecipient":  "0x691cd372bb63a5126a324513b634040d0ba3747c0a625207d99b6ba302c51a23",
    "RedeemInstant":                     "0x1af12536d161c2c30ad907b0abe442f94c4a7824f2463585b3fc893275247cce",
    "RedeemInstantWithCustomRecipient":  "0x4fd0e2f3f27549d8d0c242f7193eaa0f61546e887fec39e69dfbff6b2384a4c3",
    "WithdrawToken":                     "0x9ca7c1e047552a8048d924a5a8d3c150eb861086a72a9100e5f19d1176c1b746",
    "Paused":                            "0x62e78cea01bee320cd4e420270b5ea74000d11b0c9f74754ebdbfc544b05a258",
    "Unpaused":                          "0x5db9ee0a495bf2e6ff9c91a7834c1ba4fdd244a5e8aa4e537bd38aeae4b073aa",
}
TOPIC_TO_NAME = {v: k for k, v in TOPIC.items()}

# ── Vault definitions ──────────────────────────────────────────────────────────
# Key: (product, chain, role)
# role drives SLA routing and alert classification
# monitor: set of event names to watch on this vault
VAULTS = {
    ("mTBILL", "ethereum", "issuance"): {
        "address": "0x99361435420711723aF805F08187c9E6bF796683",
        "monitor": {
            "DepositInstant", "DepositInstantWithCustomRecipient",
            "DepositRequest", "SafeApproveRequest",
            "Paused", "Unpaused",
        },
    },
    ("mTBILL", "ethereum", "standard_redemption"): {
        "address": "0xF6e51d24F4793Ac5e71e0502213a9BBE3A6d4517",
        "monitor": {
            "RedeemRequest", "RedeemRequestWithCustomRecipient",
            "ApproveRequest", "SafeApproveRequest",
            "Paused", "Unpaused",
        },
    },
    # This vault handles both instant exits and standard-queue fallback
    # when the MSL pool is depleted — confirmed from on-chain history.
    ("mTBILL", "ethereum", "instant_redemption"): {
        "address": "0x569D7dccBF6923350521ecBC28A555A500c4f0Ec",
        "monitor": {
            "RedeemInstant", "RedeemInstantWithCustomRecipient",
            "RedeemRequest", "RedeemRequestWithCustomRecipient",
            "ApproveRequest", "SafeApproveRequest",
            "WithdrawToken", "Paused", "Unpaused",
        },
    },
    ("mTBILL", "base", "issuance"): {
        "address": "0x8978e327FE7C72Fa4eaF4649C23147E279ae1470",
        "monitor": {"DepositInstant", "DepositInstantWithCustomRecipient"},
    },
    ("mTBILL", "base", "instant_redemption"): {
        "address": "0x2a8c22E3b10036f3AEF5875d04f8441d4188b656",
        "monitor": {"RedeemInstant", "RedeemInstantWithCustomRecipient"},
    },
    ("mfONE", "ethereum", "issuance"): {
        "address": "0x41438435c20B1C2f1fcA702d387889F346A0C3DE",
        "monitor": {
            "DepositInstant", "DepositInstantWithCustomRecipient",
            "DepositRequest", "SafeApproveRequest",
            "Paused", "Unpaused",
        },
    },
    # Single vault handles all redemption flows for mF-ONE.
    # Both ApproveRequest and SafeApproveRequest close RedeemRequests here —
    # confirmed from on-chain history (27 ApproveRequest, 18 SafeApproveRequest).
    ("mfONE", "ethereum", "redemption"): {
        "address": "0x44b0440e35c596e858cEA433D0d82F5a985fD19C",
        "monitor": {
            "RedeemRequest", "RedeemRequestWithCustomRecipient",
            "RedeemInstant", "RedeemInstantWithCustomRecipient",
            "ApproveRequest", "SafeApproveRequest",
            "RejectRequest", "WithdrawToken",
            "Paused", "Unpaused",
        },
    },
}

def vault_key(product: str, chain: str, role: str) -> str:
    return f"{product}_{chain}_{role}"

# Reverse map: vault_key → vault address, for alert enrichment lookups
VAULT_KEY_TO_ADDRESS = {
    vault_key(p, c, r): cfg["address"]
    for (p, c, r), cfg in VAULTS.items()
}

# ── State file ─────────────────────────────────────────────────────────────────
def empty_state() -> dict:
    return {
        "meta": {"schema_version": "1", "last_updated": None},
        "last_seen_blocks": {
            "ethereum": {vault_key(p, c, r): 0 for (p, c, r) in VAULTS if c == "ethereum"},
            "base":     {vault_key(p, c, r): 0 for (p, c, r) in VAULTS if c == "base"},
        },
        "redemption_requests": {},
        "issuance_requests":   {},
        "events":              [],
    }

def load_state() -> dict:
    if STATE_FILE.exists():
        with STATE_FILE.open() as f:
            return json.load(f)
    return empty_state()

def save_state(state: dict) -> None:
    state["meta"]["last_updated"] = utcnow()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    state["events"] = [e for e in state["events"] if e.get("timestamp", "") >= cutoff]
    with STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2)

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ── Low-level helpers ──────────────────────────────────────────────────────────
def to_hex(val) -> str:
    if isinstance(val, bytes):
        return "0x" + val.hex()
    return val

def to_int(val) -> int:
    if isinstance(val, int):
        return val
    return int(val, 16)

def data_word(data_hex: str, n: int) -> int:
    """Return the nth 32-byte ABI word from a stripped hex data string."""
    start, end = n * 64, (n + 1) * 64
    return int(data_hex[start:end], 16) if len(data_hex) >= end else 0

def request_key(vault_address: str, request_id: int) -> str:
    """Stable key for a request: vault_address_requestId.
    Scopes SLA tracking to the originating vault — cross-vault matches
    are structurally impossible with this key."""
    return f"{vault_address.lower()}_{request_id}"

def fmt_addr(addr: str) -> str:
    return addr[:6] + "..." + addr[-4:]

def fmt_usd(usd: float) -> str:
    if usd >= 1_000_000:
        return f"${usd / 1_000_000:.2f}M"
    if usd >= 1_000:
        return f"${usd:,.0f}"
    return f"${usd:.2f}"

def fmt_usd_from_norm18(raw: int) -> str:
    """Format a Midas 18-decimal normalised USD amount (all vault event amounts use this)."""
    return fmt_usd(raw / 1e18)

def fmt_usd_from_mtoken(mtoken_raw: int, nav: float | None) -> str:
    if nav is None:
        return f"{mtoken_raw / 1e18:,.4f} tokens (NAV unavailable)"
    return fmt_usd(mtoken_raw / 1e18 * nav)

# Block timestamp cache — capped at 200 entries to bound memory over long runs.
# Evicts the oldest half when full.
_BLOCK_TS_CACHE: dict[int, str] = {}
_BLOCK_TS_CACHE_MAX = 200

def block_to_ts(w3_conn: Web3, block_num: int) -> str:
    if block_num not in _BLOCK_TS_CACHE:
        if len(_BLOCK_TS_CACHE) >= _BLOCK_TS_CACHE_MAX:
            # Drop the oldest half by sorted block number
            keep = sorted(_BLOCK_TS_CACHE)[-(_BLOCK_TS_CACHE_MAX // 2):]
            for k in list(_BLOCK_TS_CACHE):
                if k not in keep:
                    del _BLOCK_TS_CACHE[k]
        try:
            b = w3_conn.eth.get_block(block_num)
            _BLOCK_TS_CACHE[block_num] = datetime.fromtimestamp(
                b["timestamp"], tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            _BLOCK_TS_CACHE[block_num] = utcnow()
    return _BLOCK_TS_CACHE[block_num]

def tx_url(chain: str, tx_hash: str) -> str:
    if chain == "base":
        return f"https://basescan.org/tx/{tx_hash}"
    return f"https://etherscan.io/tx/{tx_hash}"

# ── Event decoding ─────────────────────────────────────────────────────────────
def decode_log(raw_log: dict) -> dict | None:
    """
    Decode a raw web3 log into a structured dict.
    Returns None if the topic is not in our watch set.

    Parameter layouts confirmed from live ABI exploration (Session 1):
      DepositInstant(user[idx], tokenIn[idx], amountUsd, amountToken, fee, minted, referrerId)
      DepositRequest(requestId[idx], user[idx], tokenIn[idx], amountToken, amountUsd, fee, ...)
      RedeemRequest(requestId[idx], user[idx], tokenOut[idx], amountMTokenIn, feeAmount)
      ApproveRequest(requestId[idx], newOutRate)
      SafeApproveRequest(requestId[idx], newOutRate)
      RejectRequest(requestId[idx], user[idx])
      RedeemInstant(user[idx], tokenOut[idx], amount, feeAmount, amountTokenOut)
      WithdrawToken(caller[idx], token[idx], withdrawTo[idx], amount)
    """
    topics = raw_log.get("topics", [])
    if not topics:
        return None
    t0   = to_hex(topics[0])
    name = TOPIC_TO_NAME.get(t0)
    if name is None:
        return None

    topics_hex = [to_hex(t) for t in topics]
    data_raw   = to_hex(raw_log.get("data", "0x"))[2:]

    def addr(t: str) -> str:
        return "0x" + t[-40:].lower()

    r: dict = {"event": name}

    if name in ("DepositInstant", "DepositInstantWithCustomRecipient"):
        r["investor"]         = addr(topics_hex[1])
        r["token_in"]         = addr(topics_hex[2])
        r["amount_usd_raw"]   = data_word(data_raw, 0)
        r["amount_token_raw"] = data_word(data_raw, 1)
        r["fee_raw"]          = data_word(data_raw, 2)
        r["minted_raw"]       = data_word(data_raw, 3)

    elif name in ("DepositRequest", "DepositRequestWithCustomRecipient"):
        r["request_id"]       = to_int(topics_hex[1])
        r["investor"]         = addr(topics_hex[2])
        r["token_in"]         = addr(topics_hex[3])
        r["amount_token_raw"] = data_word(data_raw, 0)
        r["amount_usd_raw"]   = data_word(data_raw, 1)
        r["fee_raw"]          = data_word(data_raw, 2)

    elif name in ("RedeemRequest", "RedeemRequestWithCustomRecipient"):
        r["request_id"]        = to_int(topics_hex[1])
        r["investor"]          = addr(topics_hex[2])
        r["token_out"]         = addr(topics_hex[3])
        r["amount_mtoken_raw"] = data_word(data_raw, 0)
        r["fee_amount_raw"]    = data_word(data_raw, 1)

    elif name in ("ApproveRequest", "SafeApproveRequest"):
        r["request_id"]   = to_int(topics_hex[1])
        r["new_out_rate"] = data_word(data_raw, 0)

    elif name == "RejectRequest":
        r["request_id"] = to_int(topics_hex[1])
        r["investor"]   = addr(topics_hex[2])

    elif name in ("RedeemInstant", "RedeemInstantWithCustomRecipient"):
        r["investor"]             = addr(topics_hex[1])
        r["token_out"]            = addr(topics_hex[2])
        r["amount_mtoken_raw"]    = data_word(data_raw, 0)
        r["fee_amount_raw"]       = data_word(data_raw, 1)
        r["amount_token_out_raw"] = data_word(data_raw, 2)

    elif name == "WithdrawToken":
        r["caller"]      = addr(topics_hex[1])
        r["token"]       = addr(topics_hex[2])
        r["withdraw_to"] = addr(topics_hex[3])
        r["amount_raw"]  = data_word(data_raw, 0)

    # Paused / Unpaused carry no additional fields

    return r

# ── State mutation helpers ─────────────────────────────────────────────────────
def open_redemption_request(
    state: dict, product: str, chain: str, role: str,
    vault_addr: str, decoded: dict,
    tx_hash: str, block_num: int, timestamp: str,
) -> None:
    key = request_key(vault_addr, decoded["request_id"])
    state["redemption_requests"][key] = {
        "request_key":       key,
        "product":           product,
        "vault_key":         vault_key(product, chain, role),
        "vault_address":     vault_addr.lower(),
        "chain":             chain,
        "request_id":        str(decoded["request_id"]),
        "investor":          decoded.get("investor"),
        "token_out":         decoded.get("token_out"),
        "amount_mtoken_raw": str(decoded.get("amount_mtoken_raw", 0)),
        "fee_amount_raw":    str(decoded.get("fee_amount_raw", 0)),
        "submitted_at":      timestamp,
        "submitted_block":   block_num,
        "submitted_tx":      tx_hash,
        "status":            "pending",
        "resolved_at":       None,
        "resolved_block":    None,
        "resolved_tx":       None,
        "resolution_type":   None,
    }

def close_redemption_request(
    state: dict, vault_addr: str, decoded: dict, resolution: str,
    tx_hash: str, block_num: int, timestamp: str,
) -> None:
    key = request_key(vault_addr, decoded["request_id"])
    req = state["redemption_requests"].get(key)
    if req:
        req.update({
            "status":          resolution,
            "resolved_at":     timestamp,
            "resolved_block":  block_num,
            "resolved_tx":     tx_hash,
            "resolution_type": resolution,
        })
    else:
        log.warning(
            f"Resolution event for unknown redemption request: "
            f"vault={vault_addr} id={decoded['request_id']} type={resolution}"
        )

def open_issuance_request(
    state: dict, product: str, chain: str, role: str,
    vault_addr: str, decoded: dict,
    tx_hash: str, block_num: int, timestamp: str,
) -> None:
    key = request_key(vault_addr, decoded["request_id"])
    state["issuance_requests"][key] = {
        "request_key":      key,
        "product":          product,
        "vault_key":        vault_key(product, chain, role),
        "vault_address":    vault_addr.lower(),
        "chain":            chain,
        "request_id":       str(decoded["request_id"]),
        "investor":         decoded.get("investor"),
        "token_in":         decoded.get("token_in"),
        "amount_token_raw": str(decoded.get("amount_token_raw", 0)),
        "amount_usd_raw":   str(decoded.get("amount_usd_raw", 0)),
        "fee_raw":          str(decoded.get("fee_raw", 0)),
        "submitted_at":     timestamp,
        "submitted_block":  block_num,
        "submitted_tx":     tx_hash,
        "status":           "pending",
        "resolved_at":      None,
        "resolved_tx":      None,
        "resolution_type":  None,
    }

def close_issuance_request(
    state: dict, vault_addr: str, decoded: dict, resolution: str,
    tx_hash: str, block_num: int, timestamp: str,
) -> None:
    key = request_key(vault_addr, decoded["request_id"])
    req = state["issuance_requests"].get(key)
    if req:
        req.update({
            "status":          resolution,
            "resolved_at":     timestamp,
            "resolved_block":  block_num,
            "resolved_tx":     tx_hash,
            "resolution_type": resolution,
        })
    else:
        log.warning(
            f"Approval for unknown issuance request: "
            f"vault={vault_addr} id={decoded['request_id']}"
        )

# ── Alert building ─────────────────────────────────────────────────────────────
def build_alert(
    product: str, chain: str, role: str,
    decoded: dict, tx_hash: str, timestamp: str,
    state: dict, nav_cache: dict,
) -> str | None:
    name        = decoded["event"]
    chain_label = chain.capitalize()
    link        = f'<a href="{tx_url(chain, tx_hash)}">View on Etherscan</a>'

    if name in ("RedeemRequest", "RedeemRequestWithCustomRecipient"):
        if product not in nav_cache:
            nav_cache[product] = get_nav(product)
        amt = fmt_usd_from_mtoken(decoded.get("amount_mtoken_raw", 0), nav_cache[product])
        if "instant" in role:
            vault_label = "Instant Redemption vault (standard queue — pool depleted)"
        else:
            vault_label = "Standard Redemption vault"
        return (
            f"🔵 REDEEM REQUEST — {DISPLAY_NAME.get(product, product)}\n"
            f"Investor: {fmt_addr(decoded.get('investor', 'unknown'))}\n"
            f"Amount: {amt}\n"
            f"Chain: {chain_label} | {vault_label}\n"
            f"Time: {timestamp}\n"
            f"{link}"
        )

    if name in ("ApproveRequest", "SafeApproveRequest") and "issuance" not in role:
        label    = "SAFE APPROVED" if name == "SafeApproveRequest" else "APPROVED"
        req_id   = decoded["request_id"]
        vault_addr = VAULT_KEY_TO_ADDRESS[vault_key(product, chain, role)]
        req      = state["redemption_requests"].get(request_key(vault_addr, req_id), {})
        if product not in nav_cache:
            nav_cache[product] = get_nav(product)
        amt_raw  = int(req.get("amount_mtoken_raw", 0))
        amt      = fmt_usd_from_mtoken(amt_raw, nav_cache[product]) if amt_raw else "unknown"
        investor = req.get("investor", "unknown")
        return (
            f"✅ REDEEM {label} — {DISPLAY_NAME.get(product, product)}\n"
            f"Investor: {fmt_addr(investor)}\n"
            f"Amount: {amt}\n"
            f"Request ID: {req_id} | Chain: {chain_label}\n"
            f"Time: {timestamp}\n"
            f"{link}"
        )

    if name == "RejectRequest":
        req_id     = decoded["request_id"]
        vault_addr = VAULT_KEY_TO_ADDRESS[vault_key(product, chain, role)]
        req        = state["redemption_requests"].get(request_key(vault_addr, req_id), {})
        if product not in nav_cache:
            nav_cache[product] = get_nav(product)
        amt_raw  = int(req.get("amount_mtoken_raw", 0))
        amt      = fmt_usd_from_mtoken(amt_raw, nav_cache[product]) if amt_raw else "unknown"
        return (
            f"🔴 REDEEM REJECTED — {DISPLAY_NAME.get(product, product)}\n"
            f"Investor: {fmt_addr(decoded.get('investor', 'unknown'))}\n"
            f"Amount: {amt}\n"
            f"Request ID: {req_id} | Chain: {chain_label}\n"
            f"Time: {timestamp}\n"
            f"{link}"
        )

    if name in ("DepositRequest", "DepositRequestWithCustomRecipient"):
        amt = fmt_usd_from_norm18(decoded.get("amount_usd_raw", 0))
        return (
            f"🟡 ISSUANCE REQUEST — {DISPLAY_NAME.get(product, product)}\n"
            f"Investor: {fmt_addr(decoded.get('investor', 'unknown'))}\n"
            f"Amount: {amt}\n"
            f"Chain: {chain_label}\n"
            f"Time: {timestamp}\n"
            f"{link}"
        )

    if name == "SafeApproveRequest" and "issuance" in role:
        req_id     = decoded["request_id"]
        vault_addr = VAULT_KEY_TO_ADDRESS[vault_key(product, chain, role)]
        req        = state["issuance_requests"].get(request_key(vault_addr, req_id), {})
        amt_raw    = int(req.get("amount_usd_raw", 0))
        amt        = fmt_usd_from_norm18(amt_raw) if amt_raw else "unknown"
        return (
            f"✅ ISSUANCE APPROVED — {DISPLAY_NAME.get(product, product)}\n"
            f"Investor: {fmt_addr(req.get('investor', 'unknown'))}\n"
            f"Amount: {amt}\n"
            f"Request ID: {req_id} | Chain: {chain_label}\n"
            f"Time: {timestamp}\n"
            f"{link}"
        )

    if name == "Paused":
        return (
            f"⛔ CONTRACT PAUSED — {DISPLAY_NAME.get(product, product)}\n"
            f"Vault: {role} | Chain: {chain_label}\n"
            f"Time: {timestamp}\n"
            f"{link}"
        )

    if name == "Unpaused":
        return (
            f"🟢 CONTRACT UNPAUSED — {DISPLAY_NAME.get(product, product)}\n"
            f"Vault: {role} | Chain: {chain_label}\n"
            f"Time: {timestamp}\n"
            f"{link}"
        )

    # DepositInstant, RedeemInstant, WithdrawToken — record only, no alert
    return None

# ── Process a single decoded event ────────────────────────────────────────────
def process_event(
    state: dict, product: str, chain: str, role: str,
    vault_addr: str, decoded: dict,
    tx_hash: str, block_num: int, timestamp: str,
    nav_cache: dict,
) -> str | None:
    name         = decoded["event"]
    vk           = vault_key(product, chain, role)
    is_issuance  = "issuance" in role

    # Deduplication guard — public Base RPC can return duplicate log entries;
    # also protects against mid-cycle restart re-processing the same block range.
    if any(e["tx_hash"] == tx_hash and e["event_type"] == name for e in state["events"]):
        log.debug(f"Duplicate event skipped: {name} tx={tx_hash[:16]}...")
        return None

    # Rolling event log — raw ints serialised as strings to avoid JSON precision issues
    state["events"].append({
        "event_type":    name,
        "product":       product,
        "vault_key":     vk,
        "vault_address": vault_addr.lower(),
        "chain":         chain,
        "timestamp":     timestamp,
        "block":         block_num,
        "tx_hash":       tx_hash,
        **{k: str(v) if isinstance(v, int) else v
           for k, v in decoded.items() if k != "event"},
    })

    # SLA-relevant state mutations
    if name in ("RedeemRequest", "RedeemRequestWithCustomRecipient"):
        open_redemption_request(
            state, product, chain, role, vault_addr,
            decoded, tx_hash, block_num, timestamp,
        )

    elif name in ("ApproveRequest", "SafeApproveRequest") and not is_issuance:
        resolution = "safe_approved" if name == "SafeApproveRequest" else "approved"
        close_redemption_request(
            state, vault_addr, decoded, resolution, tx_hash, block_num, timestamp,
        )

    elif name in ("DepositRequest", "DepositRequestWithCustomRecipient"):
        open_issuance_request(
            state, product, chain, role, vault_addr,
            decoded, tx_hash, block_num, timestamp,
        )

    elif name == "SafeApproveRequest" and is_issuance:
        close_issuance_request(
            state, vault_addr, decoded, "safe_approved", tx_hash, block_num, timestamp,
        )

    elif name == "RejectRequest":
        close_redemption_request(
            state, vault_addr, decoded, "rejected", tx_hash, block_num, timestamp,
        )

    return build_alert(product, chain, role, decoded, tx_hash, timestamp, state, nav_cache)

# ── getLogs — chain-specific implementations ───────────────────────────────────
# Ethereum: Etherscan API V2, arbitrary block range, timestamp included in response.
# Base:     public Base RPC, no auth, up to ~10k blocks per call.

def _get_logs_ethereum(address: str, from_block: int, to_block: int) -> list[dict]:
    """
    Fetch logs via Etherscan API V2 with pagination.
    Returns logs in Etherscan format (hex blockNumber, timeStamp field available).
    """
    all_logs: list[dict] = []
    page = 1
    while True:
        try:
            r = http.get(ETHERSCAN_API, params={
                "chainid": 1, "module": "logs", "action": "getLogs",
                "address": address,
                "fromBlock": from_block, "toBlock": to_block,
                "page": page, "offset": 1000,
                "apikey": ETHERSCAN_KEY,
            }, timeout=30)
            data = r.json()
        except Exception as e:
            log.error(f"Etherscan getLogs request failed: {e}")
            break
        if data.get("status") != "1":
            if data.get("result") not in ("", None) or data.get("message") != "No records found":
                if data.get("message") != "No records found":
                    log.warning(f"Etherscan getLogs: {data.get('message')} — {str(data.get('result',''))[:100]}")
            break
        batch = data["result"]
        all_logs.extend(batch)
        if len(batch) < 1000:
            break
        page += 1
        time.sleep(0.4)   # Etherscan free tier: 3 req/s — applies between paginated pages too
    return all_logs

def _get_logs_base(address: str, from_block: int, to_block: int) -> list[dict]:
    """
    Fetch logs via public Base RPC (https://mainnet.base.org).
    Returns logs in standard JSON-RPC format (no timeStamp field).
    """
    try:
        r = http.post(BASE_PUBLIC_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "eth_getLogs",
            "params": [{"address": address,
                        "fromBlock": hex(from_block),
                        "toBlock":   hex(to_block)}],
        }, timeout=30)
        data = r.json()
        if "result" in data:
            return data["result"]
        log.warning(f"Base getLogs error: {data.get('error', data)}")
    except Exception as e:
        log.error(f"Base public RPC getLogs failed: {e}")
    return []

GET_LOGS = {
    "ethereum": _get_logs_ethereum,
    "base":     _get_logs_base,
}

# Max blocks to advance per poll cycle.
# Ethereum: 10 000 blocks (~33h at 12s) — Etherscan handles any range; cap keeps
#           state pointer advancing predictably.
# Base:     2 000 blocks (~67min at 2s) — stays well inside public RPC limit.
MAX_BLOCKS = {"ethereum": 10_000, "base": 2_000}

# ── Poll one vault for new events ──────────────────────────────────────────────
def poll_vault(
    state: dict,
    product: str, chain: str, role: str,
    config: dict,
    nav_cache: dict,
) -> list[str]:
    w3_conn    = W3[chain]
    address    = config["address"]
    vk         = vault_key(product, chain, role)
    monitored  = config["monitor"]

    try:
        current = w3_conn.eth.block_number
    except Exception as e:
        log.error(f"block_number failed on {chain}: {e}")
        return []

    chain_blocks = state["last_seen_blocks"].setdefault(chain, {})
    last = chain_blocks.get(vk, 0)
    if last == 0:
        # First run: look back 24h on Ethereum (~7200 blocks), 5.5h on Base (~10000)
        lookback = 7200 if chain == "ethereum" else 10_000
        last = max(0, current - lookback)
        log.info(f"First run {vk} — starting from block {last}")

    if last >= current:
        return []

    from_block = last + 1
    to_block   = min(current, from_block + MAX_BLOCKS[chain] - 1)

    logs = GET_LOGS[chain](address, from_block, to_block)

    # Filter to monitored events only (public Base RPC returns all events)
    monitored_topics = {TOPIC[e] for e in monitored if e in TOPIC}

    alerts: list[str] = []
    for raw_log in logs:
        topics = raw_log.get("topics", [])
        if not topics:
            continue
        t0 = to_hex(topics[0]) if topics else None
        if t0 not in monitored_topics:
            continue

        tx_hash   = to_hex(raw_log.get("transactionHash", b""))
        block_num = to_int(raw_log.get("blockNumber", 0))

        # Etherscan logs include a timeStamp field — use it to avoid RPC call
        if "timeStamp" in raw_log:
            ts_int    = int(raw_log["timeStamp"], 16)
            timestamp = datetime.fromtimestamp(ts_int, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            timestamp = block_to_ts(w3_conn, block_num)

        decoded = decode_log(dict(raw_log))
        if decoded is None:
            continue

        log.info(f"{vk}: {decoded['event']:35s} block={block_num} tx={tx_hash[:16]}...")
        alert = process_event(
            state, product, chain, role, address,
            decoded, tx_hash, block_num, timestamp, nav_cache,
        )
        if alert:
            alerts.append(alert)

    chain_blocks[vk] = to_block
    if logs:
        log.info(f"{vk}: {len(logs)} raw events filtered to {sum(1 for l in logs if to_hex((l.get('topics') or [''])[0]) in monitored_topics)}, blocks {from_block}–{to_block}")

    return alerts

# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> None:
    try:
        r = http.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if not r.ok:
            log.warning(f"Telegram {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")

# ── Main loop ──────────────────────────────────────────────────────────────────
def main() -> None:
    log.info(f"Midas bot starting — poll interval {POLL_INTERVAL}s")
    state = load_state()

    while True:
        cycle_start = time.monotonic()
        nav_cache: dict = {}
        all_alerts: list[str] = []

        for (product, chain, role), config in VAULTS.items():
            try:
                alerts = poll_vault(state, product, chain, role, config, nav_cache)
                all_alerts.extend(alerts)
            except Exception as e:
                log.error(
                    f"Unhandled error polling {vault_key(product, chain, role)}: {e}",
                    exc_info=True,
                )
            if chain == "ethereum":
                time.sleep(1.0)  # Etherscan free tier: 3 req/s — stay within limit

        save_state(state)

        for alert in all_alerts:
            send_telegram(alert)
            time.sleep(0.5)

        elapsed   = time.monotonic() - cycle_start
        sleep_for = max(0, POLL_INTERVAL - elapsed)
        log.debug(f"Cycle in {elapsed:.1f}s, sleeping {sleep_for:.0f}s")
        time.sleep(sleep_for)

if __name__ == "__main__":
    main()
