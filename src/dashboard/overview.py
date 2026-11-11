"""Pure helpers for the dashboard "at a glance" panel (no Streamlit imports, testable)."""
from __future__ import annotations
from typing import Any
import os


def runtime_label(config: dict[str, Any], env: dict[str, str]|None=None) -> dict[str, str]:
    """Where the dashboard/bot is running: Windows computer or Linux server."""
    env=os.environ if env is None else env
    expected=str(config.get('runtime',{}).get('location','local')).strip().lower()
    running=str(env.get('BOT_LOCATION','local')).strip().lower()
    place='Linux server' if running=='server' else 'Windows kompüter'
    if expected!=running:
        return {'place':place,'state':'warn','text':f'{place} · config "{expected}" gözləyir: bot burada başlamayacaq'}
    return {'place':place,'state':'good','text':place}


def _limit(used_loss: float, balance: float, limit_pct: float) -> dict[str, float]:
    limit_money=max(0.0,float(balance))*float(limit_pct)/100
    loss=max(0.0,-float(used_loss))
    share=min(1.0,loss/limit_money) if limit_money>0 else 0.0
    return {'limit_pct':float(limit_pct),'limit_money':limit_money,'loss_money':loss,
            'left_money':max(0.0,limit_money-loss),'used_share':share}


def risk_overview(config: dict[str, Any], account_type: str, balance: float|None,
                  pnl_today: float, pnl_week: float, floating: float) -> dict[str, Any]:
    """Risk per trade and how much of the daily/weekly loss limits is already used.

    Mirrors the bot: account type from MT5 status, DEMO/REAL profile from
    src/risk_profile.py, floating P/L counted against the limits."""
    from src.risk_profile import account_risk_profile
    demo=str(account_type).upper()=='DEMO'
    known=str(account_type).upper() in {'DEMO','REAL'}
    if 'demo_risk' in config:
        profile=account_risk_profile(config,demo)
    else:
        risk=config.get('risk',{})
        profile={'demo':demo,'risk_per_trade_pct':float(risk.get('risk_per_trade_pct',.25)),
                 'daily_loss_limit_pct':float(risk.get('daily_loss_limit_pct',3)),'weekly_loss_limit_pct':float(risk.get('weekly_loss_limit_pct',8)),
                 'brain_min_risk_pct':0.0,'brain_max_risk_pct':0.0}
    base=float(balance or 0)
    risk_pct=float(profile['risk_per_trade_pct'])
    return {'account':'DEMO' if demo else 'REAL' if known else 'NAMƏLUM','known':known,
            'risk_pct':risk_pct,'risk_money':base*risk_pct/100,
            'brain_min_pct':float(profile['brain_min_risk_pct']),'brain_max_pct':float(profile['brain_max_risk_pct']),
            'daily':_limit(float(pnl_today)+float(floating),base,float(profile['daily_loss_limit_pct'])),
            'weekly':_limit(float(pnl_week)+float(floating),base,float(profile['weekly_loss_limit_pct']))}


def brain_overview(config: dict[str, Any], data_dir) -> dict[str, Any]:
    """Short brain status for the dashboard, same rules the scanner uses."""
    from src.brain.live import live_status
    status=live_status(config.get('brain',{}),data_dir)
    if status['active']:
        return {'state':'good','title':'Beyin aktivdir','text':'Pis siqnalları bloklayır və riski ehtimala görə seçir','reasons':[]}
    labels={'brain.enabled is false':'config-də söndürülüb (brain.enabled: false)',
            'brain.shadow is true':'müşahidə rejimi (brain.shadow: true)',
            'acceptance gate has not passed':'qəbul testindən keçməyib (gate.json PASS deyil)',
            'live drift guard returned the brain to shadow mode':'canlı sapma qoruyucusu müşahidəyə qaytarıb'}
    return {'state':'neutral','title':'Beyin müşahidə edir','text':'Ticarətə təsir etmir',
            'reasons':[labels.get(reason,reason) for reason in status['reasons']]}


def stop_state(root) -> str:
    """'halt' (full stop), 'pause' (no new entries) or 'none', from the STOP files."""
    import re
    from pathlib import Path
    state='none'
    for name in ('STOP','STOP.txt'):
        path=Path(root)/name
        if not path.exists(): continue
        try: content=path.read_text(encoding='utf-8')
        except OSError: content=''
        if re.search(r'\bHALT\b',content,re.IGNORECASE): return 'halt'
        state='pause'
    return state
