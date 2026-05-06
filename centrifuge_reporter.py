"""
reporter.py — Daily reporter agent for JAAA and JTRSY pools.

Reads analyst_output.json and produces:
  - JAAA_Daily_Report.pdf
  - JTRSY_Daily_Report.pdf
  - Operator_Daily_Digest.pdf
  - Operator Telegram digest (sent via existing bot)

Usage:
    python3 reporter.py
    python3 reporter.py --no-telegram         # skip Telegram send
    python3 reporter.py --stdout-telegram     # print digest to stdout instead of sending

Dependencies:
    pip3 install weasyprint "python-telegram-bot==20.7" python-dotenv
    macOS: brew install pango  (required system dependency for WeasyPrint)
    Set DYLD_LIBRARY_PATH=/opt/homebrew/lib if WeasyPrint cannot find libgobject.
"""

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from weasyprint import HTML

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_here = Path(__file__).parent
load_dotenv(_here / ".env", override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

INPUT_FILE = Path("analyst_output.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("reporter")


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
    elif abs_val >= 1_000:
        return f"{sign}${abs_val / 1_000:.{decimals}f}K"
    return f"{sign}${abs_val:.2f}"


def fmt_usd_change(value, decimals=1):
    """Format a delta USD value. Returns — for exactly zero."""
    if value is None:
        return "N/A"
    if value == 0:
        return "&mdash;"
    sign = "+" if value > 0 else ""
    return f"{sign}{fmt_usd(value, decimals)}"


def fmt_usd_change_plain(value, decimals=1):
    """Plain-text version of fmt_usd_change for Telegram."""
    if value is None:
        return "N/A"
    if value == 0:
        return "—"
    sign = "+" if value > 0 else ""
    abs_val = abs(value)
    fmtd = fmt_usd(value, decimals).lstrip("-").lstrip("+").lstrip("$")
    return f"{sign}${abs_val / 1_000_000:.{decimals}f}M" if abs_val >= 1_000_000 else f"{sign}${abs_val:.2f}"


def fmt_hours_human(hours) -> str:
    """Convert decimal hours to 'X days Y hours' human-readable format."""
    if hours is None:
        return "N/A"
    days = int(hours // 24)
    hrs = int(hours % 24)
    if days == 0:
        return f"{hrs}h"
    if hrs == 0:
        return f"{days}d"
    return f"{days}d {hrs}h"


def fmt_ts(iso_str):
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%d %b %Y %H:%M UTC")
    except Exception:
        return iso_str


def fmt_report_date(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%d %B %Y")
    except Exception:
        return iso_str[:10]


def sla_badge_html(sla: dict, pool_report: bool = True) -> str:
    """Render SLA status badge. pool_report=True uses 'No requests overdue' language."""
    if sla["sla_24hr_escalation_count"] > 0:
        n = sla["sla_24hr_escalation_count"]
        return f'<span class="badge badge-red">&#9888; {n} escalation{"s" if n > 1 else ""}</span>'
    if sla["sla_6hr_breach_count"] > 0:
        n = sla["sla_6hr_breach_count"]
        return f'<span class="badge badge-amber">&#9873; {n} breach{"es" if n > 1 else ""}</span>'
    label = "No requests overdue" if pool_report else "No SLA breaches"
    return f'<span class="badge badge-green">&#10003; {label}</span>'


def epoch_status_html(epoch_hrs, has_pending: bool, compact: bool = False) -> str:
    """Three-state epoch age display:
       - recent (<48h): plain age string
       - stale (>48h) + no pending: age + plain note (omitted in compact mode)
       - stale (>48h) + pending: age + red badge
    compact=True is used in the operator digest to avoid cell wrapping — the pending
    row already shows whether there are open requests, making the qualifier redundant.
    """
    if epoch_hrs is None:
        return "N/A"
    age_str = fmt_hours_human(epoch_hrs)
    if epoch_hrs <= 48:
        return age_str
    if has_pending:
        return f'{age_str} <span class="badge badge-red">&#9888; Stale &mdash; pending requests</span>'
    if compact:
        return age_str
    return f'{age_str} &mdash; no pending requests'


def sign_class(value) -> str:
    if value is None:
        return "neutral"
    return "positive" if value >= 0 else "negative"


def n_or_dash(n) -> str:
    """Return — for zero counts. str(0) or '—' is a common bug since '0' is truthy."""
    return "&mdash;" if not n else str(n)


def fmt_pending_invest(count, usd) -> str:
    """Show — when no open invest requests."""
    if count == 0:
        return "&mdash;"
    return f"{count}&nbsp;&nbsp;({fmt_usd(usd)})"


def fmt_pending_redeem(count, shares) -> str:
    """Show — when no open redeem requests."""
    if count == 0:
        return "&mdash;"
    return f"{count}&nbsp;&nbsp;({shares:,.2f} shares)"


def fmt_epoch_usd(value) -> str:
    """Show — for zero epoch values (nothing processed)."""
    if value == 0:
        return "&mdash;"
    return fmt_usd(value)


def fmt_epoch_shares(value) -> str:
    """Show — for zero epoch shares."""
    if value == 0:
        return "&mdash;"
    return f"{value:,.2f}"


# ---------------------------------------------------------------------------
# Shared CSS
# ---------------------------------------------------------------------------

BASE_CSS = """
@page {
    size: A4;
    margin: 20mm 18mm 38mm 18mm;
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
    margin: 3mm 0 5mm 0;
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
    color: #999;
}

/* ── Section headers ── */
h2 {
    font-size: 8.5pt;
    font-weight: bold;
    color: #fff;
    background: #1a2744;
    margin: 6mm 0 0 0;
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
    margin: 4mm 0 1mm 0;
    font-family: Helvetica, Arial, sans-serif;
    page-break-after: avoid;
}

/* ── Tables ── */
table {
    width: 100%;
    border-collapse: collapse;
    margin: 0 0 4mm 0;
    font-size: 10pt;
    border: 0.5pt solid #d0d4da;
    border-top: none;
    page-break-inside: avoid;
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

/* Label column: muted sans-serif */
td.label-col {
    font-family: Helvetica, Arial, sans-serif;
    font-size: 9pt;
    color: #666;
    width: 55%;
}

/* Value column: right-aligned, weight 500 */
td.value-col {
    font-weight: 500;
    text-align: right;
}

/* Three-column tables: count centre, value right */
td.count-col {
    font-weight: 500;
    text-align: center;
}

tr:last-child td {
    border-bottom: none;
}

tr.alt td {
    background: #fafbfc;
}

tr.highlight td {
    background: #fff8e1;
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

/* ── Flags box ── */
.flags-box {
    background: #f4f5f8;
    border: 0.5pt solid #d0d4da;
    border-left: 3pt solid #1a2744;
    padding: 3.5mm 4mm;
    margin: 0 0 4mm 0;
    font-size: 10pt;
    line-height: 1.7;
    page-break-inside: avoid;
}

/* ── Compact table variant (operator digest comparison table) ── */
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

/* ── Footer ── */
.page-footer {
    position: fixed;
    bottom: 8mm;
    left: 18mm;
    right: 18mm;
    font-size: 7.5pt;
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
# Pool-specific PDF
# ---------------------------------------------------------------------------

def _kv_row(label, value, alt=False) -> str:
    cls = ' class="alt"' if alt else ""
    return f'<tr{cls}><td class="label-col">{label}</td><td class="value-col">{value}</td></tr>'


def build_pool_html(pool_name: str, data: dict) -> str:
    d         = data[pool_name]
    nav       = d["nav"]
    epoch     = d["epoch"]
    pend      = d["pending"]
    sla       = d["sla"]
    act       = d["activity_24hr"]
    flags     = d.get("flags_and_exceptions", "")
    pool_full = d.get("pool_name", pool_name)
    report_ts   = data["report_timestamp"]
    report_date = fmt_report_date(report_ts)

    # AUM 24hr change
    aum_change     = nav.get("aum_24hr_change_usd")
    aum_change_str = fmt_usd_change(aum_change)
    aum_cls        = sign_class(aum_change)

    # Net flow
    net     = act.get("net_flow_usd", 0)
    net_str = fmt_usd_change(net)
    net_cls = sign_class(net) if net != 0 else "neutral"

    # Epoch status — three-state
    epoch_hrs = epoch.get("hours_since_last_close")
    has_pending = (
        pend["open_deposit_requests_count"] > 0
        or pend["open_redeem_requests_count"] > 0
    )
    epoch_status_str = epoch_status_html(epoch_hrs, has_pending)

    # Oldest unactioned — prefer SLA record, fall back to pending section
    oldest = sla.get("oldest_unactioned_hours") or pend.get("oldest_unactioned_hours")
    oldest_str = fmt_hours_human(oldest) if oldest else "&mdash;"

    # SLA badge (pool report language)
    sla_badge = sla_badge_html(sla, pool_report=True)

    # SLA breach detail rows
    breach_rows = ""
    for br in sla.get("sla_24hr_escalations", []):
        breach_rows += f"""
        <tr class="highlight">
            <td style="font-family:monospace;font-size:8pt">{br['tx'][:18]}&hellip;</td>
            <td>{br.get('type','&mdash;')}</td>
            <td style="font-size:8pt">{br['submitted_at']}</td>
            <td><strong>{br['age_hours']}h</strong></td>
            <td><span class="badge badge-red">24hr escalation</span></td>
        </tr>"""
    escalated_txs = {b["tx"] for b in sla.get("sla_24hr_escalations", [])}
    for br in sla.get("sla_6hr_breaches", []):
        if br["tx"] in escalated_txs:
            continue
        breach_rows += f"""
        <tr>
            <td style="font-family:monospace;font-size:8pt">{br['tx'][:18]}&hellip;</td>
            <td>{br.get('type','&mdash;')}</td>
            <td style="font-size:8pt">{br['submitted_at']}</td>
            <td>{br['age_hours']}h</td>
            <td><span class="badge badge-amber">6hr breach</span></td>
        </tr>"""

    sla_breach_table = ""
    if breach_rows:
        sla_breach_table = f"""
        <h3>Overdue Request Detail</h3>
        <table>
            <tr><th>Tx Hash</th><th>Type</th><th>Submitted</th><th>Age</th><th>Status</th></tr>
            {breach_rows}
        </table>"""

    # Stale claimable positions
    stale_rows = ""
    for c in sla.get("claimable_stale_48hr_plus", []):
        stale_rows += f"""
        <tr>
            <td style="font-family:monospace;font-size:8pt">{c['tx'][:18]}&hellip;</td>
            <td>{c.get('type','&mdash;')}</td>
            <td style="font-size:8pt">{c.get('claimable_at','&mdash;')}</td>
            <td>{fmt_hours_human(c['age_hours'])}</td>
        </tr>"""
    stale_table = ""
    if stale_rows:
        stale_table = f"""
        <h3>Unclaimed Positions (48h+) <span class="badge badge-amber">Investor action pending</span></h3>
        <table>
            <tr><th>Tx Hash</th><th>Type</th><th>Claimable Since</th><th>Age</th></tr>
            {stale_rows}
        </table>"""

    # Sub-minimum transactions
    sub_rows = ""
    for t in act.get("sub_minimum_transactions", []):
        sub_rows += f"""
        <tr>
            <td style="font-family:monospace;font-size:8pt">{t['tx'][:18]}&hellip;</td>
            <td>{t['type']}</td>
            <td>${t['amount_usd']:,.0f}</td>
            <td>{t['chain']}</td>
        </tr>"""
    sub_table = ""
    if sub_rows:
        sub_table = f"""
        <h3>Sub-minimum Transactions <span class="badge badge-amber">Anomaly</span></h3>
        <table>
            <tr><th>Tx Hash</th><th>Type</th><th>Amount (USD)</th><th>Chain</th></tr>
            {sub_rows}
        </table>"""

    # Lifecycle gap note
    gap_note = ""
    if sla.get("lifecycle_gap_note"):
        gap_note = f'<p style="font-size:8pt;color:#bbb;margin-top:2mm;font-family:Helvetica,Arial,sans-serif;"><em>Note: {sla["lifecycle_gap_note"]}</em></p>'

    # Flags text — plain text, newlines become <br>
    flags_html = flags.replace("\n\n", "<br><br>").replace("\n", " ").strip() if flags else "<em>No flags or exceptions.</em>"

    # requests_actioned_count supports both new and old field name
    actioned_count = act.get("requests_actioned_count", act.get("requests_executed_count", 0))

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>{BASE_CSS}</style></head>
<body>

<div class="page-footer">
    Confidential &mdash; For fund administrator use only &mdash;
    {pool_name} Daily Operations Report &mdash; {report_date}
</div>

<div class="report-header">
    <div class="header-top">
        <div>
            <div class="eyebrow">Fund Operations &mdash; Daily Report</div>
            <h1>{pool_name}</h1>
            <div class="header-pool-name">{pool_full}</div>
        </div>
        <div class="confidential-label">Confidential</div>
    </div>
    <div class="header-meta">
        Report date: {report_date} &nbsp;&bull;&nbsp;
        Generated: {fmt_ts(report_ts)} &nbsp;&bull;&nbsp;
        Source: Centrifuge GraphQL API
    </div>
</div>

<div class="kpi-row">
    <div class="kpi-block">
        <div class="kpi-label">NAV per Share</div>
        <div class="kpi-value">${nav['nav_per_share']:.6f}</div>
    </div>
    <div class="kpi-block">
        <div class="kpi-label">AUM</div>
        <div class="kpi-value">{fmt_usd(nav['aum_usd'])}</div>
        <div class="kpi-sub {aum_cls}">24hr: {aum_change_str}</div>
    </div>
    <div class="kpi-block">
        <div class="kpi-label">30d Yield (Ann.)</div>
        <div class="kpi-value">{nav['yield_30d_annualized_pct']:.2f}%</div>
        <div class="kpi-sub neutral">7d: {nav['yield_7d_annualized_pct']:.2f}%</div>
    </div>
    <div class="kpi-block">
        <div class="kpi-label">YTD Return</div>
        <div class="kpi-value">{nav['yield_ytd_pct']:.4f}%</div>
        <div class="kpi-sub neutral">Since inception: {nav['yield_since_inception_pct']:.4f}%</div>
    </div>
</div>

<h2>Section 1 &mdash; Epoch Status</h2>
<table>
    <tr><th class="label-col">Field</th><th>Value</th></tr>
    {_kv_row("Latest Epoch", f"#{epoch.get('latest_epoch_index', 'N/A')}")}
    {_kv_row("Last Epoch Approved", fmt_ts(epoch.get('last_epoch_approved_at')), alt=True)}
    {_kv_row("Last Epoch Closed (Issued)", fmt_ts(epoch.get('last_epoch_issued_at')))}
    {_kv_row("Time Since Last Close", epoch_status_str, alt=True)}
    {_kv_row("Deposits Processed at Last Epoch", fmt_epoch_usd(epoch.get('last_epoch_deposit_usd', 0)))}
    {_kv_row("Shares Issued at Last Epoch", fmt_epoch_shares(epoch.get('last_epoch_shares_issued', 0)), alt=True)}
    {_kv_row("Redemptions Processed at Last Epoch", fmt_epoch_usd(epoch.get('last_epoch_redeem_usd', 0)))}
    {_kv_row("Shares Redeemed at Last Epoch", fmt_epoch_shares(epoch.get('last_epoch_shares_redeemed', 0)), alt=True)}
    {_kv_row("NAV at Last Epoch Close", f"${epoch.get('last_epoch_nav_at_issue', 0):.6f}")}
</table>

<h2>Section 2 &mdash; Pending Actions</h2>
<table>
    <tr><th class="label-col">Field</th><th>Value</th></tr>
    {_kv_row("Open Deposit Requests", fmt_pending_invest(pend['open_deposit_requests_count'], pend['pending_deposit_usd']))}
    {_kv_row("Open Redeem Requests", fmt_pending_redeem(pend['open_redeem_requests_count'], pend['pending_redeem_shares']), alt=True)}
    {_kv_row("Oldest Unactioned Request", oldest_str)}
    {_kv_row("Status", sla_badge, alt=True)}
</table>
{sla_breach_table}
{stale_table}
{sub_table}
{gap_note}

<h2>Section 3 &mdash; Activity (Last 24 Hours)</h2>
<table>
    <tr><th class="label-col">Field</th><th style="text-align:center">Count</th><th style="text-align:right">Value</th></tr>
    <tr>
        <td class="label-col">New Deposit Requests</td>
        <td class="count-col">{act['new_deposit_requests_count'] or '&mdash;'}</td>
        <td class="value-col">{fmt_usd(act['new_deposit_requests_usd']) if act['new_deposit_requests_usd'] else '&mdash;'}</td>
    </tr>
    <tr class="alt">
        <td class="label-col">New Redeem Requests</td>
        <td class="count-col">{act['new_redeem_requests_count'] or '&mdash;'}</td>
        <td class="value-col">&mdash;</td>
    </tr>
    <tr>
        <td class="label-col">Requests Actioned (Claimable)</td>
        <td class="count-col">{actioned_count or '&mdash;'}</td>
        <td class="value-col">&mdash;</td>
    </tr>
    <tr class="alt">
        <td class="label-col">Claims Made by Investors</td>
        <td class="count-col">{act['claims_made_count'] or '&mdash;'}</td>
        <td class="value-col">&mdash;</td>
    </tr>
    <tr>
        <td class="label-col">Net Flow</td>
        <td class="count-col">&mdash;</td>
        <td class="value-col"><span class="{net_cls}">{net_str}</span></td>
    </tr>
</table>

<h2>Section 4 &mdash; Flags &amp; Exceptions</h2>
<div class="flags-box">{flags_html}</div>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Operator Daily Digest PDF
# ---------------------------------------------------------------------------

def build_operator_html(data: dict) -> str:
    report_ts   = data["report_timestamp"]
    report_date = fmt_report_date(report_ts)
    j = data["JAAA"]
    t = data["JTRSY"]

    def op_row(label, jv, tv, alt=False):
        cls = ' class="alt"' if alt else ""
        return (
            f'<tr{cls}>'
            f'<td class="label-col">{label}</td>'
            f'<td style="text-align:right;font-weight:500">{jv}</td>'
            f'<td style="text-align:right;font-weight:500">{tv}</td>'
            f'</tr>'
        )

    j_aum_ch    = j["nav"].get("aum_24hr_change_usd")
    t_aum_ch    = t["nav"].get("aum_24hr_change_usd")
    j_epoch_hrs = j["epoch"].get("hours_since_last_close")
    t_epoch_hrs = t["epoch"].get("hours_since_last_close")

    j_has_pending = j["pending"]["open_deposit_requests_count"] > 0 or j["pending"]["open_redeem_requests_count"] > 0
    t_has_pending = t["pending"]["open_deposit_requests_count"] > 0 or t["pending"]["open_redeem_requests_count"] > 0

    j_epoch_str = epoch_status_html(j_epoch_hrs, j_has_pending, compact=True)
    t_epoch_str = epoch_status_html(t_epoch_hrs, t_has_pending, compact=True)

    j_net = j["activity_24hr"]["net_flow_usd"]
    t_net = t["activity_24hr"]["net_flow_usd"]

    j_actioned = j["activity_24hr"].get("requests_actioned_count", j["activity_24hr"].get("requests_executed_count", 0))
    t_actioned = t["activity_24hr"].get("requests_actioned_count", t["activity_24hr"].get("requests_executed_count", 0))

    combined_flags = data.get("operator_combined_flags", "")
    flags_html = combined_flags.replace("\n\n", "<br><br>").replace("\n", " ").strip() if combined_flags else "<em>No flags across either pool.</em>"
    j_flags = j.get("flags_and_exceptions", "").replace("\n\n", "<br><br>").replace("\n", " ").strip() or "<em>No flags.</em>"
    t_flags = t.get("flags_and_exceptions", "").replace("\n\n", "<br><br>").replace("\n", " ").strip() or "<em>No flags.</em>"

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
            <div class="header-pool-name">JAAA &amp; JTRSY &mdash; Janus Henderson Anemoy Funds</div>
        </div>
        <div class="confidential-label">Confidential &mdash; Internal</div>
    </div>
    <div class="header-meta">
        Report date: {report_date} &nbsp;&bull;&nbsp;
        Generated: {fmt_ts(report_ts)} &nbsp;&bull;&nbsp;
        Source: Centrifuge GraphQL API
    </div>
</div>

<h2>Pool Comparison</h2>
<table class="compact">
    <tr><th class="label-col">Metric</th><th style="text-align:right">JAAA</th><th style="text-align:right">JTRSY</th></tr>
    {op_row("NAV per Share",
        f"${j['nav']['nav_per_share']:.6f}",
        f"${t['nav']['nav_per_share']:.6f}")}
    {op_row("AUM",
        fmt_usd(j['nav']['aum_usd']),
        fmt_usd(t['nav']['aum_usd']), alt=True)}
    {op_row("24hr AUM Change",
        f'<span class="{sign_class(j_aum_ch)}">{fmt_usd_change(j_aum_ch)}</span>',
        f'<span class="{sign_class(t_aum_ch)}">{fmt_usd_change(t_aum_ch)}</span>')}
    {op_row("30d Yield (Ann.)",
        f"{j['nav']['yield_30d_annualized_pct']:.2f}%",
        f"{t['nav']['yield_30d_annualized_pct']:.2f}%", alt=True)}
    {op_row("7d Yield (Ann.)",
        f"{j['nav']['yield_7d_annualized_pct']:.2f}%",
        f"{t['nav']['yield_7d_annualized_pct']:.2f}%")}
    {op_row("YTD Return",
        f"{j['nav']['yield_ytd_pct']:.4f}%",
        f"{t['nav']['yield_ytd_pct']:.4f}%", alt=True)}
    {op_row("Latest Epoch",
        f"#{j['epoch'].get('latest_epoch_index','N/A')}",
        f"#{t['epoch'].get('latest_epoch_index','N/A')}")}
    {op_row("Time Since Last Epoch Close", j_epoch_str, t_epoch_str, alt=True)}
    {op_row("Open Deposit Requests",
        fmt_pending_invest(j['pending']['open_deposit_requests_count'], j['pending']['pending_deposit_usd']),
        fmt_pending_invest(t['pending']['open_deposit_requests_count'], t['pending']['pending_deposit_usd']))}
    {op_row("Open Redeem Requests",
        fmt_pending_redeem(j['pending']['open_redeem_requests_count'], j['pending']['pending_redeem_shares']),
        fmt_pending_redeem(t['pending']['open_redeem_requests_count'], t['pending']['pending_redeem_shares']), alt=True)}
    {op_row("SLA Status", sla_badge_html(j["sla"], pool_report=False), sla_badge_html(t["sla"], pool_report=False))}
    {op_row("Net Flow (24hr)",
        f'<span class="{sign_class(j_net)}">{fmt_usd_change(j_net)}</span>',
        f'<span class="{sign_class(t_net)}">{fmt_usd_change(t_net)}</span>',
        alt=True)}
    {op_row("New Deposit Requests (24hr)",
        n_or_dash(j['activity_24hr']['new_deposit_requests_count']),
        n_or_dash(t['activity_24hr']['new_deposit_requests_count']))}
    {op_row("New Redeem Requests (24hr)",
        n_or_dash(j['activity_24hr']['new_redeem_requests_count']),
        n_or_dash(t['activity_24hr']['new_redeem_requests_count']), alt=True)}
    {op_row("Requests Actioned (Claimable)",
        n_or_dash(j_actioned),
        n_or_dash(t_actioned))}
    {op_row("Claims Made (24hr)",
        n_or_dash(j['activity_24hr']['claims_made_count']),
        n_or_dash(t['activity_24hr']['claims_made_count']), alt=True)}
    {op_row("Sub-minimum Transactions",
        n_or_dash(len(j['activity_24hr']['sub_minimum_transactions'])),
        n_or_dash(len(t['activity_24hr']['sub_minimum_transactions'])))}
</table>

<h2 style="page-break-before: always">Combined Flags &amp; Exceptions</h2>
<div class="flags-box">{flags_html}</div>

<h2>JAAA &mdash; Flags &amp; Exceptions</h2>
<div class="flags-box">{j_flags}</div>

<h2>JTRSY &mdash; Flags &amp; Exceptions</h2>
<div class="flags-box">{t_flags}</div>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Telegram digest
# ---------------------------------------------------------------------------

def build_telegram_digest(data: dict) -> str:
    j  = data["JAAA"]
    t  = data["JTRSY"]
    ts = data["report_timestamp"]

    try:
        dt       = datetime.fromisoformat(ts)
        date_str = dt.strftime("%d %b %Y")
        time_str = dt.strftime("%H:%M")
    except Exception:
        date_str = ts[:10]
        time_str = ""

    def sla_line(sla):
        if sla["sla_24hr_escalation_count"] > 0:
            return f"⚠️ {sla['sla_24hr_escalation_count']}× 24hr escalation"
        if sla["sla_6hr_breach_count"] > 0:
            return f"⚑ {sla['sla_6hr_breach_count']}× 6hr breach"
        return "✓ No requests overdue"

    def epoch_line(epoch_hrs, has_pending):
        age = fmt_hours_human(epoch_hrs) if epoch_hrs else "N/A"
        if epoch_hrs and epoch_hrs > 48 and has_pending:
            return f"{age} ⚠️"
        return age

    j_epoch_hrs = j["epoch"].get("hours_since_last_close")
    t_epoch_hrs = t["epoch"].get("hours_since_last_close")
    j_has_pending = j["pending"]["open_deposit_requests_count"] > 0 or j["pending"]["open_redeem_requests_count"] > 0
    t_has_pending = t["pending"]["open_deposit_requests_count"] > 0 or t["pending"]["open_redeem_requests_count"] > 0

    j_aum_ch = j["nav"].get("aum_24hr_change_usd", 0)
    t_aum_ch = t["nav"].get("aum_24hr_change_usd", 0)
    j_net    = j["activity_24hr"]["net_flow_usd"]
    t_net    = t["activity_24hr"]["net_flow_usd"]

    def plain_usd_change(value):
        if value is None or value == 0:
            return "—"
        sign = "+" if value > 0 else ""
        abs_val = abs(value)
        if abs_val >= 1_000_000:
            return f"{sign}${abs_val/1_000_000:.1f}M"
        return f"{sign}${abs_val:.2f}"

    combined = data.get("operator_combined_flags", "No flags.")
    if len(combined) > 320:
        combined = combined[:317] + "…"

    j_actioned = j["activity_24hr"].get("requests_actioned_count", j["activity_24hr"].get("requests_executed_count", 0))
    t_actioned = t["activity_24hr"].get("requests_actioned_count", t["activity_24hr"].get("requests_executed_count", 0))

    def count_or_dash(n):
        return str(n) if n else "—"

    return (
        f"📊 *Daily Ops Digest — {date_str}*\n"
        f"_Generated {time_str} UTC_\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*JAAA — AAA CLO Fund*\n"
        f"NAV: `${j['nav']['nav_per_share']:.4f}` | AUM: `{fmt_usd(j['nav']['aum_usd'])}` (`{plain_usd_change(j_aum_ch)}` 24hr)\n"
        f"30d APY: `{j['nav']['yield_30d_annualized_pct']:.2f}%` | 7d: `{j['nav']['yield_7d_annualized_pct']:.2f}%`\n"
        f"Epoch: `#{j['epoch'].get('latest_epoch_index','N/A')}` — `{epoch_line(j_epoch_hrs, j_has_pending)}` ago\n"
        f"Pending: `{j['pending']['open_deposit_requests_count']}` deposits / `{j['pending']['open_redeem_requests_count']}` redeems\n"
        f"SLA: {sla_line(j['sla'])}\n"
        f"24hr: `{count_or_dash(j['activity_24hr']['new_deposit_requests_count'])}` new deposits · "
        f"`{count_or_dash(j_actioned)}` actioned · "
        f"`{count_or_dash(j['activity_24hr']['claims_made_count'])}` claims\n"
        f"Net flow: `{plain_usd_change(j_net)}`\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*JTRSY — Treasury Fund*\n"
        f"NAV: `${t['nav']['nav_per_share']:.4f}` | AUM: `{fmt_usd(t['nav']['aum_usd'])}` (`{plain_usd_change(t_aum_ch)}` 24hr)\n"
        f"30d APY: `{t['nav']['yield_30d_annualized_pct']:.2f}%` | 7d: `{t['nav']['yield_7d_annualized_pct']:.2f}%`\n"
        f"Epoch: `#{t['epoch'].get('latest_epoch_index','N/A')}` — `{epoch_line(t_epoch_hrs, t_has_pending)}` ago\n"
        f"Pending: `{t['pending']['open_deposit_requests_count']}` deposits / `{t['pending']['open_redeem_requests_count']}` redeems\n"
        f"SLA: {sla_line(t['sla'])}\n"
        f"24hr: `{count_or_dash(t['activity_24hr']['new_deposit_requests_count'])}` new deposits · "
        f"`{count_or_dash(t_actioned)}` actioned · "
        f"`{count_or_dash(t['activity_24hr']['claims_made_count'])}` claims\n"
        f"Net flow: `{plain_usd_change(t_net)}`\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Flags & Exceptions*\n"
        f"{combined}"
    )


# ---------------------------------------------------------------------------
# Telegram send
# ---------------------------------------------------------------------------

async def _send_telegram(message: str) -> None:
    from telegram import Bot
    from telegram.error import TelegramError
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="Markdown",
        )
        log.info("Telegram digest sent.")
    except TelegramError as exc:
        log.error("Telegram send failed: %s", exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Centrifuge daily reporter agent")
    parser.add_argument("--no-telegram",     action="store_true", help="Skip Telegram send")
    parser.add_argument("--stdout-telegram", action="store_true", help="Print Telegram digest to stdout instead of sending")
    args = parser.parse_args()

    if not INPUT_FILE.exists():
        log.error("analyst_output.json not found — run analyst.py first.")
        raise SystemExit(1)

    data = json.loads(INPUT_FILE.read_text())
    log.info("Loaded analyst_output.json (report_timestamp=%s)", data.get("report_timestamp"))

    # Pool-specific PDFs
    for pool in ("JAAA", "JTRSY"):
        html     = build_pool_html(pool, data)
        out_path = Path(f"{pool}_Daily_Report.pdf")
        HTML(string=html).write_pdf(out_path)
        log.info("Written: %s", out_path)

    # Operator digest PDF
    op_html  = build_operator_html(data)
    op_path  = Path("Operator_Daily_Digest.pdf")
    HTML(string=op_html).write_pdf(op_path)
    log.info("Written: %s", op_path)

    # Telegram digest
    digest = build_telegram_digest(data)

    if args.stdout_telegram:
        print("\n--- TELEGRAM DIGEST ---")
        print(digest)
        print("-----------------------\n")
    elif args.no_telegram:
        log.info("Telegram skipped (--no-telegram).")
    else:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            log.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping.")
        else:
            asyncio.run(_send_telegram(digest))

    log.info("Reporter complete.")


if __name__ == "__main__":
    main()
