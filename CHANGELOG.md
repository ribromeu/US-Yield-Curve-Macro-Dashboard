# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [1.0.0] - 2026-05-08

### Added

- **Core framework** (`yield_curve_dashboard.py`) structured in 8 numbered blocks with inline documentation headers (what it does, why, dependencies, downstream usage)
- **Treasury data ingestion** — pulls all 11 maturities (1M through 30Y) from the FRED API on each run
- **Yield curve analytics** — computes 10Y-2Y and 10Y-3M spreads, detects inversion episodes, and classifies monetary regime
- **Univariate probit model** (NY Fed methodology) with dynamic parameter re-estimation from the Fed's own published series instead of static 1998 values; automatic fallback to Estrella & Mishkin (1998) when estimated beta turns positive
- **DGS3MO fallback** — fills missing same-day values with DTB3 to prevent NaN propagation in the probit panel
- **Multivariate probit model** (Favara, Gilchrist, Lewis & Zakrajšek 2016) adding HY spread and Fed Funds Rate as predictors
- **Implied forward rates** — 1Y1Y, 2Y5Y, 5Y5Y computed from the spot curve
- **Term premium decomposition** using the ACM model (FRED THREEFYTP10) to contextualize inversion signals
- **Corporate credit spreads** — IG and HY OAS via BofA BAML series on FRED
- **International yield differentials** — US vs German Bund, UK Gilt, and Japanese JGB
- **Three PNG dashboards** generated on each run:
  - Main dashboard: current curve vs crisis snapshots, 10Y-2Y history with NBER bands, recession probability, metrics table
    - Economic analysis: forward rates, probit comparison, monetary regime classification
      - Historical evolution: maturity time-series, monthly heatmap, 3D surface, snapshot archive
      - **Interactive Plotly 3D surface** of yield curve evolution
      - **Email alert system** (`yield_curve_email.py`) using Python stdlib only (smtplib, email, ssl)
        - Daily HTML report with inline CID dashboard images
          - Urgent alert triggered by configurable thresholds (inversion, recession probability, HY spread, term premium)
          - **Excel persistence** with upsert logic — no duplicate rows across runs
          - **GitHub Actions workflow** (`yield_curve_daily.yml`) for manual-trigger execution
          - **CI workflow** (`ci.yml`) running pytest on every push and pull request to main
          - **Unit test suite** (`tests/test_model.py`) with 14 tests covering probit model outputs, inversion detection, fallback logic, and alert thresholds
          - **Project scaffolding**: `requirements.txt` with version constraints, `.env.example`, `.gitignore`, `LICENSE` (MIT)
