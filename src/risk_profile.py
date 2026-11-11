"""Account-type risk profiles: DEMO and REAL accounts get separate, locked limits.

The account type always comes from MT5 (``account_info().trade_mode``), never from
``config.yaml``: a REAL account is sized with REAL limits even if the config says demo.

REAL limits are the original locked ceilings in ``src/config.py`` and are unchanged.
DEMO limits are larger (virtual money) but still have hard ceilings here.
"""
from __future__ import annotations
from typing import Any
import math

# Hard ceilings for DEMO accounts. Config may lower them, never raise them.
DEMO_RISK_CEILINGS={'risk_per_trade_pct':2.0,'daily_loss_limit_pct':10.0,'weekly_loss_limit_pct':25.0,
                    'brain_min_risk_pct':2.0,'brain_max_risk_pct':2.0,'rolling_drawdown_pct':10.0}
# Brain dynamic-risk bounds for REAL accounts can never exceed the locked per-trade ceiling.
REAL_BRAIN_MAX_RISK_PCT=0.25

DEFAULT_DEMO={'risk_per_trade_pct':1.0,'daily_loss_limit_pct':10.0,'weekly_loss_limit_pct':25.0,
              'brain_min_risk_pct':1.0,'brain_max_risk_pct':2.0,'rolling_drawdown_pct':6.0}
DEFAULT_REAL_BRAIN={'brain_min_risk_pct':0.1,'brain_max_risk_pct':0.25}
# Small-balance fallback: trade the broker minimum lot when the % budget is too small,
# as long as that minimum lot risks at most this share of equity. DEMO only.
DEFAULT_MIN_LOT_MAX_RISK_PCT=3.0
MIN_LOT_MAX_RISK_CEILING_PCT=5.0


def is_demo_account(account: Any, mt5: Any=None) -> bool:
    """True only when MT5 reports a DEMO account. Unknown accounts are treated as REAL."""
    if account is None: return False
    mode=getattr(account,'trade_mode',None)
    if mode is None and isinstance(account,dict): mode=account.get('trade_mode')
    demo_value=getattr(mt5,'ACCOUNT_TRADE_MODE_DEMO',0) if mt5 is not None else 0
    try: return mode is not None and int(mode)==int(demo_value)
    except (TypeError,ValueError): return False


def _number(section: dict, key: str, default: float) -> float:
    try: value=float(section.get(key,default))
    except (TypeError,ValueError) as exc: raise ValueError(f'{key} must be a number') from exc
    if not math.isfinite(value) or value<0: raise ValueError(f'{key} must be finite and non-negative')
    return value


def validate_demo_risk(config: dict[str, Any]) -> None:
    """Reject DEMO settings above their hard ceilings or with inconsistent brain bounds."""
    section=config.get('demo_risk',{}) or {}
    if not isinstance(section,dict): raise ValueError('demo_risk must be a mapping')
    for key,ceiling in DEMO_RISK_CEILINGS.items():
        value=_number(section,key,DEFAULT_DEMO[key])
        if value>ceiling: raise ValueError(f'demo_risk.{key} exceeds locked DEMO ceiling {ceiling}')
    if _number(section,'brain_min_risk_pct',DEFAULT_DEMO['brain_min_risk_pct'])>_number(section,'brain_max_risk_pct',DEFAULT_DEMO['brain_max_risk_pct']):
        raise ValueError('demo_risk.brain_min_risk_pct cannot exceed brain_max_risk_pct')
    fallback=_number(section,'min_lot_max_risk_pct',DEFAULT_MIN_LOT_MAX_RISK_PCT)
    if fallback>MIN_LOT_MAX_RISK_CEILING_PCT: raise ValueError(f'demo_risk.min_lot_max_risk_pct exceeds locked DEMO ceiling {MIN_LOT_MAX_RISK_CEILING_PCT}')
    brain=config.get('brain',{}) or {}
    real=brain.get('real_risk',{}) or {}
    low=_number(real,'brain_min_risk_pct',DEFAULT_REAL_BRAIN['brain_min_risk_pct'])
    high=_number(real,'brain_max_risk_pct',DEFAULT_REAL_BRAIN['brain_max_risk_pct'])
    if high>REAL_BRAIN_MAX_RISK_PCT: raise ValueError(f'brain.real_risk.brain_max_risk_pct exceeds locked REAL ceiling {REAL_BRAIN_MAX_RISK_PCT}')
    if low>high: raise ValueError('brain.real_risk.brain_min_risk_pct cannot exceed brain_max_risk_pct')


def account_risk_profile(config: dict[str, Any], demo: bool) -> dict[str, float|bool]:
    """Return the per-trade risk, loss limits and brain bounds for this account type."""
    risk=config.get('risk',{})
    if demo:
        section=config.get('demo_risk',{}) or {}
        values={key:min(_number(section,key,DEFAULT_DEMO[key]),DEMO_RISK_CEILINGS[key]) for key in DEFAULT_DEMO}
        values['brain_min_risk_pct']=min(values['brain_min_risk_pct'],values['brain_max_risk_pct'])
        return {'demo':True,**values}
    real=(config.get('brain',{}) or {}).get('real_risk',{}) or {}
    per_trade=float(risk.get('risk_per_trade_pct',.25))
    high=min(_number(real,'brain_max_risk_pct',DEFAULT_REAL_BRAIN['brain_max_risk_pct']),REAL_BRAIN_MAX_RISK_PCT,per_trade)
    low=min(_number(real,'brain_min_risk_pct',DEFAULT_REAL_BRAIN['brain_min_risk_pct']),high)
    return {'demo':False,'risk_per_trade_pct':per_trade,
            'rolling_drawdown_pct':float(config.get('protections',{}).get('rolling_drawdown_pct',2.0)),
            'daily_loss_limit_pct':float(risk.get('daily_loss_limit_pct',3.0)),
            'weekly_loss_limit_pct':float(risk.get('weekly_loss_limit_pct',8.0)),
            'brain_min_risk_pct':low,'brain_max_risk_pct':high}


def base_risk_pct(config: dict[str, Any], demo: bool, trade_mode: str='main') -> float:
    """Fixed per-trade risk before any brain adjustment.

    DEMO: one fixed amount for every mode (main, weekend, exploration).
    REAL: original behaviour; weekend mode may only lower the risk."""
    profile=account_risk_profile(config,demo)
    if demo: return float(profile['risk_per_trade_pct'])
    risk_pct=float(profile['risk_per_trade_pct'])
    if trade_mode in ('weekend','weekend_exploration'):
        risk_pct=min(risk_pct,float(config.get('weekend',{}).get('risk_per_trade_pct',risk_pct)))
    return risk_pct


def min_lot_cap_pct(config: dict[str, Any], demo: bool) -> float:
    """Largest equity share one minimum-lot trade may risk on a small balance (0 = disabled).

    REAL accounts never use the fallback: their locked 0.25% ceiling always applies
    (use an XM Micro account for small real balances)."""
    if not demo: return 0.0
    section=config.get('demo_risk',{}) or {}
    return min(_number(section,'min_lot_max_risk_pct',DEFAULT_MIN_LOT_MAX_RISK_PCT),MIN_LOT_MAX_RISK_CEILING_PCT)


def applied_risk_pct(config: dict[str, Any], demo: bool, trade_mode: str='main', features: dict|None=None) -> float:
    """Base risk, or the brain's bounded risk when the scanner marked brain sizing live.

    The brain value is re-clamped here with the account's own bounds, so a stale or
    tampered feature payload can never exceed the account ceiling."""
    base=base_risk_pct(config,demo,trade_mode)
    min_lot=(features or {}).get('min_lot',{}) if isinstance(features,dict) else {}
    if isinstance(min_lot,dict) and min_lot.get('active'):
        # Ceiling for the minimum-lot trade; callers still trade only the minimum volume.
        cap=min_lot_cap_pct(config,demo)
        if cap>base: return cap
    brain=(features or {}).get('brain',{}) if isinstance(features,dict) else {}
    sizing=brain.get('sizing',{}) if isinstance(brain,dict) else {}
    if not isinstance(sizing,dict) or not sizing.get('active'): return base
    try: value=float(sizing.get('risk_pct'))
    except (TypeError,ValueError): return base
    if not math.isfinite(value) or value<=0: return base
    profile=account_risk_profile(config,demo)
    return max(float(profile['brain_min_risk_pct']),min(float(profile['brain_max_risk_pct']),value))


def loss_limits(config: dict[str, Any], demo: bool) -> tuple[float,float]:
    """Daily and weekly loss limits (percent of balance) for this account type."""
    profile=account_risk_profile(config,demo)
    return float(profile['daily_loss_limit_pct']),float(profile['weekly_loss_limit_pct'])
