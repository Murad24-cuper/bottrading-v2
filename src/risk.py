"""Pure position sizing and risk gates; this module never calls MT5."""
from __future__ import annotations
from pathlib import Path
from datetime import datetime,timezone
import math
from .symbols import underlying_key,underlying_group

def calc_volume(equity: float, risk_pct: float, entry: float, sl: float, symbol_spec: dict) -> tuple[float,str]:
    """Size from stop risk and round down to the broker volume step."""
    distance=abs(float(entry)-float(sl))
    tick_value=float(symbol_spec.get('trade_tick_value_loss') or symbol_spec.get('trade_tick_value') or symbol_spec.get('tick_value') or 0)
    tick_size=float(symbol_spec.get('trade_tick_size') or symbol_spec.get('tick_size') or 0)
    contract_size=float(symbol_spec.get('trade_contract_size') or symbol_spec.get('contract_size') or 0)
    if tick_value<=0 and contract_size>0 and tick_size>0 and distance>0:
        tick_value=contract_size*tick_size
    minimum=float(symbol_spec.get('volume_min') or 0); step=float(symbol_spec.get('volume_step') or 0); maximum=float(symbol_spec.get('volume_max') or 0)
    if not all(math.isfinite(x) for x in (distance,tick_value,tick_size)) or distance<=0 or tick_value<=0 or tick_size<=0: return 0,'invalid stop or tick specification'
    if not math.isfinite(equity) or not math.isfinite(risk_pct) or equity<=0 or risk_pct<=0: return 0,'equity and risk percent must be positive'
    if not all(math.isfinite(x) for x in (minimum,step,maximum)) or minimum<=0 or step<=0 or maximum<minimum: return 0,'invalid volume specification'
    per_lot=distance/tick_size*tick_value; allowed=float(equity)*float(risk_pct)/100
    if per_lot*minimum>allowed+1e-12: return 0,'min lot exceeds risk'
    raw=min(allowed/per_lot,maximum)
    # Volume grids start at volume_min; floor avoids exceeding the requested risk.
    units=int((raw-minimum+1e-12)/step); volume=min(maximum,minimum+units*step)
    volume=round(volume,8)
    if per_lot*volume>allowed+1e-9:
        volume=round(max(0,volume-step),8)
    return (volume,'ok') if volume>=minimum else (0,'min lot exceeds risk')


def min_lot_risk_money(entry: float, sl: float, symbol_spec: dict, extra_cost_per_lot: float=0.0) -> float|None:
    """Money lost at the stop with the broker's minimum volume (plus optional cost per lot)."""
    distance=abs(float(entry)-float(sl))
    tick_value=float(symbol_spec.get('trade_tick_value_loss') or symbol_spec.get('trade_tick_value') or symbol_spec.get('tick_value') or 0)
    tick_size=float(symbol_spec.get('trade_tick_size') or symbol_spec.get('tick_size') or 0)
    contract_size=float(symbol_spec.get('trade_contract_size') or symbol_spec.get('contract_size') or 0)
    if tick_value<=0 and contract_size>0 and tick_size>0: tick_value=contract_size*tick_size
    minimum=float(symbol_spec.get('volume_min') or 0)
    values=(distance,tick_value,tick_size,minimum,float(extra_cost_per_lot))
    if not all(math.isfinite(x) for x in values) or distance<=0 or tick_value<=0 or tick_size<=0 or minimum<=0: return None
    return (distance/tick_size*tick_value+max(0.0,float(extra_cost_per_lot)))*minimum


def calc_volume_stressed(equity: float, risk_pct: float, entry: float, sl: float,
                         symbol_spec: dict, extra_cost_per_lot: float) -> tuple[float,str]:
    """Size so stop loss plus conservative per-lot costs stay inside the risk budget."""
    if not math.isfinite(float(extra_cost_per_lot)) or float(extra_cost_per_lot)<0:
        return 0,'invalid stressed cost estimate'
    distance=abs(float(entry)-float(sl))
    tick_value=float(symbol_spec.get('trade_tick_value_loss') or symbol_spec.get('trade_tick_value') or symbol_spec.get('tick_value') or 0)
    tick_size=float(symbol_spec.get('trade_tick_size') or symbol_spec.get('tick_size') or 0)
    contract_size=float(symbol_spec.get('trade_contract_size') or symbol_spec.get('contract_size') or 0)
    if tick_value<=0 and contract_size>0 and tick_size>0 and distance>0:
        tick_value=contract_size*tick_size
    if distance<=0 or tick_value<=0 or tick_size<=0: return 0,'invalid stop or tick specification'
    stressed_spec=dict(symbol_spec)
    stressed_spec['trade_tick_value_loss']=tick_value+float(extra_cost_per_lot)*tick_size/distance
    return calc_volume(equity,risk_pct,entry,sl,stressed_spec)

def _value(obj,key,default=None): return obj.get(key,default) if isinstance(obj,dict) else getattr(obj,key,default)

def _group(position):
    explicit=_value(position,'asset_group')
    if explicit: return explicit
    symbol=str(_value(position,'symbol','')).upper()
    patterns={'metals':('XAU','XAG','GOLD','SILVER'),'crypto':('BTC','ETH','CRYPTO'),'energy':('OIL','WTI','BRENT','GAS'),'indices':('US30','US100','NAS','SPX','DAX','UK100')}
    for group,parts in patterns.items():
        if any(part in symbol for part in parts): return group
    if len(symbol)>=6 and symbol[:6].isalpha(): return 'forex'
    return 'stocks' if symbol.rstrip('#m.') and len(symbol.rstrip('#m.'))<=5 and symbol.rstrip('#m.').isalpha() else 'other'

def _currency_pair(symbol,spec=None,position=None):
    spec=spec or {}; position=position or {}
    base=spec.get('currency_base') or _value(position,'currency_base')
    quote=spec.get('currency_profit') or _value(position,'currency_profit')
    if base and quote: return str(base).upper(),str(quote).upper()
    clean=underlying_key(symbol)
    return (clean[:3],clean[3:6]) if len(clean)>=6 and clean[:6].isalpha() else None

def _currency_exposure(positions,extra=None):
    totals={}
    for position in [*positions,*([extra] if extra else [])]:
        group=_value(position,'asset_group') or _group(position)
        if group not in {'forex','metals'}: continue
        pair=_currency_pair(str(_value(position,'symbol','')),position=position)
        if not pair: continue
        sign=1 if str(_value(position,'direction','')).lower()=='buy' else -1
        base,quote=pair; totals[base]=totals.get(base,0)+sign; totals[quote]=totals.get(quote,0)-sign
    return totals

def check_trade_allowed(signal, account, open_positions, pnl_today, pnl_week, settings, cost_estimate, symbol_group, now_utc: datetime, required_margin, volume: float | None = None, symbol_spec: dict | None = None, symbol_cooldown: bool = False, symbol_trades_today: int = 0, session_closing: bool = False, trades_today: int = 0, group_trades_today: int = 0):
    """Apply all configured risk gates in the fixed order defined by the brief."""
    config=settings.config if hasattr(settings,'config') else settings
    risk=config['risk']; root=Path(getattr(settings,'root',Path.cwd()))
    if (root/'STOP').exists() or (root/'STOP.txt').exists(): return False,'kill switch active'
    now_utc=now_utc.replace(tzinfo=timezone.utc) if now_utc.tzinfo is None else now_utc.astimezone(timezone.utc)
    if session_closing: return False,'symbol session closes soon'
    floating=sum(float(_value(p,'profit',0) or 0) for p in open_positions)
    balance=float(_value(account,'balance',_value(account,'equity',0)))
    from .risk_profile import is_demo_account,loss_limits
    daily_limit,weekly_limit=loss_limits(config,is_demo_account(account)) if 'demo_risk' in config else (risk['daily_loss_limit_pct'],risk['weekly_loss_limit_pct'])
    if float(pnl_today)+floating<=-balance*daily_limit/100: return False,'daily loss limit reached'
    if float(pnl_week)+floating<=-balance*weekly_limit/100: return False,'weekly loss limit reached'
    if len(open_positions)>=risk['max_open_positions']: return False,'maximum open positions reached'
    if sum(_group(p)==symbol_group for p in open_positions)>=risk['max_positions_per_group']: return False,'maximum positions for asset group reached'
    if any(_value(p,'symbol')==signal.symbol for p in open_positions): return False,'symbol already has open position'
    if any(underlying_key(_value(p,'symbol',''))==underlying_key(signal.symbol) for p in open_positions): return False,'underlying already has open position'
    if symbol_cooldown: return False,'symbol cooldown active'
    if int(symbol_trades_today)>=risk['max_trades_per_symbol_per_day']: return False,'maximum trades for symbol today reached'
    if int(trades_today)>=int(risk.get('max_trades_per_day',150)): return False,'maximum trades today reached'
    group_share=float(risk.get('max_group_share_today',0) or 0)
    # 0 (or >= 1) disables the rule; only 0 < share < 1 limits one group's share of today's trades.
    if 0<group_share<1 and int(trades_today)>=10 and int(group_trades_today)>=math.ceil(group_share*int(trades_today)):
        return False,'maximum asset-group share today reached'
    candidate_underlying=underlying_key(signal.symbol); candidate_group=underlying_group(signal.symbol,symbol_group)
    for position in open_positions:
        group=_value(position,'exposure_group') or underlying_group(_value(position,'symbol',''),_value(position,'asset_group') or _group(position))
        underlying=_value(position,'underlying') or underlying_key(_value(position,'symbol',''))
        if candidate_group and group==candidate_group and underlying==candidate_underlying: return False,'underlying already has open position'
    if candidate_group:
        group_count=sum((_value(p,'exposure_group') or underlying_group(_value(p,'symbol',''),_value(p,'asset_group') or _group(p)))==candidate_group for p in open_positions)
        if group_count>=risk.get('max_positions_per_underlying_group',2): return False,'maximum positions for underlying group reached'
    pair=_currency_pair(signal.symbol,spec=symbol_spec)
    if pair:
        base,quote=pair
        exposure=_currency_exposure(open_positions,{'symbol':signal.symbol,'asset_group':symbol_group,'direction':signal.direction,'currency_base':base,'currency_profit':quote})
        for currency,value in exposure.items():
            if abs(value)>risk.get('max_positions_per_currency',2): return False,f'maximum net exposure for {currency} reached'
    sl,tp,entry=signal.sl_price,signal.tp_price,signal.entry_hint
    if sl is None: return False,'stop-loss is required'
    if tp is None or entry is None: return False,'entry and take-profit are required'
    if signal.direction=='buy' and not sl<entry<tp: return False,'invalid SL/TP side for buy'
    if signal.direction=='sell' and not tp<entry<sl: return False,'invalid SL/TP side for sell'
    cost=float(cost_estimate.get('cost',0) or 0) if isinstance(cost_estimate,dict) else float(cost_estimate or 0)
    spec=symbol_spec or {}
    tick_value=float(spec.get('trade_tick_value_profit') or spec.get('trade_tick_value') or spec.get('tick_value') or spec.get('trade_tick_value_loss') or 0)
    loss_tick_value=float(spec.get('trade_tick_value_loss') or spec.get('trade_tick_value') or spec.get('tick_value') or tick_value)
    tick_size=float(spec.get('trade_tick_size') or spec.get('tick_size') or 0)
    if volume is None or not all(math.isfinite(x) for x in (float(volume),tick_value,loss_tick_value,tick_size)) or volume<=0 or tick_value<=0 or loss_tick_value<=0 or tick_size<=0:
        return False,'cost-to-target estimate cannot be converted to money'
    target_money=abs(float(tp)-float(entry))/tick_size*tick_value*volume
    risk_money=abs(float(entry)-float(sl))/tick_size*loss_tick_value*volume
    if not math.isfinite(target_money) or not math.isfinite(cost) or target_money<=0 or cost<0 or cost/target_money>risk['max_cost_to_target_ratio']: return False,'cost-to-target ratio too high'
    if (target_money-cost)/risk_money<float(risk['min_risk_reward']): return False,'net risk/reward below minimum'
    equity=float(_value(account,'equity',balance)); margin=float(_value(account,'margin',0))
    if not math.isfinite(equity) or not math.isfinite(margin) or not math.isfinite(float(required_margin)) or equity<=0 or required_margin<0 or (margin+float(required_margin))/equity*100>risk['max_margin_usage_pct']: return False,'margin usage cap exceeded'
    return True,'allowed'
