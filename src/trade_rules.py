"""Pure helpers for short-horizon entries and exits."""
from __future__ import annotations
import math
import decimal
from datetime import timezone
import pandas as pd
from . import indicators as ind


def weekend_mode_active(now_utc,config,forex_open=False):
    """Use weekend policy on weekends until the broker's forex session reopens."""
    now=now_utc.replace(tzinfo=timezone.utc) if now_utc.tzinfo is None else now_utc.astimezone(timezone.utc)
    return bool(config.get('weekend',{}).get('enabled',False)) and now.weekday()>=5 and not forex_open


def mtf_direction_agrees(candles: dict[str, pd.DataFrame], direction: str, timeframes=('M1', 'M5', 'M15'), fast=5, slow=13, min_agree=None) -> bool:
    """Require closed-bar EMA direction to agree on the configured count of frames."""
    required=len(timeframes) if min_agree is None else int(min_agree)
    if required<1 or required>len(timeframes): return False
    agreed=0
    for timeframe in timeframes:
        frame = candles.get(timeframe)
        if frame is None or len(frame) < slow + 2: continue
        fast_ema = ind.ema(frame.close, fast).iloc[-1]
        slow_ema = ind.ema(frame.close, slow).iloc[-1]
        close = float(frame.close.iloc[-1])
        if not all(math.isfinite(float(x)) for x in (fast_ema, slow_ema, close)): continue
        if direction == 'buy' and close > fast_ema > slow_ema: agreed+=1
        elif direction == 'sell' and close < fast_ema < slow_ema: agreed+=1
    return agreed>=required


def strategy_mtf_allowed(strategy_name: str, candles: dict[str, pd.DataFrame], direction: str,
                         enabled: bool=True, fast: int=5, slow: int=13, min_agree: int=3) -> bool:
    """Apply generic trend alignment except to range-reversion entries, which validate M1 reversal internally."""
    if not enabled or strategy_name in ('mean_reversion', 'vwap_reversion'):
        return True
    return mtf_direction_agrees(candles,direction,fast=fast,slow=slow,min_agree=min_agree)


def exploration_mtf_allowed(strategy_name: str, signal_timeframe: str, candles: dict[str, pd.DataFrame],
                            direction: str, fast: int=5, slow: int=13, min_agree: int=2) -> bool:
    """Exploration uses the family frame for H1/H4/D1, M15 families retain MTF confirmation."""
    timeframe=str(signal_timeframe).upper()
    if timeframe in {'H1','H4','D1'}: return True
    if timeframe=='M15': return strategy_mtf_allowed(strategy_name,candles,direction,enabled=True,fast=fast,slow=slow,min_agree=min_agree)
    return False


def stop_target_from_atr(entry: float, direction: str, atr: float, spread: float, sl_mult: float, tp_mult: float, horizon_minutes: float) -> tuple[float, float]:
    """Build symmetric directional stop and target distances for a short horizon."""
    horizon=max(1.0,float(horizon_minutes))
    stop_distance = float(atr) * math.sqrt(horizon) * float(sl_mult)
    target_distance = float(atr) * math.sqrt(horizon) * float(tp_mult)
    # Include the current spread in the target's required distance.
    target_distance += max(0.0, float(spread))
    if not math.isfinite(stop_distance) or not math.isfinite(target_distance) or stop_distance <= 0:
        raise ValueError('ATR must be finite and positive')
    sign = 1 if direction == 'buy' else -1
    return float(entry) - sign * stop_distance, float(entry) + sign * target_distance


def rebase_signal_levels(signal_entry: float, stop: float, target: float, direction: str,
                         executable_entry: float) -> tuple[float, float]:
    """Keep strategy-defined stop/target distances when moving to the live quote."""
    stop_distance=abs(float(signal_entry)-float(stop))
    target_distance=abs(float(target)-float(signal_entry))
    sign=1 if direction=='buy' else -1
    return float(executable_entry)-sign*stop_distance, float(executable_entry)+sign*target_distance


def hours_for_symbol(symbol: str, universe: dict | None) -> dict | None:
    """Trading-hours window for this broker symbol: a per-symbol override or the default."""
    universe = universe or {}
    hours = universe.get('trading_hours') or {}
    aliases = universe.get('symbol_aliases', {}) or {}
    name = str(symbol).upper()
    for logical, override in (hours.get('per_symbol') or {}).items():
        patterns = [str(p).upper() for p in aliases.get(logical, [])] + [str(logical).upper()]
        if any(name == p or name.startswith(p) for p in patterns):
            return {**{k: v for k, v in hours.items() if k != 'per_symbol'}, **override}
    return hours


def within_trading_hours(now_utc, hours: dict | None) -> bool:
    """True when new entries are allowed by universe.trading_hours (always True when disabled)."""
    if not hours or not hours.get('enabled', False): return True
    from datetime import time as dtime
    from zoneinfo import ZoneInfo
    local = now_utc.astimezone(ZoneInfo(str(hours.get('timezone', 'UTC'))))
    if local.weekday() >= 5: return False
    start = dtime.fromisoformat(str(hours.get('start', '00:00'))); end = dtime.fromisoformat(str(hours.get('end', '23:59')))
    now = local.time()
    return start <= now < end if start <= end else (now >= start or now < end)


def spread_within_median(current: float, median: float, ratio: float) -> bool:
    """Reject absent/invalid baseline spreads and abnormally wide current spreads."""
    return all(math.isfinite(float(x)) for x in (current, median, ratio)) and median > 0 and current <= median * ratio

def spread_within_expected_move(spread_price: float, atr_price: float, horizon_bars: float, max_ratio: float) -> bool:
    """Compare spread and square-root-time ATR in the same price units."""
    values=(float(spread_price),float(atr_price),float(horizon_bars),float(max_ratio))
    if not all(math.isfinite(value) for value in values) or values[0]<0 or values[1]<=0 or values[2]<=0 or values[3]<=0:
        return False
    return values[0] <= values[1]*math.sqrt(values[2])*values[3]


def confidence_allowed(confidence: float, minimum: float) -> bool:
    """Check a normalized strategy confidence against its configured floor."""
    return math.isfinite(float(confidence)) and 0 <= float(confidence) <= 1 and float(confidence) >= float(minimum)


def adverse_exit_score(frame: pd.DataFrame, direction: str, config: dict) -> tuple[int, list[str]]:
    """Score adverse M1 conditions using closed bars only."""
    needed = max(int(config['early_exit_ema_slow']) + 2, int(config['early_exit_swing_lookback']) + 2,
                 int(config['early_exit_rsi_period']) + 2, int(config['early_exit_roc_period']) + 2,
                 int(config['early_exit_volume_lookback']) + 2)
    if frame is None or len(frame) < needed:
        return 0, []
    buy = direction == 'buy'
    close = frame.close
    fast = ind.ema(close, int(config['early_exit_ema_fast']))
    slow = ind.ema(close, int(config['early_exit_ema_slow']))
    previous = len(frame) - 2
    current = len(frame) - 1
    reasons = []
    weights = config.get('early_exit_weights', {})
    score = 0
    def add(key, condition, reason):
        nonlocal score
        if condition:
            score += int(weights.get(key, 1)); reasons.append(reason)
    add('ema_cross', fast.iloc[current] < slow.iloc[current] if buy else fast.iloc[current] > slow.iloc[current], 'EMA kəsişməsi əks istiqamətdə')
    lookback = int(config['early_exit_swing_lookback'])
    prior = frame.iloc[-lookback-1:-1]
    add('swing_break', float(close.iloc[-1]) < float(prior.low.min()) if buy else float(close.iloc[-1]) > float(prior.high.max()), 'son M1 diapazonu qırıldı')
    rsi = ind.rsi(close, int(config['early_exit_rsi_period']))
    roc = ind.roc(close, int(config['early_exit_roc_period']))
    add('momentum', (rsi.iloc[-1] < 50 and roc.iloc[-1] < 0) if buy else (rsi.iloc[-1] > 50 and roc.iloc[-1] > 0), 'RSI/ROC impulsu zəifləyib')
    volumes = frame.tick_volume if 'tick_volume' in frame else pd.Series(0, index=frame.index)
    baseline = float(volumes.iloc[-int(config['early_exit_volume_lookback'])-1:-1].mean())
    adverse_candle = float(frame.close.iloc[-1]) < float(frame.open.iloc[-1]) if buy else float(frame.close.iloc[-1]) > float(frame.open.iloc[-1])
    add('volume_spike', baseline > 0 and float(volumes.iloc[-1]) >= baseline * float(config['early_exit_volume_spike_ratio']) and adverse_candle, 'əks istiqamətdə həcm sıçrayışı')
    return score, reasons


def should_exit_early(current_r: float, score: int, config: dict) -> bool:
    """Trigger within the configured adverse R band once enough evidence agrees."""
    return (bool(config.get('early_exit_enabled', True)) and
            float(config['early_exit_min_r']) <= float(current_r) <= float(config['early_exit_max_r']) and
            int(score) >= int(config['early_exit_score_threshold']))


def scale_out_volume(open_volume: float, step: float, minimum: float, percent: float) -> tuple[float, bool]:
    """Return a valid partial close; flag a full close when the remainder cannot be split."""
    if min(open_volume, step, minimum, percent) <= 0 or percent >= 100:
        raise ValueError('volume grid and scale-out percent must be positive')
    unit = decimal.Decimal(str(step)); desired = decimal.Decimal(str(open_volume))*decimal.Decimal(str(percent))/100
    partial = (desired/unit).to_integral_value(rounding=decimal.ROUND_FLOOR)*unit
    opened = decimal.Decimal(str(open_volume)); min_volume=decimal.Decimal(str(minimum))
    if partial < min_volume or opened-partial < min_volume:
        return float(opened), True
    return float(partial), False


def stop_only_tightens(direction: str, old_stop: float, new_stop: float) -> bool:
    """True when a proposed stop is no farther from entry than the existing stop."""
    return new_stop >= old_stop if direction == 'buy' else new_stop <= old_stop


def stagnation_due(age_minutes: float, peak_r: float, after_minutes: float, minimum_r: float) -> bool:
    """Check whether a trade failed to make the configured progress in time."""
    return float(age_minutes) >= float(after_minutes) and float(peak_r) < float(minimum_r)


def may_extend(in_profit: bool, scaled_out: bool, stop_protects_profit: bool, mtf_agrees: bool,
               momentum_rising: bool, continuation_score: float, minimum_score: float, age_minutes: float,
               max_minutes: float) -> bool:
    """Allow an extension only when every profit-protection condition is met."""
    return (float(age_minutes) < float(max_minutes) and in_profit and scaled_out and
            stop_protects_profit and mtf_agrees and momentum_rising and continuation_score >= minimum_score)


def stop_protects_profit(direction: str, stop: float, entry: float, cost_distance: float) -> bool:
    """Require the stop to cover entry and estimated round-trip costs."""
    return float(stop) >= float(entry)+float(cost_distance) if direction == 'buy' else float(stop) <= float(entry)-float(cost_distance)


def continuation_metrics(candles: dict[str, pd.DataFrame], direction: str, fast=5, slow=13, roc_period=5) -> tuple[bool, bool, float]:
    """Return directional MTF agreement, improving directional momentum and score."""
    aligned=mtf_direction_agrees(candles,direction,('M1','M5','M15'),fast,slow)
    frame=candles.get('M1')
    if frame is None or len(frame)<roc_period*2+1: return aligned,False,0.0
    close=frame.close.astype(float)
    latest=float(close.iloc[-1]/close.iloc[-1-roc_period]-1)
    previous=float(close.iloc[-1-roc_period]/close.iloc[-1-2*roc_period]-1)
    rising=(latest>0 and latest>previous) if direction=='buy' else (latest<0 and latest<previous)
    score=float(aligned)+float(rising)
    return aligned,rising,score/2
