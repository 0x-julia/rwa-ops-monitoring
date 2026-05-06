"""
reporter.py — Daily reporter agent for Midas mTBILL and mF-ONE.

Reads data/analyst_output.json and produces:
  - reports/mTBILL_Daily_Report.pdf
  - reports/mF-ONE_Daily_Report.pdf
  - reports/Operator_Daily_Digest.pdf
  - Operator Telegram digest (sent via bot, HTML parse mode)

Usage:
    python3 reporter.py
    python3 reporter.py --no-telegram          # skip Telegram, print digest to stdout
    python3 reporter.py --show-html mTBILL     # print mTBILL HTML to stdout and exit
    python3 reporter.py --show-html mF-ONE     # print mF-ONE HTML to stdout and exit
    python3 reporter.py --show-html operator   # print Operator Digest HTML to stdout and exit
    python3 reporter.py --show-html telegram   # print Telegram digest to stdout and exit

Dependencies:
    pip3 install weasyprint requests python-dotenv
    macOS: brew install pango
    DYLD_LIBRARY_PATH=/opt/homebrew/lib is set automatically before WeasyPrint import.
"""

import os

# Must be set before WeasyPrint import on macOS — Homebrew pango lives here.
os.environ.setdefault("DYLD_LIBRARY_PATH", "/opt/homebrew/lib")

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from weasyprint import HTML

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_here = Path(__file__).parent
load_dotenv(_here / ".env", override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

INPUT_FILE  = _here / "data" / "analyst_output.json"
REPORTS_DIR = _here / "reports"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("reporter")

# ---------------------------------------------------------------------------
# DEMO: Yield figures manually sourced from midas.app / rwa.xyz —
# to be replaced with computed values from nav_history.json once
# 30+ days of NAV history has accumulated.
# ---------------------------------------------------------------------------
YIELD_FIGURES = {
    "mTBILL": {
        "yield_7d":  2.47,
        "yield_30d": 3.10,
    },
    "mfONE": {
        "yield_7d":  11.40,
        "yield_30d": 11.71,
    },
}

# Static product metadata
PRODUCT_META = {
    "mTBILL": {
        "display_name": "mTBILL",
        "full_name":    "Midas US Treasury Bills Token",
        "manager":      "BlackRock",
        "underlying":   "Short-dated US Treasury Bills",
        "has_rejected": False,
        "has_base":     True,
        "aum_label":        "AUM (USD — Ethereum + Base)",
        "scope_note":       "Vault monitoring covers Ethereum and Base. Holder intelligence reflects Ethereum only. Other chains not monitored in this build.",
        "vault_holder_note": None,
    },
    "mfONE": {
        "display_name": "mF-ONE",
        "full_name":    "Midas Fasanara F-ONE",
        "manager":      "Fasanara Capital",
        "underlying":   "Private credit — Fasanara Capital F-ONE strategy",
        "has_rejected": True,
        "has_base":     False,
        "aum_label":        "AUM (USD — Ethereum)",
        "scope_note":       "Vault monitoring and holder intelligence reflect Ethereum only.",
        "vault_holder_note": "Holder #4 (0x44b0440e…) is the mF-ONE redemption vault — tokens held represent pending redemption requests awaiting processing.",
    },
}

# Human-readable labels for vault_key values that appear in open_requests
VAULT_KEY_LABELS = {
    "mTBILL_ethereum_standard_redemption": "Std. Redemption (ETH)",
    "mTBILL_ethereum_instant_redemption":  "Instant Vault (ETH)",
    "mfONE_ethereum_redemption":           "Redemption (ETH)",
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_usd(value, decimals=1):
    if value is None:
        return "N/A"
    abs_val = abs(value)
    sign = "-" if value < 0 else ""
    if abs_val >= 1_000_000:
        return f"{sign}${abs_val / 1_000_000:.{decimals}f}M"
    if abs_val >= 1_000:
        return f"{sign}${abs_val / 1_000:.{decimals}f}K"
    return f"{sign}${abs_val:,.2f}"


def fmt_usd_change(value, decimals=1):
    if value is None:
        return "N/A"
    if value == 0:
        return "&mdash;"
    sign = "+" if value > 0 else ""
    return f"{sign}{fmt_usd(value, decimals)}"


def fmt_nav_change(change, pct):
    """Formats 24hr NAV change with null-safe handling."""
    if change is None or pct is None:
        return "N/A (first run)"
    if change == 0.0:
        return "&mdash;"
    sign = "+" if change > 0 else ""
    return f"{sign}${change:.6f} ({sign}{pct:.4f}%)"


def fmt_ts(iso_str):
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y %H:%M UTC")
    except Exception:
        return iso_str


def fmt_report_date(iso_str):
    try:
        return datetime.fromisoformat(iso_str).strftime("%d %B %Y")
    except Exception:
        return iso_str[:10]


def fmt_hours_human(hours):
    if hours is None:
        return "N/A"
    days = int(hours // 24)
    hrs  = int(hours % 24)
    if days == 0:
        return f"{hrs}h"
    if hrs == 0:
        return f"{days}d"
    return f"{days}d {hrs}h"


def fmt_tokens(value):
    if value is None:
        return "N/A"
    return f"{value:,.0f}"


def fmt_pct(value):
    if value is None:
        return "N/A"
    return f"{value:.2f}%"


def sign_class(value):
    if value is None or value == 0:
        return "neutral"
    return "positive" if value > 0 else "negative"


def n_or_dash(n):
    return "&mdash;" if not n else str(n)


def vault_label(vault_key):
    return VAULT_KEY_LABELS.get(vault_key, vault_key)


# ---------------------------------------------------------------------------
# Shared CSS — adapted from Centrifuge reporter reference implementation
# ---------------------------------------------------------------------------

BASE_CSS = """
@page {
    size: A4;
    margin: 20mm 18mm 46mm 18mm;
}

body {
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 10pt;
    color: #1a1a1a;
    line-height: 1.5;
}

/* ── Header ── */
.eyebrow {
    font-size: 7.5pt;
    font-family: Helvetica, Arial, sans-serif;
    text-transform: uppercase;
    letter-spacing: 0.7pt;
    color: #888;
    margin-bottom: 2mm;
}

h1 {
    font-size: 18pt;
    font-weight: 500;
    margin: 0 0 1.5mm 0;
    color: #0d1b2a;
    letter-spacing: 0.1pt;
}

.header-pool-name {
    font-size: 10pt;
    color: #555;
    margin-top: 1mm;
    font-style: italic;
}

.header-meta {
    font-size: 8.5pt;
    color: #999;
    margin-top: 2.5mm;
}

.report-header {
    border-bottom: 0.5pt solid #c8ccd4;
    padding-bottom: 4mm;
    margin-bottom: 5mm;
}

.header-top {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
}

.confidential-label {
    font-size: 7.5pt;
    color: #bbb;
    text-align: right;
    text-transform: uppercase;
    letter-spacing: 0.5pt;
    font-family: Helvetica, Arial, sans-serif;
    padding-top: 1mm;
    white-space: nowrap;
}

/* ── KPI tiles ── */
.kpi-row {
    display: flex;
    gap: 3.5mm;
    margin: 3mm 0 3mm 0;
    page-break-inside: avoid;
}

.kpi-block {
    flex: 1;
    background: #f4f5f8;
    border-radius: 3px;
    padding: 3mm 3.5mm 2.5mm 3.5mm;
}

.kpi-label {
    font-size: 7.5pt;
    color: #888;
    text-transform: uppercase;
    letter-spacing: 0.5pt;
    font-family: Helvetica, Arial, sans-serif;
}

.kpi-value {
    font-size: 15pt;
    font-weight: 500;
    color: #0d1b2a;
    margin-top: 1mm;
    line-height: 1.2;
}

.kpi-sub {
    font-size: 8pt;
    margin-top: 1mm;
}

/* ── Section headers ── */
h2 {
    font-size: 8.5pt;
    font-weight: bold;
    color: #fff;
    background: #1a2744;
    margin: 3mm 0 0 0;
    padding: 2.2mm 3.5mm;
    text-transform: uppercase;
    letter-spacing: 0.8pt;
    font-family: Helvetica, Arial, sans-serif;
    border-radius: 3px 3px 0 0;
    page-break-after: avoid;
}

h3 {
    font-size: 9pt;
    font-weight: bold;
    color: #1a2744;
    margin: 2mm 0 1mm 0;
    font-family: Helvetica, Arial, sans-serif;
    page-break-after: avoid;
}

/* ── Tables ── */
table {
    width: 100%;
    border-collapse: collapse;
    margin: 0 0 8mm 0;
    font-size: 10pt;
    border: 0.5pt solid #d0d4da;
    border-top: none;
}

th {
    background: #eef0f4;
    color: #555;
    padding: 2mm 3.5mm;
    text-align: left;
    font-size: 7.5pt;
    font-weight: bold;
    font-family: Helvetica, Arial, sans-serif;
    letter-spacing: 0.2pt;
    border-bottom: 0.5pt solid #c8ccd4;
}

td {
    padding: 2.5mm 3.5mm;
    border-bottom: 0.3pt solid #ebebeb;
    vertical-align: top;
}

td.label-col {
    font-family: Helvetica, Arial, sans-serif;
    font-size: 9pt;
    color: #666;
    width: 55%;
}

td.value-col {
    font-weight: 500;
    text-align: right;
}

td.count-col {
    font-weight: 500;
    text-align: center;
}

td.mono {
    font-family: 'Courier New', Courier, monospace;
    font-size: 8pt;
}

tr:last-child td {
    border-bottom: none;
}

tr.alt td {
    background: #fafbfc;
}

tr.flag-48h td {
    background: #fff8e1;
}

tr.flag-96h td {
    background: #fdecea;
}

tr.rejected td {
    background: #fdecea;
}

/* ── Badges ── */
.badge {
    display: inline-block;
    font-size: 7pt;
    padding: 0.4mm 2mm;
    font-weight: bold;
    font-family: Helvetica, Arial, sans-serif;
    border-radius: 2px;
}

.badge-green { background: #e8f5e9; color: #1a7a3c; border: 0.3pt solid #1a7a3c; }
.badge-amber { background: #fff8e1; color: #b8720a; border: 0.3pt solid #b8720a; }
.badge-red   { background: #fdecea; color: #c0392b; border: 0.3pt solid #c0392b; }

/* ── Flags and clean-queue boxes ── */
.flags-box {
    background: #f4f5f8;
    border: 0.5pt solid #d0d4da;
    border-left: 3pt solid #1a2744;
    padding: 3mm 4mm;
    margin: 0 0 0 0;
    font-size: 10pt;
    line-height: 1.6;
    page-break-inside: avoid;
}

.clean-box {
    background: #e8f5e9;
    border: 0.5pt solid #1a7a3c;
    border-left: 3pt solid #1a7a3c;
    padding: 3mm 4mm;
    margin: 0 0 2mm 0;
    font-size: 9.5pt;
    color: #1a7a3c;
    font-family: Helvetica, Arial, sans-serif;
    page-break-inside: avoid;
}

/* ── Compact table (operator digest comparison) ── */
table.compact td {
    padding: 1.5mm 3.5mm;
    font-size: 9pt;
}

table.compact th {
    padding: 1.5mm 3.5mm;
}

/* ── Colour helpers ── */
.positive { color: #1a7a3c; }
.negative { color: #c0392b; }
.neutral  { color: #888; }

/* ── Fixed footer — repeats on every PDF page ── */
.page-footer {
    position: fixed;
    bottom: 3mm;
    left: 18mm;
    right: 18mm;
    font-size: 7pt;
    color: #ccc;
    border-top: 0.5pt solid #e0e0e0;
    padding-top: 2mm;
    text-align: center;
    font-family: Helvetica, Arial, sans-serif;
    text-transform: uppercase;
    letter-spacing: 0.3pt;
}
"""


# ---------------------------------------------------------------------------
# Product-specific PDF
# ---------------------------------------------------------------------------

def _kv_row(label, value, alt=False, row_class=None):
    cls = f' class="{row_class}"' if row_class else (' class="alt"' if alt else "")
    return f'<tr{cls}><td class="label-col">{label}</td><td class="value-col">{value}</td></tr>'


def _act_row(label, sub, alt=False, row_class=None):
    """Three-column activity row: label | count | usd."""
    count = sub.get("count", 0)
    usd   = sub.get("usd", 0.0)
    c_display = n_or_dash(count)
    v_display = fmt_usd(usd) if usd else "&mdash;"
    cls = f' class="{row_class}"' if row_class else (' class="alt"' if alt else "")
    return (
        f'<tr{cls}>'
        f'<td class="label-col">{label}</td>'
        f'<td class="count-col">{c_display}</td>'
        f'<td class="value-col">{v_display}</td>'
        f'</tr>'
    )


def build_product_html(product_key: str, data: dict) -> str:
    meta      = PRODUCT_META[product_key]
    d         = data[product_key]
    nav       = d["nav"]
    aum       = d["aum"]
    act       = d["activity_24h"]
    sla       = d["sla_status"]
    holders   = d["holder_intelligence"]
    flags_txt = d.get("flags_and_exceptions", "")
    yield_fig = YIELD_FIGURES[product_key]

    report_ts   = data["meta"]["generated_at"]
    report_date = fmt_report_date(data["meta"]["report_date"])

    # NAV change display
    nav_change_str = fmt_nav_change(nav.get("change_24h"), nav.get("change_24h_pct"))
    nav_change_cls = sign_class(nav.get("change_24h"))

    # Net flow
    net     = act.get("net_flow_usd", 0)
    net_str = fmt_usd_change(net)
    net_cls = sign_class(net)

    # Flags prose
    flags_html = (
        flags_txt.replace("\n\n", "<br><br>").replace("\n", " ").strip()
        if flags_txt else "<em>No flags or exceptions.</em>"
    )

    # ── Section 2 — Supply rows ──────────────────────────────────────────────
    supply_rows = _kv_row("Total supply (tokens)", fmt_tokens(aum.get("total_supply_tokens")))
    if meta["has_base"]:
        supply_rows += _kv_row("  &mdash; Ethereum", fmt_tokens(aum.get("total_supply_ethereum")), alt=True)
        base_supply  = aum.get("total_supply_base")
        supply_rows += _kv_row("  &mdash; Base", fmt_tokens(base_supply) if base_supply else "&mdash;")
    else:
        supply_rows += _kv_row("  &mdash; Ethereum", fmt_tokens(aum.get("total_supply_ethereum")), alt=True)

    # ── Section 3 — Pending Actions ─────────────────────────────────────────
    if sla["escalation_96h_count"] > 0:
        n = sla["escalation_96h_count"]
        sla_badge = f'<span class="badge badge-red">&#9888; {n} escalation{"s" if n > 1 else ""} (96hr)</span>'
    elif sla["flag_48h_count"] > 0:
        n = sla["flag_48h_count"]
        sla_badge = f'<span class="badge badge-amber">&#9873; {n} approaching SLA (48hr)</span>'
    else:
        sla_badge = '<span class="badge badge-green">&#10003; No SLA exposure</span>'

    if sla["open_count"] > 0:
        oldest_str = fmt_hours_human(sla.get("oldest_age_hours"))
        summary_table = f"""
<table>
    <tr><th class="label-col">Field</th><th>Value</th></tr>
    {_kv_row("Open requests", str(sla["open_count"]))}
    {_kv_row("Total pending (USD est.)", fmt_usd(sla["total_usd_pending"], 2), alt=True)}
    {_kv_row("Oldest request age", oldest_str)}
    {_kv_row("SLA status", sla_badge, alt=True)}
</table>"""
        req_rows = ""
        for i, req in enumerate(sla["open_requests"]):
            if req["escalation_96h"]:
                row_cls = "flag-96h"
                badge   = '<span class="badge badge-red">&#9888; 96hr escalation</span>'
            elif req["flag_48h"]:
                row_cls = "flag-48h"
                badge   = '<span class="badge badge-amber">&#9873; 48hr flag</span>'
            else:
                row_cls = "alt" if i % 2 else ""
                badge   = ""
            addr = req["investor"]
            addr_short = addr[:10] + "&hellip;" + addr[-6:]
            req_rows += (
                f'<tr class="{row_cls}">'
                f'<td class="mono">{addr_short}</td>'
                f'<td style="font-size:8.5pt">{vault_label(req["vault_key"])}</td>'
                f'<td style="font-size:8.5pt">{fmt_ts(req["submitted_at"])}</td>'
                f'<td style="text-align:right">{fmt_hours_human(req["age_hours"])}</td>'
                f'<td style="text-align:right">{fmt_usd(req["usd"], 2)}</td>'
                f'<td>{badge}</td>'
                f'</tr>'
            )
        detail_table = f"""
<h3>Open Request Detail</h3>
<table>
    <tr>
        <th>Investor</th><th>Vault</th><th>Submitted</th>
        <th style="text-align:right">Age</th>
        <th style="text-align:right">USD Est.</th>
        <th>Flag</th>
    </tr>
    {req_rows}
</table>"""
        pending_section = summary_table + detail_table
    else:
        pending_section = '<div class="clean-box">&#10003; No pending actions &mdash; queue is clean</div>'

    # ── Section 4 — Holder Intelligence ─────────────────────────────────────
    holder_rows = ""
    for i, h in enumerate(holders.get("top_10", [])):
        row_cls = "alt" if i % 2 else ""
        addr        = h["address"]
        addr_short  = addr[:10] + "&hellip;" + addr[-6:]
        holder_rows += (
            f'<tr class="{row_cls}">'
            f'<td style="text-align:center">{h["rank"]}</td>'
            f'<td class="mono">{addr_short}</td>'
            f'<td style="text-align:right">{fmt_tokens(h["balance_tokens"])}</td>'
            f'<td style="text-align:right">{fmt_usd(h["usd"], 1)}</td>'
            f'<td style="text-align:right">{fmt_pct(h["share_pct"])}</td>'
            f'</tr>'
        )

    new_wallets = holders.get("new_wallets", [])
    if new_wallets:
        nw_items        = "".join(
            f'<li><code>{w["address"][:10]}&hellip;{w["address"][-6:]}</code>'
            f' &mdash; {fmt_tokens(w["balance_tokens"])} tokens ({fmt_usd(w["usd"], 1)})</li>'
            for w in new_wallets
        )
        new_wallets_html = f"<ul style='margin:1mm 0 3mm 4mm;font-size:9pt'>{nw_items}</ul>"
    else:
        new_wallets_html = "<p style='color:#888;font-size:9pt;margin:1mm 0 3mm 0'>None detected since last report.</p>"

    large_red = holders.get("large_reductions", [])
    if large_red:
        lr_items       = "".join(
            f'<li><code>{r["address"][:10]}&hellip;{r["address"][-6:]}</code>'
            f' &mdash; reduced by {fmt_tokens(r["prev_balance_tokens"] - r["curr_balance_tokens"])} tokens'
            f' ({fmt_usd(r["reduction_usd"], 1)}, {r["reduction_pct"]:.1f}%)</li>'
            for r in large_red
        )
        large_red_html = f"<ul style='margin:1mm 0 3mm 4mm;font-size:9pt'>{lr_items}</ul>"
    else:
        large_red_html = "<p style='color:#888;font-size:9pt;margin:1mm 0 3mm 0'>None detected since last report.</p>"

    # ── Activity table rows ───────────────────────────────────────────────────
    rejected      = act.get("standard_redemption_rejected")
    rejected_row  = ""
    if meta["has_rejected"] and rejected is not None:
        rej_cls      = "rejected" if rejected.get("count", 0) > 0 else "alt"
        rejected_row = _act_row("Standard redemptions rejected", rejected, row_class=rej_cls)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>{BASE_CSS}</style></head>
<body>

<div class="page-footer">
    Confidential &mdash; For strategy manager use only &mdash;
    {meta['display_name']} Daily Operations Report &mdash; {report_date}
</div>

<div class="report-header">
    <div class="header-top">
        <div>
            <div class="eyebrow">Fund Operations &mdash; Daily Report</div>
            <h1>{meta['display_name']}</h1>
            <div class="header-pool-name">{meta['full_name']}</div>
        </div>
        <div class="confidential-label">Confidential</div>
    </div>
    <div class="header-meta">
        Strategy manager: {meta['manager']} &nbsp;&bull;&nbsp; Underlying: {meta['underlying']}
    </div>
    <div class="header-meta" style="margin-top:1mm;">
        Report date: {report_date} &nbsp;&bull;&nbsp; Generated: {fmt_ts(report_ts)}
    </div>
</div>

<div class="kpi-row">
    <div class="kpi-block">
        <div class="kpi-label">NAV per Token</div>
        <div class="kpi-value">${nav['current']:.4f}</div>
        <div class="kpi-sub {nav_change_cls}">{nav_change_str}</div>
    </div>
    <div class="kpi-block">
        <div class="kpi-label">AUM</div>
        <div class="kpi-value">{fmt_usd(aum['aum_usd'])}</div>
    </div>
    <div class="kpi-block">
        <div class="kpi-label">7-Day Yield (Ann.)</div>
        <div class="kpi-value">{yield_fig['yield_7d']:.2f}%</div>
        <div class="kpi-sub neutral">Source: midas.app</div>
    </div>
    <div class="kpi-block">
        <div class="kpi-label">30-Day Yield (Ann.)</div>
        <div class="kpi-value">{yield_fig['yield_30d']:.2f}%</div>
        <div class="kpi-sub neutral">Source: midas.app</div>
    </div>
</div>

<h2>Section 1 &mdash; NAV and AUM</h2>
<table>
    <tr><th class="label-col">Field</th><th>Value</th></tr>
    {_kv_row("NAV per token", f"${nav['current']:.6f}")}
    {_kv_row("24hr NAV change", f'<span class="{nav_change_cls}">{nav_change_str}</span>', alt=True)}
    {_kv_row("Oracle last updated", fmt_ts(nav.get("oracle_updated_at")))}
    {supply_rows}
    {_kv_row(meta['aum_label'], fmt_usd(aum['aum_usd'], 2), alt=True)}
    <tr>
        <td colspan="2" style="font-style:italic;font-size:8pt;color:#888;background:#f4f5f8;padding:1.5mm 3.5mm;border-top:0.3pt solid #d0d4da;">
            {meta['scope_note']}
        </td>
    </tr>
    {_kv_row("7-day yield (Ann.)", f"{yield_fig['yield_7d']:.2f}%")}
    {_kv_row("30-day yield (Ann.)", f"{yield_fig['yield_30d']:.2f}%", alt=True)}
</table>

<h2 style="page-break-before: always;">Section 2 &mdash; 24hr Activity</h2>
<table>
    <tr>
        <th class="label-col">Activity</th>
        <th style="text-align:center">Count</th>
        <th style="text-align:right">USD</th>
    </tr>
    {_act_row("Instant issuance", act['instant_issuance'])}
    {_act_row("Standard issuance &mdash; submitted", act['standard_issuance_submitted'], alt=True)}
    {_act_row("Standard issuance &mdash; processed", act['standard_issuance_processed'])}
    {_act_row("Instant redemption", act['instant_redemption'], alt=True)}
    {_act_row("Standard redemption &mdash; submitted", act['standard_redemption_submitted'])}
    {_act_row("Standard redemption &mdash; processed", act['standard_redemption_processed'], alt=True)}
    {rejected_row}
    <tr>
        <td class="label-col">Net flow</td>
        <td class="count-col">&mdash;</td>
        <td class="value-col"><span class="{net_cls}">{net_str}</span></td>
    </tr>
</table>

<h2>Section 3 &mdash; Pending Actions</h2>
{pending_section}

<h2 style="page-break-before: always;">Section 4 &mdash; Holder Intelligence (Ethereum)</h2>
<table style="font-size: 9pt; margin-bottom: 2mm;">
    <tr>
        <th style="text-align:center;width:8%">Rank</th>
        <th>Address</th>
        <th style="text-align:right">Balance (tokens)</th>
        <th style="text-align:right">USD Value</th>
        <th style="text-align:right">Share</th>
    </tr>
    {holder_rows}
</table>
<p style="font-size:9pt;color:#444;font-family:Helvetica,Arial,sans-serif;margin:0 0 2mm 0">
    Top-5 concentration (Ethereum): <strong>{fmt_pct(holders.get('top5_concentration_pct'))}</strong>
</p>
{f'<p style="font-size:8pt;color:#888;font-style:italic;font-family:Helvetica,Arial,sans-serif;margin:0 0 3mm 0">{meta["vault_holder_note"]}</p>' if meta.get("vault_holder_note") else ""}

<h3>New Wallets Since Last Report</h3>
{new_wallets_html}

<h3>Large Position Reductions</h3>
{large_red_html}

<h2 style="page-break-before: avoid;">Section 5 &mdash; Flags &amp; Exceptions</h2>
<div class="flags-box">{flags_html}</div>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Operator Daily Digest PDF
# ---------------------------------------------------------------------------

def build_operator_html(data: dict) -> str:
    report_ts   = data["meta"]["generated_at"]
    report_date = fmt_report_date(data["meta"]["report_date"])
    m = data["mTBILL"]
    f = data["mfONE"]

    def op_row(label, mv, fv, alt=False):
        cls = ' class="alt"' if alt else ""
        return (
            f'<tr{cls}>'
            f'<td class="label-col">{label}</td>'
            f'<td style="text-align:right;font-weight:500">{mv}</td>'
            f'<td style="text-align:right;font-weight:500">{fv}</td>'
            f'</tr>'
        )

    m_act = m["activity_24h"]
    f_act = f["activity_24h"]
    m_sla = m["sla_status"]
    f_sla = f["sla_status"]

    m_net = m_act.get("net_flow_usd", 0)
    f_net = f_act.get("net_flow_usd", 0)

    m_nav_chg = fmt_nav_change(m["nav"].get("change_24h"), m["nav"].get("change_24h_pct"))
    f_nav_chg = fmt_nav_change(f["nav"].get("change_24h"), f["nav"].get("change_24h_pct"))

    # SLA breaches = requests at 48hr flag or beyond
    m_breaches = m_sla["flag_48h_count"]
    f_breaches = f_sla["flag_48h_count"]

    f_rejected = f_act.get("standard_redemption_rejected") or {"count": 0}

    def pending_str(sla_d):
        if sla_d["open_count"] == 0:
            return "&mdash;"
        oldest = fmt_hours_human(sla_d.get("oldest_age_hours"))
        n = sla_d["open_count"]
        return f"{n} request{'s' if n > 1 else ''} (oldest: {oldest})"

    m_flags = (
        m.get("flags_and_exceptions", "").replace("\n\n", "<br><br>").replace("\n", " ").strip()
        or "<em>No flags.</em>"
    )
    f_flags = (
        f.get("flags_and_exceptions", "").replace("\n\n", "<br><br>").replace("\n", " ").strip()
        or "<em>No flags.</em>"
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>{BASE_CSS}</style></head>
<body>

<div class="page-footer">
    Confidential &mdash; Internal operator use only &mdash;
    Operator Daily Digest &mdash; {report_date}
</div>

<div class="report-header">
    <div class="header-top">
        <div>
            <div class="eyebrow">Fund Operations &mdash; Operator Daily Digest</div>
            <h1>Operator Daily Digest</h1>
            <div class="header-pool-name">mTBILL &amp; mF-ONE &mdash; Midas</div>
        </div>
        <div class="confidential-label">Confidential &mdash; Internal</div>
    </div>
    <div class="header-meta">
        Report date: {report_date} &nbsp;&bull;&nbsp; Generated: {fmt_ts(report_ts)}
    </div>
</div>

<h2>Product Comparison</h2>
<table class="compact">
    <tr>
        <th class="label-col">Metric</th>
        <th style="text-align:right">mTBILL</th>
        <th style="text-align:right">mF-ONE</th>
    </tr>
    {op_row("NAV per token",
        f"${m['nav']['current']:.6f}",
        f"${f['nav']['current']:.6f}")}
    {op_row("24hr NAV change",
        f'<span class="{sign_class(m["nav"].get("change_24h"))}">{m_nav_chg}</span>',
        f'<span class="{sign_class(f["nav"].get("change_24h"))}">{f_nav_chg}</span>',
        alt=True)}
    {op_row("AUM",
        fmt_usd(m['aum']['aum_usd']),
        fmt_usd(f['aum']['aum_usd']))}
    {op_row("24hr net flow",
        f'<span class="{sign_class(m_net)}">{fmt_usd_change(m_net)}</span>',
        f'<span class="{sign_class(f_net)}">{fmt_usd_change(f_net)}</span>',
        alt=True)}
    {op_row("Instant issuance today",
        fmt_usd(m_act['instant_issuance']['usd']) if m_act['instant_issuance']['count'] else "&mdash;",
        fmt_usd(f_act['instant_issuance']['usd']) if f_act['instant_issuance']['count'] else "&mdash;")}
    {op_row("Instant redemptions today",
        fmt_usd(m_act['instant_redemption']['usd']) if m_act['instant_redemption']['count'] else "&mdash;",
        fmt_usd(f_act['instant_redemption']['usd']) if f_act['instant_redemption']['count'] else "&mdash;",
        alt=True)}
    {op_row("Pending standard redemptions",
        pending_str(m_sla),
        pending_str(f_sla))}
    {op_row("SLA breaches (48hr+)",
        n_or_dash(m_breaches),
        n_or_dash(f_breaches), alt=True)}
    {op_row("Rejected requests",
        "&mdash;",
        n_or_dash(f_rejected.get("count", 0)))}
</table>

<h2><span style="text-transform: none;">mTBILL</span> &mdash; Flags &amp; Exceptions</h2>
<div class="flags-box">{m_flags}</div>

<h2><span style="text-transform: none;">mF-ONE</span> &mdash; Flags &amp; Exceptions</h2>
<div class="flags-box">{f_flags}</div>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Telegram digest — HTML parse mode, consistent with midas_bot.py
# ---------------------------------------------------------------------------

def build_telegram_digest(data: dict) -> str:
    ts          = data["meta"]["generated_at"]
    report_date = fmt_report_date(data["meta"]["report_date"])
    m = data["mTBILL"]
    f = data["mfONE"]

    try:
        dt       = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        time_str = dt.strftime("%H:%M")
    except Exception:
        time_str = ""

    def nav_change_plain(nav_d):
        c = nav_d.get("change_24h")
        p = nav_d.get("change_24h_pct")
        if c is None:
            return "N/A (first run)"
        if c == 0.0:
            return "flat 24hr"
        sign = "+" if c > 0 else ""
        return f"{sign}${c:.6f} ({sign}{p:.4f}%) 24hr"

    def net_plain(usd):
        if usd == 0.0:
            return "—"
        sign    = "+" if usd > 0 else "-" if usd < 0 else ""
        abs_val = abs(usd)
        if abs_val >= 1_000_000:
            return f"{sign}${abs_val / 1_000_000:.2f}M"
        if abs_val >= 1_000:
            return f"{sign}${abs_val / 1_000:.1f}K"
        return f"{sign}${abs_val:.2f}"

    def sla_line(sla_d):
        if sla_d["escalation_96h_count"] > 0:
            return f"⚠️ {sla_d['escalation_96h_count']}× 96hr escalation"
        if sla_d["flag_48h_count"] > 0:
            return f"⚑ {sla_d['flag_48h_count']}× approaching 48hr SLA"
        return "✓ Queue clear"

    def pending_line(sla_d):
        if sla_d["open_count"] == 0:
            return "none"
        oldest = fmt_hours_human(sla_d.get("oldest_age_hours"))
        n = sla_d["open_count"]
        return f"{n} request{'s' if n > 1 else ''} (oldest: {oldest})"

    m_sla = m["sla_status"]
    f_sla = f["sla_status"]
    m_act = m["activity_24h"]
    f_act = f["activity_24h"]
    f_rejected = f_act.get("standard_redemption_rejected") or {"count": 0}

    rejected_line = ""
    if f_rejected.get("count", 0) > 0:
        rejected_line = f"\n⚠️ Rejected redemptions: {f_rejected['count']}"

    has_issues = (
        m_sla["escalation_96h_count"] > 0
        or m_sla["flag_48h_count"] > 0
        or f_sla["escalation_96h_count"] > 0
        or f_sla["flag_48h_count"] > 0
        or f_rejected.get("count", 0) > 0
    )
    footer = (
        "⚠️ Action required — see flags above."
        if has_issues
        else "✅ No issues requiring immediate action."
    )

    return (
        f"<b>Midas Daily Ops — {report_date}</b>\n"
        f"<i>Generated {time_str} UTC</i>\n"
        "\n"
        "────────────────────\n"
        "<b>mTBILL — US Treasury Bills</b>\n"
        f"NAV: <code>${m['nav']['current']:.6f}</code>  ({nav_change_plain(m['nav'])})\n"
        f"AUM: <code>{fmt_usd(m['aum']['aum_usd'])}</code>\n"
        f"Net flow: <code>{net_plain(m_act['net_flow_usd'])}</code>\n"
        f"Pending redemptions: {pending_line(m_sla)}\n"
        f"SLA: {sla_line(m_sla)}\n"
        "\n"
        "────────────────────\n"
        "<b>mF-ONE — Fasanara F-ONE</b>\n"
        f"NAV: <code>${f['nav']['current']:.6f}</code>  ({nav_change_plain(f['nav'])})\n"
        f"AUM: <code>{fmt_usd(f['aum']['aum_usd'])}</code>\n"
        f"Net flow: <code>{net_plain(f_act['net_flow_usd'])}</code>\n"
        f"Pending redemptions: {pending_line(f_sla)}\n"
        f"SLA: {sla_line(f_sla)}"
        f"{rejected_line}\n"
        "\n"
        "────────────────────\n"
        f"{footer}"
    )


# ---------------------------------------------------------------------------
# Telegram send — uses requests, consistent with midas_bot.py
# ---------------------------------------------------------------------------

def send_telegram(message: str) -> None:
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            log.info("Telegram digest sent.")
        else:
            log.error("Telegram send failed: %s %s", r.status_code, r.text[:200])
    except requests.RequestException as exc:
        log.error("Telegram request error: %s", exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Midas daily reporter agent")
    parser.add_argument("--no-telegram", action="store_true",
                        help="Skip Telegram send — digest is printed to stdout instead")
    parser.add_argument("--show-html",   metavar="PRODUCT",
                        help="Print HTML for mTBILL / mF-ONE / operator / telegram to stdout and exit")
    args = parser.parse_args()

    if not INPUT_FILE.exists():
        log.error("data/analyst_output.json not found — run analyst.py first.")
        raise SystemExit(1)

    data = json.loads(INPUT_FILE.read_text())
    log.info("Loaded analyst_output.json  generated_at=%s", data["meta"].get("generated_at"))

    # ── Show-HTML inspection mode — exit after printing ──────────────────────
    if args.show_html:
        target = args.show_html.lower()
        if target == "mtbill":
            print(build_product_html("mTBILL", data))
        elif target in ("mfone", "mf-one"):
            print(build_product_html("mfONE", data))
        elif target == "operator":
            print(build_operator_html(data))
        elif target == "telegram":
            print(build_telegram_digest(data))
        else:
            print(f"Unknown target '{args.show_html}'. Options: mTBILL, mF-ONE, operator, telegram")
        return

    REPORTS_DIR.mkdir(exist_ok=True)
    date_slug = data["meta"]["report_date"]  # YYYY-MM-DD, e.g. 2026-05-04

    # ── Product PDFs ──────────────────────────────────────────────────────────
    for product_key, filename in (
        ("mTBILL", f"mTBILL_Daily_Report_{date_slug}.pdf"),
        ("mfONE",  f"mF-ONE_Daily_Report_{date_slug}.pdf"),
    ):
        html     = build_product_html(product_key, data)
        out_path = REPORTS_DIR / filename
        HTML(string=html).write_pdf(out_path)
        log.info("Written: %s", out_path)

    # ── Operator Digest PDF ───────────────────────────────────────────────────
    op_html  = build_operator_html(data)
    op_path  = REPORTS_DIR / f"Operator_Daily_Digest_{date_slug}.pdf"
    HTML(string=op_html).write_pdf(op_path)
    log.info("Written: %s", op_path)

    # ── Telegram digest ───────────────────────────────────────────────────────
    digest = build_telegram_digest(data)
    if args.no_telegram:
        log.info("Telegram skipped (--no-telegram).")
        print("\n--- TELEGRAM DIGEST ---")
        print(digest)
        print("-----------------------\n")
    else:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            log.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping.")
        else:
            send_telegram(digest)

    log.info("Reporter complete.")


if __name__ == "__main__":
    main()
