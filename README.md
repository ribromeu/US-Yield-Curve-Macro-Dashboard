# US Yield Curve Macro Dashboard

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow)
![GitHub Actions](https://img.shields.io/badge/CI-GitHub%20Actions-2088FF?logo=github-actions&logoColor=white)
![Tests](https://img.shields.io/badge/Tests-pytest-brightgreen?logo=pytest&logoColor=white)
![FRED API](https://img.shields.io/badge/Data-FRED%20API-red)
![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux-lightgrey)

A Python framework that monitors the US Treasury yield curve daily, estimates recession probability using the NY Fed probit model, and delivers automated email alerts via GitHub Actions.

Built as a personal quant project at the intersection of fixed-income research, macro analysis, and software engineering.

---

## What it does

Every time it runs, the system:

1. Pulls all 11 Treasury maturities (1M → 30Y) from the FRED API
2. Computes the 10Y-2Y and 10Y-3M spreads and detects inversion episodes
3. Re-estimates the NY Fed probit model parameters dynamically from the Fed's own published series
4. Calculates implied forward rates (1Y1Y, 2Y5Y, 5Y5Y), term premium decomposition (ACM model), corporate credit spreads (IG/HY OAS), and international yield differentials (US vs Bund, Gilt, JGB)
5. Runs a multivariate recession probability model with three predictors: yield curve slope, HY spread, and Fed Funds Rate
6. Generates three PNG dashboards and one interactive Plotly 3D surface
7. Sends two emails — a full daily report and an urgent alert if any threshold is breached
8. Persists all data to an Excel workbook with upsert logic (no duplicates across runs)

---

## Dashboards

**Main dashboard** — current yield curve vs three historical crisis snapshots, 10Y-2Y spread history with NBER recession bands, NY Fed recession probability, and a current metrics table.

<img width="2591" height="1770" alt="yield_curve_dashboard" src="https://github.com/user-attachments/assets/cad9adce-ac80-40c9-b5db-98b97162aeae" />


**Economic analysis** — implied forward rates, univariate vs multivariate probit comparison, and monetary regime classification with colored FFR background.

<img width="3125" height="1191" alt="yield_curve_economic" src="https://github.com/user-attachments/assets/ebd3301d-a87c-447f-b98c-0868832a1a0e" />


**Historical evolution** — key maturity rates over time, yield curve heatmap (monthly), static 3D surface (quarterly), and a snapshot history from every previous run.
<img width="2898" height="2059" alt="yield_curve_historical" src="https://github.com/user-attachments/assets/f6b85c0e-008c-408f-8f91-b03dd6c3b012" />


---

## Recession model

The framework uses two complementary probit models:

**Univariate (NY Fed):**
```
P(recession in 12m) = Φ(α + β · spread₁₀ᵧ₋₃ₘ)
```
Parameters are re-estimated each run from the NY Fed's own published Excel file rather than using static 1998 values. This keeps the model consistent with the Fed's current methodology without manual updates.

**Multivariate (Favara et al., 2016):**
```
P = Φ(−2.40 − 0.55·spread₁₀ᵧ₋₃ₘ + 0.08·HY_spread + 0.10·FFR)
```
Adds financial stress (HY OAS) and monetary policy level (FFR) as predictors. When both models agree above 30–50%, the signal is considered robust.

---

## Email alert system

Two email types, both HTML with inline dashboard images:

| Type | Trigger | Content |
|---|---|---|
| Daily report | Every run | Full metrics table + dashboard PNG |
| Urgent alert | Threshold breach | Red header + triggered conditions highlighted |

**Default thresholds:**
- Curve inverted (10Y-2Y < 0)
- P(recession) univariate or multivariate > 30%
- HY spread > 600 bps
- Term premium < −0.5 pp

All thresholds live in one dictionary at the top of `yield_curve_email.py` — adjustable without touching any other logic.

---

## Architecture

```
yield_curve_dashboard.py   — core framework (8 blocks)
yield_curve_email.py       — email system (4 blocks)
.github/
  workflows/
    yield_curve_daily.yml  — GitHub Actions (manual trigger)
```

The codebase is structured in numbered blocks, each with an inline comment header explaining what it does, why it was built that way, its dependencies, and where it feeds downstream:

```python
# ──────────────────────────────────────────────────────────────────
# BLOCK 3 — NY FED RECESSION PROBABILITY MODEL
#
# WHAT IT DOES: implements the probit model published by the Federal
# Reserve Bank of New York to estimate the probability of a US
# recession within the next 12 months.
#
# WHY THIS WAY: parameters are re-estimated dynamically from the
# Fed's own published series — not hardcoded 1998 values.
#
# DEPENDENCIES: BLOCK 2 (10y_3m spread)
# USED IN: BLOCK 5 (alert), BLOCK 6 (plots), main() steps 5-6
# ──────────────────────────────────────────────────────────────────
```

---

## Key design decisions

**Dynamic parameter estimation.** The standard approach hardcodes Estrella & Mishkin (1998) parameters. This framework re-estimates α and β each run via OLS in probit space on the NY Fed's own published probability series. When estimated β turns positive (economically implausible), the system automatically falls back to the 1998 values with a warning.

**DGS3MO fallback.** FRED publishes the 3-month T-Bill series (`DGS3MO`) with a 1-day lag. On the day of the run, the latest value is often NaN — which would blank out the entire probit panel. The framework detects this and fills missing values with `DTB3` (the daily T-Bill series that publishes same-day). Both series track the same instrument.

**Term premium interpretation.** A negative term premium (common during QE periods) means part of a yield curve inversion may be artificial — driven by excess demand for duration rather than genuine recession expectations. The dashboard decomposes the 10Y yield into term premium and rate expectation components using the ACM model so the inversion signal can be interpreted in context.

**No external email libraries.** The entire email system uses Python stdlib only (`smtplib`, `email`, `ssl`). Dashboard PNGs are embedded as inline CID images — they render directly in the email body on Gmail, Outlook, and Apple Mail without requiring the recipient to download attachments.

---

## Setup

**Requirements**
```bash
pip install pandas numpy matplotlib scipy requests python-dotenv openpyxl xlrd plotly
```

**Environment variables**
```
FRED_API_KEY    = your key from fred.stlouisfed.org
GMAIL_USER      = your.address@gmail.com
GMAIL_APP_PASS  = 16-char Gmail App Password (myaccount.google.com/apppasswords)
ALERT_TO        = recipient@example.com
```

For local runs, place these in a `.env` file. For GitHub Actions, add them as repository secrets under Settings → Secrets → Actions.

**Run locally**
```bash
python yield_curve_dashboard.py
```

**Run via GitHub Actions**
Go to Actions → Yield Curve Daily Monitor → Run workflow. The workflow is configured as manual-trigger only — no scheduled runs unless you add a cron expression to the `.yml`.

---

## Data sources and references

| Data | Source |
|---|---|
| Treasury yields (1M–30Y) | FRED / St. Louis Fed |
| Recession probabilities | NY Fed Capital Markets Research |
| Term premium (ACM model) | FRED `THREEFYTP10` |
| Corporate spreads (IG/HY) | BofA BAML via FRED |
| International yields | FRED (Bund, Gilt, JGB) |
| Recession dates | NBER |

| Model | Reference |
|---|---|
| Univariate probit | Estrella & Mishkin (1998) |
| Term premium decomposition | Adrian, Crump & Moench (2013) |
| Multivariate probit | Favara, Gilchrist, Lewis & Zakrajšek (2016) |
| Excess bond premium | Gilchrist & Zakrajšek (2012) |

---
## AI Attribution

This project was developed with the assistance of [Claude](https://www.anthropic.com/claude) (Anthropic). Claude was used to support code organization, improve code structure and readability, and enhance overall efficiency of the implementation.

---

## Background

I built this during my BS Economics program (minors in Finance and Data Analysis) as a way to apply fixed-income theory, econometric modeling, and software engineering to a real monitoring problem. The yield curve is one of the most empirically robust leading indicators in macroeconomics ( every US recession since 1955 was preceded by an inversion ) but the raw signal needs context: term premium decomposition, credit conditions, monetary regime, and international flows all affect how the same spread reading should be interpreted.

The goal was to build something rigorous enough to use as an actual analytical tool, not just a class project.
