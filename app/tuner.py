"""
AxeForge Tuning Engine
======================
State machine per miner:
  IDLE → APPLYING → OBSERVING → EVALUATING → (loop or COMPLETE)
  Any state → SAFETY_BACKOFF  (on hard threshold breach during observation)
  Any state → COMPLETE        (on stop / time limit / space exhausted)

Stability scoring considers:
  - Hashrate:    mean, coefficient of variation, trend, alignment with 1m rolling avg
  - Temperature: mean, trend (rising = thermal saturation), variance
  - Error rate:  mean, peak spike, trend, statistical confidence (share count)
  - Power:       variance, efficiency (GH/W)
  - Confidence:  sample count, share count, window length adequacy
"""

import asyncio
import bisect
import logging
import math
import time
import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from database import Database
from miner_client import MinerClient

logger = logging.getLogger(__name__)

# ── Hard ceilings (immutable) ─────────────────────────────────────────────────
HARD_MAX_TEMP     = 80.0
HARD_MAX_VRM_TEMP = 90.0
HARD_MAX_VOLTAGE  = 1400
HARD_MIN_FREQ     = 400
HARD_MAX_FREQ     = 1100

# ── Voltage step sizes ────────────────────────────────────────────────────────
STEP_VOLT = {"fast": 20, "slow": 10}   # mV

# ── BM1370 hardware frequency table ──────────────────────────────────────────
# Valid frequencies are 25*N/D MHz for D ∈ {1,2,3,4,6,8}.
# D=4 and D=8 are only valid up to 737.5 MHz (hardware PLL limit); above that
# only D ∈ {1,2,3,6} produce valid register values, giving ~4.17 MHz spacing.
def _build_freq_table() -> List[float]:
    seen: set = set()
    for d in [1, 2, 3, 4, 6, 8]:
        n_min = math.ceil(HARD_MIN_FREQ * d / 25)
        n_max = math.floor(HARD_MAX_FREQ * d / 25)
        for n in range(n_min, n_max + 1):
            f = round(25.0 * n / d, 6)
            if f < HARD_MIN_FREQ or f > HARD_MAX_FREQ:
                continue
            if d in (4, 8) and f > 737.5 + 1e-9:
                continue
            seen.add(f)
    return sorted(seen)

BM1370_FREQ_TABLE: List[float] = _build_freq_table()

# How many table entries to skip per step in each mode
FREQ_FAST_STEP = 6   # ≈ 12.5 MHz per step during initial discovery
FREQ_SLOW_STEP = 1   # single hardware step (~1–4 MHz) for precision tuning


def _freq_snap(f: float) -> float:
    """Return the nearest valid BM1370 frequency to f."""
    if not BM1370_FREQ_TABLE:
        return f
    idx = bisect.bisect_left(BM1370_FREQ_TABLE, f)
    if idx == 0:
        return BM1370_FREQ_TABLE[0]
    if idx >= len(BM1370_FREQ_TABLE):
        return BM1370_FREQ_TABLE[-1]
    lo, hi = BM1370_FREQ_TABLE[idx - 1], BM1370_FREQ_TABLE[idx]
    return hi if (hi - f) < (f - lo) else lo


def _freq_idx(f: float) -> int:
    """Return the table index of the nearest entry to f."""
    snapped = _freq_snap(f)
    idx = bisect.bisect_left(BM1370_FREQ_TABLE, snapped)
    # bisect may land on the exact value or one past it
    if idx < len(BM1370_FREQ_TABLE) and abs(BM1370_FREQ_TABLE[idx] - snapped) < 0.001:
        return idx
    if idx > 0 and abs(BM1370_FREQ_TABLE[idx - 1] - snapped) < 0.001:
        return idx - 1
    return max(0, min(idx, len(BM1370_FREQ_TABLE) - 1))


def _freq_next(f: float, steps: int = 1) -> Optional[float]:
    """Return the frequency `steps` table entries above f, or None if at/past ceiling."""
    new_idx = _freq_idx(f) + steps
    if new_idx >= len(BM1370_FREQ_TABLE):
        return None
    return BM1370_FREQ_TABLE[new_idx]


def _freq_prev(f: float, steps: int = 1) -> float:
    """Return the frequency `steps` table entries below f (clamped to HARD_MIN_FREQ)."""
    return BM1370_FREQ_TABLE[max(0, _freq_idx(f) - steps)]

# ── Observation windows by primary priority (seconds) ─────────────────────────
OBS_WINDOW = {"hashrate": 60, "temp": 120, "error_rate": 45}

# ── Stability thresholds ──────────────────────────────────────────────────────
MIN_SAMPLES_TRUST   = 20      # per-second readings before trusting result
MIN_SHARES_TRUST    = 5       # share delta before trusting error rate
HR_CV_EXCELLENT     = 0.02    # hashrate coefficient of variation tiers
HR_CV_GOOD          = 0.05
HR_CV_MARGINAL      = 0.10
HR_CV_POOR          = 0.15
TEMP_TREND_FINE     = 1.0     # °C rise over window: fine
TEMP_TREND_MARGINAL = 3.0     # °C: start penalising
TEMP_TREND_BAD      = 6.0     # °C: thermal saturation
NEAR_LIMIT_FRACTION = 0.88    # fraction of ceiling to trigger slow-mode switch


class TunerState(str, Enum):
    IDLE           = "idle"
    APPLYING       = "applying"
    RESTARTING     = "restarting"   # kept for completeness, never entered
    OBSERVING      = "observing"
    EVALUATING     = "evaluating"
    SAFETY_BACKOFF = "safety_backoff"
    COMPLETE       = "complete"
    ERROR          = "error"


@dataclass
class TunerConfig:
    priority:           List[str]
    step_mode:          str
    max_temp:           float
    max_vrm_temp:       float
    max_voltage:        int
    max_freq:           float
    error_threshold:    float
    time_limit_minutes: Optional[int]
    baseline_freq:      float
    baseline_voltage:   int


@dataclass
class StabilityResult:
    """Full stability analysis for one observation window."""
    # Composite
    score:              float   # 0-1, performance × stability
    stability_mult:     float   # 0-1, stability penalty
    performance_score:  float   # 0-1, priority-weighted performance
    confidence:         float   # 0-1, trust in the result

    # Hashrate
    avg_hashrate:       float
    hashrate_cv:        float   # std / mean
    hashrate_trend_pct: float   # % change first-third → last-third
    hashrate_vs_1m:     float   # % deviation from 1-min rolling avg

    # Temperature
    avg_temp:           float
    temp_trend:         float   # °C change start → end of window
    temp_variance:      float   # std dev
    avg_vrm_temp:       float
    vrm_temp_trend:     float
    vrm_temp_headroom:  float   # °C below ceiling

    # Error rate
    avg_error_rate:     float
    peak_error_rate:    float
    error_trend:        float   # positive = worsening
    share_delta:        int
    shares_accepted:    int     # accepted shares submitted during this window
    shares_rejected:    int     # rejected shares submitted during this window

    # Power
    avg_power:          float
    efficiency_gh_w:    float
    power_cv:           float

    # Verdict
    verdict:            str         # stable | marginal | unstable | critical
    should_proceed:     bool
    backoff_steps:      int         # 0, 1, or 2
    reasons:            List[str]


@dataclass
class Trial:
    freq:       int
    voltage:    int
    hashrate:   float
    temp:       float
    error_rate: float
    score:      float
    verdict:    str


@dataclass
class TunerStatus:
    state:        TunerState = TunerState.IDLE
    message:      str        = "Idle"
    step_mode:    str        = "slow"
    restarting:   bool       = False

    current_freq:    float = 0.0
    current_voltage: int   = 0
    best_freq:       float = 0.0
    best_voltage:    int   = 0
    best_hashrate:   float = 0.0
    best_temp:       float = 0.0
    best_error_rate: float = 0.0
    best_score:      float = -1.0

    # Per-metric bests — independent of composite score, only on stable/marginal verdicts
    best_hr_freq:    float = 0.0
    best_hr_volt:    int   = 0
    best_hr_value:   float = -1.0   # -1 = not yet recorded
    best_temp_freq:  float = 0.0
    best_temp_volt:  int   = 0
    best_temp_value: float = -1.0
    best_err_freq:   float = 0.0
    best_err_volt:   int   = 0
    best_err_value:  float = -1.0
    best_err_shares_acc: int = 0
    best_err_shares_rej: int = 0

    session_id:  Optional[int]        = None
    start_time:  Optional[float]      = None
    config:      Optional[TunerConfig] = None

    # Per-second observation accumulators
    obs_hashrates:      List[float] = field(default_factory=list)
    obs_hashrates_1m:   List[float] = field(default_factory=list)
    obs_temps:          List[float] = field(default_factory=list)
    obs_vrm_temps:      List[float] = field(default_factory=list)
    obs_error_pcts:     List[float] = field(default_factory=list)
    obs_powers:         List[float] = field(default_factory=list)
    obs_fanspeeds:      List[float] = field(default_factory=list)
    obs_shares_acc_start: int = 0
    obs_shares_rej_start: int = 0
    obs_shares_acc_end:   int = 0
    obs_shares_rej_end:   int = 0
    obs_window_secs:      int = 60

    trials: List[Trial] = field(default_factory=list)

    _safety_breach:  bool = False
    _stop_requested: bool = False
    _stop_event:     Optional[asyncio.Event] = field(default=None, repr=False)


# ── Stability analyser ────────────────────────────────────────────────────────

def analyze_stability(s: TunerStatus, cfg: TunerConfig) -> StabilityResult:
    """Pure function — analyses collected observation data and returns a verdict."""

    hrs    = s.obs_hashrates
    hrs_1m = s.obs_hashrates_1m
    temps  = s.obs_temps
    errs   = s.obs_error_pcts
    pwrs   = s.obs_powers
    n      = len(hrs)
    reasons: List[str] = []

    # ── 1. Confidence modifier ────────────────────────────────────────────────
    confidence = 1.0
    if n < MIN_SAMPLES_TRUST:
        confidence *= max(0.3, n / MIN_SAMPLES_TRUST)
        reasons.append(f"Low sample count ({n} of {MIN_SAMPLES_TRUST} ideal)")

    acc_delta   = max(0, s.obs_shares_acc_end - s.obs_shares_acc_start)
    rej_delta   = max(0, s.obs_shares_rej_end - s.obs_shares_rej_start)
    share_delta = acc_delta + rej_delta
    err_conf    = 1.0
    if share_delta < MIN_SHARES_TRUST:
        err_conf = max(0.3, share_delta / MIN_SHARES_TRUST)
        reasons.append(f"Only {share_delta} shares submitted — error rate estimate low confidence")

    # ── 2. Hashrate analysis ──────────────────────────────────────────────────
    avg_hr   = _mean(hrs)
    cv_hr    = _stdev(hrs) / avg_hr if avg_hr > 0 else 1.0
    hr_trend = _trend_pct(hrs)

    avg_hr_1m    = _mean(hrs_1m) if hrs_1m else avg_hr
    hr_vs_1m_pct = abs(avg_hr - avg_hr_1m) / avg_hr_1m * 100 if avg_hr_1m > 0 else 0

    if cv_hr <= HR_CV_EXCELLENT:
        hr_stab = 1.0
    elif cv_hr <= HR_CV_GOOD:
        hr_stab = 0.85
    elif cv_hr <= HR_CV_MARGINAL:
        hr_stab = 0.60
    elif cv_hr <= HR_CV_POOR:
        hr_stab = 0.35
    else:
        hr_stab = 0.10
        reasons.append(f"Hashrate highly variable (CV={cv_hr:.3f})")

    if hr_trend < -5.0:
        hr_stab *= 0.55
        reasons.append(f"Hashrate declining ({hr_trend:.1f}% over window)")
    elif hr_trend < -2.0:
        hr_stab *= 0.80

    if hr_vs_1m_pct > 15:
        hr_stab *= 0.70
        reasons.append(f"Live rate {hr_vs_1m_pct:.1f}% below 1-min rolling avg")
    elif hr_vs_1m_pct > 8:
        hr_stab *= 0.88

    # ── 3. Temperature analysis ───────────────────────────────────────────────
    avg_tmp   = _mean(temps)
    std_tmp   = _stdev(temps)
    tmp_trend = _trend_abs(temps)

    eff_max_tmp  = min(cfg.max_temp, HARD_MAX_TEMP)
    headroom_pct = (eff_max_tmp - avg_tmp) / eff_max_tmp if eff_max_tmp > 0 else 0

    if headroom_pct >= 0.20:
        tmp_stab = 1.0
    elif headroom_pct >= 0.12:
        tmp_stab = 0.80
    elif headroom_pct >= 0.06:
        tmp_stab = 0.50
    else:
        tmp_stab = 0.20
        reasons.append(f"Temp {avg_tmp:.1f}°C close to ceiling {eff_max_tmp:.0f}°C")

    if tmp_trend >= TEMP_TREND_BAD:
        tmp_stab *= 0.30
        reasons.append(f"Temp rising rapidly (+{tmp_trend:.1f}°C) — thermal saturation likely")
    elif tmp_trend >= TEMP_TREND_MARGINAL:
        tmp_stab *= 0.60
        reasons.append(f"Temp trending up (+{tmp_trend:.1f}°C during window)")
    elif tmp_trend >= TEMP_TREND_FINE:
        tmp_stab *= 0.85

    if std_tmp > 3.0:
        tmp_stab *= 0.75
        reasons.append(f"Temperature unstable (σ={std_tmp:.1f}°C — possible throttling)")

    # ── 4. VRM temperature analysis ───────────────────────────────────────────
    vrm_temps  = s.obs_vrm_temps
    avg_vrm    = _mean(vrm_temps) if vrm_temps else 0.0
    vrm_trend  = _trend_abs(vrm_temps) if vrm_temps else 0.0
    eff_max_vrm = min(cfg.max_vrm_temp, HARD_MAX_VRM_TEMP)
    vrm_headroom = eff_max_vrm - avg_vrm

    if avg_vrm <= 0:
        # VRM temp not reported by this miner — don't penalise
        vrm_stab = 1.0
    elif vrm_headroom >= eff_max_vrm * 0.20:
        vrm_stab = 1.0
    elif vrm_headroom >= eff_max_vrm * 0.12:
        vrm_stab = 0.80
    elif vrm_headroom >= eff_max_vrm * 0.06:
        vrm_stab = 0.50
        reasons.append(f"VRM temp {avg_vrm:.1f}°C close to ceiling {eff_max_vrm:.0f}°C")
    else:
        vrm_stab = 0.15
        reasons.append(f"VRM temp {avg_vrm:.1f}°C critically close to ceiling {eff_max_vrm:.0f}°C")

    if vrm_trend >= TEMP_TREND_BAD:
        vrm_stab *= 0.35
        reasons.append(f"VRM temp rising rapidly (+{vrm_trend:.1f}°C) — thermal issue")
    elif vrm_trend >= TEMP_TREND_MARGINAL:
        vrm_stab *= 0.65
        reasons.append(f"VRM temp trending up (+{vrm_trend:.1f}°C)")
    elif vrm_trend >= TEMP_TREND_FINE:
        vrm_stab *= 0.88

    # ── 4b. Fan speed headroom modifier ──────────────────────────────────────
    # If the fan has room to spin up further and VRM temps aren't rising, the
    # thermal situation has real headroom — soften the VRM penalty slightly.
    # If the fan IS increasing but VRM temps are still climbing, the fan can't
    # keep up: treat that as full-weight (no bonus applied).
    fan_speeds = s.obs_fanspeeds
    if fan_speeds:
        avg_fan = _mean(fan_speeds)
        fan_trend = _trend_abs(fan_speeds)
        fan_headroom = max(0.0, (100.0 - avg_fan) / 100.0)  # 0 at 100%, 1 at 0%
        # Only apply the bonus when the fan isn't spinning up to compensate
        fan_compensating = fan_trend > 5.0 and vrm_trend > TEMP_TREND_FINE
        if not fan_compensating and avg_vrm > 0 and fan_headroom > 0.15:
            # Soft upward modifier: up to +15% on vrm_stab, capped at 1.0
            vrm_stab = min(1.0, vrm_stab * (1.0 + 0.15 * fan_headroom))

    # ── 5. Error rate analysis ────────────────────────────────────────────────
    if share_delta >= MIN_SHARES_TRUST:
        win_err = (rej_delta / share_delta * 100.0) if share_delta > 0 else 0.0
    else:
        win_err = _mean(errs)

    peak_err  = max(errs) if errs else 0.0
    err_trend = _trend_abs(errs)
    eff_thresh = cfg.error_threshold

    if win_err <= eff_thresh * 0.30:
        err_stab = 1.0
    elif win_err <= eff_thresh * 0.60:
        err_stab = 0.85
    elif win_err <= eff_thresh:
        err_stab = 0.65
    elif win_err <= eff_thresh * 1.5:
        err_stab = 0.30
        reasons.append(f"Error rate {win_err:.2f}% exceeds threshold {eff_thresh:.1f}%")
    else:
        err_stab = 0.0
        reasons.append(f"Error rate {win_err:.2f}% critically high")

    if peak_err >= eff_thresh * 3:
        err_stab *= 0.40
        reasons.append(f"Error rate spiked to {peak_err:.2f}% — ASIC instability")
    elif peak_err >= eff_thresh * 1.5:
        err_stab *= 0.65
        reasons.append(f"Error rate spike detected ({peak_err:.2f}%)")

    if err_trend > 0.5:
        err_stab *= 0.75
        reasons.append("Error rate trending upward during window")

    err_stab = err_stab * err_conf

    # ── 6. Power analysis ─────────────────────────────────────────────────────
    avg_pwr  = _mean(pwrs)
    cv_pwr   = _stdev(pwrs) / avg_pwr if avg_pwr > 0 else 0
    eff_gh_w = avg_hr / avg_pwr if avg_pwr > 0 else 0

    pwr_stab = 1.0
    if cv_pwr > 0.10:
        pwr_stab = 0.70
        reasons.append(f"Power draw unstable (CV={cv_pwr:.3f}) — possible ASIC issue")
    elif cv_pwr > 0.05:
        pwr_stab = 0.88

    # ── 7. Composite stability multiplier ─────────────────────────────────────
    # Weights have a safety floor for every component, then a priority bonus
    # pool redistributes influence based on the user's priority ordering.
    # "temp" priority bonus is split 60/40 between ASIC temp and VRM temp.
    # All 6 priority permutations sum to exactly 1.0.
    _w_hr  = 0.12
    _w_tmp = 0.12
    _w_vrm = 0.10
    _w_err = 0.10
    _w_pwr = 0.06
    _bonus = {0: 0.30, 1: 0.15, 2: 0.05}
    for _rank, _metric in enumerate(cfg.priority):
        _b = _bonus[_rank]
        if _metric == "hashrate":
            _w_hr  += _b
        elif _metric == "temp":
            _w_tmp += _b * 0.60
            _w_vrm += _b * 0.40
        elif _metric == "error_rate":
            _w_err += _b

    stability_mult = max(0.0, min(1.0,
        _w_tmp * tmp_stab +
        _w_hr  * hr_stab  +
        _w_vrm * vrm_stab +
        _w_err * err_stab +
        _w_pwr * pwr_stab
    ) * confidence)

    # ── 8. Performance score ──────────────────────────────────────────────────
    weights = {
        cfg.priority[0]: 0.60,
        cfg.priority[1]: 0.30,
        cfg.priority[2]: 0.10,
    }
    norm_hr  = min(avg_hr / 2000.0, 1.0)
    norm_tmp = max(0.0, 1.0 - avg_tmp / eff_max_tmp)
    norm_err = max(0.0, 1.0 - win_err / max(eff_thresh, 0.01))

    perf = (
        weights["hashrate"]   * norm_hr  +
        weights["temp"]       * norm_tmp +
        weights["error_rate"] * norm_err
    )

    final_score = round(perf * stability_mult, 4)

    # ── 9. Verdict ────────────────────────────────────────────────────────────
    hard_breach = (
        avg_tmp  >= eff_max_tmp or
        (avg_vrm > 0 and avg_vrm >= eff_max_vrm) or
        win_err  >= eff_thresh * 1.5
    )

    if hard_breach:
        verdict, should_proceed, backoff_steps = "critical", False, 2
        if avg_tmp >= eff_max_tmp:
            reasons.append(f"Hard ceiling breach: ASIC {avg_tmp:.1f}°C ≥ {eff_max_tmp:.0f}°C")
        if avg_vrm > 0 and avg_vrm >= eff_max_vrm:
            reasons.append(f"Hard ceiling breach: VRM {avg_vrm:.1f}°C ≥ {eff_max_vrm:.0f}°C")
        if win_err >= eff_thresh * 1.5:
            reasons.append(f"Hard ceiling breach: error {win_err:.2f}% ≥ {eff_thresh*1.5:.1f}%")
    elif stability_mult >= 0.75:
        verdict, should_proceed, backoff_steps = "stable", True, 0
        if not reasons:
            reasons.append("All metrics within healthy ranges")
    elif stability_mult >= 0.55:
        verdict, should_proceed, backoff_steps = "marginal", False, 0
        reasons.append("Marginal stability — holding, re-evaluating")
    elif stability_mult >= 0.35:
        verdict, should_proceed, backoff_steps = "unstable", False, 1
        reasons.append("Unstable — backing off one frequency step")
    else:
        verdict, should_proceed, backoff_steps = "critical", False, 2
        reasons.append("Critically unstable — backing off two steps")

    return StabilityResult(
        score=final_score, stability_mult=round(stability_mult,4),
        performance_score=round(perf,4), confidence=round(confidence,4),
        avg_hashrate=round(avg_hr,2), hashrate_cv=round(cv_hr,4),
        hashrate_trend_pct=round(hr_trend,2), hashrate_vs_1m=round(hr_vs_1m_pct,2),
        avg_temp=round(avg_tmp,1), temp_trend=round(tmp_trend,2),
        temp_variance=round(std_tmp,2),
        avg_vrm_temp=round(avg_vrm,1), vrm_temp_trend=round(vrm_trend,2),
        vrm_temp_headroom=round(vrm_headroom,1),
        avg_error_rate=round(win_err,3), peak_error_rate=round(peak_err,3),
        error_trend=round(err_trend,3), share_delta=share_delta,
        shares_accepted=acc_delta, shares_rejected=rej_delta,
        avg_power=round(avg_pwr,2), efficiency_gh_w=round(eff_gh_w,3),
        power_cv=round(cv_pwr,4),
        verdict=verdict, should_proceed=should_proceed,
        backoff_steps=backoff_steps, reasons=reasons,
    )


# ── Tuner manager ─────────────────────────────────────────────────────────────

class TunerManager:
    def __init__(self, db: Database):
        self.db = db
        self._statuses: Dict[int, TunerStatus] = {}

    def get_status(self, miner_id: int) -> Dict:
        s = self._statuses.get(miner_id, TunerStatus())
        remaining = None
        time_limit = s.config.time_limit_minutes if s.config else None
        if time_limit and s.start_time:
            remaining = max(0, int(time_limit * 60 - (time.time() - s.start_time)))
        return {
            "state":            s.state.value,
            "message":          s.message,
            "step_mode":        s.step_mode,
            "restarting":       False,
            "current_freq":     s.current_freq,
            "current_voltage":  s.current_voltage,
            "best_freq":        s.best_freq,
            "best_voltage":     s.best_voltage,
            "best_hashrate":    round(s.best_hashrate, 2),
            "best_temp":        round(s.best_temp, 1),
            "best_error_rate":  round(s.best_error_rate, 3),
            "session_id":       s.session_id,
            "time_remaining_s": remaining,
            # Per-metric bests (freq/volt that produced the best value for each metric)
            "best_hr_freq":    round(s.best_hr_freq, 3)  if s.best_hr_value  >= 0 else None,
            "best_hr_volt":    s.best_hr_volt             if s.best_hr_value  >= 0 else None,
            "best_hr_value":   round(s.best_hr_value, 2) if s.best_hr_value  >= 0 else None,
            "best_temp_freq":  round(s.best_temp_freq, 3)  if s.best_temp_value >= 0 else None,
            "best_temp_volt":  s.best_temp_volt             if s.best_temp_value >= 0 else None,
            "best_temp_value": round(s.best_temp_value, 1) if s.best_temp_value >= 0 else None,
            "best_err_freq":       round(s.best_err_freq, 3)  if s.best_err_value >= 0 else None,
            "best_err_volt":       s.best_err_volt             if s.best_err_value >= 0 else None,
            "best_err_value":      round(s.best_err_value, 3) if s.best_err_value >= 0 else None,
            "best_err_shares_acc": s.best_err_shares_acc       if s.best_err_value >= 0 else None,
            "best_err_shares_rej": s.best_err_shares_rej       if s.best_err_value >= 0 else None,
        }

    async def start_tuning(self, miner_id: int, config: TunerConfig) -> bool:
        existing = self._statuses.get(miner_id)
        if existing and existing.state not in (
            TunerState.IDLE, TunerState.COMPLETE, TunerState.ERROR
        ):
            return False
        s            = TunerStatus()
        s.config     = config
        s.step_mode  = config.step_mode
        s.start_time = time.time()
        s.state      = TunerState.APPLYING
        s.message    = "Starting…"
        self._statuses[miner_id] = s
        asyncio.create_task(self._run(miner_id))
        return True

    async def stop_tuning(self, miner_id: int):
        s = self._statuses.get(miner_id)
        if s:
            s._stop_requested = True
            if s._stop_event:
                s._stop_event.set()   # immediately interrupt the observation sleep

    # ── Called every second by the polling loop ───────────────────────────────

    def on_stats_update(self, miner_id: int, stats: Dict):
        s = self._statuses.get(miner_id)
        if not s or s.state != TunerState.OBSERVING:
            return
        s.obs_hashrates.append(   stats.get("hashRate",        0) or 0)
        s.obs_hashrates_1m.append(stats.get("hashRate_1m",     0) or
                                   stats.get("hashRate",        0) or 0)
        s.obs_temps.append(       stats.get("temp",            0) or 0)
        s.obs_vrm_temps.append(   stats.get("vrTemp",          0) or
                                   stats.get("boardtemp1",     0) or 0)
        s.obs_error_pcts.append(  stats.get("errorPercentage", 0) or 0)
        s.obs_powers.append(      stats.get("power",           0) or 0)
        s.obs_fanspeeds.append(   stats.get("fanspeed",        0) or 0)
        s.obs_shares_acc_end = stats.get("sharesAccepted", s.obs_shares_acc_start)
        s.obs_shares_rej_end = stats.get("sharesRejected", s.obs_shares_rej_start)

        cfg = s.config
        if cfg:
            asic_temp = stats.get("temp") or 0
            vrm_temp  = stats.get("vrTemp") or stats.get("boardtemp1") or 0
            if asic_temp >= min(cfg.max_temp, HARD_MAX_TEMP):
                s._safety_breach = True
                s.message = f"⚠ ASIC temp {asic_temp}°C hit ceiling — backing off"
                if s._stop_event:
                    s._stop_event.set()
            elif vrm_temp > 0 and vrm_temp >= min(cfg.max_vrm_temp, HARD_MAX_VRM_TEMP):
                s._safety_breach = True
                s.message = f"⚠ VRM temp {vrm_temp}°C hit ceiling — backing off"
                if s._stop_event:
                    s._stop_event.set()

    def on_miner_offline(self, miner_id: int):
        s = self._statuses.get(miner_id)
        if s and s.state == TunerState.OBSERVING:
            s._safety_breach = True
            s.message = "⚠ Miner went offline during observation"
            if s._stop_event:
                s._stop_event.set()

    # ── Main tuning loop ──────────────────────────────────────────────────────

    async def _run(self, miner_id: int):
        s     = self._statuses[miner_id]
        cfg   = s.config
        miner = self.db.get_miner_by_id(miner_id)
        if not miner:
            s.state   = TunerState.ERROR
            s.message = "Miner not found in DB"
            return

        client       = MinerClient(miner["ip"])
        session_name = "Session " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        session_id   = self.db.create_session(
            miner_id, session_name, ",".join(cfg.priority),
            cfg.step_mode, cfg.baseline_freq, cfg.baseline_voltage,
        )
        s.session_id   = session_id
        cur_freq       = _freq_snap(float(cfg.baseline_freq))
        cur_volt       = cfg.baseline_voltage
        backoff_count  = 0
        marginal_count = 0
        first_iter     = True

        try:
            while True:
                if cfg.time_limit_minutes and s.start_time:
                    if (time.time() - s.start_time) / 60 >= cfg.time_limit_minutes:
                        s.message = "⏱ Time limit reached"
                        break
                if s._stop_requested:
                    s.message = "Stopped by user"
                    break

                eff_ceil_freq = float(min(cfg.max_freq, HARD_MAX_FREQ))
                cur_freq = _freq_snap(max(float(HARD_MIN_FREQ), min(cur_freq, eff_ceil_freq)))
                cur_volt = min(cur_volt, HARD_MAX_VOLTAGE,
                               min(cfg.max_voltage, HARD_MAX_VOLTAGE))

                if first_iter:
                    first_iter = False
                    live = await client.get_stats() or {}
                    live_freq = float(live.get("frequency",   cur_freq) or cur_freq)
                    cur_freq  = _freq_snap(live_freq)
                    cur_volt  = int(live.get("coreVoltage", cur_volt) or cur_volt)
                    s.current_freq    = cur_freq
                    s.current_voltage = cur_volt
                    s.message = f"Observing current state: {cur_freq:.3f} MHz / {cur_volt} mV"
                else:
                    s.state           = TunerState.APPLYING
                    s.current_freq    = cur_freq
                    s.current_voltage = cur_volt
                    s.message         = f"Applying {cur_freq:.3f} MHz / {cur_volt} mV…"
                    ok = await client.apply_settings(cur_freq, cur_volt)
                    if not ok:
                        s.state   = TunerState.ERROR
                        s.message = "Failed to apply settings to miner"
                        break
                    # Interruptible 2-second settle so stop requests aren't delayed
                    settle_event = asyncio.Event()
                    s._stop_event = settle_event
                    try:
                        await asyncio.wait_for(settle_event.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                    finally:
                        s._stop_event = None
                    if s._stop_requested:
                        s.message = "Stopped by user"
                        break

                # Snapshot share counts at start of observation window
                stats_now = await client.get_stats() or {}
                s.obs_shares_acc_start = stats_now.get("sharesAccepted", 0) or 0
                s.obs_shares_rej_start = stats_now.get("sharesRejected", 0) or 0
                s.obs_shares_acc_end   = s.obs_shares_acc_start
                s.obs_shares_rej_end   = s.obs_shares_rej_start
                s.obs_hashrates    = []
                s.obs_hashrates_1m = []
                s.obs_temps        = []
                s.obs_vrm_temps    = []
                s.obs_error_pcts   = []
                s.obs_powers       = []
                s.obs_fanspeeds    = []
                s._safety_breach   = False

                obs_secs = OBS_WINDOW.get(cfg.priority[0], 60)
                s.obs_window_secs = obs_secs
                s.state   = TunerState.OBSERVING
                s.message = f"Observing {cur_freq:.3f} MHz / {cur_volt} mV ({obs_secs}s)…"

                # Use an event so stop requests interrupt immediately
                stop_event = asyncio.Event()
                s._stop_event = stop_event
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=obs_secs)
                except asyncio.TimeoutError:
                    pass   # Normal path — observation window completed
                finally:
                    s._stop_event = None

                # Check for user stop first — exit immediately
                if s._stop_requested:
                    s.message = "Stopped by user"
                    break

                # Hard safety breach during observation
                if s._safety_breach:
                    s.state = TunerState.SAFETY_BACKOFF
                    backoff_count += 1
                    step_entries = FREQ_FAST_STEP if s.step_mode == "fast" else FREQ_SLOW_STEP
                    cur_freq = _freq_prev(cur_freq, step_entries * 2)
                    if backoff_count >= 3:
                        s.message = "Stable limit reached after repeated safety back-offs"
                        break
                    continue

                s.state  = TunerState.EVALUATING
                result   = analyze_stability(s, cfg)

                # Rich status message
                reason_str = " | ".join(result.reasons) if result.reasons else "All OK"
                s.message  = (
                    f"[{result.verdict.upper()}] {cur_freq}MHz/{cur_volt}mV "
                    f"score={result.score:.3f} stab={result.stability_mult:.2f} "
                    f"HR={result.avg_hashrate:.0f}GH/s "
                    f"T={result.avg_temp:.1f}°C "
                    f"err={result.avg_error_rate:.2f}% "
                    f"eff={result.efficiency_gh_w:.2f}GH/W — {reason_str}"
                )

                s.trials.append(Trial(
                    cur_freq, cur_volt,
                    result.avg_hashrate, result.avg_temp,
                    result.avg_error_rate, result.score, result.verdict,
                ))
                self.db.add_session_datapoint(
                    session_id, result.avg_hashrate, result.avg_temp,
                    result.avg_error_rate, cur_freq, cur_volt, result.score,
                )

                if result.score > s.best_score:
                    s.best_score      = result.score
                    s.best_freq       = cur_freq
                    s.best_voltage    = cur_volt
                    s.best_hashrate   = result.avg_hashrate
                    s.best_temp       = result.avg_temp
                    s.best_error_rate = result.avg_error_rate

                # Per-metric bests — only on stable or marginal trials so we
                # don't record a great number from an unstable operating point
                if result.verdict in ("stable", "marginal"):
                    if result.avg_hashrate > s.best_hr_value:
                        s.best_hr_value = result.avg_hashrate
                        s.best_hr_freq  = cur_freq
                        s.best_hr_volt  = cur_volt
                    if s.best_temp_value < 0 or result.avg_temp < s.best_temp_value:
                        s.best_temp_value = result.avg_temp
                        s.best_temp_freq  = cur_freq
                        s.best_temp_volt  = cur_volt
                    if s.best_err_value < 0 or result.avg_error_rate < s.best_err_value:
                        s.best_err_value      = result.avg_error_rate
                        s.best_err_freq       = cur_freq
                        s.best_err_volt       = cur_volt
                        s.best_err_shares_acc = result.shares_accepted
                        s.best_err_shares_rej = result.shares_rejected

                step_entries = FREQ_FAST_STEP if s.step_mode == "fast" else FREQ_SLOW_STEP

                # Handle backoff — use table-based steps for precise navigation
                if result.backoff_steps > 0:
                    cur_freq = _freq_prev(cur_freq, step_entries * result.backoff_steps)
                    backoff_count += 1
                    if backoff_count >= 3:
                        if cfg.time_limit_minutes:
                            s.message = "Stable limit reached after repeated back-offs"
                            break
                        # No time limit — reset and re-observe at best known frequency
                        backoff_count  = 0
                        marginal_count = 0
                        cur_freq = s.best_freq if s.best_freq > 0.0 else cur_freq
                        cur_volt = s.best_voltage if s.best_voltage > 0 else cur_volt
                        s.message = f"Re-validating best settings: {cur_freq:.3f} MHz / {cur_volt} mV"
                    continue

                backoff_count = 0

                # Marginal — re-observe before deciding
                if result.verdict == "marginal":
                    marginal_count += 1
                    if marginal_count >= 2:
                        if cfg.time_limit_minutes:
                            s.message = "Stable ceiling found (consistent marginal stability)"
                            break
                        # No time limit — drop one step to find genuinely stable point
                        marginal_count = 0
                        cur_freq = _freq_prev(cur_freq, step_entries)
                        s.message = f"Marginal stability — stepping down → {cur_freq:.3f} MHz"
                    continue

                marginal_count = 0

                # Auto-switch to slow near ceilings
                eff_max_volt = min(cfg.max_voltage, HARD_MAX_VOLTAGE)
                eff_max_vrm  = min(cfg.max_vrm_temp, HARD_MAX_VRM_TEMP)
                avg_vrm_now  = _mean(s.obs_vrm_temps) if s.obs_vrm_temps else 0
                if (result.avg_temp >= min(cfg.max_temp, HARD_MAX_TEMP) * NEAR_LIMIT_FRACTION or
                        (avg_vrm_now > 0 and avg_vrm_now >= eff_max_vrm * NEAR_LIMIT_FRACTION) or
                        result.avg_error_rate >= cfg.error_threshold * (NEAR_LIMIT_FRACTION - 0.1) or
                        cur_volt >= eff_max_volt * NEAR_LIMIT_FRACTION or
                        cur_freq >= eff_ceil_freq * NEAR_LIMIT_FRACTION):
                    s.step_mode = "slow"
                    step_entries = FREQ_SLOW_STEP  # recalc after mode switch

                volt_step = STEP_VOLT[s.step_mode]
                next_freq = _freq_next(cur_freq, step_entries)

                if result.should_proceed and next_freq is not None and next_freq <= eff_ceil_freq:
                    # Stable — step up to next valid hardware frequency
                    cur_freq  = next_freq
                    s.message = f"Stable ✓ stepping up → {cur_freq:.3f} MHz"
                elif cur_volt + volt_step <= eff_max_volt:
                    # Frequency ceiling reached at this voltage — bump voltage and re-explore
                    cur_volt += volt_step
                    restart = _freq_next(s.best_freq if s.best_freq > 0.0 else cur_freq,
                                         step_entries)
                    cur_freq = min(restart, eff_ceil_freq) if restart is not None else cur_freq
                    s.message = (f"Voltage bump → {cur_volt} mV, "
                                 f"exploring from {cur_freq:.3f} MHz")
                else:
                    # Both frequency and voltage ceilings hit
                    if cfg.time_limit_minutes:
                        s.message = "Parameter space fully explored"
                        break
                    # No time limit — re-validate best settings continuously
                    cur_freq = s.best_freq if s.best_freq > 0.0 else cur_freq
                    cur_volt = s.best_voltage if s.best_voltage > 0 else cur_volt
                    s.message = (f"Continuous mode — re-validating "
                                 f"{cur_freq:.3f} MHz / {cur_volt} mV")

        except asyncio.CancelledError:
            s.message = "Tuning task cancelled"
        except Exception as e:
            s.state   = TunerState.ERROR
            s.message = f"Unexpected error: {e}"
            logger.exception(f"Tuner error miner {miner_id}")

        await self._finalise(miner_id, client)

    # ── Finalise ──────────────────────────────────────────────────────────────

    async def _finalise(self, miner_id: int, client: MinerClient):
        s = self._statuses[miner_id]
        if s.best_freq > 0:
            s.message = f"Applying best settings: {s.best_freq} MHz / {s.best_voltage} mV"
            await client.apply_settings(s.best_freq, s.best_voltage)

        cfg = s.config
        p_labels = {"hashrate": "Hashrate", "temp": "Temperature", "error_rate": "Error Rate"}
        p_str    = " › ".join(p_labels.get(p, p) for p in (cfg.priority if cfg else []))
        duration = round((time.time() - s.start_time) / 60, 1) if s.start_time else 0
        pri = cfg.priority[0] if cfg else "hashrate"

        if pri == "hashrate":
            why = f"Highest stable hashrate ({s.best_hashrate:.2f} GH/s) confirmed stable across all metrics."
        elif pri == "temp":
            why = f"Best thermal efficiency ({s.best_temp:.1f}°C) with stable hashrate and error rate."
        else:
            why = f"Lowest confirmed error rate ({s.best_error_rate:.3f}%) with acceptable performance."

        summary = {
            "stop_reason":     s.message,
            "priority":        p_str,
            "best_freq":       s.best_freq,
            "best_voltage":    s.best_voltage,
            "best_hashrate":   round(s.best_hashrate, 2),
            "best_temp":       round(s.best_temp, 1),
            "best_error_rate": round(s.best_error_rate, 3),
            "trials":          len(s.trials),
            "duration_min":    duration,
            "why":             why,
        }

        if s.session_id:
            self.db.update_session(
                s.session_id,
                ended_at        = int(time.time()),
                best_freq       = s.best_freq,
                best_voltage    = s.best_voltage,
                best_hashrate   = s.best_hashrate,
                best_temp       = s.best_temp,
                best_error_rate = s.best_error_rate,
                best_score      = s.best_score,
                summary         = str(summary),
                status          = "complete",
            )

        s.state   = TunerState.COMPLETE
        s.message = f"Complete — best: {s.best_freq} MHz / {s.best_voltage} mV"


# ── Math helpers ──────────────────────────────────────────────────────────────

def _mean(lst: List[float]) -> float:
    return sum(lst) / len(lst) if lst else 0.0

def _stdev(lst: List[float]) -> float:
    if len(lst) < 2:
        return 0.0
    m = _mean(lst)
    return math.sqrt(sum((x - m) ** 2 for x in lst) / (len(lst) - 1))

def _trend_pct(lst: List[float]) -> float:
    """% change from average of first third to average of last third."""
    n = len(lst)
    if n < 6:
        return 0.0
    t = n // 3
    first = _mean(lst[:t])
    last  = _mean(lst[n - t:])
    return ((last - first) / first * 100.0) if first > 0 else 0.0

def _trend_abs(lst: List[float]) -> float:
    """Absolute change from average of first third to average of last third."""
    n = len(lst)
    if n < 6:
        return 0.0
    t = n // 3
    return _mean(lst[n - t:]) - _mean(lst[:t])
