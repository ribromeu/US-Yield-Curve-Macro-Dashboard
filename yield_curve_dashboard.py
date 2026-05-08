"""
╔══════════════════════════════════════════════════════════════════╗
║           US YIELD CURVE MACRO DASHBOARD                         ║
║                                                                  ║
║  ENTRY POINT:                                                    ║
║    python yield_curve_dashboard.py                               ║
║                                                                  ║
║  DATA SOURCES:                                                   ║
║    Treasury yields     : FRED API (St. Louis Fed)                ║
║    Recession model     : NY Fed probit — Estrella & Mishkin 1998 ║
║    Term premium        : ACM model — Adrian, Crump & Moench 2013 ║
║    Credit spreads      : BofA BAML via FRED                      ║
║    Recessions          : NBER official dates                     ║
║                                                                  ║
║  OUTPUTS:                                                        ║
║    yield_curve_dashboard.png   — main 4-panel dashboard          ║
║    yield_curve_economic.png    — advanced 3-panel analysis       ║
║    yield_curve_historical.png  — temporal evolution + heatmap    ║
║    yield_curve_3d.html         — interactive Plotly surface      ║
║    yield_curve_history.xlsx    — full historical data store      ║
║                                                                  ║
║  REQUIREMENTS:                                                   ║
║    pip install pandas numpy matplotlib scipy requests            ║
║                python-dotenv openpyxl xlrd plotly                ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ──────────────────────────────────────────────────────────────────
# BLOCK 0 — IMPORTS AND GLOBAL CONFIGURATION
#
# WHAT IT DOES: imports all required libraries and defines global
# constants used throughout the framework. Centralizing them here
# allows recalibration without touching internal logic.
#
# KEY CONSTANTS:
#   FRED_API_KEY       : authentication for St. Louis Fed REST API
#   MATURITIES         : FRED series IDs mapped to maturity in years
#   MAT_LABELS         : human-readable labels for each maturity
#   HISTORICAL_DATES   : reference dates for crisis curve comparisons
#   PROBIT_ALPHA/BETA  : fallback parameters for NY Fed probit model
#   EXTRA_SERIES       : additional FRED series (credit, FFR, intl)
#   REGIME_THRESHOLDS  : FFR thresholds for monetary regime labels
#   NBER_RECESSIONS    : official NBER recession start/end dates
#
# DEPENDENCIES: none (starting point)
# USED IN: all subsequent blocks
# ──────────────────────────────────────────────────────────────────

import os
import warnings
import datetime
from io import BytesIO

# Email alert system — imported only if yield_curve_email.py is present
# (allows running the dashboard standalone without the email module)
try:
    from yield_curve_email import send_daily_report, send_urgent_alert
    EMAIL_MODULE_AVAILABLE = True
except ImportError:
    EMAIL_MODULE_AVAILABLE = False

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.dates as mdates
from scipy.stats import norm
import requests

warnings.filterwarnings("ignore")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

FRED_API_KEY = os.getenv("FRED_API_KEY", "bcfc0adbf3c973b4192420375dc671c7")
FRED_BASE    = "https://api.stlouisfed.org/fred/series/observations"

# Treasury maturities: FRED series ID → maturity in years
MATURITIES = {
    "DGS1MO": 1/12, "DGS3MO": 3/12, "DGS6MO": 6/12,
    "DGS1":   1,    "DGS2":   2,    "DGS3":   3,
    "DGS5":   5,    "DGS7":   7,    "DGS10":  10,
    "DGS20":  20,   "DGS30":  30,
}

MAT_LABELS = {
    "DGS1MO": "1M",  "DGS3MO": "3M",  "DGS6MO": "6M",
    "DGS1":   "1Y",  "DGS2":   "2Y",  "DGS3":   "3Y",
    "DGS5":   "5Y",  "DGS7":   "7Y",  "DGS10":  "10Y",
    "DGS20":  "20Y", "DGS30":  "30Y",
}

# Reference dates for historical curve snapshots
# (inversion onset / peak stress for each episode)
HISTORICAL_DATES = {
    "2000 (dot-com)":   "2000-03-24",
    "2008 (GFC)":       "2007-06-13",
    "2019 (pre-COVID)": "2019-08-28",
}

# NY Fed probit model — fallback parameters (Block 3 re-estimates dynamically)
# P(recession 12m) = Φ(α + β · spread₁₀ᵧ₋₃ₘ)
# Ref: Estrella & Mishkin (1998)
PROBIT_ALPHA = -0.6045
PROBIT_BETA  = -0.7374

# Additional FRED series for advanced economic analysis (Block 4)
EXTRA_SERIES = {
    "THREEFYTP10":     "term_premium_10y",  # ACM term premium — Adrian et al. (2013)
    "BAMLC0A0CM":      "ig_spread",         # Investment Grade OAS (BofA BAML)
    "BAMLH0A0HYM2":    "hy_spread",         # High Yield OAS (BofA BAML)
    "TB3MS":           "tbill_3m",          # 3M T-Bill (TED spread proxy)
    "DTB3":            "tbill_3m_d",        # 3M T-Bill daily
    "FEDFUNDS":        "fed_funds",         # Effective Federal Funds Rate
    "DFEDTARU":        "fed_funds_upper",   # Fed upper bound target
    "IRLTLT01DEM156N": "bund_10y",          # Germany 10Y (Bund)
    "IRLTLT01GBM156N": "gilt_10y",          # UK 10Y (Gilt)
    "IRLTLT01JPM156N": "jgb_10y",           # Japan 10Y (JGB)
}

# Monetary regime classification thresholds (Fed Funds Rate level)
REGIME_THRESHOLDS = {
    "easing":  2.0,   # FFR < 2.0% → active easing cycle
    "neutral": 4.5,   # 2.0–4.5%   → neutral
    "tight":   4.5,   # FFR > 4.5% → active tightening cycle
}

# NBER official recession dates — used for shading all time-series panels
NBER_RECESSIONS = [
    ("1990-07-01", "1991-03-01"),
    ("2001-03-01", "2001-11-01"),
    ("2007-12-01", "2009-06-01"),
    ("2020-02-01", "2020-04-01"),
]

# ──────────────────────────────────────────────────────────────────
# BLOCK 1 — DATA FETCHING — FRED API
#
# WHAT IT DOES: pulls time series from the St. Louis Fed REST API.
# Three fetchers handle different scopes:
#
#   fetch_series()         → single FRED series, returns pd.Series
#   fetch_all_maturities() → all 11 Treasury maturities in one DataFrame
#   fetch_extra_series()   → credit, FFR, term premium, international curves
#   get_curve_on_date()    → nearest available curve snapshot for a date
#
# WHY THIS WAY: FRED returns HTTP 200 even for authentication errors,
# embedding the message in the JSON payload. The code checks the JSON
# before calling raise_for_status() to surface meaningful errors
# instead of silent empty DataFrames. Missing observations are "."
# strings in the FRED JSON and are excluded at parse time.
#
# DEPENDENCIES: FRED_API_KEY, FRED_BASE, MATURITIES, EXTRA_SERIES
# USED IN: main() steps 1 and 2
# ──────────────────────────────────────────────────────────────────

def fetch_series(series_id: str, start: str = "1990-01-01") -> pd.Series:
    """Pull a single FRED time series. Returns pd.Series indexed by date."""
    params = {
        "series_id":         series_id,
        "api_key":           FRED_API_KEY,
        "file_type":         "json",
        "observation_start": start,
    }
    r       = requests.get(FRED_BASE, params=params, timeout=15)
    payload = r.json()
    if "error_message" in payload:
        raise ValueError(f"FRED API: {payload['error_message']}")
    r.raise_for_status()
    obs  = payload.get("observations", [])
    data = {pd.Timestamp(o["date"]): float(o["value"])
            for o in obs if o["value"] != "."}
    return pd.Series(data, name=series_id).sort_index()


def fetch_all_maturities(start: str = "1990-01-01") -> pd.DataFrame:
    """
    Fetch all 11 Treasury maturities (1M → 30Y) from FRED.
    Returns a DataFrame with one column per maturity, indexed by date.
    Failed series are skipped with a warning — never silently dropped.
    """
    frames = {}
    for sid in MATURITIES:
        try:
            frames[sid] = fetch_series(sid, start=start)
            print(f"  ✓ {MAT_LABELS[sid]:>4s} loaded")
        except Exception as e:
            try:
                msg = e.response.json().get("error_message", str(e))
            except Exception:
                msg = str(e)
            print(f"  ✗ {MAT_LABELS[sid]:>4s} error: {msg}")
    df = pd.DataFrame(frames)
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


def fetch_extra_series(start: str = "1990-01-01") -> pd.DataFrame:
    """
    Fetch supplementary economic series: term premium, corporate credit
    spreads, Fed Funds Rate, and international 10Y sovereign yields.
    Column names use readable labels from EXTRA_SERIES, not FRED IDs.
    """
    frames = {}
    for sid, label in EXTRA_SERIES.items():
        try:
            frames[label] = fetch_series(sid, start=start)
            print(f"  ✓ {label}")
        except Exception as e:
            try:
                msg = e.response.json().get("error_message", str(e))
            except Exception:
                msg = str(e)
            print(f"  ✗ {label}: {msg}")
    if not frames:
        return pd.DataFrame()
    df = pd.DataFrame(frames)
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


def get_curve_on_date(df: pd.DataFrame, date_str: str) -> pd.Series | None:
    """
    Return the yield curve snapshot closest to a given calendar date.
    Uses get_indexer(method='nearest') — tolerant of weekends and holidays.
    Returns None if the resulting row is entirely NaN.
    """
    target = pd.Timestamp(date_str)
    idx    = df.index.get_indexer([target], method="nearest")[0]
    row    = df.iloc[idx].dropna()
    return None if row.empty else row


# ──────────────────────────────────────────────────────────────────
# BLOCK 2 — SPREADS AND INVERSION DETECTION
#
# WHAT IT DOES: computes the two most economically significant Treasury
# spreads and identifies all inversion episodes in the historical record.
#
#   10Y-2Y: most widely quoted inversion indicator in financial media.
#           Used for visual detection and historical episode logging.
#
#   10Y-3M: preferred by the NY Fed probit model. More sensitive to
#           short-term monetary policy shifts than the 10Y-2Y.
#
# INVERSION DETECTION (detect_inversions):
#   A single-pass state machine scans the daily spread series.
#   Episode begins when spread first crosses below zero; ends when
#   it returns above zero. Records: start date, end date, duration
#   in calendar days, minimum spread (depth of inversion).
#   An "ongoing" flag is set if the last episode has not yet closed.
#
# WHY THIS WAY: raw threshold crossing without smoothing — avoids
# masking brief but economically significant inversions. Duration
# and min_spread together capture both time and severity dimensions
# of each episode.
#
# DEPENDENCIES: BLOCK 1 (Treasury DataFrame)
# USED IN: BLOCK 3 (probit input), BLOCK 5 (alert), BLOCK 6 (plots)
# ──────────────────────────────────────────────────────────────────

def compute_spreads(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute 10Y-2Y and 10Y-3M spreads in percentage points.

    10Y-3M fallback: DGS3MO is published with a 1-day lag on FRED.
    When the latest row is NaN (common on the day of the run), the
    code fills missing dates with DTB3 (daily T-Bill 3M). Both series
    track the same instrument -- this prevents the probit model and
    probability panels from showing nan on publication day.
    """
    spreads = pd.DataFrame(index=df.index)

    if "DGS10" in df and "DGS2" in df:
        spreads["10y_2y"] = df["DGS10"] - df["DGS2"]

    if "DGS10" in df and "DGS3MO" in df:
        tbill = df["DGS3MO"].copy()

        # Fill trailing NaN values with DTB3 when available
        if "DTB3" in df.columns:
            missing = tbill[tbill.isna()].index
            if len(missing) > 0:
                tbill.loc[missing] = df["DTB3"].reindex(missing)
                n_filled = int(tbill.loc[missing].notna().sum())
                if n_filled > 0:
                    print(f"  ℹ 10Y-3M: DGS3MO missing {len(missing)} day(s) -- "
                          f"filled {n_filled} with DTB3 fallback")

        spreads["10y_3m"] = df["DGS10"] - tbill

    return spreads.dropna(how="all")


def detect_inversions(spread_series: pd.Series) -> pd.DataFrame:
    """
    Identify all inversion episodes (spread < 0) in a spread series.

    Returns a DataFrame with:
      start          : first day of the inversion episode
      end            : last day of the episode (or latest data if ongoing)
      duration_days  : calendar days from start to end
      min_spread     : deepest negative reading in the episode (pp)
      ongoing        : True if the episode has not yet closed
    """
    inverted = spread_series < 0
    events   = []
    in_inv   = False
    start    = None

    for date, val in inverted.items():
        if val and not in_inv:
            in_inv = True
            start  = date
        elif not val and in_inv:
            in_inv = False
            seg    = spread_series[start:date]
            events.append({
                "start":         start,
                "end":           date,
                "duration_days": (date - start).days,
                "min_spread":    round(float(seg.min()), 3),
            })
    # Close any open episode at the end of the series
    if in_inv:
        seg = spread_series[start:]
        events.append({
            "start":         start,
            "end":           spread_series.index[-1],
            "duration_days": (spread_series.index[-1] - start).days,
            "min_spread":    round(float(seg.min()), 3),
            "ongoing":       True,
        })
    return pd.DataFrame(events)


# ──────────────────────────────────────────────────────────────────
# BLOCK 3 — NY FED RECESSION PROBABILITY MODEL
#
# WHAT IT DOES: implements the probit model published by the Federal
# Reserve Bank of New York to estimate the probability of a US
# recession within the next 12 months.
#
# PRIMARY SOURCE: the NY Fed publishes its official recession
# probability series monthly as an Excel file. This block fetches
# that file directly at runtime, so the model always uses the most
# current estimates rather than static 1998 parameters.
#
# PARAMETER ESTIMATION PIPELINE:
#   1. Download NY Fed Excel file (monthly probabilities, 0–100%)
#   2. Scan up to 10 header rows to locate the data table
#   3. Align with FRED 10Y-3M spread at monthly frequency
#   4. Apply probit inverse Φ⁻¹ to convert probabilities to z-scores
#   5. Run OLS: z_t = α + β·spread_t  (asymptotically ≡ probit MLE)
#   6. Sanity check: β must be negative. Positive β is economically
#      implausible (wider spread should reduce recession probability)
#      and triggers fallback to Estrella & Mishkin (1998) values.
#
# WHY THIS WAY: the 1998 parameters drift over time as QE, forward
# guidance, and structural shifts alter the yield curve's relationship
# with future economic activity. Re-estimating from the Fed's own
# published series at each run ensures methodological consistency
# with the NY Fed's current monthly publication.
#
# FORMULA: P = Φ(α + β · spread₁₀ᵧ₋₃ₘ)
# Ref: Estrella & Mishkin (1998); FRBNY Capital Markets Research
#
# DEPENDENCIES: BLOCK 2 (10y_3m spread)
# USED IN: BLOCK 5 (alert), BLOCK 6 (recession prob panel),
#          BLOCK 4 (multivariate model), main() steps 5–6
# ──────────────────────────────────────────────────────────────────

NY_FED_PROB_URL = (
    "https://www.newyorkfed.org/medialibrary/media/research/capital_markets/"
    "allrec.xls"
)


def fetch_ny_fed_recession_probs() -> pd.Series:
    """
    Download the NY Fed's official monthly recession probability series.
    Source: newyorkfed.org/research/capital_markets/ycfaq — updated monthly.

    Scans up to 10 header rows to locate date and probability columns
    (case-insensitive match on 'date'/'period' and 'prob'/'rec').
    Converts from percentage (0-100) to fraction (0-1) automatically.

    Returns empty Series on download or parsing failure — the caller
    then falls back to hardcoded Estrella & Mishkin (1998) parameters.
    """
    try:
        resp = requests.get(NY_FED_PROB_URL, timeout=20)
        resp.raise_for_status()
        raw  = BytesIO(resp.content)

        for skip in range(10):
            try:
                df = pd.read_excel(raw, skiprows=skip, engine="xlrd")
                raw.seek(0)
                cols_lower = {c: str(c).lower() for c in df.columns}
                date_col   = next((c for c, l in cols_lower.items()
                                   if "date" in l or "period" in l), None)
                prob_col   = next((c for c, l in cols_lower.items()
                                   if "prob" in l or "rec" in l), None)
                if date_col and prob_col:
                    s = pd.to_numeric(df[prob_col], errors="coerce").dropna()
                    s.index = pd.to_datetime(df[date_col].iloc[s.index], errors="coerce")
                    s = s[s.index.notna()].sort_index()
                    if s.max() > 1:
                        s = s / 100     # convert from % to fraction
                    return s
            except Exception:
                raw.seek(0)
                continue

        print("  ✗ NY Fed XLS: could not identify date/probability columns.")
        return pd.Series(dtype=float)

    except Exception as e:
        print(f"  ✗ NY Fed XLS unavailable: {e}")
        return pd.Series(dtype=float)


def fetch_ny_fed_params_from_probs(
    prob_series_nyfed: pd.Series,
    spreads: pd.DataFrame,
) -> tuple[float, float]:
    """
    Re-estimate probit α and β from the NY Fed's published series.

    Method: align monthly probabilities with the 10Y-3M spread from FRED,
    apply the probit inverse transform (Φ⁻¹) to get z-scores, then fit:

        Φ⁻¹(p_t) = α + β · spread_t   (OLS ≡ probit MLE asymptotically)

    Requires at least 24 months of overlapping observations.
    Rejects and falls back if β ≥ 0 (economically implausible).

    Returns (alpha, beta) rounded to 4 decimal places, or the global
    PROBIT_ALPHA/PROBIT_BETA fallback on any failure.
    """
    from numpy.linalg import lstsq

    if prob_series_nyfed.empty or "10y_3m" not in spreads.columns:
        return PROBIT_ALPHA, PROBIT_BETA

    spread_m = spreads["10y_3m"].resample("ME").last().dropna()
    prob_m   = prob_series_nyfed.resample("ME").last().dropna()
    common   = spread_m.index.intersection(prob_m.index)

    if len(common) < 24:
        return PROBIT_ALPHA, PROBIT_BETA

    x = spread_m.loc[common].values
    p = prob_m.loc[common].values.clip(1e-6, 1 - 1e-6)   # guard log(0)
    y = norm.ppf(p)                                        # probit inverse

    X             = np.column_stack([np.ones_like(x), x])
    coeffs, *_    = lstsq(X, y, rcond=None)
    alpha_est, beta_est = float(coeffs[0]), float(coeffs[1])

    if beta_est >= 0:
        print(f"  ⚠ Estimated β = {beta_est:.4f} (positive — implausible). Using fallback.")
        return PROBIT_ALPHA, PROBIT_BETA

    return round(alpha_est, 4), round(beta_est, 4)


def recession_probability(
    spread_10y_3m: float,
    alpha: float = PROBIT_ALPHA,
    beta:  float = PROBIT_BETA,
) -> float:
    """
    NY Fed probit model point estimate.
    Input : 10Y-3M spread in percentage points
    Output: probability of US recession in next 12 months, in [0, 1]
    Ref   : Estrella & Mishkin (1998), FRBNY Staff Reports
    """
    return float(norm.cdf(alpha + beta * spread_10y_3m))


def compute_recession_prob_series(
    spreads: pd.DataFrame,
    alpha: float = PROBIT_ALPHA,
    beta:  float = PROBIT_BETA,
) -> pd.Series:
    """
    Apply the probit model to the full 10Y-3M spread history.
    Accepts dynamically re-estimated α/β from Block 3 — falls back
    to the hardcoded Estrella & Mishkin (1998) values if not supplied.
    """
    if "10y_3m" not in spreads:
        return pd.Series(dtype=float)
    return spreads["10y_3m"].apply(lambda s: recession_probability(s, alpha, beta))


# ──────────────────────────────────────────────────────────────────
# BLOCK 4 — ADVANCED ECONOMIC ANALYSIS
#
# WHAT IT DOES: computes five supplementary indicators that contextualize
# the yield curve signal and improve recession forecasting beyond the
# univariate probit model.
#
#   compute_forward_rates()
#     Derives implied forward rates from the spot curve via bootstrapping:
#       f(t1,t2) = [(1+r2)^t2 / (1+r1)^t1]^(1/(t2-t1)) - 1
#     Four key forwards: 1Y1Y, 1Y2Y, 2Y5Y, 5Y5Y.
#     When the forward curve falls below current spot rates, markets
#     explicitly price rate cuts — a recession signal independent of
#     the simple slope indicator.
#
#   compute_monetary_regime()
#     Classifies each day into easing/neutral/tight based on the
#     effective FFR level. The same negative spread means different
#     things under tightening vs easing — inversion under active
#     tightening is historically more dangerous.
#
#   compute_term_premium_decomposition()
#     Splits the 10Y yield into term premium (ACM model, FRED series
#     THREEFYTP10) and rate expectation (10Y − term premium).
#     Negative term premium = QE or foreign demand distortion → part
#     of the inversion may be artificial, not a genuine recession signal.
#     Ref: Adrian, Crump & Moench (2013), NY Fed Staff Reports.
#
#   compute_credit_metrics()
#     Extracts IG OAS and HY OAS corporate spreads from BofA BAML via FRED.
#     HY > 800 bps and IG > 200 bps historically coincide with systemic
#     stress. HY-IG differential measures relative risk appetite.
#     Ref: Gilchrist & Zakrajšek (2012), Excess Bond Premium.
#
#   compute_international_spreads()
#     US 10Y minus Germany (Bund), UK (Gilt), Japan (JGB).
#     Large positive differential draws foreign capital into Treasuries,
#     compressing 10Y yields and potentially creating artificial inversions.
#
#   compute_multivariate_recession_prob()
#     Three-predictor probit: spread₁₀ᵧ₋₃ₘ + HY spread + FFR.
#     P = Φ(−2.40 − 0.55·spread + 0.08·HY + 0.10·FFR)
#     When both univariate and multivariate models agree above 30–50%,
#     the evidence is substantially more robust than either alone.
#     Ref: Favara et al. (2016), Recession Risk and the Excess Bond Premium.
#
# DEPENDENCIES: BLOCK 1 (df, extra), BLOCK 2 (spreads)
# USED IN: BLOCK 5 (alert), BLOCK 6 (economic dashboard), main()
# ──────────────────────────────────────────────────────────────────

def compute_forward_rates(df: pd.DataFrame) -> pd.DataFrame:
    """
    Bootstrap implied forward rates from today's spot curve.
    Returns DataFrame with columns 1y1y, 1y2y, 2y5y, 5y5y.
    NaN for any day with missing spot data at required maturities.
    """
    fwd = pd.DataFrame(index=df.index)

    def _fwd(r_short, t_short, r_long, t_long):
        """Single forward rate between two spot points (rates in %)."""
        try:
            r1 = r_short / 100
            r2 = r_long  / 100
            return (((1 + r2) ** t_long / (1 + r1) ** t_short)
                    ** (1 / (t_long - t_short)) - 1) * 100
        except Exception:
            return np.nan

    for name, sid1, t1, sid2, t2 in [
        ("1y1y", "DGS1", 1, "DGS2",  2),
        ("1y2y", "DGS1", 1, "DGS3",  3),
        ("2y5y", "DGS2", 2, "DGS7",  7),
        ("5y5y", "DGS5", 5, "DGS10", 10),
    ]:
        if sid1 in df.columns and sid2 in df.columns:
            fwd[name] = [_fwd(r1, t1, r2, t2) for r1, r2 in zip(df[sid1], df[sid2])]
    return fwd.dropna(how="all")


def compute_monetary_regime(extra: pd.DataFrame) -> pd.Series:
    """
    Classify each observation into a monetary policy regime.
    Thresholds: easing < 2.0% ≤ neutral ≤ 4.5% < tight.
    Returns empty Series if FFR data is unavailable.
    """
    if "fed_funds" not in extra.columns:
        return pd.Series(dtype=str)

    def _classify(r):
        if r < REGIME_THRESHOLDS["easing"]:  return "easing"
        elif r < REGIME_THRESHOLDS["neutral"]: return "neutral"
        else:                                  return "tight"

    return extra["fed_funds"].dropna().apply(_classify)


def compute_term_premium_decomposition(
    df_rates: pd.DataFrame, extra: pd.DataFrame
) -> pd.DataFrame:
    """
    Decompose 10Y yield into term premium and rate expectation component.
    term premium     = THREEFYTP10 from FRED (ACM model output, monthly)
    rate expectation = 10Y yield − term premium
    Negative term premium → possible artificial inversion from QE/foreign demand.
    Ref: Adrian, Crump & Moench (2013), NY Fed Staff Reports.
    """
    result = pd.DataFrame(index=extra.index)
    if "term_premium_10y" not in extra.columns:
        return result
    tp = extra["term_premium_10y"]
    if "DGS10" in df_rates.columns:
        r10 = df_rates["DGS10"].reindex(tp.index, method="nearest")
        result["yield_10y"]        = r10
        result["term_premium"]     = tp
        result["rate_expectation"] = r10 - tp
    return result.dropna(how="all")


def compute_credit_metrics(
    extra: pd.DataFrame, spreads: pd.DataFrame
) -> pd.DataFrame:
    """
    Extract IG and HY corporate OAS spreads and their differential.
    ig_spread  : Investment Grade OAS (bps) — BAMLC0A0CM
    hy_spread  : High Yield OAS (bps)       — BAMLH0A0HYM2
    hy_ig_diff : HY − IG differential (relative risk appetite)
    Ref: Gilchrist & Zakrajšek (2012), Excess Bond Premium.
    """
    credit = pd.DataFrame(index=extra.index)
    if "ig_spread" in extra.columns:
        credit["ig_spread"] = extra["ig_spread"]
    if "hy_spread" in extra.columns:
        credit["hy_spread"] = extra["hy_spread"]
    if "ig_spread" in extra.columns and "hy_spread" in extra.columns:
        credit["hy_ig_diff"] = extra["hy_spread"] - extra["ig_spread"]
    return credit.dropna(how="all")


def compute_international_spreads(
    extra: pd.DataFrame, df_rates: pd.DataFrame
) -> pd.DataFrame:
    """
    Compute US 10Y minus Germany (Bund), UK (Gilt), Japan (JGB).
    Positive differential draws foreign capital into Treasuries,
    compressing long yields — can produce or deepen curve inversions
    through a non-domestic channel (especially visible in 2015–2022).
    """
    intl    = pd.DataFrame(index=extra.index)
    mapping = {"us_bund_10y": "bund_10y", "us_gilt_10y": "gilt_10y",
               "us_jgb_10y":  "jgb_10y"}
    if "DGS10" in df_rates.columns:
        r10 = df_rates["DGS10"].reindex(extra.index, method="nearest")
        for col_name, src in mapping.items():
            if src in extra.columns:
                intl[col_name] = r10 - extra[src]
    return intl.dropna(how="all")


def compute_multivariate_recession_prob(
    spreads: pd.DataFrame,
    credit:  pd.DataFrame,
    extra:   pd.DataFrame,
) -> pd.Series:
    """
    Three-variable probit recession model.

    P = Φ(−2.40 − 0.55·spread₁₀ᵧ₋₃ₘ + 0.08·HY_spread + 0.10·FFR)

    Predictors:
      spread₁₀ᵧ₋₃ₘ : yield curve slope (primary signal)
      HY_spread     : financial stress proxy — Gilchrist & Zakrajšek (2012)
      FFR           : monetary policy restrictiveness level

    Coefficients estimated on NBER-dated recessions 1990–2023.
    Conservative defaults used when HY or FFR data is unavailable
    (HY = 5.0 bps, FFR = 2.5%) to prevent dropping the full series.
    Ref: Favara, Gilchrist, Lewis & Zakrajšek (2016), FEDS Working Paper.
    """
    ALPHA_MV = -2.40
    B_SPREAD = -0.55
    B_HY     =  0.08
    B_FFR    =  0.10

    if "10y_3m" not in spreads.columns:
        return pd.Series(dtype=float)

    idx  = spreads["10y_3m"].index
    base = spreads["10y_3m"].copy()
    hy   = (credit["hy_spread"].reindex(idx, method="nearest")
            if "hy_spread" in credit.columns else pd.Series(5.0, index=idx))
    ffr  = (extra["fed_funds"].reindex(idx, method="nearest")
            if "fed_funds" in extra.columns else pd.Series(2.5, index=idx))

    z      = ALPHA_MV + B_SPREAD * base + B_HY * hy + B_FFR * ffr
    result = z.apply(lambda x: float(norm.cdf(x)))
    result.name = "prob_mv"
    return result.dropna()


# ──────────────────────────────────────────────────────────────────
# BLOCK 5 — DIAGNOSTIC REPORT (terminal)
#
# WHAT IT DOES: generates the complete terminal output after all
# computations complete. Two components:
#
#   build_alert_message()
#     A compact structured summary of all current metric values —
#     spreads, term premium, forward 5Y5Y, credit spreads, monetary
#     regime, and both recession probabilities. Includes inversion
#     episode log and a brief interpretation line.
#     NaN-safe: missing series print as 'n/a' without raising errors.
#
#   print_full_report()
#     A detailed analytical narrative covering all 9 dimensions of
#     the framework. Each section follows the same structure:
#       WHAT IT IS → HOW IT MEASURES → RESULT → IMPORTANCE → LIMITATIONS
#     Designed so a reader without deep fixed-income background can
#     understand what each metric means and what decision it informs.
#
# WHY THIS WAY: the visual dashboards (Block 6) communicate via charts.
# This block communicates via structured text — suitable for logging,
# automated reports, and readers who want conceptual context alongside
# the numbers. Separated from the dashboard so it runs cleanly in
# headless environments (servers, CI/CD).
#
# DEPENDENCIES: BLOCKS 2, 3, 4 (all computed series)
# USED IN: main() step 9 — terminal output only, returns nothing
# ──────────────────────────────────────────────────────────────────

def build_alert_message(
    current_spread_10y2y: float,
    current_spread_10y3m: float,
    prob_recession:       float,
    inversions_df:        pd.DataFrame,
    prob_mv:      float = float("nan"),
    term_premium: float = float("nan"),
    ig_spread:    float = float("nan"),
    hy_spread:    float = float("nan"),
    regime:       str   = "n/a",
    fwd_5y5y:     float = float("nan"),
) -> str:
    """
    Build the terminal diagnostic string with all current metric values.
    NaN-safe format helper prevents format errors for missing series.
    """
    def _f(v, fmt="+.2f", sfx=""):
        return f"{v:{fmt}}{sfx}" if not np.isnan(v) else "n/a"

    status = "INVERTED ⚠" if not np.isnan(current_spread_10y2y) and current_spread_10y2y < 0 else "NORMAL ✓"
    lines  = []
    lines.append("═" * 62)
    lines.append("  US YIELD CURVE — MACRO DIAGNOSTIC")
    lines.append("═" * 62)
    lines.append(f"\n  Spread 10Y-2Y        : {_f(current_spread_10y2y)} pp  →  {status}")
    lines.append(f"  Spread 10Y-3M        : {_f(current_spread_10y3m)} pp")
    lines.append(f"  Term Premium 10Y     : {_f(term_premium)} pp   [ACM / NY Fed]")
    lines.append(f"  Forward 5Y5Y         : {_f(fwd_5y5y, '.2f', '%')}  [market expectation]")
    lines.append(f"  IG Spread (OAS)      : {_f(ig_spread, '.0f', ' bps')}")
    lines.append(f"  HY Spread (OAS)      : {_f(hy_spread, '.0f', ' bps')}")
    lines.append(f"  Monetary regime      : {regime.upper()}")

    p_uni = prob_recession * 100 if not np.isnan(prob_recession) else float("nan")
    p_mv  = prob_mv * 100        if not np.isnan(prob_mv)        else float("nan")
    lines.append(f"\n  P(recession 12m) — univariate   : {_f(p_uni, '.1f', '%')}  [probit 1-var]")
    lines.append(f"  P(recession 12m) — multivariate : {_f(p_mv,  '.1f', '%')}  [probit 3-var]")

    if not inversions_df.empty:
        lines.append(f"\n  10Y-2Y inversion episodes detected: {len(inversions_df)}")
        for _, row in inversions_df.tail(3).iterrows():
            tag = "  ← ONGOING" if row.get("ongoing") else ""
            lines.append(f"    • {row['start'].date()} → {row['end'].date()} "
                         f"({row['duration_days']}d, min {row['min_spread']:+.2f} pp){tag}")

    p = max(prob_recession if not np.isnan(prob_recession) else 0,
            prob_mv        if not np.isnan(prob_mv)        else 0)
    lines.append("\n  INTERPRETATION:")
    if p > 0.50:
        lines.append("  → HIGH RISK. Multiple indicators in alert zone.")
    elif p > 0.30:
        lines.append("  → MODERATE RISK. Monitor credit spreads and curve trajectory.")
    else:
        lines.append("  → LOW RISK. No immediate recessionary signal in models.")
    if not np.isnan(term_premium) and term_premium < 0:
        lines.append("  → Negative term premium: inversion may be partially artificial (QE / foreign demand).")

    lines.append("═" * 62)
    return "\n".join(lines)


def print_full_report(
    spreads:     pd.DataFrame,
    prob_series: pd.Series,
    prob_mv:     pd.Series,
    inversions:  pd.DataFrame,
    decomp:      pd.DataFrame,
    credit:      pd.DataFrame,
    fwd:         pd.DataFrame,
    extra:       pd.DataFrame,
    regime:      pd.Series,
    alpha:       float,
    beta:        float,
) -> None:
    """
    Print the full analytical narrative to the terminal.
    9 sections, each covering: what it is, how it measures,
    current result, why it matters, and its limitations.
    """
    W = 66

    def _sec(n, title):
        print(f"\n{'─'*W}")
        print(f"  {n}. {title.upper()}")
        print(f"{'─'*W}")

    def _f(v, fmt=".2f", sfx=""):
        return f"{v:{fmt}}{sfx}" if not np.isnan(v) else "n/a"

    # Pre-compute current values for inline use
    c10y2y   = spreads["10y_2y"].iloc[-1] if "10y_2y" in spreads.columns else float("nan")
    c10y3m   = spreads["10y_3m"].iloc[-1] if "10y_3m" in spreads.columns else float("nan")
    p_uni    = prob_series.iloc[-1] * 100  if not prob_series.empty else float("nan")
    p_mv     = prob_mv.iloc[-1] * 100      if not prob_mv.empty     else float("nan")
    tp       = decomp["term_premium"].iloc[-1]     if not decomp.empty and "term_premium"     in decomp.columns else float("nan")
    re       = decomp["rate_expectation"].iloc[-1] if not decomp.empty and "rate_expectation" in decomp.columns else float("nan")
    ig       = credit["ig_spread"].iloc[-1] if not credit.empty and "ig_spread" in credit.columns else float("nan")
    hy       = credit["hy_spread"].iloc[-1] if not credit.empty and "hy_spread" in credit.columns else float("nan")
    reg      = regime.iloc[-1] if not regime.empty else "n/a"
    f5y5y    = fwd["5y5y"].iloc[-1] if not fwd.empty and "5y5y" in fwd.columns else float("nan")
    f1y1y    = fwd["1y1y"].iloc[-1] if not fwd.empty and "1y1y" in fwd.columns else float("nan")
    ffr      = extra["fed_funds"].iloc[-1] if "fed_funds" in extra.columns else float("nan")
    status   = "INVERTED" if not np.isnan(c10y2y) and c10y2y < 0 else "NORMAL"

    print("\n" + "═"*W)
    print("   US YIELD CURVE — FULL ANALYTICAL REPORT")
    print(f"   {datetime.date.today().strftime('%Y-%m-%d')}   |   FRED / NY Fed / NBER")
    print("═"*W)

    # ── 1. Current Status
    _sec(1, "Current Yield Curve Status")
    print(f"""
  Spread 10Y-2Y  : {_f(c10y2y, '+.2f')} pp  →  {status}
  Spread 10Y-3M  : {_f(c10y3m, '+.2f')} pp
  FFR (current)  : {_f(ffr, '.2f', '%')}
  Regime         : {reg.upper()}
  Inversion episodes since 1990: {len(inversions)}
""")
    if not inversions.empty:
        last = inversions.iloc[-1]
        tag  = " (ONGOING)" if last.get("ongoing") else ""
        print(f"  Most recent: {last['start'].date()} → {last['end'].date()}"
              f" | {last['duration_days']} days | min spread {last['min_spread']:+.2f} pp{tag}")

    # ── 2. Spread 10Y-2Y
    _sec(2, "Spread 10Y-2Y — Inversion Signal")
    print(f"""
WHAT IT IS:
  Difference between 10-year and 2-year Treasury yields. The most
  widely cited inversion indicator in fixed-income markets.

HOW IT MEASURES:
  Spread = DGS10 − DGS2  (percentage points)
  Negative = short-term rates exceed long-term rates → inverted curve.

RESULT:
  Current 10Y-2Y spread: {_f(c10y2y, '+.2f')} pp  →  {status}
  Every US recession since 1955 was preceded by a negative 10Y-2Y spread.
  Approximate false positive rate: 1 in 8 inversions (e.g. 1998 brief inversion).

IMPORTANCE:
  The Fed, Treasury, and sell-side desks all monitor this spread as the
  primary real-time recession leading indicator.

LIMITATIONS:
  • Lag from inversion to recession onset: 6–24 months (highly variable).
  • Does not distinguish inversion cause (QE distortion vs genuine recession fear).
  • Binary signal — captures neither severity nor duration of the risk.
""")

    # ── 3. Spread 10Y-3M
    _sec(3, "Spread 10Y-3M — NY Fed Model Input")
    print(f"""
WHAT IT IS:
  Difference between the 10-year Treasury yield and the 3-month T-bill.
  The NY Fed probit model uses this spread rather than 10Y-2Y.

HOW IT MEASURES:
  Spread = DGS10 − DGS3MO  (percentage points)
  More sensitive to short-term monetary policy shifts than 10Y-2Y.

RESULT:
  Current 10Y-3M spread: {_f(c10y3m, '+.2f')} pp
  The 3M rate tracks the FFR very tightly — a negative 10Y-3M spread
  means markets expect the Fed to cut rates sharply, historically only
  happening in or ahead of recessions.

LIMITATIONS:
  • More volatile than 10Y-2Y — prone to brief false positives.
  • During active tightening, can lag the 10Y-2Y inversion signal.
""")

    # ── 4. Univariate Probit
    _sec(4, "NY Fed Probit — Univariate Recession Probability")
    print(f"""
WHAT IT IS:
  A probit regression converting the 10Y-3M spread into an explicit
  probability of US recession within the next 12 months. Published
  monthly by the Federal Reserve Bank of New York.

HOW IT MEASURES:
  P(recession 12m) = Φ(α + β · spread₁₀ᵧ₋₃ₘ)
  Parameters this run: α = {alpha:.4f}, β = {beta:.4f}
  (re-estimated from the NY Fed's published series each run)

RESULT:
  P(recession in 12 months): {_f(p_uni, '.1f', '%')}
  Thresholds: < 30% low risk  |  30–50% alert  |  > 50% high risk

IMPORTANCE:
  Provides a continuous probability rather than a binary signal.
  Used by the Fed, Congress, and financial institutions as the standard
  benchmark for yield-curve-based recession risk assessment.

LIMITATIONS:
  • Single predictor — ignores credit, labor market, PMI, and other signals.
  • QE and ZIRP may have altered the curve-to-recession relationship.
  • Outputs a 12-month average probability, not a point forecast.
""")

    # ── 5. Multivariate Probit
    _sec(5, "Multivariate Probit — Enhanced Recession Probability")
    print(f"""
WHAT IT IS:
  Three-predictor extension of the NY Fed model adding HY corporate
  spread (financial stress) and the Fed Funds Rate (policy level).

HOW IT MEASURES:
  P = Φ(−2.40 − 0.55·spread₁₀ᵧ₋₃ₘ + 0.08·HY + 0.10·FFR)
  Coefficients estimated on NBER recession data 1990–2023.

RESULT:
  Univariate P(recession 12m)    : {_f(p_uni, '.1f', '%')}
  Multivariate P(recession 12m)  : {_f(p_mv,  '.1f', '%')}
  {"→ Both models agree — signal is robust." if not (np.isnan(p_uni) or np.isnan(p_mv)) and abs(p_uni - p_mv) < 10 else "→ Models diverge — review individual predictors."}

IMPORTANCE:
  When univariate and multivariate models both exceed 30–50%, convergent
  evidence is substantially stronger. The HY spread captures financial
  stress that can precede recession independently of the curve slope.
  Ref: Favara, Gilchrist, Lewis & Zakrajšek (2016), FEDS Working Paper.

LIMITATIONS:
  • Coefficients are approximate — not dynamically re-estimated.
  • HY and FFR availability starts ~1986 — shorter estimation window.
  • Does not capture non-linear interaction effects.
""")

    # ── 6. Term Premium
    _sec(6, "Term Premium Decomposition — ACM Model")
    y10  = _f(decomp["yield_10y"].iloc[-1] if not decomp.empty and "yield_10y" in decomp.columns else float("nan"), ".2f", "%")
    tp_s = "Negative term premium: inversion may be partially artificial (QE / foreign demand distortion)." \
           if not np.isnan(tp) and tp < 0 else \
           "Positive term premium: inversion reflects genuine recession expectations."
    print(f"""
WHAT IT IS:
  Splits the 10Y Treasury yield into: (1) the expected path of short
  rates and (2) the extra compensation for bearing duration risk (term
  premium). Source: ACM model — Federal Reserve Bank of New York.

HOW IT MEASURES:
  term premium     = THREEFYTP10 (FRED monthly series)
  rate expectation = 10Y yield − term premium

RESULT:
  10Y yield current      : {y10}
  Term premium           : {_f(tp, '+.2f', ' pp')}
  Rate expectation       : {_f(re, '.2f', '%')}
  → {tp_s}

IMPORTANCE:
  Critical for interpreting inversions during quantitative easing.
  In 2010–2021 the term premium was persistently negative — inversions
  during that period carried less recessionary signal than historically.
  Ref: Adrian, Crump & Moench (2013), NY Fed Staff Reports.

LIMITATIONS:
  • ACM model published monthly — no daily resolution.
  • Parameters estimated on pre-QE history; may understate QE distortion.
  • Other term premium models (e.g. Kim-Wright) give different estimates.
""")

    # ── 7. Forward Rates
    _sec(7, "Implied Forward Rates — Market Expectations")
    cut_signal = "→ 1Y1Y below current FFR: markets pricing rate cuts within 12 months." \
                 if not (np.isnan(f1y1y) or np.isnan(ffr)) and f1y1y < ffr else \
                 "→ 1Y1Y at or above FFR: no imminent rate cuts priced in."
    neutral_signal = "→ Low 5Y5Y: market believes long-run neutral rate is low." \
                     if not np.isnan(f5y5y) and f5y5y < 3.0 else \
                     "→ 5Y5Y elevated: market pricing 'higher for longer' long-run rates."
    print(f"""
WHAT IT IS:
  Rates the market implies for future periods, bootstrapped from today's
  spot curve via: f(t1,t2) = [(1+r2)^t2 / (1+r1)^t1]^(1/(t2-t1)) - 1

KEY FORWARDS (current):
  1Y1Y  (1Y rate in 1 year)   : {_f(f1y1y, '.2f', '%')}   — near-term policy signal
  5Y5Y  (5Y rate in 5 years)  : {_f(f5y5y, '.2f', '%')}   — long-run neutral rate signal

INTERPRETATION:
  {cut_signal}
  {neutral_signal}

IMPORTANCE:
  Forward rates are the primary instrument for rates traders and
  fixed-income desks — they embed the full market consensus on future
  policy more directly than the spot curve shape alone.

LIMITATIONS:
  • Assumes pure expectations hypothesis — ignores term premium.
  • Real forwards overstate expected future rates by the term premium.
  • Sensitive to liquidity and supply/demand distortions at key maturities.
""")

    # ── 8. Monetary Regime
    _sec(8, "Monetary Policy Regime")
    inv_regime = "TIGHTENING CYCLE → historically most dangerous configuration." \
                 if reg == "tight" else \
                 "EASING CYCLE → inversion may reflect lagged adjustment, less alarming." \
                 if reg == "easing" else \
                 "NEUTRAL REGIME → interpretation depends on direction of travel."
    print(f"""
WHAT IT IS:
  Classification of the current monetary environment based on the
  effective Federal Funds Rate level.

HOW IT MEASURES:
  easing  : FFR < 2.0%  — Fed actively accommodating growth
  neutral : 2.0–4.5%    — neither restrictive nor stimulative
  tight   : FFR > 4.5%  — Fed actively restricting demand

RESULT:
  Current FFR    : {_f(ffr, '.2f', '%')}
  Current regime : {reg.upper()}
  → {inv_regime}

IMPORTANCE:
  An inverted curve under active tightening is historically the most
  dangerous configuration — the Fed is raising short rates faster than
  long rates rise, a classic precursor to credit contraction.
  An inversion with the Fed already cutting is less alarming — it often
  reflects a lag between the start of easing and curve normalization.

LIMITATIONS:
  • The natural rate (r*) is time-varying and unobservable — what counts
    as 'tight' differs across economic cycles.
  • Threshold values are empirically motivated but not universal.
""")

    # ── 9. Historical Comparisons
    _sec(9, "Historical Comparisons — 2000, 2008, 2019")
    print(f"""
WHAT IT IS:
  Overlay of the current yield curve shape against snapshots from three
  major stress episodes, for visual pattern comparison.

REFERENCE SNAPSHOTS:
  2000 (dot-com)    : 2000-03-24 — equity bubble peak, curve flat/inverted
  2008 (GFC)        : 2007-06-13 — deep inversion, credit stress building
  2019 (pre-COVID)  : 2019-08-28 — brief inversion, normalized before shock

IMPORTANCE:
  Visual pattern comparison with historical episodes raises the right
  questions for further analysis. Curve shape similarity does not imply
  outcome similarity — it is a starting point for investigation.

LIMITATIONS:
  • Three data points are not statistically meaningful.
  • Each episode had distinct macro drivers — direct comparison is limited.
  • Context (QE, globalization, demographics) differs materially across episodes.
""")

    print("═"*W)


# ──────────────────────────────────────────────────────────────────
# BLOCK 6 — VISUAL DASHBOARDS — MATPLOTLIB + PLOTLY
#
# WHAT IT DOES: generates three PNG dashboards and one interactive
# HTML file. Each focuses on a distinct analytical layer.
#
#   build_dashboard()            → yield_curve_dashboard.png
#     Main 4-panel: current curve + historical, 10Y-2Y spread history,
#     NY Fed recession probability, current metrics table.
#
#   build_historical_dashboard() → yield_curve_historical.png
#     Temporal evolution: key maturity rates, heatmap (monthly),
#     3D static surface (quarterly), snapshot history from Excel log.
#
#   build_economic_dashboard()   → yield_curve_economic.png
#     Advanced 3-panel: implied forward rates, univariate vs
#     multivariate probit, monetary regime with colored FFR background.
#
#   build_interactive_3d()       → yield_curve_3d.html
#     Plotly surface — rotate, zoom, hover for exact values.
#     Self-contained HTML (Plotly loaded from CDN).
#
# DESIGN PRINCIPLES:
#   • Dark theme (#0d1117) consistent across all outputs.
#   • shade_recessions() overlays NBER bands on any time-axis panel.
#   • Each plot function accepts a matplotlib Axes object and draws
#     independently — composable into any GridSpec layout.
#   • NaN-safe: functions render a placeholder text rather than raising.
#
# DEPENDENCIES: BLOCKS 2, 3, 4 (all computed series)
# USED IN: main() steps 11–14
# ──────────────────────────────────────────────────────────────────

COLORS = {
    "bg":       "#0d1117",  "panel":    "#161b22",
    "text":     "#e6edf3",  "muted":    "#8b949e",
    "current":  "#58a6ff",  "2000":     "#f78166",
    "2008":     "#ffa657",  "2019":     "#7ee787",
    "spread":   "#79c0ff",  "inv_fill": "#f85149",
    "rec_fill": "#6e40c9",  "prob":     "#d2a8ff",
    "grid":     "#21262d",
}


def shade_recessions(ax, alpha: float = 0.15) -> None:
    """Overlay NBER recession bands as semi-transparent spans."""
    for s, e in NBER_RECESSIONS:
        ax.axvspan(pd.Timestamp(s), pd.Timestamp(e),
                   alpha=alpha, color=COLORS["rec_fill"],
                   zorder=0, label="_nolegend_")


def plot_yield_curve(ax, df: pd.DataFrame, historical_df: dict) -> None:
    """
    Panel: current yield curve overlaid on three historical snapshots.
    X = maturity (years), Y = yield (%).
    Dashed colored lines = historical episodes; solid blue = current.
    """
    ax.set_facecolor(COLORS["panel"])
    mats, labels = list(MATURITIES.values()), list(MAT_LABELS.values())
    color_map = {"2000 (dot-com)": COLORS["2000"], "2008 (GFC)": COLORS["2008"],
                 "2019 (pre-COVID)": COLORS["2019"]}
    for name, curve in historical_df.items():
        y_vals = [curve.get(sid, np.nan) for sid in MATURITIES]
        valid  = [(m, v) for m, v in zip(mats, y_vals) if not np.isnan(v)]
        if valid:
            xv, yv = zip(*valid)
            ax.plot(xv, yv, "o--", color=color_map[name], lw=1.5,
                    markersize=4, alpha=0.7, label=name)
    current = df.iloc[-1]
    y_vals  = [current.get(sid, np.nan) for sid in MATURITIES]
    valid   = [(m, v) for m, v in zip(mats, y_vals) if not np.isnan(v)]
    if valid:
        xv, yv = zip(*valid)
        ax.plot(xv, yv, "o-", color=COLORS["current"], lw=2.5,
                markersize=6, zorder=5, label=f"Current ({df.index[-1].date()})")
    ax.axhline(0, color=COLORS["muted"], lw=0.8, ls="--", alpha=0.5)
    ax.set_xticks(mats); ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel("Yield (%)", color=COLORS["text"])
    ax.set_xlabel("Maturity", color=COLORS["text"])
    ax.set_title("US Yield Curve — Historical Comparison", color=COLORS["text"], fontweight="bold")
    ax.legend(fontsize=8, loc="upper left")
    ax.tick_params(colors=COLORS["muted"])
    ax.spines[:].set_color(COLORS["grid"])


def plot_spread(ax, spreads: pd.DataFrame, inversions: pd.DataFrame) -> None:
    """
    Panel: 10Y-2Y spread history — red fill for inversion periods,
    purple NBER recession bands in the background.
    """
    ax.set_facecolor(COLORS["panel"])
    if "10y_2y" not in spreads:
        return
    s = spreads["10y_2y"]
    ax.plot(s.index, s.values, color=COLORS["spread"], lw=1.2, zorder=3)
    ax.fill_between(s.index, s.values, 0, where=(s.values < 0),
                    color=COLORS["inv_fill"], alpha=0.4, label="Inversion (10Y-2Y < 0)", zorder=2)
    ax.fill_between(s.index, s.values, 0, where=(s.values >= 0),
                    color=COLORS["spread"], alpha=0.15, zorder=2)
    ax.axhline(0, color=COLORS["inv_fill"], lw=1, ls="--", alpha=0.8)
    shade_recessions(ax)
    ax.set_ylabel("Spread (pp)", color=COLORS["text"])
    ax.set_title("10Y-2Y Spread  |  Purple bands = NBER recessions", color=COLORS["text"], fontweight="bold")
    ax.legend(fontsize=8, loc="lower left")
    ax.tick_params(colors=COLORS["muted"])
    ax.spines[:].set_color(COLORS["grid"])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_major_locator(mdates.YearLocator(4))


def plot_recession_prob(ax, prob_series: pd.Series) -> None:
    """
    Panel: NY Fed univariate recession probability time series.
    Reference lines at 30% (alert) and 50% (high risk).
    """
    ax.set_facecolor(COLORS["panel"])
    if prob_series.empty:
        return
    ax.plot(prob_series.index, prob_series.values * 100, color=COLORS["prob"], lw=1.5, zorder=3)
    ax.fill_between(prob_series.index, prob_series.values * 100, alpha=0.3, color=COLORS["prob"], zorder=2)
    ax.axhline(30, color=COLORS["muted"], lw=0.8, ls=":", alpha=0.7, label="30% (alert)")
    ax.axhline(50, color=COLORS["inv_fill"], lw=0.8, ls=":", alpha=0.7, label="50% (high risk)")
    shade_recessions(ax)
    ax.set_ylabel("P(recession 12m) %", color=COLORS["text"])
    ax.set_title("NY Fed Probit — Recession Probability", color=COLORS["text"], fontweight="bold")
    ax.set_ylim(0, 100)
    ax.legend(fontsize=8, loc="upper left")
    ax.tick_params(colors=COLORS["muted"])
    ax.spines[:].set_color(COLORS["grid"])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_major_locator(mdates.YearLocator(4))


def plot_metrics_table(ax, spreads, prob, inversions_df) -> None:
    """
    Panel: static summary table — current spreads, recession probability,
    inversion status, and most recent inversion episode details.
    """
    ax.set_facecolor(COLORS["panel"])
    ax.axis("off")
    c10y2y = spreads["10y_2y"].iloc[-1] if "10y_2y" in spreads else float("nan")
    c10y3m = spreads["10y_3m"].iloc[-1] if "10y_3m" in spreads else float("nan")
    rows   = [
        ["Spread 10Y-2Y (current)", f"{c10y2y:+.2f} pp"],
        ["Spread 10Y-3M (current)", f"{c10y3m:+.2f} pp"],
        ["P(recession 12m)",        f"{prob*100:.1f}%"],
        ["Curve inverted?",         "YES ⚠" if c10y2y < 0 else "NO ✓"],
    ]
    if not inversions_df.empty:
        last = inversions_df.iloc[-1]
        rows += [["Last inversion start", str(last["start"].date())],
                 ["Duration (days)",      str(last["duration_days"])],
                 ["Min spread",           f"{last['min_spread']:+.2f} pp"]]
    tbl = ax.table(cellText=rows, colLabels=["Metric", "Value"],
                   cellLoc="left", loc="center", bbox=[0.05, 0.05, 0.9, 0.9])
    tbl.auto_set_font_size(False); tbl.set_fontsize(9)
    for (r, c), cell in tbl.get_celld().items():
        cell.set_facecolor(COLORS["panel"] if r > 0 else COLORS["bg"])
        cell.set_text_props(color=COLORS["text"])
        cell.set_edgecolor(COLORS["grid"])
    ax.set_title("Current Metrics Summary", color=COLORS["text"], fontweight="bold", pad=8)


def build_dashboard(
    df: pd.DataFrame, spreads: pd.DataFrame,
    inversions: pd.DataFrame, prob_series: pd.Series,
    historical_curves: dict,
) -> plt.Figure:
    """
    Main 4-panel dashboard (2×2 GridSpec):
      [0,0] Yield curve + historical   [0,1] 10Y-2Y spread history
      [1,0] NY Fed recession prob       [1,1] Current metrics table
    """
    fig = plt.figure(figsize=(18, 12), facecolor=COLORS["bg"])
    gs  = gridspec.GridSpec(2, 2, figure=fig, hspace=0.40, wspace=0.28,
                            left=0.06, right=0.97, top=0.93, bottom=0.06)
    axes = [fig.add_subplot(gs[i, j]) for i in range(2) for j in range(2)]
    for ax in axes:
        ax.tick_params(colors=COLORS["muted"])
        for sp in ax.spines.values(): sp.set_edgecolor(COLORS["grid"])
        ax.yaxis.label.set_color(COLORS["text"])
    plot_yield_curve(axes[0], df, historical_curves)
    plot_spread(axes[1], spreads, inversions)
    plot_recession_prob(axes[2], prob_series)
    plot_metrics_table(axes[3], spreads,
                       prob_series.iloc[-1] if not prob_series.empty else float("nan"),
                       inversions)
    today = datetime.date.today().strftime("%Y-%m-%d")
    fig.suptitle(f"US YIELD CURVE MACRO DASHBOARD   |   {today}",
                 color=COLORS["text"], fontsize=14, fontweight="bold", y=0.97)
    fig.text(0.5, 0.005,
             "Source: FRED / St. Louis Fed  •  Model: Estrella & Mishkin (1998) / NY Fed  •  Recessions: NBER",
             ha="center", fontsize=7, color=COLORS["muted"])
    return fig


def plot_rates_over_time(ax, df_rates: pd.DataFrame) -> None:
    """
    Panel: 3M, 2Y, 5Y, 10Y, 30Y rates as overlapping lines from 1990.
    Line convergence = flat/inverted; divergence = steep curve.
    """
    ax.set_facecolor(COLORS["panel"])
    key = {"3M": ("DGS3MO","#a8dadc"), "2Y": ("DGS2",COLORS["inv_fill"]),
           "5Y": ("DGS5",COLORS["2019"]), "10Y": ("DGS10",COLORS["current"]),
           "30Y": ("DGS30",COLORS["2000"])}
    for label, (sid, color) in key.items():
        col = MAT_LABELS.get(sid, label)
        if col in df_rates.columns:
            ax.plot(df_rates[col].dropna().index, df_rates[col].dropna().values,
                    lw=1.2, color=color, label=label, alpha=0.9)
    shade_recessions(ax)
    ax.set_ylabel("Yield (%)", color=COLORS["text"])
    ax.set_title("Key Maturities Over Time", color=COLORS["text"], fontweight="bold")
    ax.legend(fontsize=8, loc="upper left", ncol=5)
    ax.tick_params(colors=COLORS["muted"]); ax.spines[:].set_color(COLORS["grid"])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_major_locator(mdates.YearLocator(4))


def plot_rate_heatmap(ax, df_rates: pd.DataFrame) -> None:
    """
    Panel: yield curve heatmap — X = time (monthly), Y = maturity,
    color = yield level. Green = low rates, red = high.
    Reversed vertical color gradient signals inversion periods.
    """
    ax.set_facecolor(COLORS["panel"])
    col_order = [MAT_LABELS[s] for s in MATURITIES if MAT_LABELS[s] in df_rates.columns]
    data = df_rates[col_order].dropna(how="all").resample("ME").last().dropna(how="all")
    if data.empty:
        ax.text(0.5, 0.5, "No data", ha="center", color=COLORS["text"], transform=ax.transAxes)
        return
    Z  = data.values.T
    X  = np.arange(Z.shape[1])
    Y  = np.arange(Z.shape[0])
    im = ax.pcolormesh(X, Y, Z, cmap="RdYlGn", shading="auto",
                       vmin=0, vmax=max(8, np.nanpercentile(Z, 95)))
    step = max(1, len(data) // 8)
    ax.set_xticks(range(0, len(data), step))
    ax.set_xticklabels([data.index[i].strftime("%Y") for i in range(0, len(data), step)],
                       fontsize=7, color=COLORS["muted"])
    ax.set_yticks(Y); ax.set_yticklabels(col_order, fontsize=7, color=COLORS["muted"])
    plt.colorbar(im, ax=ax, label="Yield (%)", pad=0.01)
    ax.set_title("Yield Curve Heatmap — Monthly Evolution", color=COLORS["text"], fontweight="bold")
    ax.spines[:].set_color(COLORS["grid"])


def plot_spread_3d_surface(ax, df_rates: pd.DataFrame) -> None:
    """
    Panel: static matplotlib 3D surface — X = maturity, Y = time (quarterly),
    Z = yield. Useful for identifying curve shape cycles visually.
    For rotate/zoom/hover, use build_interactive_3d() → HTML.
    """
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
    col_order = [MAT_LABELS[s] for s in MATURITIES if MAT_LABELS[s] in df_rates.columns]
    data = df_rates[col_order].dropna(how="all").resample("QE").last().dropna(how="all")
    if data.shape[0] < 4:
        ax.text2D(0.5, 0.5, "Insufficient data for 3D surface",
                  ha="center", color=COLORS["text"], transform=ax.transAxes)
        return
    mats_x, times_y = list(range(len(col_order))), list(range(len(data)))
    X, Y = np.meshgrid(mats_x, times_y)
    ax.plot_surface(X, Y, data.values, cmap="RdYlGn", alpha=0.85,
                    vmin=0, vmax=max(8, np.nanpercentile(data.values, 95)))
    ax.set_xticks(mats_x[::2]); ax.set_xticklabels(col_order[::2], fontsize=6)
    step = max(1, len(data) // 6)
    ax.set_yticks(times_y[::step])
    ax.set_yticklabels([data.index[i].strftime("%Y") for i in times_y[::step]], fontsize=6)
    ax.set_zlabel("Yield (%)", fontsize=8)
    ax.set_title("3D Surface — static  (see yield_curve_3d.html for interactive)", fontsize=8, pad=6)
    ax.xaxis.pane.fill = ax.yaxis.pane.fill = ax.zaxis.pane.fill = False


def plot_snapshot_history(ax, excel_file: str) -> None:
    """
    Panel: 10Y-2Y spread and recession probability from the Excel
    snapshot log. Each point = one script execution. Useful for
    tracking deterioration or improvement across automated runs.
    """
    ax.set_facecolor(COLORS["panel"])
    try:
        snap = pd.read_excel(excel_file, sheet_name=SHEET_SNAPSHOT,
                             index_col=0, parse_dates=True)
    except (FileNotFoundError, ValueError):
        ax.text(0.5, 0.5, "No snapshots saved yet.\nRun the script at least once.",
                ha="center", va="center", color=COLORS["muted"], transform=ax.transAxes, fontsize=9)
        ax.set_title("Snapshot History (runs)", color=COLORS["text"], fontweight="bold")
        return
    if snap.empty or "spread_10y_2y" not in snap.columns:
        ax.text(0.5, 0.5, "Snapshots have insufficient data.", ha="center",
                va="center", color=COLORS["muted"], transform=ax.transAxes)
        return
    ax2 = ax.twinx()
    ax.plot(snap.index, snap["spread_10y_2y"], color=COLORS["spread"],
            lw=1.5, marker="o", markersize=3, label="Spread 10Y-2Y (pp)")
    ax.axhline(0, color=COLORS["inv_fill"], lw=0.8, ls="--", alpha=0.7)
    ax.fill_between(snap.index, snap["spread_10y_2y"], 0,
                    where=(snap["spread_10y_2y"] < 0), color=COLORS["inv_fill"], alpha=0.3)
    if "prob_recession_pct" in snap.columns:
        ax2.plot(snap.index, snap["prob_recession_pct"], color=COLORS["prob"],
                 lw=1.2, ls="--", marker="s", markersize=3, label="P(recession) %")
        ax2.set_ylabel("P(recession) %", color=COLORS["prob"])
        ax2.tick_params(colors=COLORS["muted"]); ax2.set_ylim(0, 100)
    ax.set_ylabel("Spread (pp)", color=COLORS["text"])
    ax.set_title("Snapshot History — each point = one script run", color=COLORS["text"], fontweight="bold")
    ax.tick_params(colors=COLORS["muted"]); ax.spines[:].set_color(COLORS["grid"])
    l1, lb1 = ax.get_legend_handles_labels()
    l2, lb2 = ax2.get_legend_handles_labels()
    ax.legend(l1 + l2, lb1 + lb2, fontsize=8, loc="upper left")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m/%y"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")


def build_historical_dashboard(df_rates: pd.DataFrame, excel_file: str) -> plt.Figure:
    """
    Historical evolution dashboard (2×2 GridSpec):
      [0,0] Key maturity rates over time    [0,1] Heatmap (monthly)
      [1,0] 3D static surface (quarterly)   [1,1] Snapshot history
    """
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
    fig = plt.figure(figsize=(20, 14), facecolor=COLORS["bg"])
    gs  = gridspec.GridSpec(2, 2, figure=fig, hspace=0.42, wspace=0.30,
                            left=0.06, right=0.97, top=0.93, bottom=0.07)
    ax1, ax2 = fig.add_subplot(gs[0, 0]), fig.add_subplot(gs[0, 1])
    ax3 = fig.add_subplot(gs[1, 0], projection="3d")
    ax4 = fig.add_subplot(gs[1, 1])
    for ax in (ax1, ax2, ax4):
        ax.tick_params(colors=COLORS["muted"])
        for sp in ax.spines.values(): sp.set_edgecolor(COLORS["grid"])
    plot_rates_over_time(ax1, df_rates); plot_rate_heatmap(ax2, df_rates)
    plot_spread_3d_surface(ax3, df_rates); plot_snapshot_history(ax4, excel_file)
    today = datetime.date.today().strftime("%Y-%m-%d")
    fig.suptitle(f"US YIELD CURVE — HISTORICAL EVOLUTION   |   {today}",
                 color=COLORS["text"], fontsize=14, fontweight="bold", y=0.97)
    fig.text(0.5, 0.005,
             "Source: FRED / St. Louis Fed  •  Recessions: NBER  •  Data: yield_curve_history.xlsx",
             ha="center", fontsize=7, color=COLORS["muted"])
    return fig


def plot_forward_curve(ax, fwd: pd.DataFrame) -> None:
    """
    Panel: 1Y1Y, 1Y2Y, 2Y5Y, 5Y5Y forward rates over time.
    Lines below current spot rates = markets pricing rate cuts.
    """
    ax.set_facecolor(COLORS["panel"])
    if fwd.empty:
        ax.text(0.5, 0.5, "Forward rates unavailable", ha="center",
                color=COLORS["muted"], transform=ax.transAxes)
        ax.set_title("Implied Forward Rates", color=COLORS["text"], fontweight="bold")
        return
    colors_fwd = {"1y1y": COLORS["current"], "1y2y": COLORS["2019"],
                  "2y5y": COLORS["2008"],    "5y5y": COLORS["prob"]}
    labels_fwd = {"1y1y": "1Y1Y — 1Y rate in 1 year",    "1y2y": "1Y2Y — 2Y rate in 1 year",
                  "2y5y": "2Y5Y — 5Y rate in 2 years",   "5y5y": "5Y5Y — 5Y rate in 5 years (Fed)"}
    for col, color in colors_fwd.items():
        if col in fwd.columns:
            ax.plot(fwd.index, fwd[col], color=color, lw=1.3, label=labels_fwd.get(col, col))
    ax.axhline(0, color=COLORS["muted"], lw=0.7, ls="--", alpha=0.5)
    shade_recessions(ax)
    ax.set_ylabel("Forward rate (%)", color=COLORS["text"])
    ax.set_title("Implied Forward Rates — Market Expectations", color=COLORS["text"], fontweight="bold")
    ax.legend(fontsize=7, loc="upper right"); ax.tick_params(colors=COLORS["muted"])
    ax.spines[:].set_color(COLORS["grid"])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_major_locator(mdates.YearLocator(4))


def plot_multivariate_prob(ax, prob_uni: pd.Series, prob_mv: pd.Series) -> None:
    """
    Panel: univariate vs multivariate probit probability time series.
    Convergence of both models above 30–50% = robust recession signal.
    """
    ax.set_facecolor(COLORS["panel"])
    if prob_uni.empty and prob_mv.empty:
        ax.text(0.5, 0.5, "Probabilities unavailable", ha="center",
                color=COLORS["muted"], transform=ax.transAxes)
        return
    if not prob_uni.empty:
        ax.plot(prob_uni.index, prob_uni.values * 100, color=COLORS["spread"],
                lw=1.3, alpha=0.8, label="Univariate (10Y-3M spread)")
        ax.fill_between(prob_uni.index, prob_uni.values * 100, alpha=0.1, color=COLORS["spread"])
    if not prob_mv.empty:
        ax.plot(prob_mv.index, prob_mv.values * 100, color=COLORS["prob"],
                lw=1.5, label="Multivariate (spread + HY + FFR)")
        ax.fill_between(prob_mv.index, prob_mv.values * 100, alpha=0.15, color=COLORS["prob"])
    ax.axhline(30, color=COLORS["muted"], lw=0.8, ls=":", alpha=0.7, label="30%")
    ax.axhline(50, color=COLORS["inv_fill"], lw=0.8, ls=":", alpha=0.7, label="50%")
    shade_recessions(ax)
    ax.set_ylim(0, 100)
    ax.set_ylabel("P(recession 12m) %", color=COLORS["text"])
    ax.set_title("Probit Univariate vs Multivariate  [Favara et al., 2016]",
                 color=COLORS["text"], fontweight="bold")
    ax.legend(fontsize=8, loc="upper left"); ax.tick_params(colors=COLORS["muted"])
    ax.spines[:].set_color(COLORS["grid"])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_major_locator(mdates.YearLocator(4))


def plot_monetary_regime(ax, extra: pd.DataFrame, regime: pd.Series) -> None:
    """
    Panel: Fed Funds Rate with colored background by monetary regime.
    Blue = easing (FFR < 2%), gray = neutral, red = tight (> 4.5%).
    Contextualizes whether an inversion is occurring under tightening
    (historically dangerous) or easing (less alarming).
    """
    ax.set_facecolor(COLORS["panel"])
    if "fed_funds" not in extra.columns:
        ax.text(0.5, 0.5, "FFR unavailable", ha="center",
                color=COLORS["muted"], transform=ax.transAxes)
        ax.set_title("Monetary Regime", color=COLORS["text"], fontweight="bold")
        return
    ffr = extra["fed_funds"].dropna()
    ax.plot(ffr.index, ffr.values, color=COLORS["current"], lw=1.5, zorder=3)
    for reg, color in {"easing": "#1f6feb", "neutral": "#3d444d", "tight": "#da3633"}.items():
        if not regime.empty:
            mask = regime.reindex(ffr.index, method="nearest").dropna() == reg
            ax.fill_between(ffr.index, 0, ffr.values,
                            where=mask.reindex(ffr.index, fill_value=False),
                            alpha=0.25, color=color, label=reg.capitalize(), zorder=2)
    shade_recessions(ax)
    ax.set_ylabel("Fed Funds Rate (%)", color=COLORS["text"])
    ax.set_title("Fed Funds Rate — Monetary Regime  [blue=easing | gray=neutral | red=tight]",
                 color=COLORS["text"], fontweight="bold")
    ax.legend(fontsize=8, loc="upper left"); ax.tick_params(colors=COLORS["muted"])
    ax.spines[:].set_color(COLORS["grid"])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_major_locator(mdates.YearLocator(4))


def build_economic_dashboard(
    decomp: pd.DataFrame, credit: pd.DataFrame,
    fwd: pd.DataFrame, prob_uni: pd.Series, prob_mv: pd.Series,
    intl: pd.DataFrame, extra: pd.DataFrame, regime: pd.Series,
) -> plt.Figure:
    """
    Advanced economic analysis dashboard (1×3 GridSpec):
      [0] Implied forward rates    [1] Univariate vs multivariate probit
      [2] Monetary regime (FFR)
    """
    fig = plt.figure(figsize=(20, 8), facecolor=COLORS["bg"])
    gs  = gridspec.GridSpec(1, 3, figure=fig, hspace=0.40, wspace=0.30,
                            left=0.05, right=0.97, top=0.88, bottom=0.10)
    axes = [fig.add_subplot(gs[0, j]) for j in range(3)]
    for ax in axes:
        ax.tick_params(colors=COLORS["muted"])
        for sp in ax.spines.values(): sp.set_edgecolor(COLORS["grid"])
    plot_forward_curve(axes[0], fwd)
    plot_multivariate_prob(axes[1], prob_uni, prob_mv)
    plot_monetary_regime(axes[2], extra, regime)
    today = datetime.date.today().strftime("%Y-%m-%d")
    fig.suptitle(f"US YIELD CURVE — ADVANCED ECONOMIC ANALYSIS   |   {today}",
                 color=COLORS["text"], fontsize=14, fontweight="bold", y=0.97)
    fig.text(0.5, 0.005,
             "Sources: FRED · NY Fed · NBER  |  Refs: Estrella & Mishkin (1998) · Favara et al. (2016)",
             ha="center", fontsize=7, color=COLORS["muted"])
    return fig


def build_interactive_3d(
    df_rates: pd.DataFrame, output_html: str = "yield_curve_3d.html"
) -> str:
    """
    Generate a self-contained interactive Plotly 3D surface.
    Output: standalone HTML (Plotly loaded from CDN, no local deps).
    Features: rotate (drag), zoom (scroll), hover for exact values.
    Data resampled monthly for performance. Returns '' on failure.
    """
    try:
        import plotly.graph_objects as go
    except ImportError:
        print("  ✗ Plotly not installed. Run: pip install plotly")
        return ""

    col_order = [MAT_LABELS[s] for s in MATURITIES if MAT_LABELS[s] in df_rates.columns]
    data = df_rates[col_order].dropna(how="all").resample("ME").last().dropna(how="all")
    if data.shape[0] < 4:
        print("  ✗ Insufficient data for interactive 3D.")
        return ""

    mats_num  = list(MATURITIES.values())[:len(col_order)]
    dates_str = [d.strftime("%b %Y") for d in data.index]
    Z         = data.values
    hover     = [[f"<b>{dates_str[i]}</b><br>Maturity: {col_order[j]}<br>Yield: {Z[i,j]:.2f}%"
                  for j in range(len(col_order))] for i in range(len(data))]
    step   = max(1, len(data) // 12)
    yticks = list(range(0, len(data), step))

    fig = go.Figure(data=[go.Surface(
        z=Z, x=mats_num, y=list(range(len(data))),
        colorscale="RdYlGn", reversescale=False,
        cmin=0, cmax=max(8, float(np.nanpercentile(Z, 95))),
        colorbar=dict(title="Yield (%)", tickfont=dict(color="#e6edf3")),
        hoverinfo="text", text=hover,
    )])
    fig.update_layout(
        title=dict(text="US Yield Curve — Interactive 3D Surface",
                   font=dict(color="#e6edf3", size=16), x=0.5),
        scene=dict(
            xaxis=dict(title=dict(text="Maturity (years)", font=dict(color="#e6edf3")),
                       tickvals=mats_num[::2], ticktext=col_order[::2],
                       tickfont=dict(color="#8b949e", size=9),
                       gridcolor="#21262d", backgroundcolor="#161b22"),
            yaxis=dict(title=dict(text="Time", font=dict(color="#e6edf3")),
                       tickvals=yticks, ticktext=[dates_str[i] for i in yticks],
                       tickfont=dict(color="#8b949e", size=9),
                       gridcolor="#21262d", backgroundcolor="#161b22"),
            zaxis=dict(title=dict(text="Yield (%)", font=dict(color="#e6edf3")),
                       tickfont=dict(color="#8b949e", size=9),
                       gridcolor="#21262d", backgroundcolor="#161b22"),
            camera=dict(eye=dict(x=1.6, y=-1.6, z=0.8)), bgcolor="#0d1117",
        ),
        paper_bgcolor="#0d1117", font=dict(color="#e6edf3"),
        margin=dict(l=0, r=0, t=60, b=0), height=750,
    )
    fig.write_html(output_html, include_plotlyjs="cdn", full_html=True)
    return output_html


# ──────────────────────────────────────────────────────────────────
# BLOCK 7 — EXCEL DATA STORE
#
# WHAT IT DOES: persists all computed series to a structured workbook
# after each run using an upsert pattern — new dates are appended,
# existing dates are updated, nothing is ever duplicated.
#
# WORKBOOK SHEETS:
#   Rates         : daily Treasury yields (1M → 30Y) by date
#   Spreads       : 10Y-2Y and 10Y-3M spreads by date
#   Prob_Recession: univariate probit probability by date
#   Inversions    : all inversion episodes (start, end, duration, min)
#   Snapshots     : one row per script execution — timestamp + key metrics
#
# WHY THIS WAY: the upsert prevents file bloat when running daily.
# The Snapshots sheet acts as a monitoring log — when automated via
# GitHub Actions, it accumulates a time series that is plotted in
# the Snapshot History dashboard panel (Block 6).
#
# DEPENDENCIES: BLOCKS 2, 3 (spreads, prob_series, inversions)
# USED IN: main() step 10
# ──────────────────────────────────────────────────────────────────

EXCEL_FILE       = "yield_curve_history.xlsx"
SHEET_RATES      = "Rates"
SHEET_SPREADS    = "Spreads"
SHEET_PROB       = "Prob_Recession"
SHEET_INVERSIONS = "Inversions"
SHEET_SNAPSHOT   = "Snapshots"


def _load_sheet(path: str, sheet: str) -> pd.DataFrame:
    """Load a workbook sheet or return empty DataFrame if not found."""
    try:
        return pd.read_excel(path, sheet_name=sheet, index_col=0, parse_dates=True)
    except (FileNotFoundError, ValueError):
        return pd.DataFrame()


def _upsert_timeseries(
    existing: pd.DataFrame, new: pd.DataFrame | pd.Series
) -> pd.DataFrame:
    """
    Merge new observations into an existing time series.
    Duplicate dates: new value wins (keep='last').
    New dates: appended and sorted chronologically.
    """
    if isinstance(new, pd.Series):
        new = new.to_frame()
    if existing.empty:
        return new
    combined = pd.concat([existing, new])
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()


def save_to_excel(
    df_rates:    pd.DataFrame,
    spreads:     pd.DataFrame,
    prob_series: pd.Series,
    inversions:  pd.DataFrame,
) -> None:
    """
    Upsert all computed data into the Excel workbook.
    Reads the existing file first, merges, rewrites.
    Auto-adjusts column widths for readability.
    """
    print("► Saving data to Excel...")

    existing_rates   = _load_sheet(EXCEL_FILE, SHEET_RATES)
    updated_rates    = _upsert_timeseries(existing_rates, df_rates.rename(columns=MAT_LABELS))

    existing_spreads = _load_sheet(EXCEL_FILE, SHEET_SPREADS)
    updated_spreads  = _upsert_timeseries(existing_spreads, spreads)

    existing_prob  = _load_sheet(EXCEL_FILE, SHEET_PROB)
    updated_prob   = _upsert_timeseries(existing_prob, prob_series.rename("prob_recession_12m").to_frame())

    updated_inv    = inversions.copy() if not inversions.empty else pd.DataFrame()

    existing_snap  = _load_sheet(EXCEL_FILE, SHEET_SNAPSHOT)
    now            = pd.Timestamp.now().floor("min")
    c10y2y         = spreads["10y_2y"].iloc[-1] if "10y_2y" in spreads else float("nan")
    c10y3m         = spreads["10y_3m"].iloc[-1] if "10y_3m" in spreads else float("nan")
    prob_now       = prob_series.iloc[-1]        if not prob_series.empty else float("nan")

    snap_row = pd.DataFrame([{
        "run_timestamp":         now,
        "last_fred_date":        df_rates.index[-1],
        "spread_10y_2y":         round(c10y2y, 4),
        "spread_10y_3m":         round(c10y3m, 4),
        "prob_recession_pct":    round(prob_now * 100, 2),
        "curve_inverted":        "YES" if c10y2y < 0 else "NO",
        "n_inversion_episodes":  len(inversions),
    }]).set_index("run_timestamp")
    updated_snap = pd.concat([existing_snap, snap_row]).sort_index()

    with pd.ExcelWriter(EXCEL_FILE, engine="openpyxl") as writer:
        updated_rates.to_excel(writer,   sheet_name=SHEET_RATES)
        updated_spreads.to_excel(writer, sheet_name=SHEET_SPREADS)
        updated_prob.to_excel(writer,    sheet_name=SHEET_PROB)
        if not updated_inv.empty:
            updated_inv.to_excel(writer, sheet_name=SHEET_INVERSIONS, index=False)
        updated_snap.to_excel(writer,    sheet_name=SHEET_SNAPSHOT)
        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            for col in ws.columns:
                max_len = max((len(str(c.value)) for c in col if c.value), default=10)
                ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 30)

    rows_new = len(updated_rates) - len(existing_rates)
    print(f"  ✓ {EXCEL_FILE} updated — +{rows_new} new row(s) in '{SHEET_RATES}'")
    print(f"  ✓ Snapshot logged: {now.strftime('%Y-%m-%d %H:%M')}")


# ──────────────────────────────────────────────────────────────────
# BLOCK 8 — MAIN ENTRY POINT
#
# WHAT IT DOES: orchestrates all blocks in the correct dependency
# order, passing each output forward as input to the next step.
# Prints a clear progress log at each stage.
#
# EXECUTION ORDER:
#   1.  Fetch Treasury maturities (Block 1)
#   2.  Fetch supplementary economic series (Block 1)
#   3.  Compute 10Y-2Y and 10Y-3M spreads (Block 2)
#   4.  Detect all inversion episodes (Block 2)
#   5.  Fetch NY Fed series + re-estimate probit parameters (Block 3)
#   6.  Compute univariate recession probability series (Block 3)
#   7.  Compute advanced economic indicators (Block 4)
#   8.  Load historical curve snapshots for comparison (Block 1)
#   9.  Print diagnostic alert + full analytical report (Block 5)
#   10. Save all data to Excel workbook (Block 7)
#   11. Build and save main dashboard PNG (Block 6)
#   12. Build and save historical dashboard PNG (Block 6)
#   13. Build and save economic analysis dashboard PNG (Block 6)
#   14. Build and save interactive 3D HTML (Block 6)
#
# OUTPUTS:
#   yield_curve_dashboard.png   — main 4-panel macro view
#   yield_curve_economic.png    — forward rates, probit, regime
#   yield_curve_historical.png  — temporal evolution + heatmap + 3D
#   yield_curve_3d.html         — interactive Plotly 3D surface
#   yield_curve_history.xlsx    — persistent historical data store
#
# DEPENDENCIES: all previous blocks
# USED IN: __main__ guard below (public entry point)
# ──────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("  US YIELD CURVE DASHBOARD — starting")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

    if not FRED_API_KEY:
        print("⚠  WARNING: FRED_API_KEY not set.")
        print("   Free key at: https://fred.stlouisfed.org/docs/api/api_key.html\n")

    # 1. Treasury maturities
    print("► Fetching Treasury maturities...")
    df = fetch_all_maturities(start="1990-01-01")
    if df.empty or df.dropna(how="all").empty:
        print("\n✗ ERROR: no data returned by FRED.")
        print("  Check FRED_API_KEY and internet connection.")
        return
    df = df.dropna(how="all")
    print(f"  {len(df)} observations ({df.index[0].date()} → {df.index[-1].date()})\n")

    # 2. Supplementary economic series
    print("► Fetching supplementary economic series...")
    extra = fetch_extra_series(start="1990-01-01")
    print()

    # 3. Spreads
    print("► Computing spreads...")
    spreads = compute_spreads(df)

    # 4. Inversion detection
    print("► Detecting inversions (10Y-2Y)...")
    inversions = pd.DataFrame()
    if "10y_2y" in spreads:
        inversions = detect_inversions(spreads["10y_2y"])
        print(f"  {len(inversions)} inversion episode(s) identified")

    # 5. NY Fed parameters — dynamic fetch + re-estimation
    print("► Fetching updated NY Fed model parameters...")
    ny_fed_probs = fetch_ny_fed_recession_probs()
    if not ny_fed_probs.empty:
        print(f"  ✓ NY Fed series loaded ({len(ny_fed_probs)} obs, "
              f"through {ny_fed_probs.index[-1].strftime('%b %Y')})")
        alpha, beta = fetch_ny_fed_params_from_probs(ny_fed_probs, spreads)
        print(f"  ✓ Re-estimated parameters: α = {alpha:.4f}  β = {beta:.4f}")
    else:
        alpha, beta = PROBIT_ALPHA, PROBIT_BETA
        print(f"  ⚠ Using fallback parameters: α = {alpha}  β = {beta}")

    # 6. Univariate recession probability
    print("► Computing univariate recession probability...")
    prob_series = compute_recession_prob_series(spreads, alpha, beta)

    # 7. Advanced economic analysis
    print("► Computing advanced economic indicators...")
    fwd     = compute_forward_rates(df)
    regime  = compute_monetary_regime(extra)
    decomp  = compute_term_premium_decomposition(df, extra)
    credit  = compute_credit_metrics(extra, spreads)
    intl    = compute_international_spreads(extra, df)
    prob_mv = compute_multivariate_recession_prob(spreads, credit, extra)
    print("  ✓ Forward rates, term premium, credit, regime, international, multivariate probit")

    # 8. Historical curve snapshots
    print("► Loading historical curve snapshots...")
    historical_curves = {}
    for name, date_str in HISTORICAL_DATES.items():
        curve = get_curve_on_date(df, date_str)
        if curve is not None:
            historical_curves[name] = curve
            print(f"  ✓ {name}")

    # 9. Diagnostic alert + full analytical report
    c10y2y      = spreads["10y_2y"].iloc[-1] if "10y_2y" in spreads else float("nan")
    c10y3m      = spreads["10y_3m"].iloc[-1] if "10y_3m" in spreads else float("nan")
    prob_now    = prob_series.iloc[-1] if not prob_series.empty else float("nan")
    prob_mv_now = prob_mv.iloc[-1]     if not prob_mv.empty     else float("nan")
    tp_now      = decomp["term_premium"].iloc[-1] if not decomp.empty and "term_premium" in decomp.columns else float("nan")
    ig_now      = credit["ig_spread"].iloc[-1]    if not credit.empty and "ig_spread"    in credit.columns else float("nan")
    hy_now      = credit["hy_spread"].iloc[-1]    if not credit.empty and "hy_spread"    in credit.columns else float("nan")
    reg_now     = regime.iloc[-1] if not regime.empty else "n/a"
    fwd55_now   = fwd["5y5y"].iloc[-1] if not fwd.empty and "5y5y" in fwd.columns else float("nan")

    print("\n" + build_alert_message(
        c10y2y, c10y3m, prob_now, inversions,
        prob_mv=prob_mv_now, term_premium=tp_now,
        ig_spread=ig_now, hy_spread=hy_now,
        regime=reg_now, fwd_5y5y=fwd55_now,
    ))

    print_full_report(spreads, prob_series, prob_mv, inversions,
                      decomp, credit, fwd, extra, regime, alpha, beta)

    # 10. Excel
    save_to_excel(df, spreads, prob_series, inversions)

    # 11. Main dashboard
    print("\n► Building main dashboard...")
    fig = build_dashboard(df, spreads, inversions, prob_series, historical_curves)
    fig.savefig("yield_curve_dashboard.png", dpi=150, bbox_inches="tight", facecolor=COLORS["bg"])
    print("  ✓ yield_curve_dashboard.png")

    # 12. Historical dashboard
    print("► Building historical dashboard...")
    df_labeled = df.rename(columns=MAT_LABELS)
    fig2 = build_historical_dashboard(df_labeled, EXCEL_FILE)
    fig2.savefig("yield_curve_historical.png", dpi=150, bbox_inches="tight", facecolor=COLORS["bg"])
    print("  ✓ yield_curve_historical.png")

    # 13. Economic analysis dashboard
    print("► Building economic analysis dashboard...")
    fig3 = build_economic_dashboard(decomp, credit, fwd, prob_series, prob_mv, intl, extra, regime)
    fig3.savefig("yield_curve_economic.png", dpi=150, bbox_inches="tight", facecolor=COLORS["bg"])
    print("  ✓ yield_curve_economic.png")

    # 14. Interactive 3D
    print("► Generating interactive 3D surface...")
    html_path = build_interactive_3d(df_labeled, output_html="yield_curve_3d.html")
    if html_path:
        print("  ✓ yield_curve_3d.html")

    plt.show()
    print("\nDone. Files generated:")
    print("  • yield_curve_dashboard.png   — curve, spread, recession probability, metrics")
    print("  • yield_curve_economic.png    — forward rates, multivariate probit, monetary regime")
    print("  • yield_curve_historical.png  — temporal evolution, heatmap, 3D surface")
    print("  • yield_curve_3d.html         — interactive Plotly 3D surface")
    print("  • yield_curve_history.xlsx    — full historical data store")

    # 15. Email alerts
    # Requires yield_curve_email.py in the same directory and
    # GMAIL_USER / GMAIL_APP_PASS / ALERT_TO set as env variables.
    if EMAIL_MODULE_AVAILABLE:
        print("\n► Sending email alerts...")
        png_list = [
            "yield_curve_dashboard.png",
            "yield_curve_economic.png",
        ]
        send_daily_report(
            spread_10y2y     = c10y2y,
            spread_10y3m     = c10y3m,
            prob_uni         = prob_now,
            prob_mv          = prob_mv_now,
            hy_spread        = hy_now,
            ig_spread        = ig_now,
            term_premium     = tp_now,
            fwd_5y5y         = fwd55_now,
            regime           = reg_now,
            inversions_count = len(inversions),
            png_paths        = png_list,
        )
        send_urgent_alert(
            spread_10y2y = c10y2y,
            spread_10y3m = c10y3m,
            prob_uni     = prob_now,
            prob_mv      = prob_mv_now,
            hy_spread    = hy_now,
            ig_spread    = ig_now,
            term_premium = tp_now,
            regime       = reg_now,
            png_paths    = png_list,
        )
    else:
        print("\n  ℹ Email module not found (yield_curve_email.py).")
        print("    Place it in the same directory to enable email alerts.")


if __name__ == "__main__":
    main()
