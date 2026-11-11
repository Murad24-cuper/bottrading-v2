"""When may the brain act on live trades, and how much risk may it suggest?

The brain stays shadow-only (observe, never act) unless EVERY condition holds:
  1. config brain.enabled is true and brain.shadow is false
  2. data/brain/gate.json reports status PASS (offline acceptance test)
  3. data/brain/live_gate.json has not switched the brain back to shadow (live drift)

When live, the brain may only *block* a signal it rejects and choose a risk
inside the account's locked bounds (see src/risk_profile.py). It can never open
extra trades or bypass the risk manager.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any
import json
import math


def live_status(brain_cfg: dict[str, Any], data_dir: str|Path) -> dict[str, Any]:
    """Return {'active': bool, 'reasons': [...]} explaining why the brain is (not) live."""
    reasons=[]
    if not brain_cfg.get('enabled',False): reasons.append('brain.enabled is false')
    if brain_cfg.get('shadow',True): reasons.append('brain.shadow is true')
    brain_dir=Path(data_dir)/'brain'
    try: gate=json.loads((brain_dir/'gate.json').read_text(encoding='utf-8'))
    except (OSError,ValueError): gate={}
    if gate.get('status')!='PASS': reasons.append('acceptance gate has not passed')
    try: live_gate=json.loads((brain_dir/'live_gate.json').read_text(encoding='utf-8'))
    except (OSError,ValueError): live_gate={}
    if live_gate.get('mode')=='shadow': reasons.append('live drift guard returned the brain to shadow mode')
    return {'active':not reasons,'reasons':reasons}


def dynamic_risk(memory: dict[str, Any], decision: dict[str, Any], kelly_fraction: float,
                 min_pct: float, max_pct: float, recent_losses: int=0, loss_streak_limit: int=3) -> dict[str, Any]:
    """Map the brain's calibrated probability to a bounded risk percentage.

    Returns {'active': bool, 'risk_pct': float|None, 'reason': str}. Inactive means
    "use the account's fixed base risk". The result is always within [min_pct, max_pct]."""
    from .sizing import fractional_kelly_risk_pct
    if not decision.get('accepted'): return {'active':False,'risk_pct':None,'reason':'brain rejected the signal'}
    if memory.get('out_of_distribution') is not False:
        return {'active':False,'risk_pct':None,'reason':'situation not verified as in-distribution'}
    probability=memory.get('probability'); win_r=float(memory.get('avg_win_r',0) or 0)
    if probability is None or not math.isfinite(float(probability)):
        return {'active':False,'risk_pct':None,'reason':'no calibrated probability'}
    if int(recent_losses)>=int(loss_streak_limit):
        return {'active':False,'risk_pct':None,'reason':f'{int(recent_losses)} consecutive losses; base risk until a win'}
    low=float(min_pct); high=float(max_pct)
    if not (math.isfinite(low) and math.isfinite(high)) or high<=0 or low>high:
        return {'active':False,'risk_pct':None,'reason':'invalid brain risk bounds'}
    # fractional_kelly_risk_pct already applies the Kelly fraction; bound it to this account.
    kelly=fractional_kelly_risk_pct(float(probability),win_r,float(kelly_fraction),minimum=0.0,maximum=100.0)
    if kelly<=0: return {'active':False,'risk_pct':None,'reason':'no positive Kelly edge'}
    risk_pct=max(low,min(high,kelly))
    return {'active':True,'risk_pct':risk_pct,'kelly_pct':kelly,
            'reason':f'p={float(probability):.3f}, avg_win_r={win_r:.2f}, kelly={kelly:.3f}% -> {risk_pct:.3f}%'}
