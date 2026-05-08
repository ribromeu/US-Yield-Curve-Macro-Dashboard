"""
╔══════════════════════════════════════════════════════════════════╗
║         US YIELD CURVE — EMAIL ALERT SYSTEM                      ║
║                                                                  ║
║  TWO EMAIL TYPES:                                                ║
║    Daily report  : sent every run, HTML with full metrics        ║
║    Urgent alert  : sent only when thresholds are breached        ║
║                                                                  ║
║  ALERT THRESHOLDS (configurable below):                          ║
║    Curve inverted (10Y-2Y < 0)          → always triggers        ║
║    P(recession) univariate  > 30%       → triggers               ║
║    P(recession) multivariate > 30%      → triggers               ║
║    HY spread > 600 bps                  → triggers               ║
║    Term premium < -0.5 pp               → triggers               ║
║                                                                  ║
║  SETUP:                                                          ║
║    1. Enable Gmail 2FA at myaccount.google.com/security          ║
║    2. Create App Password (Mail / Other device)                  ║
║    3. Add secrets to .env or GitHub Secrets:                     ║
║         GMAIL_USER     = your.address@gmail.com                  ║
║         GMAIL_APP_PASS = 16-char app password (no spaces)        ║
║         ALERT_TO       = recipient@example.com                   ║
║                                                                  ║
║  REQUIREMENTS:                                                   ║
║    No extra installs — uses Python stdlib only                   ║
║    (smtplib, email, ssl — included in Python 3.6+)               ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ──────────────────────────────────────────────────────────────────
# BLOCK 0 — IMPORTS AND ALERT CONFIGURATION
#
# WHAT IT DOES: imports stdlib email modules and defines all alert
# thresholds in one place. Changing a threshold here affects both
# the trigger logic and the HTML badge color in the email body.
#
# ALERT_THRESHOLDS:
#   inversion        : 10Y-2Y < 0 → curve inverted
#   prob_uni_pct     : univariate probit > this % → alert
#   prob_mv_pct      : multivariate probit > this % → alert
#   hy_spread_bps    : HY OAS > this level → credit stress
#   term_premium_pp  : ACM term premium < this → QE distortion
#
# DEPENDENCIES: none
# USED IN: BLOCK 2 (should_send_alert), BLOCK 3 (build bodies)
# ──────────────────────────────────────────────────────────────────

import os
import ssl
import smtplib
import math
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from datetime import datetime

# Alert thresholds — edit here to recalibrate sensitivity
ALERT_THRESHOLDS = {
    "inversion":       True,   # trigger if 10Y-2Y < 0 (bool flag)
    "prob_uni_pct":    30.0,   # univariate probit   > 30% → alert
    "prob_mv_pct":     30.0,   # multivariate probit > 30% → alert
    "hy_spread_bps":   600.0,  # HY OAS > 600 bps   → credit stress
    "term_premium_pp": -0.5,   # term premium < -0.5 → QE distortion
}


# ──────────────────────────────────────────────────────────────────
# BLOCK 1 — GMAIL SMTP SENDER
#
# WHAT IT DOES: establishes a TLS-encrypted connection to Gmail's
# SMTP server and sends a pre-built MIMEMultipart message.
#
# WHY GMAIL APP PASSWORD (not regular password):
#   Google disabled basic auth for SMTP in 2022. An App Password is
#   a 16-character token that bypasses OAuth but still uses 2FA —
#   safer than storing the account password. The token only works
#   for SMTP and can be revoked independently.
#
# ATTACHMENT HANDLING: PNG files are embedded as inline CID images
# (Content-ID), not attachments — they render directly in the email
# body on Gmail, Outlook, and Apple Mail without the user needing
# to download anything.
#
# DEPENDENCIES: GMAIL_USER, GMAIL_APP_PASS, ALERT_TO env vars
# USED IN: send_daily_report(), send_urgent_alert()
# ──────────────────────────────────────────────────────────────────

def _send_email(subject: str, html_body: str, png_paths: list[str]) -> bool:
    """
    Send an HTML email via Gmail SMTP with inline PNG attachments.

    Reads credentials from environment variables:
      GMAIL_USER     : sender Gmail address
      GMAIL_APP_PASS : 16-char App Password (no spaces)
      ALERT_TO       : recipient address (comma-separated for multiple)

    Returns True on success, False on any SMTP or auth error.
    Prints a clear error message without raising — the dashboard
    should continue running even if email fails.
    """
    gmail_user = os.getenv("GMAIL_USER", "")
    gmail_pass = os.getenv("GMAIL_APP_PASS", "")
    alert_to   = os.getenv("ALERT_TO", "")

    if not all([gmail_user, gmail_pass, alert_to]):
        print("  ⚠ Email skipped: GMAIL_USER / GMAIL_APP_PASS / ALERT_TO not set in environment.")
        return False

    # Build MIME message — multipart/related allows inline CID images
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"]    = f"Yield Curve Monitor <{gmail_user}>"
    msg["To"]      = alert_to

    # Attach HTML body
    msg.attach(MIMEText(html_body, "html"))

    # Embed each PNG as an inline image with CID = filename (no extension)
    for path in png_paths:
        if not os.path.exists(path):
            continue
        cid = os.path.splitext(os.path.basename(path))[0]
        with open(path, "rb") as f:
            img = MIMEImage(f.read(), _subtype="png")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=os.path.basename(path))
        msg.attach(img)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, alert_to.split(","), msg.as_string())
        print(f"  ✓ Email sent → {alert_to}")
        return True
    except smtplib.SMTPAuthenticationError:
        print("  ✗ Email failed: authentication error.")
        print("    Check GMAIL_APP_PASS — must be a 16-char App Password, not your Gmail password.")
        print("    Generate one at: https://myaccount.google.com/apppasswords")
        return False
    except Exception as e:
        print(f"  ✗ Email failed: {e}")
        return False


# ──────────────────────────────────────────────────────────────────
# BLOCK 2 — ALERT TRIGGER LOGIC
#
# WHAT IT DOES: evaluates current metric values against ALERT_THRESHOLDS
# and returns a dict of triggered conditions. The dict is used both
# to decide whether to send an urgent email and to populate the
# alert badges in the email HTML body.
#
# RETURNS:
#   dict mapping condition name → (triggered: bool, message: str)
#   Example:
#     {"inversion":  (True,  "10Y-2Y = -0.32 pp — INVERTED"),
#      "prob_uni":   (True,  "Univariate P = 38.4%"),
#      "hy_spread":  (False, "HY = 420 bps")}
#
# DEPENDENCIES: ALERT_THRESHOLDS
# USED IN: should_send_alert(), build_urgent_body()
# ──────────────────────────────────────────────────────────────────

def evaluate_alerts(
    spread_10y2y: float,
    spread_10y3m: float,
    prob_uni:     float,
    prob_mv:      float,
    hy_spread:    float,
    term_premium: float,
) -> dict:
    """
    Evaluate all alert conditions against current metric values.
    NaN values are treated as non-triggered to avoid false alerts
    from missing data.
    """
    def _nan(v): return math.isnan(v) if isinstance(v, float) else False

    checks = {}

    # 1. Curve inversion
    if ALERT_THRESHOLDS["inversion"] and not _nan(spread_10y2y):
        triggered = spread_10y2y < 0
        checks["inversion"] = (
            triggered,
            f"10Y-2Y = {spread_10y2y:+.2f} pp — {'INVERTED ⚠' if triggered else 'normal'}"
        )

    # 2. Univariate recession probability
    if not _nan(prob_uni):
        p = prob_uni * 100
        triggered = p > ALERT_THRESHOLDS["prob_uni_pct"]
        checks["prob_uni"] = (
            triggered,
            f"Univariate P(recession 12m) = {p:.1f}%  [threshold: {ALERT_THRESHOLDS['prob_uni_pct']:.0f}%]"
        )

    # 3. Multivariate recession probability
    if not _nan(prob_mv):
        p = prob_mv * 100
        triggered = p > ALERT_THRESHOLDS["prob_mv_pct"]
        checks["prob_mv"] = (
            triggered,
            f"Multivariate P(recession 12m) = {p:.1f}%  [threshold: {ALERT_THRESHOLDS['prob_mv_pct']:.0f}%]"
        )

    # 4. High Yield spread
    if not _nan(hy_spread):
        triggered = hy_spread > ALERT_THRESHOLDS["hy_spread_bps"]
        checks["hy_spread"] = (
            triggered,
            f"HY OAS = {hy_spread:.0f} bps  [threshold: {ALERT_THRESHOLDS['hy_spread_bps']:.0f} bps]"
        )

    # 5. Negative term premium
    if not _nan(term_premium):
        triggered = term_premium < ALERT_THRESHOLDS["term_premium_pp"]
        checks["term_premium"] = (
            triggered,
            f"Term premium = {term_premium:+.2f} pp  [threshold: {ALERT_THRESHOLDS['term_premium_pp']:+.1f} pp]"
        )

    return checks


def should_send_alert(checks: dict) -> bool:
    """Return True if any single alert condition is triggered."""
    return any(triggered for triggered, _ in checks.values())


# ──────────────────────────────────────────────────────────────────
# BLOCK 3 — HTML EMAIL BUILDERS
#
# WHAT IT DOES: builds the HTML body for each email type.
#
#   build_daily_body()
#     Full daily report — sent every run regardless of alerts.
#     Contains: date header, status banner (NORMAL / INVERTED),
#     metrics table, alert status for each condition, and an inline
#     embed of yield_curve_dashboard.png via CID reference.
#     Dark header (#0d1117) matching the dashboard visual identity.
#
#   build_urgent_body()
#     Compact urgent alert — sent only when at least one threshold
#     is breached. Contains: red ALERT banner, triggered conditions
#     highlighted, all current metrics, and the same dashboard embed.
#     Designed to be readable as a push notification preview.
#
# WHY HTML (not plain text):
#   Gmail and most clients render inline images and tables only in
#   HTML mode. Plain text would not display the embedded dashboard.
#
# DEPENDENCIES: evaluate_alerts() output (checks dict)
# USED IN: send_daily_report(), send_urgent_alert()
# ──────────────────────────────────────────────────────────────────

def _badge(triggered: bool, text: str) -> str:
    """Render a colored inline badge — red if triggered, green if clear."""
    color = "#c0392b" if triggered else "#27ae60"
    bg    = "#fdecea" if triggered else "#eafaf1"
    icon  = "⚠" if triggered else "✓"
    return (f'<span style="background:{bg};color:{color};padding:3px 10px;'
            f'border-radius:4px;font-weight:bold;font-size:13px;">'
            f'{icon} {text}</span>')


def _metric_row(label: str, value: str, highlight: bool = False) -> str:
    """Render a single metrics table row."""
    bg = "#fdecea" if highlight else "#f8f7f4"
    return (f'<tr style="background:{bg};">'
            f'<td style="padding:7px 14px;color:#555;font-size:13px;">{label}</td>'
            f'<td style="padding:7px 14px;font-weight:bold;font-size:13px;">{value}</td>'
            f'</tr>')


def build_daily_body(
    spread_10y2y: float,
    spread_10y3m: float,
    prob_uni:     float,
    prob_mv:      float,
    hy_spread:    float,
    ig_spread:    float,
    term_premium: float,
    fwd_5y5y:     float,
    regime:       str,
    inversions_count: int,
    checks:       dict,
) -> str:
    """
    Build the full daily report HTML body.
    Dashboard PNG is embedded inline via CID (no download required).
    """
    def _f(v, fmt=".2f", sfx=""):
        return f"{v:{fmt}}{sfx}" if not math.isnan(v) else "n/a"

    today      = datetime.today().strftime("%B %d, %Y")
    inverted   = not math.isnan(spread_10y2y) and spread_10y2y < 0
    hdr_color  = "#c0392b" if inverted else "#0d1117"
    hdr_label  = "⚠ CURVE INVERTED" if inverted else "✓ CURVE NORMAL"
    any_alert  = should_send_alert(checks)

    # Build alerts summary section
    alert_rows = ""
    for name, (triggered, msg) in checks.items():
        alert_rows += f'<tr><td style="padding:5px 14px;">{_badge(triggered, msg)}</td></tr>'

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0ede8;font-family:Arial,sans-serif;">

<!-- Header -->
<table width="620" cellpadding="0" cellspacing="0" align="center"
       style="margin:24px auto;border-radius:10px;overflow:hidden;
              box-shadow:0 2px 12px rgba(0,0,0,0.10);">
  <tr>
    <td style="background:{hdr_color};padding:22px 28px;color:#e6edf3;">
      <p style="margin:0;font-size:11px;opacity:0.7;letter-spacing:1px;">
        US YIELD CURVE MONITOR — DAILY REPORT</p>
      <p style="margin:6px 0 0;font-size:22px;font-weight:bold;">{hdr_label}</p>
      <p style="margin:4px 0 0;font-size:12px;opacity:0.65;">{today}</p>
    </td>
  </tr>

  <!-- Metrics table -->
  <tr><td style="background:#ffffff;padding:20px 28px;">
    <p style="margin:0 0 12px;font-size:13px;font-weight:bold;color:#333;">
      CURRENT METRICS</p>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;border-radius:6px;overflow:hidden;">
      {_metric_row("Spread 10Y-2Y",     _f(spread_10y2y, "+.2f", " pp"),  inverted)}
      {_metric_row("Spread 10Y-3M",     _f(spread_10y3m, "+.2f", " pp"))}
      {_metric_row("P(recession) — univariate",
                   _f(prob_uni * 100 if not math.isnan(prob_uni) else float("nan"), ".1f", "%"),
                   not math.isnan(prob_uni) and prob_uni * 100 > ALERT_THRESHOLDS["prob_uni_pct"])}
      {_metric_row("P(recession) — multivariate",
                   _f(prob_mv * 100 if not math.isnan(prob_mv) else float("nan"), ".1f", "%"),
                   not math.isnan(prob_mv) and prob_mv * 100 > ALERT_THRESHOLDS["prob_mv_pct"])}
      {_metric_row("Term Premium 10Y (ACM)", _f(term_premium, "+.2f", " pp"),
                   not math.isnan(term_premium) and term_premium < ALERT_THRESHOLDS["term_premium_pp"])}
      {_metric_row("Forward 5Y5Y",      _f(fwd_5y5y, ".2f", "%"))}
      {_metric_row("IG Spread (OAS)",   _f(ig_spread, ".0f", " bps"))}
      {_metric_row("HY Spread (OAS)",   _f(hy_spread, ".0f", " bps"),
                   not math.isnan(hy_spread) and hy_spread > ALERT_THRESHOLDS["hy_spread_bps"])}
      {_metric_row("Monetary Regime",   regime.upper())}
      {_metric_row("Inversion Episodes (10Y-2Y, since 1990)", str(inversions_count))}
    </table>
  </td></tr>

  <!-- Alert status -->
  <tr><td style="background:#ffffff;padding:0 28px 20px;">
    <p style="margin:0 0 10px;font-size:13px;font-weight:bold;color:#333;">
      ALERT STATUS</p>
    <table cellpadding="0" cellspacing="4">
      {alert_rows}
    </table>
    {"<p style='margin:10px 0 0;font-size:12px;color:#c0392b;font-weight:bold;'>"
     "⚠ One or more alert thresholds were breached — an urgent alert was also sent.</p>"
     if any_alert else ""}
  </td></tr>

  <!-- Dashboard image -->
  <tr><td style="background:#0d1117;padding:20px 28px;">
    <p style="margin:0 0 12px;font-size:11px;color:#8b949e;letter-spacing:1px;">
      DASHBOARD — MAIN VIEW</p>
    <img src="cid:yield_curve_dashboard" width="564"
         style="display:block;border-radius:6px;" alt="Yield Curve Dashboard" />
  </td></tr>

  <!-- Footer -->
  <tr>
    <td style="background:#f0ede8;padding:14px 28px;text-align:center;">
      <p style="margin:0;font-size:10px;color:#999;">
        Source: FRED / St. Louis Fed · Model: Estrella &amp; Mishkin (1998) / NY Fed ·
        Recessions: NBER<br>
        Thresholds: inversion | P &gt; {ALERT_THRESHOLDS['prob_uni_pct']:.0f}% |
        HY &gt; {ALERT_THRESHOLDS['hy_spread_bps']:.0f} bps |
        TP &lt; {ALERT_THRESHOLDS['term_premium_pp']:+.1f} pp
      </p>
    </td>
  </tr>
</table>

</body>
</html>
"""
    return html


def build_urgent_body(
    spread_10y2y: float,
    spread_10y3m: float,
    prob_uni:     float,
    prob_mv:      float,
    hy_spread:    float,
    term_premium: float,
    regime:       str,
    checks:       dict,
) -> str:
    """
    Build the compact urgent alert HTML body.
    Red header, triggered conditions listed first, then full metrics.
    Designed to be readable as a push notification summary.
    """
    def _f(v, fmt=".2f", sfx=""):
        return f"{v:{fmt}}{sfx}" if not math.isnan(v) else "n/a"

    today   = datetime.today().strftime("%B %d, %Y — %H:%M UTC")
    n_alerts = sum(1 for t, _ in checks.values() if t)

    triggered_rows   = ""
    untriggered_rows = ""
    for name, (triggered, msg) in checks.items():
        row = f'<tr><td style="padding:5px 14px;">{_badge(triggered, msg)}</td></tr>'
        if triggered:
            triggered_rows   += row
        else:
            untriggered_rows += row

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0ede8;font-family:Arial,sans-serif;">

<table width="620" cellpadding="0" cellspacing="0" align="center"
       style="margin:24px auto;border-radius:10px;overflow:hidden;
              box-shadow:0 2px 12px rgba(0,0,0,0.15);">

  <!-- Red alert header -->
  <tr>
    <td style="background:#c0392b;padding:22px 28px;color:#ffffff;">
      <p style="margin:0;font-size:11px;opacity:0.8;letter-spacing:1px;">
        US YIELD CURVE MONITOR — URGENT ALERT</p>
      <p style="margin:6px 0 0;font-size:22px;font-weight:bold;">
        🚨 {n_alerts} THRESHOLD{"S" if n_alerts > 1 else ""} BREACHED</p>
      <p style="margin:4px 0 0;font-size:12px;opacity:0.75;">{today}</p>
    </td>
  </tr>

  <!-- Triggered conditions (highlighted) -->
  <tr><td style="background:#ffffff;padding:20px 28px;">
    <p style="margin:0 0 12px;font-size:13px;font-weight:bold;color:#c0392b;">
      TRIGGERED CONDITIONS</p>
    <table cellpadding="0" cellspacing="6">
      {triggered_rows}
    </table>
  </td></tr>

  <!-- All conditions (context) -->
  <tr><td style="background:#ffffff;padding:0 28px 20px;">
    <p style="margin:0 0 10px;font-size:13px;font-weight:bold;color:#333;">
      ALL CONDITIONS</p>
    <table cellpadding="0" cellspacing="4">
      {triggered_rows}
      {untriggered_rows}
    </table>
  </td></tr>

  <!-- Snapshot metrics -->
  <tr><td style="background:#ffffff;padding:0 28px 20px;">
    <p style="margin:0 0 10px;font-size:13px;font-weight:bold;color:#333;">
      CURRENT VALUES</p>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;border-radius:6px;overflow:hidden;">
      {_metric_row("Spread 10Y-2Y",  _f(spread_10y2y, "+.2f", " pp"),
                   not math.isnan(spread_10y2y) and spread_10y2y < 0)}
      {_metric_row("Spread 10Y-3M",  _f(spread_10y3m, "+.2f", " pp"))}
      {_metric_row("P(recession) — univariate",
                   _f(prob_uni * 100 if not math.isnan(prob_uni) else float("nan"), ".1f", "%"),
                   not math.isnan(prob_uni) and prob_uni * 100 > ALERT_THRESHOLDS["prob_uni_pct"])}
      {_metric_row("P(recession) — multivariate",
                   _f(prob_mv * 100 if not math.isnan(prob_mv) else float("nan"), ".1f", "%"),
                   not math.isnan(prob_mv) and prob_mv * 100 > ALERT_THRESHOLDS["prob_mv_pct"])}
      {_metric_row("Term Premium 10Y", _f(term_premium, "+.2f", " pp"),
                   not math.isnan(term_premium) and term_premium < ALERT_THRESHOLDS["term_premium_pp"])}
      {_metric_row("HY Spread",        _f(hy_spread, ".0f", " bps"),
                   not math.isnan(hy_spread) and hy_spread > ALERT_THRESHOLDS["hy_spread_bps"])}
      {_metric_row("Monetary Regime",  regime.upper())}
    </table>
  </td></tr>

  <!-- Dashboard image -->
  <tr><td style="background:#0d1117;padding:20px 28px;">
    <p style="margin:0 0 12px;font-size:11px;color:#8b949e;letter-spacing:1px;">
      DASHBOARD — MAIN VIEW</p>
    <img src="cid:yield_curve_dashboard" width="564"
         style="display:block;border-radius:6px;" alt="Yield Curve Dashboard" />
  </td></tr>

  <!-- Footer -->
  <tr>
    <td style="background:#f0ede8;padding:14px 28px;text-align:center;">
      <p style="margin:0;font-size:10px;color:#999;">
        This alert was triggered automatically by the yield curve monitoring system.<br>
        Source: FRED / St. Louis Fed · Model: Estrella &amp; Mishkin (1998) / NY Fed
      </p>
    </td>
  </tr>
</table>

</body>
</html>
"""
    return html


# ──────────────────────────────────────────────────────────────────
# BLOCK 4 — PUBLIC SEND FUNCTIONS
#
# WHAT IT DOES: exposes two clean public functions that the main
# dashboard script calls after all computations complete.
#
#   send_daily_report()
#     Always sends. Subject line includes date and curve status.
#     Attaches yield_curve_dashboard.png inline.
#
#   send_urgent_alert()
#     Only sends if at least one threshold is breached.
#     Subject line leads with 🚨 for push notification visibility.
#     Attaches yield_curve_dashboard.png inline.
#     Returns False immediately (no SMTP call) if no alert triggered.
#
# Both functions accept the same set of current metric values and
# internally call evaluate_alerts() to compute the checks dict.
#
# DEPENDENCIES: BLOCKS 1, 2, 3
# USED IN: yield_curve_dashboard.py → main() step 15
# ──────────────────────────────────────────────────────────────────

def send_daily_report(
    spread_10y2y:     float,
    spread_10y3m:     float,
    prob_uni:         float,
    prob_mv:          float,
    hy_spread:        float,
    ig_spread:        float,
    term_premium:     float,
    fwd_5y5y:         float,
    regime:           str,
    inversions_count: int,
    png_paths:        list[str] | None = None,
) -> bool:
    """
    Send the full daily HTML report email.
    Always fires — independent of alert thresholds.
    """
    checks   = evaluate_alerts(spread_10y2y, spread_10y3m, prob_uni,
                                prob_mv, hy_spread, term_premium)
    inverted = not math.isnan(spread_10y2y) and spread_10y2y < 0
    status   = "INVERTED ⚠" if inverted else "Normal"
    date_str = datetime.today().strftime("%Y-%m-%d")
    subject  = f"Yield Curve Daily — {date_str} | {status}"

    html = build_daily_body(
        spread_10y2y, spread_10y3m, prob_uni, prob_mv,
        hy_spread, ig_spread, term_premium, fwd_5y5y,
        regime, inversions_count, checks,
    )
    pngs = png_paths or ["yield_curve_dashboard.png"]
    return _send_email(subject, html, pngs)


def send_urgent_alert(
    spread_10y2y: float,
    spread_10y3m: float,
    prob_uni:     float,
    prob_mv:      float,
    hy_spread:    float,
    ig_spread:    float,
    term_premium: float,
    regime:       str,
    png_paths:    list[str] | None = None,
) -> bool:
    """
    Send the urgent alert email — only if at least one threshold is breached.
    Returns False immediately without any SMTP connection if no alert fires.
    Subject line starts with 🚨 for push notification visibility.
    """
    checks = evaluate_alerts(spread_10y2y, spread_10y3m, prob_uni,
                              prob_mv, hy_spread, term_premium)

    if not should_send_alert(checks):
        print("  ✓ No alert thresholds breached — urgent email not sent.")
        return False

    n        = sum(1 for t, _ in checks.values() if t)
    date_str = datetime.today().strftime("%Y-%m-%d %H:%M")
    subject  = f"🚨 Yield Curve ALERT [{n} signal{'s' if n > 1 else ''}] — {date_str}"

    html = build_urgent_body(
        spread_10y2y, spread_10y3m, prob_uni, prob_mv,
        hy_spread, term_premium, regime, checks,
    )
    pngs = png_paths or ["yield_curve_dashboard.png"]
    return _send_email(subject, html, pngs)
