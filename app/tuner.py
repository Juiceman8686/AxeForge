"""
AxeForge Tuning Engine
======================
State machine per miner:
  IDLE → APPLYING → RESTARTING → OBSERVING → EVALUATING → (loop or COMPLETE)
  Any state → SAFETY_BACKOFF → APPLYING  (on threshold breach)
  Any state → COMPLETE                   (on stop / time limit)
"""

import asyncio
import logging
import time
import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from database import Database
from miner_client import MinerClient

logger = logging.getLogger(__name__)

# ── Hard ceilings (immutable) ────────────────────────────────────────────────
HARD_MAX_TEMP    = 80.0
HARD_MAX_VOLTAGE = 1400
HARD_MIN_FREQ    = 400
HARD_MAX_FREQ    = 625   # conservative BM1370 ceiling

# ── Step sizes ───────────────────────────────────────────────────────────────
STEP_FREQ  = {"fast": 25, "slow": 25}   # MHz  (hardware rounds to nearest valid)
STEP_VOLT  = {"fast": 50, "slow": 25}   # mV

# ── Observation windows by primary priority (seconds) ────────────────────────
OBS_WINDOW = {"hashrate": 30, "temp": 120, "error_rate": 15}

# Threshold fraction at which we auto-switch to slow step mode
NEAR_LIMIT_FRACTION = 0.88


class TunerState(str, Enum):
    IDLE          = "idle"
    APPLYING      = "applying"
    RESTARTING    = "restarting"
    OBSERVING     = "observing"
    EVALUATING    = "evaluating"
    SAFETY_BACKOFF= "safety_backoff"
    COMPLETE      = "complete"
    ERROR         = "error"


@dataclass
class TunerConfig:
    priority:            List[str]        # e.g. ["hashrate","temp","error_rate"]
    step_mode:           str              # "fast" or "slow"
    max_temp:            float            # user ceiling, capped at HARD_MAX_TEMP
    max_voltage:         int              # user ceiling, capped at HARD_MAX_VOLTAGE
    error_threshold:     float            # e.g. 1.0  → 1 %
    time_limit_minutes:  Optional[int]    # None = run until stopped
    baseline_freq:       int
    baseline_voltage:    int


@dataclass
class Trial:
    freq:       int
    voltage:    int
    hashrate:   float
    temp:       float
    error_rate: float
    score:      float


@dataclass
class TunerStatus:
    state:        TunerState = TunerState.IDLE
    message:      str        = "Idle"
    step_mode:    str        = "slow"
    restarting:   bool       = False      # suppress offline alert during restart

    current_freq:    int   = 0
    current_voltage: int   = 0
    best_freq:       int   = 0
    best_voltage:    int   = 0
    best_hashrate:   float = 0.0
    best_temp:       float = 0.0
    best_error_rate: float = 0.0
    best_score:      float = -1.0

    session_id:  Optional[int]   = None
    start_time:  Optional[float] = None
    config:      Optional[TunerConfig] = None

    # live observation accumulators (not serialised to DB)
    obs_hashrates:          List[float] = field(default_factory=list)
    obs_temps:              List[float] = field(default_factory=list)
    obs_errors:             List[float] = field(default_factory=list)
    obs_shares_acc_start:   int         = 0
    obs_shares_rej_start:   int         = 0
    obs_window_secs:        int         = 30

    trials: List[Trial] = field(default_factory=list)

    # internal flags read by the polling loop
    _safety_breach: bool = False
    _stop_requested: bool = False


class TunerManager:
    def __init__(self, db: Database):
        self.db = db
        self._statuses: Dict[int, TunerStatus] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def get_status(self, miner_id: int) -> Dict:
        s = self._statuses.get(miner_id, TunerStatus())
        remaining = None
        time_limit = s.config.time_limit_minutes if s.config else None
        if time_limit and s.start_time:
            elapsed_s = time.time() - s.start_time
            remaining = max(0, int(time_limit * 60 - elapsed_s))
        return {
            "state":        s.state.value,
            "message":      s.message,
            "step_mode":    s.step_mode,
            "restarting":   s.restarting,
            "current_freq": s.current_freq,
            "current_voltage": s.current_voltage,
            "best_freq":    s.best_freq,
            "best_voltage": s.best_voltage,
            "best_hashrate": round(s.best_hashrate, 2),
            "best_temp":    round(s.best_temp, 1),
            "best_error_rate": round(s.best_error_rate, 3),
            "session_id":   s.session_id,
            "time_remaining_s": remaining,
        }

    async def start_tuning(self, miner_id: int, config: TunerConfig) -> bool:
        existing = self._statuses.get(miner_id)
        if existing and existing.state not in (
            TunerState.IDLE, TunerState.COMPLETE, TunerState.ERROR
        ):
            return False

        s = TunerStatus()
        s.config      = config
        s.step_mode   = config.step_mode
        s.start_time  = time.time()
        s.state       = TunerState.APPLYING
        s.message     = "Starting…"
        s.time_limit_minutes = config.time_limit_minutes
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

        hashrate = stats.get("hashRate", 0) or 0
        temp     = stats.get("temp", 0)     or 0
        acc      = stats.get("sharesAccepted",  0) or 0
        rej      = stats.get("sharesRejected",  0) or 0

        s.obs_hashrates.append(hashrate)
        s.obs_temps.append(temp)

        delta_acc = max(0, acc - s.obs_shares_acc_start)
        delta_rej = max(0, rej - s.obs_shares_rej_start)
        total     = delta_acc + delta_rej
        err_rate  = (delta_rej / total * 100.0) if total > 0 else 0.0
        s.obs_errors.append(err_rate)

        cfg = s.config
        if cfg:
            eff_max_temp = min(cfg.max_temp, HARD_MAX_TEMP)
            if temp >= eff_max_temp:
                s._safety_breach = True
                s.message = f"⚠ Temp {temp:.1f}°C hit ceiling — backing off"

    def on_miner_offline(self, miner_id: int):
        s = self._statuses.get(miner_id)
        if s and s.state == TunerState.OBSERVING:
            s._safety_breach = True
            s.message = "⚠ Miner went offline during observation"

    # ── Main tuning coroutine ─────────────────────────────────────────────────

    async def _run(self, miner_id: int):
        s   = self._statuses[miner_id]
        cfg = s.config
        miner = self.db.get_miner_by_id(miner_id)
        if not miner:
            s.state   = TunerState.ERROR
            s.message = "Miner not found in DB"
            return

        client = MinerClient(miner["ip"])

        # Create session record
        session_name = "Session " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        session_id   = self.db.create_session(
            miner_id, session_name,
            ",".join(cfg.priority),
            cfg.step_mode,
            cfg.baseline_freq,
            cfg.baseline_voltage,
        )
        s.session_id = session_id

        cur_freq = cfg.baseline_freq
        cur_volt = cfg.baseline_voltage
        backoff_count = 0          # consecutive back-offs → stop exploring upward
        voltage_bumped = False     # did we already try bumping voltage?

        try:
            while True:
                # ── time limit ──────────────────────────────────────────────
                if cfg.time_limit_minutes and s.start_time:
                    elapsed_min = (time.time() - s.start_time) / 60.0
                    if elapsed_min >= cfg.time_limit_minutes:
                        s.message = "⏱ Time limit reached"
                        break

                if s._stop_requested:
                    s.message = "Stopped by user"
                    break

                # ── enforce hard ceilings on current targets ─────────────────
                cur_freq = max(HARD_MIN_FREQ, min(cur_freq, HARD_MAX_FREQ))
                cur_volt = min(cur_volt, HARD_MAX_VOLTAGE,
                               min(cfg.max_voltage, HARD_MAX_VOLTAGE))

                # ── APPLYING ─────────────────────────────────────────────────
                s.state          = TunerState.APPLYING
                s.restarting     = False
                s.current_freq   = cur_freq
                s.current_voltage = cur_volt
                s.message        = f"Applying {cur_freq} MHz / {cur_volt} mV…"

                ok = await client.apply_settings(cur_freq, cur_volt)
                if not ok:
                    s.state   = TunerState.ERROR
                    s.message = "Failed to apply settings to miner"
                    break

                await asyncio.sleep(0.5)
                await client.restart()

                # ── RESTARTING ───────────────────────────────────────────────
                s.state     = TunerState.RESTARTING
                s.restarting = True
                s.message   = "Restarting miner…"

                online = await client.wait_for_online()
                s.restarting = False
                if not online:
                    s.state   = TunerState.ERROR
                    s.message = "Miner did not come back online after restart"
                    break

                # ── OBSERVING ────────────────────────────────────────────────
                stats_now = await client.get_stats() or {}
                s.obs_shares_acc_start = stats_now.get("sharesAccepted", 0) or 0
                s.obs_shares_rej_start = stats_now.get("sharesRejected", 0) or 0
                s.obs_hashrates = []
                s.obs_temps     = []
                s.obs_errors    = []
                s._safety_breach = False

                obs_secs = self._obs_window(cfg.priority[0])
                s.obs_window_secs = obs_secs
                s.state   = TunerState.OBSERVING
                s.message = f"Observing {cur_freq} MHz / {cur_volt} mV for {obs_secs}s…"

                await asyncio.sleep(obs_secs)

                # ── EVALUATING ───────────────────────────────────────────────
                if s._safety_breach:
                    # Back off frequency one step
                    freq_step = STEP_FREQ[s.step_mode]
                    cur_freq  = max(HARD_MIN_FREQ, cur_freq - freq_step)
                    backoff_count += 1
                    s.state = TunerState.SAFETY_BACKOFF
                    if backoff_count >= 3:
                        s.message = "Reached stable limit after repeated back-offs"
                        break
                    continue

                s.state = TunerState.EVALUATING

                avg_hr  = _avg(s.obs_hashrates)
                avg_tmp = _avg(s.obs_temps)
                avg_err = _avg(s.obs_errors)

                score = self._score(avg_hr, avg_tmp, avg_err, cfg)
                trial = Trial(cur_freq, cur_volt, avg_hr, avg_tmp, avg_err, score)
                s.trials.append(trial)

                self.db.add_session_datapoint(
                    session_id, avg_hr, avg_tmp, avg_err, cur_freq, cur_volt, score
                )

                # Update best
                if score > s.best_score:
                    s.best_score      = score
                    s.best_freq       = cur_freq
                    s.best_voltage    = cur_volt
                    s.best_hashrate   = avg_hr
                    s.best_temp       = avg_tmp
                    s.best_error_rate = avg_err

                # Check error threshold
                eff_err_thresh = float(
                    self.db.get_setting("error_rate_threshold") or cfg.error_threshold
                )
                if avg_err > eff_err_thresh:
                    freq_step = STEP_FREQ[s.step_mode]
                    cur_freq  = max(HARD_MIN_FREQ, cur_freq - freq_step)
                    backoff_count += 1
                    s.message = (
                        f"Error rate {avg_err:.2f}% > {eff_err_thresh}% — backing off"
                    )
                    if backoff_count >= 3:
                        s.message = "Stable limit found (error rate)"
                        break
                    continue

                backoff_count = 0  # reset on clean trial

                # Auto-switch to slow mode near limits
                eff_max_tmp  = min(cfg.max_temp,    HARD_MAX_TEMP)
                eff_max_volt = min(cfg.max_voltage,  HARD_MAX_VOLTAGE)
                near_temp    = avg_tmp  >= eff_max_tmp  * NEAR_LIMIT_FRACTION
                near_err     = avg_err  >= eff_err_thresh * (NEAR_LIMIT_FRACTION - 0.1)
                near_volt    = cur_volt >= eff_max_volt * NEAR_LIMIT_FRACTION
                if near_temp or near_err or near_volt:
                    s.step_mode = "slow"

                # Decide next step
                freq_step = STEP_FREQ[s.step_mode]
                volt_step = STEP_VOLT[s.step_mode]
                next_freq = cur_freq + freq_step

                if next_freq <= HARD_MAX_FREQ and next_freq <= cfg.baseline_freq + 200:
                    # More frequency headroom — go up
                    cur_freq  = next_freq
                    s.message = f"Stepping up → {cur_freq} MHz"
                elif not voltage_bumped and cur_volt + volt_step <= min(eff_max_volt, HARD_MAX_VOLTAGE):
                    # Voltage headroom — try a voltage bump and re-push freq
                    cur_volt      += volt_step
                    voltage_bumped = True
                    # Re-try one step above last stable freq
                    cur_freq = min(s.best_freq + freq_step, HARD_MAX_FREQ)
                    s.message = f"Bumping voltage → {cur_volt} mV, retrying {cur_freq} MHz"
                else:
                    s.message = "Explored available parameter space"
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

        # Apply best settings found
        if s.best_freq > 0:
            s.message = (
                f"Applying best settings: {s.best_freq} MHz / {s.best_voltage} mV"
            )
            await client.apply_settings(s.best_freq, s.best_voltage)
            await asyncio.sleep(0.5)
            await client.restart()

        # Build human-readable summary
        cfg = s.config
        priority_labels = {
            "hashrate":   "Hashrate",
            "temp":       "Temperature",
            "error_rate": "Error Rate",
        }
        p_str = " › ".join(priority_labels.get(p, p) for p in (cfg.priority if cfg else []))
        duration = round((time.time() - s.start_time) / 60, 1) if s.start_time else 0
        why = ""
        if s.best_freq:
            pri = cfg.priority[0] if cfg else "hashrate"
            if pri == "hashrate":
                why = f"Highest hashrate ({s.best_hashrate:.2f} GH/s) within safe limits."
            elif pri == "temp":
                why = f"Best thermal profile ({s.best_temp:.1f}°C) at this performance."
            else:
                why = f"Lowest error rate ({s.best_error_rate:.3f}%) at stable performance."

        summary = {
            "stop_reason":  s.message,
            "priority":     p_str,
            "best_freq":    s.best_freq,
            "best_voltage": s.best_voltage,
            "best_hashrate": round(s.best_hashrate, 2),
            "best_temp":    round(s.best_temp, 1),
            "best_error_rate": round(s.best_error_rate, 3),
            "trials":       len(s.trials),
            "duration_min": duration,
            "why":          why,
        }

        if s.session_id:
            self.db.update_session(
                s.session_id,
                ended_at       = int(time.time()),
                best_freq      = s.best_freq,
                best_voltage   = s.best_voltage,
                best_hashrate  = s.best_hashrate,
                best_temp      = s.best_temp,
                best_error_rate= s.best_error_rate,
                best_score     = s.best_score,
                summary        = str(summary),
                status         = "complete",
            )

        s.state   = TunerState.COMPLETE
        s.message = f"Complete — best: {s.best_freq} MHz / {s.best_voltage} mV"

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _obs_window(primary_priority: str) -> int:
        return OBS_WINDOW.get(primary_priority, 30)

    @staticmethod
    def _score(hashrate: float, temp: float, error_rate: float,
               cfg: TunerConfig) -> float:
        weights = {
            cfg.priority[0]: 0.60,
            cfg.priority[1]: 0.30,
            cfg.priority[2]: 0.10,
        }
        eff_max_temp = min(cfg.max_temp, HARD_MAX_TEMP)

        # Normalise to [0, 1] — higher is always better
        norm_hr  = min(hashrate / 2000.0, 1.0)
        norm_tmp = max(0.0, 1.0 - (temp / eff_max_temp))
        norm_err = max(0.0, 1.0 - (error_rate / max(cfg.error_threshold, 0.01)))

        return (
            weights["hashrate"]   * norm_hr +
            weights["temp"]       * norm_tmp +
            weights["error_rate"] * norm_err
        )


def _avg(lst: list) -> float:
    return sum(lst) / len(lst) if lst else 0.0
