"""
Historical IV Crush Database
==============================
The edge in earnings vol trading is fundamentally statistical.
Without robust historical data, you're guessing. This module
maintains a per-ticker database of every historical earnings cycle,
including pre- and post-earnings IV measurements.

Data collected per earnings cycle:
  • Pre-earnings IV: ATM, 25Δ, 10Δ (calls and puts)
  • Post-earnings IV: Same strikes, next morning
  • Actual stock move (gap open %)
  • Options expected move (pre-earnings straddle)
  • IV crush magnitude at each delta point
  • Risk reversal before and after
  • Term structure ratio before and after
  • Earnings result: Beat/Meet/Miss by how much

This database is what separates disciplined vol arb from gambling.
Build it before trading. Update it after every earnings cycle.

Database Schema:
  ticker_history:
    ticker | earnings_date | timing | pre_atm_iv | post_atm_iv |
    pre_rr_25d | post_rr_25d | pre_bf_25d | post_bf_25d |
    pre_ts_ratio | post_ts_ratio | stock_move_pct | em_options_pct |
    crush_atm_pct | crush_25d_pct | eps_beat_miss | stock_move_surprise

  ticker_summary (derived from history):
    ticker | n_cycles | median_pre_atm_iv | median_crush_pct |
    crush_consistency | median_pre_rr_25d | median_post_rr_25d |
    median_em_accuracy | p_wing_breach_1x | p_wing_breach_1_2x |
    last_updated
"""

import asyncio
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional, List, Dict, Any
import numpy as np
import structlog

log = structlog.get_logger(__name__)


@dataclass
class EarningsIVRecord:
    """One complete historical earnings IV record."""
    ticker: str
    earnings_date: str         # ISO format YYYY-MM-DD
    timing: str                # AMC | BMO
    fiscal_quarter: str        # Q1/Q2/Q3/Q4 + year (e.g., "Q2-2024")

    # Pre-earnings IV (1 day before report)
    pre_atm_iv: float
    pre_25d_put_iv: float
    pre_25d_call_iv: float
    pre_10d_put_iv: float
    pre_10d_call_iv: float
    pre_rr_25d: float         # pre_25d_put_iv - pre_25d_call_iv
    pre_bf_25d: float         # butterfly
    pre_ts_ratio: float       # front/back month ratio
    pre_iv_rank: float
    options_expected_move_pct: float   # Straddle price / stock price

    # Post-earnings IV (morning of next session, first 30 min)
    post_atm_iv: float
    post_25d_put_iv: float
    post_25d_call_iv: float
    post_rr_25d: float
    post_bf_25d: float
    post_ts_ratio: float

    # Actual outcome
    stock_gap_pct: float          # Actual gap open as % (+ = up, - = down)
    stock_move_surprise_pct: float # |actual_move| - |expected_move|
    eps_reported: Optional[float] = None
    eps_estimate: Optional[float] = None
    eps_beat_pct: Optional[float] = None  # (actual - est) / |est|

    # Derived P&L for standard strategies (calculated post-hoc)
    condor_pnl_pct: Optional[float] = None    # Iron condor P&L as % of max risk
    rr_pnl_pct: Optional[float] = None        # Risk reversal P&L
    calendar_pnl_pct: Optional[float] = None  # Calendar spread P&L

    # Computed metrics
    @property
    def crush_atm_pct(self) -> float:
        """IV crush magnitude: (pre - post) / pre."""
        if self.pre_atm_iv == 0:
            return 0.0
        return (self.pre_atm_iv - self.post_atm_iv) / self.pre_atm_iv

    @property
    def crush_25d_pct(self) -> float:
        """Average 25Δ IV crush."""
        pre_avg = (self.pre_25d_put_iv + self.pre_25d_call_iv) / 2
        post_avg = (self.post_25d_put_iv + self.post_25d_call_iv) / 2
        if pre_avg == 0:
            return 0.0
        return (pre_avg - post_avg) / pre_avg

    @property
    def rr_compression_pct(self) -> float:
        """How much did the risk reversal compress?"""
        if self.pre_rr_25d == 0:
            return 0.0
        return (self.pre_rr_25d - self.post_rr_25d) / self.pre_rr_25d

    @property
    def move_vs_expected(self) -> float:
        """Ratio of actual move to options expected move."""
        if self.options_expected_move_pct == 0:
            return 1.0
        return abs(self.stock_gap_pct) / self.options_expected_move_pct

    @property
    def wings_breached_at_1x(self) -> bool:
        """Would wings at 1.0× expected move have been breached?"""
        return abs(self.stock_gap_pct) > self.options_expected_move_pct

    @property
    def wings_breached_at_1_2x(self) -> bool:
        """Would wings at 1.2× expected move have been breached?"""
        return abs(self.stock_gap_pct) > self.options_expected_move_pct * 1.2


@dataclass
class TickerIVBaseline:
    """
    Derived statistical baseline for a ticker from its earnings history.
    Used by the signal engine for edge quantification and calibration.
    """
    ticker: str
    n_cycles: int
    last_updated: datetime

    # IV levels
    median_pre_atm_iv: float
    median_post_atm_iv: float
    median_pre_rr_25d: float
    median_post_rr_25d: float
    median_pre_bf_25d: float
    median_post_bf_25d: float
    median_pre_ts_ratio: float
    median_post_ts_ratio: float

    # Crush statistics
    median_crush_atm_pct: float
    std_crush_atm_pct: float
    crush_consistency: float        # Fraction of cycles where crush > 30%
    min_observed_crush_pct: float
    max_observed_crush_pct: float

    # Risk reversal statistics
    median_rr_compression_pct: float  # How much RR compresses post-earnings
    rr_compression_consistency: float  # Fraction where RR compresses > 40%

    # Expected move accuracy
    median_em_accuracy: float         # Median |actual_move| / |expected_move|
    em_accuracy_std: float
    options_overprice_rate: float     # Fraction where EM > actual move (options rich)

    # Wing breach probabilities (critical for sizing)
    p_breach_1x_em: float             # P(gap > 1.0× expected move)
    p_breach_1_1x_em: float           # P(gap > 1.1× expected move)
    p_breach_1_2x_em: float           # P(gap > 1.2× expected move)
    p_breach_1_5x_em: float           # P(gap > 1.5× expected move)

    # Historical P&L for reference strategies
    condor_win_rate: Optional[float] = None
    condor_avg_return_pct: Optional[float] = None
    rr_win_rate: Optional[float] = None

    def to_signal_dict(self) -> dict:
        """Convert to dict format used by signal engine."""
        return {
            "n_cycles": self.n_cycles,
            "median_pre_earnings_rr_25d": self.median_pre_rr_25d,
            "median_post_earnings_rr_25d": self.median_post_rr_25d,
            "median_pre_earnings_bf_25d": self.median_pre_bf_25d,
            "median_post_earnings_bf_25d": self.median_post_bf_25d,
            "median_crush_pct": self.median_crush_atm_pct,
            "crush_consistency": self.crush_consistency,
            "options_overprice_rate": self.options_overprice_rate,
            "p_breach_1_2x": self.p_breach_1_2x_em,
        }


class IVCrushDatabase:
    """
    Historical IV Crush Database.

    Stores and analyzes per-ticker, per-cycle earnings IV data.
    Computes statistical baselines used for edge quantification.
    """

    def __init__(self, db_path: str = "data/iv_crush_history.db"):
        self.db_path = db_path
        self._cache: Dict[str, TickerIVBaseline] = {}
        self._records: Dict[str, List[EarningsIVRecord]] = {}

    async def initialize(self):
        """Initialize database connection and create tables if needed."""
        try:
            import aiosqlite
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS earnings_iv_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticker TEXT NOT NULL,
                        earnings_date TEXT NOT NULL,
                        timing TEXT,
                        fiscal_quarter TEXT,
                        pre_atm_iv REAL,
                        pre_25d_put_iv REAL,
                        pre_25d_call_iv REAL,
                        pre_10d_put_iv REAL,
                        pre_10d_call_iv REAL,
                        pre_rr_25d REAL,
                        pre_bf_25d REAL,
                        pre_ts_ratio REAL,
                        pre_iv_rank REAL,
                        options_expected_move_pct REAL,
                        post_atm_iv REAL,
                        post_25d_put_iv REAL,
                        post_25d_call_iv REAL,
                        post_rr_25d REAL,
                        post_bf_25d REAL,
                        post_ts_ratio REAL,
                        stock_gap_pct REAL,
                        stock_move_surprise_pct REAL,
                        eps_reported REAL,
                        eps_estimate REAL,
                        eps_beat_pct REAL,
                        condor_pnl_pct REAL,
                        rr_pnl_pct REAL,
                        calendar_pnl_pct REAL,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(ticker, earnings_date)
                    )
                """)
                await db.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ticker_date
                    ON earnings_iv_history(ticker, earnings_date)
                """)
                await db.commit()
            log.info("iv_database.initialized", path=self.db_path)
        except Exception as e:
            log.error("iv_database.init_failed", error=str(e))
            raise

    async def get_ticker_baseline(self, ticker: str) -> Optional[dict]:
        """
        Get the statistical baseline for a ticker.
        Returns None if insufficient history exists.
        """
        if ticker in self._cache:
            baseline = self._cache[ticker]
            if baseline.n_cycles >= 8:  # Minimum viable history
                return baseline.to_signal_dict()

        # Load from database
        records = await self._load_records(ticker)
        if len(records) < 8:
            log.warning("iv_database.insufficient_history",
                        ticker=ticker,
                        n_cycles=len(records),
                        minimum=8)
            return None

        baseline = self._compute_baseline(ticker, records)
        self._cache[ticker] = baseline
        return baseline.to_signal_dict()

    async def record_pre_earnings(self, record: EarningsIVRecord):
        """Save pre-earnings IV snapshot. Call 1 day before earnings."""
        try:
            import aiosqlite
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO earnings_iv_history
                    (ticker, earnings_date, timing, fiscal_quarter,
                     pre_atm_iv, pre_25d_put_iv, pre_25d_call_iv,
                     pre_10d_put_iv, pre_10d_call_iv,
                     pre_rr_25d, pre_bf_25d, pre_ts_ratio, pre_iv_rank,
                     options_expected_move_pct)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    record.ticker, record.earnings_date, record.timing,
                    record.fiscal_quarter, record.pre_atm_iv,
                    record.pre_25d_put_iv, record.pre_25d_call_iv,
                    record.pre_10d_put_iv, record.pre_10d_call_iv,
                    record.pre_rr_25d, record.pre_bf_25d,
                    record.pre_ts_ratio, record.pre_iv_rank,
                    record.options_expected_move_pct,
                ))
                await db.commit()
            log.info("iv_database.pre_earnings_recorded",
                     ticker=record.ticker, date=record.earnings_date)
        except Exception as e:
            log.error("iv_database.record_failed", error=str(e), ticker=record.ticker)

    async def record_post_earnings(
        self,
        ticker: str,
        earnings_date: str,
        post_atm_iv: float,
        post_25d_put_iv: float,
        post_25d_call_iv: float,
        post_rr_25d: float,
        post_bf_25d: float,
        post_ts_ratio: float,
        stock_gap_pct: float,
        eps_reported: Optional[float] = None,
        eps_estimate: Optional[float] = None,
    ):
        """Update record with post-earnings actuals. Call morning after earnings."""
        try:
            import aiosqlite
            eps_beat_pct = None
            if eps_reported and eps_estimate and eps_estimate != 0:
                eps_beat_pct = (eps_reported - eps_estimate) / abs(eps_estimate)

            # Get pre-earnings EM for surprise calculation
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT options_expected_move_pct FROM earnings_iv_history "
                    "WHERE ticker = ? AND earnings_date = ?",
                    (ticker, earnings_date)
                )
                row = await cursor.fetchone()
                em_pct = row[0] if row else 0
                surprise_pct = abs(stock_gap_pct) - abs(em_pct)

                await db.execute("""
                    UPDATE earnings_iv_history
                    SET post_atm_iv = ?, post_25d_put_iv = ?, post_25d_call_iv = ?,
                        post_rr_25d = ?, post_bf_25d = ?, post_ts_ratio = ?,
                        stock_gap_pct = ?, stock_move_surprise_pct = ?,
                        eps_reported = ?, eps_estimate = ?, eps_beat_pct = ?
                    WHERE ticker = ? AND earnings_date = ?
                """, (
                    post_atm_iv, post_25d_put_iv, post_25d_call_iv,
                    post_rr_25d, post_bf_25d, post_ts_ratio,
                    stock_gap_pct, surprise_pct,
                    eps_reported, eps_estimate, eps_beat_pct,
                    ticker, earnings_date
                ))
                await db.commit()

            # Invalidate cache
            self._cache.pop(ticker, None)
            log.info("iv_database.post_earnings_recorded",
                     ticker=ticker, date=earnings_date,
                     gap=f"{stock_gap_pct:.2%}")
        except Exception as e:
            log.error("iv_database.post_record_failed", error=str(e), ticker=ticker)

    def _compute_baseline(
        self, ticker: str, records: List[EarningsIVRecord]
    ) -> TickerIVBaseline:
        """Compute statistical baseline from historical records."""

        def safe_median(values) -> float:
            vals = [v for v in values if v is not None and not np.isnan(v)]
            return float(np.median(vals)) if vals else 0.0

        def safe_std(values) -> float:
            vals = [v for v in values if v is not None and not np.isnan(v)]
            return float(np.std(vals)) if len(vals) > 1 else 0.0

        pre_atm_ivs = [r.pre_atm_iv for r in records]
        post_atm_ivs = [r.post_atm_iv for r in records]
        crushes = [r.crush_atm_pct for r in records]
        rr_pre = [r.pre_rr_25d for r in records]
        rr_post = [r.post_rr_25d for r in records]
        bf_pre = [r.pre_bf_25d for r in records]
        bf_post = [r.post_bf_25d for r in records]
        ts_pre = [r.pre_ts_ratio for r in records]
        ts_post = [r.post_ts_ratio for r in records]
        move_vs_em = [r.move_vs_expected for r in records]
        gaps = [abs(r.stock_gap_pct) for r in records]
        em_pcts = [r.options_expected_move_pct for r in records]

        # Wing breach rates
        n = len(records)
        breach_1x = sum(1 for r in records if r.wings_breached_at_1x) / n
        breach_1_2x = sum(1 for r in records if r.wings_breached_at_1_2x) / n

        # Approximations for other levels
        breach_1_1x = sum(
            1 for g, e in zip(gaps, em_pcts)
            if e > 0 and g > e * 1.1
        ) / n
        breach_1_5x = sum(
            1 for g, e in zip(gaps, em_pcts)
            if e > 0 and g > e * 1.5
        ) / n

        crush_consistency = sum(1 for c in crushes if c > 0.30) / n
        rr_compression_consistency = sum(
            1 for r in records if r.rr_compression_pct > 0.40
        ) / n
        options_overprice_rate = sum(1 for m in move_vs_em if m < 1.0) / n

        return TickerIVBaseline(
            ticker=ticker,
            n_cycles=n,
            last_updated=datetime.now(),
            median_pre_atm_iv=safe_median(pre_atm_ivs),
            median_post_atm_iv=safe_median(post_atm_ivs),
            median_pre_rr_25d=safe_median(rr_pre),
            median_post_rr_25d=safe_median(rr_post),
            median_pre_bf_25d=safe_median(bf_pre),
            median_post_bf_25d=safe_median(bf_post),
            median_pre_ts_ratio=safe_median(ts_pre),
            median_post_ts_ratio=safe_median(ts_post),
            median_crush_atm_pct=safe_median(crushes),
            std_crush_atm_pct=safe_std(crushes),
            crush_consistency=crush_consistency,
            min_observed_crush_pct=min(crushes) if crushes else 0.0,
            max_observed_crush_pct=max(crushes) if crushes else 0.0,
            median_rr_compression_pct=safe_median(
                [r.rr_compression_pct for r in records]
            ),
            rr_compression_consistency=rr_compression_consistency,
            median_em_accuracy=safe_median(move_vs_em),
            em_accuracy_std=safe_std(move_vs_em),
            options_overprice_rate=options_overprice_rate,
            p_breach_1x_em=breach_1x,
            p_breach_1_1x_em=breach_1_1x,
            p_breach_1_2x_em=breach_1_2x,
            p_breach_1_5x_em=breach_1_5x,
        )

    async def _load_records(self, ticker: str) -> List[EarningsIVRecord]:
        """Load all historical records for a ticker from the database."""
        try:
            import aiosqlite
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    """
                    SELECT * FROM earnings_iv_history
                    WHERE ticker = ?
                    AND post_atm_iv IS NOT NULL
                    AND stock_gap_pct IS NOT NULL
                    ORDER BY earnings_date DESC
                    """,
                    (ticker,)
                )
                rows = await cursor.fetchall()

            records = []
            for row in rows:
                try:
                    record = EarningsIVRecord(
                        ticker=row["ticker"],
                        earnings_date=row["earnings_date"],
                        timing=row["timing"] or "AMC",
                        fiscal_quarter=row["fiscal_quarter"] or "",
                        pre_atm_iv=row["pre_atm_iv"] or 0.0,
                        pre_25d_put_iv=row["pre_25d_put_iv"] or 0.0,
                        pre_25d_call_iv=row["pre_25d_call_iv"] or 0.0,
                        pre_10d_put_iv=row["pre_10d_put_iv"] or 0.0,
                        pre_10d_call_iv=row["pre_10d_call_iv"] or 0.0,
                        pre_rr_25d=row["pre_rr_25d"] or 0.0,
                        pre_bf_25d=row["pre_bf_25d"] or 0.0,
                        pre_ts_ratio=row["pre_ts_ratio"] or 1.0,
                        pre_iv_rank=row["pre_iv_rank"] or 0.0,
                        options_expected_move_pct=row["options_expected_move_pct"] or 0.0,
                        post_atm_iv=row["post_atm_iv"] or 0.0,
                        post_25d_put_iv=row["post_25d_put_iv"] or 0.0,
                        post_25d_call_iv=row["post_25d_call_iv"] or 0.0,
                        post_rr_25d=row["post_rr_25d"] or 0.0,
                        post_bf_25d=row["post_bf_25d"] or 0.0,
                        post_ts_ratio=row["post_ts_ratio"] or 1.0,
                        stock_gap_pct=row["stock_gap_pct"] or 0.0,
                        stock_move_surprise_pct=row["stock_move_surprise_pct"] or 0.0,
                        eps_reported=row["eps_reported"],
                        eps_estimate=row["eps_estimate"],
                        eps_beat_pct=row["eps_beat_pct"],
                        condor_pnl_pct=row["condor_pnl_pct"],
                        rr_pnl_pct=row["rr_pnl_pct"],
                        calendar_pnl_pct=row["calendar_pnl_pct"],
                    )
                    records.append(record)
                except Exception as e:
                    log.warning("iv_database.record_parse_error",
                                error=str(e), ticker=ticker)
                    continue

            return records

        except Exception as e:
            log.error("iv_database.load_failed", ticker=ticker, error=str(e))
            return []

    async def generate_report(self, ticker: str) -> str:
        """Generate a human-readable report for a ticker's IV crush history."""
        records = await self._load_records(ticker)
        if not records:
            return f"No history for {ticker}"

        baseline = self._compute_baseline(ticker, records)

        report = f"""
╔══════════════════════════════════════════════════════╗
║  IV CRUSH DATABASE REPORT: {ticker:<26}║
╚══════════════════════════════════════════════════════╝

Historical Cycles Analyzed: {baseline.n_cycles}
Data Period: {records[-1].earnings_date} → {records[0].earnings_date}

── IV CRUSH STATISTICS ────────────────────────────────
  Median Pre-Earnings ATM IV:    {baseline.median_pre_atm_iv:.1%}
  Median Post-Earnings ATM IV:   {baseline.median_post_atm_iv:.1%}
  Median IV Crush:               {baseline.median_crush_atm_pct:.1%}
  Crush Std Dev:                 {baseline.std_crush_atm_pct:.1%}
  Crush > 30% Rate:              {baseline.crush_consistency:.0%}
  Min/Max Crush:                 {baseline.min_observed_crush_pct:.1%} / {baseline.max_observed_crush_pct:.1%}

── SKEW STATISTICS ────────────────────────────────────
  Median Pre-Earnings RR (25Δ):  {baseline.median_pre_rr_25d:.1%}
  Median Post-Earnings RR (25Δ): {baseline.median_post_rr_25d:.1%}
  RR Compression > 40% Rate:     {baseline.rr_compression_consistency:.0%}

── EXPECTED MOVE ACCURACY ─────────────────────────────
  Median EM Accuracy Ratio:      {baseline.median_em_accuracy:.2f}x
  (< 1.0 = options overpriced the move)
  Options Overpriced Move Rate:  {baseline.options_overprice_rate:.0%}

── GAP / WING RISK ────────────────────────────────────
  P(gap > 1.0× expected move):  {baseline.p_breach_1x_em:.0%}
  P(gap > 1.1× expected move):  {baseline.p_breach_1_1x_em:.0%}
  P(gap > 1.2× expected move):  {baseline.p_breach_1_2x_em:.0%}
  P(gap > 1.5× expected move):  {baseline.p_breach_1_5x_em:.0%}

── RECOMMENDATION ─────────────────────────────────────
  Min Wing Multiplier Needed:    {1.0 / (1 - baseline.p_breach_1_2x_em):.2f}×
  Strategy Suitability:          {'✓ HIGH' if baseline.crush_consistency > 0.70 else '△ MEDIUM' if baseline.crush_consistency > 0.50 else '✗ LOW'}
        """
        return report.strip()
