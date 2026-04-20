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
HARD_MAX_TEMP    = 80.0
HARD_MAX_VOLTAGE = 1400
HARD_MIN_FREQ    = 400
HARD_MAX_FREQ    = 1100

# ── Step sizes ────────────────────────────────────────────────────────────────
STEP_FREQ = {"fast": 25, "slow": 25}   # MHz
STEP_VOLT = {"fast": 50, "slow": 25}   # mV

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
    max_voltage:        int
    max_freq:           int
    error_threshold:    float
    time_limit_minutes: Optional[int]
    baseline_freq:      int
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

    # Error rate
    avg_error_rate:     float
    peak_error_rate:    float
    error_trend:        float   # positive = worsening
    share_delta:        int

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

    current_freq:    int   = 0
    current_voltage: int   = 0
    best_freq:       int   = 0
    best_voltage:    int   = 0
    best_hashrate:   float = 0.0
    best_temp:       float = 0.0
    best_error_rate: float = 0.0
    best_score:      float = -1.0

    session_id:  Optional[int]        = None
    start_time:  Optional[float]      = None
    config:      Optional[TunerConfig] = None

    # Per-second observation accumulators
    obs_hashrates:      List[float] = field(default_factory=list)
    obs_hashrates_1m:   List[float] = field(default_factory=list)
    obs_temps:          List[float] = field(default_factory=list)
    obs_error_pcts:     List[float] = field(default_factory=list)
    obs_powers:         List[float] = field(default_factory=list)
    obs_shares_acc_start: int = 0
    obs_shares_rej_start: int = 0
    obs_shares_acc_end:   int = 0
    obs_shares_rej_end:   int = 0
    obs_window_secs:      int = 60

    trials: List[Trial] = field(default_factory=list)

    _safety_breach:  bool = False
    _stop_requested: bool = False


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

    # ── 4. Error rate analysis ────────────────────────────────────────────────
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

    # ── 5. Power analysis ─────────────────────────────────────────────────────
    avg_pwr  = _mean(pwrs)
    cv_pwr   = _stdev(pwrs) / avg_pwr if avg_pwr > 0 else 0
    eff_gh_w = avg_hr / avg_pwr if avg_pwr > 0 else 0

    pwr_stab = 1.0
    if cv_pwr > 0.10:
        pwr_stab = 0.70
        reasons.append(f"Power draw unstable (CV={cv_pwr:.3f}) — possible ASIC issue")
    elif cv_pwr > 0.05:
        pwr_stab = 0.88

    # ── 6. Composite stability multiplier ─────────────────────────────────────
    # Temperature trend is the most important safety signal.
    # Hashrate consistency is next (shows ASIC health).
    # Error rate weighted third.
    # Power is a supporting signal.
    stability_mult = max(0.0, min(1.0,
        0.35 * tmp_stab  +
        0.30 * hr_stab   +
        0.25 * err_stab  +
        0.10 * pwr_stab
    ) * confidence)

    # ── 7. Performance score ──────────────────────────────────────────────────
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

    # ── 8. Verdict ────────────────────────────────────────────────────────────
    hard_breach = avg_tmp >= eff_max_tmp or win_err >= eff_thresh * 1.5

    if hard_breach:
        verdict, should_proceed, backoff_steps = "critical", False, 2
        if avg_tmp >= eff_max_tmp:
            reasons.append(f"Hard ceiling breach: {avg_tmp:.1f}°C ≥ {eff_max_tmp:.0f}°C")
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
        avg_error_rate=round(win_err,3), peak_error_rate=round(peak_err,3),
        error_trend=round(err_trend,3), share_delta=share_delta,
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

    # ── Called every second by the polling loop ───────────────────────────────

    def on_stats_update(self, miner_id: int, stats: Dict):
        s = self._statuses.get(miner_id)
        if not s or s.state != TunerState.OBSERVING:
            return
        s.obs_hashrates.append(   stats.get("hashRate",        0) or 0)
        s.obs_hashrates_1m.append(stats.get("hashRate_1m",     0) or
                                   stats.get("hashRate",        0) or 0)
        s.obs_temps.append(       stats.get("temp",            0) or 0)
        s.obs_error_pcts.append(  stats.get("errorPercentage", 0) or 0)
        s.obs_powers.append(      stats.get("power",           0) or 0)
        s.obs_shares_acc_end = stats.get("sharesAccepted", s.obs_shares_acc_start)
        s.obs_shares_rej_end = stats.get("sharesRejected", s.obs_shares_rej_start)

        cfg = s.config
        if cfg and (stats.get("temp") or 0) >= min(cfg.max_temp, HARD_MAX_TEMP):
            s._safety_breach = True
            s.message = f"⚠ Temp {stats.get('temp')}°C hit ceiling — backing off"

    def on_miner_offline(self, miner_id: int):
        s = self._statuses.get(miner_id)
        if s and s.state == TunerState.OBSERVING:
            s._safety_breach = True
            s.message = "⚠ Miner went offline during observation"

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
        cur_freq       = cfg.baseline_freq
        cur_volt       = cfg.baseline_voltage
        backoff_count  = 0
        marginal_count = 0
        voltage_bumped = False
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

                eff_ceil_freq = min(cfg.max_freq, HARD_MAX_FREQ)
                cur_freq = max(HARD_MIN_FREQ, min(cur_freq, eff_ceil_freq))
                cur_volt = min(cur_volt, HARD_MAX_VOLTAGE,
                               min(cfg.max_voltage, HARD_MAX_VOLTAGE))

                if first_iter:
                    first_iter = False
                    live = await client.get_stats() or {}
                    cur_freq = int(live.get("frequency",   cur_freq) or cur_freq)
                    cur_volt = int(live.get("coreVoltage", cur_volt) or cur_volt)
                    s.current_freq    = cur_freq
                    s.current_voltage = cur_volt
                    s.message = f"Observing current state: {cur_freq} MHz / {cur_volt} mV"
                else:
                    s.state           = TunerState.APPLYING
                    s.current_freq    = cur_freq
                    s.current_voltage = cur_volt
                    s.message         = f"Applying {cur_freq} MHz / {cur_volt} mV…"
                    ok = await client.apply_settings(cur_freq, cur_volt)
                    if not ok:
                        s.state   = TunerState.ERROR
                        s.message = "Failed to apply settings to miner"
                        break
                    await asyncio.sleep(2)

                # Snapshot share counts at start of observation window
                stats_now = await client.get_stats() or {}
                s.obs_shares_acc_start = stats_now.get("sharesAccepted", 0) or 0
                s.obs_shares_rej_start = stats_now.get("sharesRejected", 0) or 0
                s.obs_shares_acc_end   = s.obs_shares_acc_start
                s.obs_shares_rej_end   = s.obs_shares_rej_start
                s.obs_hashrates    = []
                s.obs_hashrates_1m = []
                s.obs_temps        = []
                s.obs_error_pcts   = []
                s.obs_powers       = []
                s._safety_breach   = False

                obs_secs = OBS_WINDOW.get(cfg.priority[0], 60)
                s.obs_window_secs = obs_secs
                s.state   = TunerState.OBSERVING
                s.message = f"Observing {cur_freq} MHz / {cur_volt} mV ({obs_secs}s)…"

                await asyncio.sleep(obs_secs)

                # Hard safety breach during observation
                if s._safety_breach:
                    s.state = TunerState.SAFETY_BACKOFF
                    backoff_count += 1
                    cur_freq = max(HARD_MIN_FREQ,
                                   cur_freq - STEP_FREQ[s.step_mode] * 2)
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

                # Handle backoff
                if result.backoff_steps > 0:
                    cur_freq = max(HARD_MIN_FREQ,
                                   cur_freq - STEP_FREQ[s.step_mode] * result.backoff_steps)
                    backoff_count += 1
                    if backoff_count >= 3:
                        s.message = "Stable limit reached after repeated back-offs"
                        break
                    continue

                backoff_count = 0

                # Marginal — re-observe before deciding
                if result.verdict == "marginal":
                    marginal_count += 1
                    if marginal_count >= 2:
                        s.message = "Stable ceiling found (consistent marginal stability)"
                        break
                    continue

                marginal_count = 0

                # Auto-switch to slow near ceilings
                eff_max_volt = min(cfg.max_voltage, HARD_MAX_VOLTAGE)
                if (result.avg_temp >= min(cfg.max_temp, HARD_MAX_TEMP) * NEAR_LIMIT_FRACTION or
                        result.avg_error_rate >= cfg.error_threshold * (NEAR_LIMIT_FRACTION - 0.1) or
                        cur_volt >= eff_max_volt * NEAR_LIMIT_FRACTION or
                        cur_freq >= eff_ceil_freq * NEAR_LIMIT_FRACTION):
                    s.step_mode = "slow"

                freq_step = STEP_FREQ[s.step_mode]
                volt_step = STEP_VOLT[s.step_mode]
                next_freq = cur_freq + freq_step

                if result.should_proceed and next_freq <= eff_ceil_freq:
                    cur_freq  = next_freq
                    s.message = f"Stable ✓ stepping up → {cur_freq} MHz"
                elif (not voltage_bumped and
                      cur_volt + volt_step <= min(cfg.max_voltage, HARD_MAX_VOLTAGE)):
                    cur_volt      += volt_step
                    voltage_bumped = True
                    cur_freq = min(s.best_freq + freq_step, eff_ceil_freq)
                    s.message = f"Voltage bump → {cur_volt} mV, retrying {cur_freq} MHz"
                else:
                    s.message = "Parameter space fully explored"
                    break

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
