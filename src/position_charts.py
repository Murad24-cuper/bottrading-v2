"""Small price snapshots of open positions for the dashboard charts.

The dashboard reads only local files/SQLite and never talks to MT5, so the bot
writes recent candles for each open bot position to ``data/charts/<ticket>.json``.
Writing is best effort: any failure here is logged and never affects trading."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import os

TIMEFRAMES=(('M1',120),('M5',120))


def _bars(frame) -> list[dict[str, Any]]:
    rows=[]
    for stamp,row in frame.iterrows():
        rows.append({'t':stamp.isoformat(),'o':float(row['open']),'h':float(row['high']),'l':float(row['low']),'c':float(row['close'])})
    return rows


def _atomic_write(path: Path, payload: dict) -> None:
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload,separators=(',',':')),encoding='utf-8')
    os.replace(tmp,path)


def write_position_charts(mt5, positions, data_dir: str|Path, open_rows: dict|None=None) -> int:
    """Write one chart file per open position and remove files of closed ones.

    Returns how many chart files were written."""
    from . import market_data
    from .time_utils import mt5_epoch_to_utc
    folder=Path(data_dir)/'charts'; folder.mkdir(parents=True,exist_ok=True)
    open_rows=open_rows or {}
    keep=set(); written=0; candle_cache={}
    for p in positions:
        ticket=int(p.ticket); keep.add(f'{ticket}.json')
        frames={}
        for timeframe,count in TIMEFRAMES:
            key=(str(p.symbol),timeframe)
            if key not in candle_cache: candle_cache[key]=market_data.get_candles(mt5,str(p.symbol),timeframe,count)
            frames[timeframe]=_bars(candle_cache[key])
        tick=mt5.symbol_info_tick(p.symbol)
        is_buy=int(p.type)==int(getattr(mt5,'POSITION_TYPE_BUY',0))
        current=float(tick.bid if is_buy else tick.ask) if tick is not None else None
        try: opened=mt5_epoch_to_utc(int(p.time),mt5).isoformat()
        except Exception: opened=str(open_rows.get(ticket,{}).get('open_time') or '')
        row=open_rows.get(ticket,{})
        payload={'ticket':ticket,'symbol':str(p.symbol),'direction':'buy' if is_buy else 'sell','volume':float(p.volume),
                 'open_time':opened,'open_price':float(p.price_open),'sl':float(p.sl or 0) or None,'tp':float(p.tp or 0) or None,
                 'current':current,'profit':float(getattr(p,'profit',0) or 0),
                 'strategy':row.get('strategy'),'updated_at':datetime.now(timezone.utc).isoformat(),'bars':frames}
        _atomic_write(folder/f'{ticket}.json',payload); written+=1
    for stale in folder.glob('*.json'):
        if stale.name not in keep:
            try: stale.unlink()
            except OSError: pass
    return written


def read_position_chart(data_dir: str|Path, ticket: int) -> dict|None:
    try: return json.loads((Path(data_dir)/'charts'/f'{int(ticket)}.json').read_text(encoding='utf-8'))
    except (OSError,ValueError,TypeError): return None
