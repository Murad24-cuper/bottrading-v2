"""Pure evaluation of symbol-loss and rolling-drawdown pause rules."""
from __future__ import annotations
from datetime import datetime,timezone,timedelta


def check_protections(symbol_trades, equity_snapshots, symbol, now, config):
    """Return `(blocked, reason)` for config-driven symbol/global protections."""
    now=now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    if not config.get('enabled',True): return False,''
    symbol_pauses=bool(config.get('symbol_pauses_enabled',True))   # per-symbol loss pauses; the rolling drawdown below always applies
    rows=sorted((row for row in symbol_trades if str(row.get('symbol'))==str(symbol)),
                key=lambda row:str(row.get('close_time','')),reverse=True)
    consecutive=int(config.get('consecutive_losses',3)); cooldown=float(config.get('cooldown_hours',24))
    losses=0; latest_loss=None
    for row in rows:
        net=float(row.get('net',0) or 0)
        if net>=0: break
        losses+=1
        if latest_loss is None: latest_loss=row.get('close_time')
    if symbol_pauses and losses>=consecutive and latest_loss:
        try: close=datetime.fromisoformat(str(latest_loss).replace('Z','+00:00'))
        except ValueError: close=None
        if close:
            close=close.replace(tzinfo=timezone.utc) if close.tzinfo is None else close.astimezone(timezone.utc)
            if close+timedelta(hours=cooldown)>now:
                return True,f'symbol cooldown after {losses} consecutive losses'
    day_start=now.replace(hour=0,minute=0,second=0,microsecond=0)
    day_r=sum(float(row.get('outcome_r') or 0) for row in rows
              if _utc(row.get('close_time')) and _utc(row.get('close_time'))>=day_start)
    if symbol_pauses and day_r<=-abs(float(config.get('symbol_daily_loss_r',2.0))):
        return True,f'symbol daily loss limit reached ({day_r:.2f}R)'
    window=float(config.get('drawdown_window_hours',24)); cutoff=now-timedelta(hours=window)
    values=[]
    for row in equity_snapshots:
        stamp=_utc(row.get('time'))
        value=float(row.get('equity') or 0)
        if stamp and stamp>=cutoff and value>0: values.append((stamp,value))
    values.sort(key=lambda item:item[0])
    if len(values)>=2:
        peak=max(value for _,value in values); current=values[-1][1]
        drawdown=(peak-current)/peak*100 if peak>0 else 0.0
        threshold=float(config.get('rolling_drawdown_pct',2.0))
        if drawdown>=threshold: return True,f'rolling drawdown pause ({drawdown:.2f}%)'
    return False,''


def _utc(value):
    try: stamp=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    except (TypeError,ValueError): return None
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
