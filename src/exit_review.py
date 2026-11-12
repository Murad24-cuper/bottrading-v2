"""Was the bot right to close early? Checks what the price did after each bot-decided exit.

For trades the bot closed itself (early exit, lost momentum, stagnation, time limit)
we replay the next ``review_minutes`` of M1 bars against the trade's ORIGINAL stop
and target. If the stop would have been hit first the exit saved money; if the
target would have been hit first the exit cost money. ``review_delta`` is in
account currency for the closed volume: positive = keeping the trade would have
been better, negative = closing was the better decision."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

REVIEWED_REASONS = ('early_exit', 'momentum_lost', 'stagnant', 'time')


def _utc(value) -> datetime:
    stamp = datetime.fromisoformat(str(value))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def replay(direction: str, entry: float, initial_risk: float, target: float | None, bars) -> tuple[str, float]:
    """Walk bars in order; a bar touching both stop and target counts as the stop (conservative)."""
    buy = direction == 'buy'
    stop = entry - initial_risk if buy else entry + initial_risk
    target_r = abs(float(target) - entry) / initial_risk if target else None
    last_r = 0.0
    for _, bar in bars.iterrows():
        low, high, close = float(bar['low']), float(bar['high']), float(bar['close'])
        if (low <= stop) if buy else (high >= stop): return 'sl', -1.0
        if target_r is not None and ((high >= target) if buy else (low <= target)): return 'tp', target_r
        last_r = ((close - entry) if buy else (entry - close)) / initial_risk
    return 'open', last_r


def review_exits(db, mt5, now: datetime | None = None, review_minutes: int = 30, limit: int = 10) -> int:
    """Review exits older than ``review_minutes``; returns how many trades were reviewed."""
    from . import market_data
    now = now or datetime.now(timezone.utc)
    rows = db.get_exits_to_review(REVIEWED_REASONS, (now - timedelta(minutes=review_minutes)).isoformat(),
                                  (now - timedelta(days=2)).isoformat(), limit)
    done = 0
    for row in rows:
        ticket = int(row['ticket'])
        try:
            entry = float(row['open_price']); exit_price = float(row['close_price'])
            risk = float(row.get('initial_risk') or 0)
            if risk <= 0: db.set_exit_review(ticket, 'unknown'); continue
            closed = _utc(row['close_time']); horizon = closed + timedelta(minutes=review_minutes)
            minutes = int((now - closed).total_seconds() // 60) + 5
            if minutes > 3000: db.set_exit_review(ticket, 'unknown'); continue
            frame = market_data.get_candles(mt5, str(row['symbol']), 'M1', minutes)
            bars = frame[(frame.index > closed) & (frame.index <= horizon)]
            if bars.empty: db.set_exit_review(ticket, 'unknown'); continue
            outcome, review_r = replay(row['direction'], entry, risk, row.get('tp'), bars)
            exit_r = ((exit_price - entry) if row['direction'] == 'buy' else (entry - exit_price)) / risk
            delta = None
            info = mt5.symbol_info(str(row['symbol']))
            tick_value = float(getattr(info, 'trade_tick_value', 0) or 0); tick_size = float(getattr(info, 'trade_tick_size', 0) or 0)
            if tick_value > 0 and tick_size > 0:
                risk_money = float(row.get('volume') or 0) * risk / tick_size * tick_value
                delta = round((review_r - exit_r) * risk_money, 2)
            db.set_exit_review(ticket, outcome, round(review_r, 3), delta); done += 1
        except Exception:
            continue
    return done
