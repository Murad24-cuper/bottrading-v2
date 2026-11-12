"""Live read-out of what the bot thinks about each open position.

The same rules the executor uses to manage a trade (early exit, extension,
stagnation, time limit) are evaluated here and summarised for the dashboard,
so the panel shows *why* the bot holds, closes or extends a position.
Nothing here places or closes orders."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .trade_rules import adverse_exit_score, continuation_metrics

DANGER_KEYS = ('ema_cross', 'swing_break', 'momentum', 'volume_spike')


def _f(value, default=0.0):
    try: return float(value)
    except (TypeError, ValueError): return default


def analyze(direction: str, entry: float, current: float | None, row: dict, risk_cfg: dict, scanner_cfg: dict,
            candles: dict, age_minutes: float, max_hold_minutes: float, stagnation_minutes: float,
            stagnation_min_r: float) -> dict[str, Any]:
    """Return a JSON-friendly summary; every part degrades gracefully when data is missing."""
    initial_risk = _f(row.get('initial_risk')) or abs(entry - _f(row.get('sl')))
    current_r = None
    if current is not None and initial_risk > 0:
        current_r = ((current - entry) if direction == 'buy' else (entry - current)) / initial_risk
    peak_r = max(_f(row.get('peak_favorable_r')), current_r or 0.0)

    weights = risk_cfg.get('early_exit_weights', {}) or {}
    danger_max = sum(int(weights.get(key, 1)) for key in DANGER_KEYS)
    try: danger, reasons = adverse_exit_score(candles.get('M1'), direction, risk_cfg)
    except Exception: danger, reasons = 0, []
    threshold = int(risk_cfg.get('early_exit_score_threshold', 3))
    low, high = _f(risk_cfg.get('early_exit_min_r'), -.7), _f(risk_cfg.get('early_exit_max_r'), -.2)
    in_band = current_r is not None and low <= current_r <= high
    early_enabled = bool(risk_cfg.get('early_exit_enabled', True))

    try: mtf, rising, cont = continuation_metrics(candles, direction)
    except Exception: mtf, rising, cont = False, False, 0.0
    cont_min = _f(scanner_cfg.get('extend_min_score', risk_cfg.get('extend_min_score', .7)), .7)
    extended_until = row.get('extension_until')
    hard_limit = _f(risk_cfg.get('max_hold_extended_minutes'), max_hold_minutes)

    minutes_left = max(0.0, max_hold_minutes - age_minutes)
    if early_enabled and in_band and danger >= threshold:
        plan, tone = 'Erkən bağlanır: əleyhinə əlamətlər yığılıb', 'bad'
    elif early_enabled and danger >= threshold and current_r is not None and current_r < low:
        plan, tone = 'Zərər SL-ə yaxındır: əlamətlər pisdir, SL qoruyur', 'bad'
    elif early_enabled and danger >= threshold:
        plan, tone = f'Təhlükə yüksəkdir: zərər {abs(high):.1f}R-ə çatsa erkən bağlanacaq', 'warn'
    elif age_minutes >= stagnation_minutes - 2 and peak_r < stagnation_min_r:
        plan, tone = f'Hərəkət yoxdur: {max(0.0, stagnation_minutes - age_minutes):.0f} dəq sonra bağlanacaq', 'warn'
    elif extended_until:
        plan, tone = 'Vaxt uzadılıb: qazanc qorunur, davam etdikcə saxlanır', 'good'
    elif minutes_left <= 5 and (current_r or 0) > 0 and mtf and rising and cont >= cont_min:
        plan, tone = 'Yaxşı gedir: vaxt bitəndə uzadılacaq', 'good'
    elif minutes_left <= 5:
        plan, tone = f'Vaxt bitir: {minutes_left:.0f} dəq sonra bağlanacaq', 'warn'
    else:
        plan, tone = 'Saxlanır: SL/TP və qaydalar izlənir', 'neutral'

    return {
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'current_r': current_r, 'peak_r': peak_r, 'age_minutes': age_minutes,
        'minutes_left': minutes_left, 'hard_limit_minutes': hard_limit,
        'danger': {'score': int(danger), 'max': int(danger_max), 'threshold': threshold, 'reasons': reasons,
                   'enabled': early_enabled, 'band': [low, high], 'in_band': in_band},
        'continuation': {'mtf': bool(mtf), 'rising': bool(rising), 'score': float(cont), 'minimum': cont_min},
        'extended': bool(extended_until), 'plan': plan, 'tone': tone,
    }
