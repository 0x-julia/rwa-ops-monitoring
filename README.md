# RWA Protocol Operations — Live Monitoring Systems

Operational monitoring systems built on two live Real World Asset protocols: **Centrifuge** and **Midas**. Both systems run continuously against live on-chain data, tracking investor lifecycle events, NAV movements, SLA compliance, and fund health — and delivering automated reports and real-time alerts to operators.

**Total AUM monitored: ~$1.7B across 7 chains and 4 products.**

---

## What This Is

The scripts in this repo connect directly to live deployed contracts, pull real onchain state, and generate operational outputs based on actual fund activity. The sample reports in /samples are generated from live production data rather than synthetic inputs.

The systems are designed to replicate the daily operational workflows of an onchain fund operations team — including real-time event monitoring, SLA tracking, and structured reporting outputs ready to forward to fund administrators or strategy managers.

---

## Systems Overview

### 1. Centrifuge JTRSY / JAAA System

**Products:** JTRSY (Janus Henderson Anemoy Treasury Fund) · JAAA (Janus Henderson Anemoy AAA CLO Fund)
**AUM covered:** ~$1.5B+  
**Stack:** Python · Centrifuge GraphQL API · Claude API · WeasyPrint · Telegram  
**Chains:** Ethereum · Arbitrum · Base · BNB · Avalanche · Monad · Pharos

Centrifuge pools use epoch-based settlement rather than continuous approval queues. Investor actions (deposit requests, redemptions) are batched and settled by the protocol at epoch close. JTRSY settles T+1; JAAA settles T+3.

The system runs as four agents across both pools:

| Script | Role |
|--------|------|
| `jtrsy_bot.py` | Real-time monitor for the JTRSY pool. Polls the Centrifuge GraphQL API every 60 seconds and fires Telegram alerts for deposit requests, deposit claimable, redeem requests, and redeem claimable events across all supported chains. |
| `jaaa_bot.py` | Same pattern for the JAAA pool. Independent bot per pool — pool identity injected via environment variable. |
| `centrifuge_analyst.py` | Daily run across both pools. Queries NAV, AUM, yield, pending orders, epoch state, and 24-hour activity via GraphQL. Handles chain-specific USDC decimal overrides (BNB uses 18 decimals vs. 6 on other chains). Calls Claude API for Flags & Exceptions narrative. Writes structured JSON output. |
| `centrifuge_reporter.py` | Reads analyst JSON. Produces three PDFs: JTRSY strategy manager report, JAAA strategy manager report, and Operator Digest. Sends Telegram digest to operations channel. |

**What the reports cover:**
- NAV per token and 24-hour change
- AUM and total supply across all chains
- Pending deposit and redemption orders with SLA tracking
- Epoch state and settlement timeline
- 24-hour activity summary
- AI-generated operational narrative for the Flags & Exceptions section

**Infrastructure decisions worth noting:**
- Single Centrifuge GraphQL endpoint returns activity across all seven chains with per-transaction chain tagging — no per-chain RPC connections required
- Chain-specific USDC decimal handling: BNB uses 18 decimals vs. 6 on Ethereum/Avalanche/Monad/Pharos — verified against deployed token contracts
- Sub-minimum redemption filter removes 1-share test records from live data (below the $500K fund minimum) so they don't appear as open requests in reports
- Two independent bots rather than one multi-pool bot — easier to extend to new Centrifuge pools without touching logic
- All secrets loaded from `.env` via `python-dotenv` — no credentials in source

---

### 2. Midas Daily Ops System

**Products:** mTBILL (Ethereum + Base) · mF-ONE (Ethereum)  
**AUM covered:** ~$120M+  
**Stack:** Python · Alchemy · Etherscan API · Ethplorer · Claude API · WeasyPrint · Telegram

Midas tokenises institutional fixed-income products — mTBILL tracks US Treasury Bills managed by BlackRock; mF-ONE tracks a private credit strategy run by Fasanara Capital. Both operate on a request-approve-execute lifecycle across multiple vaults, with separate issuance and redemption pathways.

The system runs as three coordinated agents:

| Script | Role |
|--------|------|
| `midas_bot.py` | 60-second polling loop across 7 vaults on Ethereum and Base. Detects deposit requests, redemption requests, approvals, rejections, instant settlements, and vault pause events. Sends Telegram alerts in real time. |
| `midas_analyst.py` | Daily run. Reads vault state, queries oracles for live NAV, pulls holder intelligence via Ethplorer, detects new wallets and large position changes, calls Claude API to generate narrative for the Flags & Exceptions section. Writes structured JSON output. |
| `midas_reporter.py` | Reads analyst JSON. Produces three PDFs: mTBILL strategy manager report, mF-ONE strategy manager report, and an internal Operator Digest. Sends Telegram digest to operations channel. |

**What the reports cover:**
- NAV per token and 24-hour change vs. oracle
- Total supply and AUM across all chains
- Open redemption requests with SLA countdown
- Top-10 holder concentration and 24-hour holder movement
- New wallet entries and large position reductions
- Rejected requests and escalation flags
- AI-generated operational narrative for the Flags & Exceptions section

**Infrastructure decisions worth noting:**
- Etherscan API V2 used for Ethereum `getLogs` (arbitrary block range; Alchemy free tier caps at 10 blocks per call)
- Public Base RPC used for Base chain events; Alchemy retained for oracle calls and block timestamps only
- Ethplorer free endpoint used for holder intelligence (Etherscan `tokenholderlist` requires paid plan)
- Event deduplication guard prevents duplicate alerts when RPC returns repeated log entries
- All secrets loaded from `.env` via `python-dotenv` — no credentials in source

---

## Repository Structure

```
├── midas_bot.py              # Midas vault watcher (live event polling)
├── midas_analyst.py          # Midas daily analyst agent (NAV, holders, narrative)
├── midas_reporter.py         # Midas PDF report generator + Telegram digest
├── jtrsy_bot.py              # Centrifuge JTRSY pool real-time monitor
├── jaaa_bot.py               # Centrifuge JAAA pool real-time monitor
├── centrifuge_analyst.py     # Centrifuge daily analyst agent (both pools)
├── centrifuge_reporter.py    # Centrifuge PDF report generator + Telegram digest
├── .env.example              # Environment variable template — copy to .env to run
├── samples/
│   ├── mTBILL_Daily_Report_sample.pdf
│   ├── mFONE_Daily_Report_sample.pdf
│   └── JTRSY_Daily_Report_sample.pdf
└── README.md
```

---

## Running the Systems

### Prerequisites

```bash
pip install web3 requests anthropic python-dotenv weasyprint "python-telegram-bot==20.7"
# macOS only:
brew install pango
```

### Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

### Midas System

```bash
# Terminal 1 — start the vault watcher (runs continuously)
python3 midas_bot.py

# Terminal 2 — run the daily analyst + reporter
python3 midas_analyst.py && python3 midas_reporter.py
```

### Centrifuge System

```bash
# Real-time monitors — run each in its own terminal
python3 jtrsy_bot.py
python3 jaaa_bot.py

# Daily reports
python3 centrifuge_analyst.py && python3 centrifuge_reporter.py
```

---

## Environment Variables

All credentials are loaded from `.env`. The `.env` file is excluded from version control via `.gitignore`. Never commit a populated `.env` file.

| Variable | Used By | Description |
|----------|---------|-------------|
| `ALCHEMY_ETH_URL` | midas_bot, midas_analyst | Alchemy Ethereum endpoint (includes API key in URL) |
| `ALCHEMY_BASE_URL` | midas_bot, midas_analyst | Alchemy Base endpoint |
| `ETHERSCAN_API_KEY` | midas_bot, midas_analyst | Etherscan API V2 key (free tier sufficient) |
| `ANTHROPIC_API_KEY` | midas_analyst, centrifuge_analyst | Claude API key for Flags & Exceptions narrative |
| `TELEGRAM_BOT_TOKEN` | all scripts | Telegram bot token |
| `TELEGRAM_CHAT_ID` | all scripts | Target channel or chat ID |
| `POLL_INTERVAL_SECONDS` | midas_bot | Polling frequency in seconds (default: 60) |
| `POOL_ID` | jtrsy_bot, jaaa_bot | Centrifuge pool ID (set per-bot in separate .env files) |

---

## Contract Addresses (Public — Ethereum Mainnet)

These are publicly deployed contract addresses. They are not credentials.

**Midas Oracles (Chainlink-compatible, 8 decimals)**
- mTBILL: `0x056339C044055819E8Db84E71f5f2E1F536b2E5b`
- mF-ONE: `0x8D51DBC85cEef637c97D02bdaAbb5E274850e68C`

**Midas Vault Contracts**
- mTBILL Issuance (ETH): `0x99361435420711723aF805F08187c9E6bF796683`
- mTBILL Standard Redemption (ETH): `0xF6e51d24F4793Ac5e71e0502213a9BBE3A6d4517`
- mTBILL Instant Redemption (ETH): `0x569D7dccBF6923350521ecBC28A555A500c4f0Ec`
- mTBILL Issuance (Base): `0x8978e327FE7C72Fa4eaF4649C23147E279ae1470`
- mTBILL Instant Redemption (Base): `0x2a8c22E3b10036f3AEF5875d04f8441d4188b656`
- mF-ONE Issuance (ETH): `0x41438435c20B1C2f1fcA702d387889F346A0C3DE`
- mF-ONE Redemption (ETH): `0x44b0440e35c596e858cEA433D0d82F5a985fD19C`

---

## Sample Outputs

The `/samples` directory contains reports and alerts generated from live data runs.

| File | Description |
|------|-------------|
| `mTBILL_Daily_Report_sample.pdf` | Midas mTBILL strategy manager report — NAV, AUM, holder intelligence, open requests, SLA tracking, Flags & Exceptions narrative |
| `mFONE_Daily_Report_sample.pdf` | Midas mF-ONE strategy manager report — same format, private credit product |
| `JTRSY_Daily_Report_sample.pdf` | Centrifuge JTRSY strategy manager report — NAV, AUM, epoch state, pending orders, SLA tracking |
| `midas_redeem_alert_sample.png` | Live Telegram alert — $1.08M mF-ONE redemption request, fired within seconds of the on-chain event. Transaction hash links directly to Etherscan. |

---

## Notes on Build Approach

A few design decisions that reflect real operational constraints rather than tutorial patterns:

**Why three separate Midas agents?** The watcher, analyst, and reporter are decoupled because they run on different schedules. The watcher runs continuously. The analyst and reporter run once daily. Combining them would require managing state across very different execution windows.

**Why four Centrifuge agents?** Same logic — the real-time bots and the daily reporting pipeline run on different schedules and serve different purposes. The bots keep operators informed of activity as it happens; the analyst and reporter produce the structured daily view.

**Why Etherscan instead of Alchemy for getLogs?** Alchemy free tier limits `eth_getLogs` to 10 blocks per call. Recovering historical state on first run would require hundreds of calls with rate limiting. Etherscan API V2 supports arbitrary block ranges with pagination — better fit for the use case.

**Why Ethplorer for holder data?** Etherscan's `tokenholderlist` endpoint requires a paid Pro plan. Ethplorer provides equivalent top-holder data on a free public key. The tradeoff is a 100-holder cap, which is sufficient for institutional tokenised funds with concentrated holder bases.

**Why two independent Centrifuge bots instead of one?** JTRSY and JAAA are separate pools with different settlement windows (T+1 vs T+3). Two independent bots with pool identity injected via environment variable is easier to extend to new pools without touching logic.

**Why a single Centrifuge analyst covering both pools?** The opposite decision from the bots — the daily analyst queries both pools in one run and writes a single JSON file, which the reporter then uses to generate both PDFs in one pass. Consolidating the daily reporting pipeline reduces scheduled job complexity.
