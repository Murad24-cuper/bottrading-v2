"""Typed configuration loading and validation."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os
import math
import yaml
from dotenv import load_dotenv

# Hard safety ceiling. These bounds may be lowered in config but cannot be
# raised by config files, self-learning, dashboard or Telegram commands.
LOCKED_RISK_CEILINGS={'risk_per_trade_pct':.25,'daily_loss_limit_pct':3.0,
    'weekly_loss_limit_pct':8.0,'max_open_positions':20,
    'max_positions_per_group':6,'max_margin_usage_pct':50.0,
    'max_total_open_risk_pct':5.0,'max_cost_to_target_ratio':.30,
    'min_risk_reward':1.2}

def validate_risk_ceilings(config: dict[str,Any]) -> None:
    """Reject attempts to raise a locked risk ceiling or lower the RR floor."""
    risk=config.get('risk',{})
    for key,ceiling in LOCKED_RISK_CEILINGS.items():
        try: value=float(risk[key])
        except (KeyError,TypeError,ValueError) as exc: raise ValueError(f'risk.{key} is required by locked risk ceiling') from exc
        if not math.isfinite(value): raise ValueError(f'risk.{key} must be finite')
        if key=='min_risk_reward':
            if value<ceiling: raise ValueError(f'risk.{key} cannot be below locked floor {ceiling}')
        elif value>ceiling:
            raise ValueError(f'risk.{key} exceeds locked ceiling {ceiling}')
    for section_name in ('demo_exploration','weekend'):
        section=config.get(section_name,{})
        if not isinstance(section,dict): continue
        for key in ('risk_per_trade_pct','max_open_positions'):
            if key in section:
                try: val=float(section[key])
                except (TypeError,ValueError) as exc: raise ValueError(f'{section_name}.{key} must be a number') from exc
                if not math.isfinite(val): raise ValueError(f'{section_name}.{key} must be finite')
                ceiling=LOCKED_RISK_CEILINGS[key]
                if val>ceiling:
                    raise ValueError(f'{section_name}.{key} exceeds locked ceiling {ceiling}')

def assert_runtime_location(config: dict[str, Any], actual: str | None = None) -> None:
    """Refuse to start a bot from a machine not selected by config."""
    expected=config.get('runtime',{}).get('location','local')
    running=(actual if actual is not None else os.getenv('BOT_LOCATION','local')).strip().lower()
    if expected != running:
        raise RuntimeError(f'Bot location guard: config requires {expected!r}, process is {running!r}')

@dataclass(frozen=True)
class RiskSettings:
    risk_per_trade_pct: float
    daily_loss_limit_pct: float
    weekly_loss_limit_pct: float
    max_open_positions: int
    max_positions_per_group: int
    max_positions_per_currency: int
    max_positions_per_underlying_group: int
    symbol_cooldown_minutes: int
    max_trades_per_symbol_per_day: int
    max_margin_usage_pct: float
    max_hold_minutes: int
    first_target_r: float
    scale_out_pct: int
    trail_atr_mult: float
    stagnation_minutes: int
    stagnation_min_r: float
    momentum_lookback_bars: int
    breakeven_cost_multiplier: float
    min_risk_reward: float
    max_cost_to_target_ratio: float
    max_spread_points: dict[str, float]
    no_entry_before_close_minutes: int
    max_session_gap_hours: float

@dataclass(frozen=True)
class Settings:
    config: dict[str, Any]
    mt5_login: int | None
    mt5_password: str
    mt5_server: str
    mt5_path: str | None
    allow_real: bool
    root: Path
    risk: RiskSettings
    @property
    def magic_number(self) -> int: return int(self.config['magic_number'])
    @property
    def db_path(self) -> Path:
        path=Path(self.config['db_path'])
        return path if path.is_absolute() else self.root/path
    @property
    def data_dir(self) -> Path:
        from .paths import project_paths
        return project_paths(self.root)[0]
    @property
    def logs_dir(self) -> Path:
        from .paths import project_paths
        return project_paths(self.root)[1]

def _clock(value: object, key: str) -> str:
    import re
    if not isinstance(value,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value):
        raise ValueError(f'risk.{key} must be a UTC time in HH:MM format')
    return value

def _validate_trading_hours(hours):
    """universe.trading_hours: HH:MM strings and a valid IANA time zone (bad values would stop entries)."""
    if not hours: return
    import re
    from zoneinfo import ZoneInfo
    pattern=re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
    try: ZoneInfo(str(hours.get('timezone','UTC')))
    except Exception as exc: raise ValueError(f'universe.trading_hours.timezone is not a valid time zone: {hours.get("timezone")!r}') from exc
    windows=[('default',hours)]+[(str(name),window) for name,window in (hours.get('per_symbol') or {}).items()]
    for name,window in windows:
        for key in ('start','end'):
            value=window.get(key,'00:00' if key=='start' else '23:59')
            if not isinstance(value,str) or not pattern.match(value):
                raise ValueError(f'universe.trading_hours {name}.{key} must be a quoted HH:MM string, got {value!r}')


def load_settings(config_path: str | Path='config.yaml', env_path: str | Path='.env', require_credentials: bool=True) -> Settings:
    path=Path(config_path).resolve()
    if not path.is_file(): raise ValueError(f'Configuration file not found: {path}')
    load_dotenv(path.parent/Path(env_path),override=False)
    try: config=yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError,yaml.YAMLError) as exc: raise ValueError(f'Cannot load config.yaml: {exc}') from exc
    if not isinstance(config,dict): raise ValueError('config.yaml must contain a YAML mapping')
    runtime=config.setdefault('runtime',{})
    if not isinstance(runtime,dict) or runtime.get('location','local') not in {'local','server'}:
        raise ValueError('runtime.location must be local or server')
    required={'account','risk','universe','timeframes','magic_number','executor','strategies','loop','scanner','history','backtest','live','supervisor','mt5','confluence','cost_model','protections','db_path'}
    missing=required-config.keys()
    if missing: raise ValueError(f'config.yaml is missing keys: {", ".join(sorted(missing))}')
    for section in ('account','risk','universe','loop','scanner','history','backtest','live','supervisor','mt5','confluence','cost_model','protections','health','schedule','brain'):
        if not isinstance(config[section],dict): raise ValueError(f'{section} must be a mapping')
    if config['account'].get('mode') not in {'demo','real'}: raise ValueError('account.mode must be demo or real')
    raw=config['risk']
    validate_risk_ceilings(config)
    from .risk_profile import validate_demo_risk
    validate_demo_risk(config)
    numeric={'risk_per_trade_pct':(0,100),'daily_loss_limit_pct':(0,100),'weekly_loss_limit_pct':(0,100),'max_margin_usage_pct':(0,100),'min_risk_reward':(0,None),'max_cost_to_target_ratio':(0,None)}
    for key,(low,high) in numeric.items():
        value=raw.get(key)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=low or (high is not None and value>high):
            raise ValueError(f'risk.{key} must be > {low}' + (f' and <= {high}' if high else ''))
    for key in ('max_open_positions','max_positions_per_group','max_positions_per_currency','max_positions_per_underlying_group','max_trades_per_day','max_hold_minutes','max_hold_extended_minutes','extend_step_minutes','gating_window','gating_disable_hours','momentum_lookback_bars','stagnation_minutes','move_horizon_minutes','no_entry_before_close_minutes','early_exit_score_threshold','early_exit_ema_fast','early_exit_ema_slow','early_exit_swing_lookback','early_exit_rsi_period','early_exit_roc_period','early_exit_volume_lookback'):
        if type(raw.get(key)) is not int or raw[key]<=0: raise ValueError(f'risk.{key} must be a positive integer')
    if isinstance(raw.get('max_session_gap_hours'),bool) or not isinstance(raw.get('max_session_gap_hours'),(int,float)) or raw['max_session_gap_hours']<=0: raise ValueError('risk.max_session_gap_hours must be positive')
    for key in ('first_target_r','trail_atr_mult','stagnation_min_r','breakeven_cost_multiplier','sl_move_mult','tp_move_mult','early_exit_volume_spike_ratio'):
        if isinstance(raw.get(key),bool) or not isinstance(raw.get(key),(int,float)) or not math.isfinite(raw[key]) or raw[key]<=0: raise ValueError(f'risk.{key} must be positive')
    if raw['max_hold_extended_minutes']<raw['max_hold_minutes']: raise ValueError('risk.max_hold_extended_minutes must be at least risk.max_hold_minutes')
    if not raw['early_exit_min_r']<raw['early_exit_max_r']<=0: raise ValueError('risk.early_exit_min_r must be less than risk.early_exit_max_r and both must be non-positive')
    if type(raw.get('early_exit_enabled')) is not bool: raise ValueError('risk.early_exit_enabled must be boolean')
    weights=raw.get('early_exit_weights')
    if not isinstance(weights,dict) or any(key not in weights or type(weights[key]) is not int or weights[key]<0 for key in ('ema_cross','swing_break','momentum','volume_spike')): raise ValueError('risk.early_exit_weights must define non-negative integer weights for every exit signal')
    if type(raw.get('scale_out_pct')) is not int or not 1<=raw['scale_out_pct']<=99: raise ValueError('risk.scale_out_pct must be an integer from 1 to 99')
    if type(raw['symbol_cooldown_minutes']) is not int or raw['symbol_cooldown_minutes']<0: raise ValueError('risk.symbol_cooldown_minutes must be a non-negative integer')
    if type(raw.get('max_trades_per_symbol_per_day')) is not int or raw['max_trades_per_symbol_per_day']<0: raise ValueError('risk.max_trades_per_symbol_per_day must be a non-negative integer (0 = no limit)')
    spreads=raw.get('max_spread_points')
    if not isinstance(spreads,dict) or any(not isinstance(v,(int,float)) or v<0 for v in spreads.values()): raise ValueError('risk.max_spread_points must map symbols to non-negative numbers')
    risk=RiskSettings(float(raw['risk_per_trade_pct']),float(raw['daily_loss_limit_pct']),float(raw['weekly_loss_limit_pct']),raw['max_open_positions'],raw['max_positions_per_group'],raw['max_positions_per_currency'],raw['max_positions_per_underlying_group'],raw['symbol_cooldown_minutes'],raw['max_trades_per_symbol_per_day'],float(raw['max_margin_usage_pct']),raw['max_hold_minutes'],float(raw['first_target_r']),int(raw['scale_out_pct']),float(raw['trail_atr_mult']),raw['stagnation_minutes'],float(raw['stagnation_min_r']),raw['momentum_lookback_bars'],float(raw['breakeven_cost_multiplier']),float(raw['min_risk_reward']),float(raw['max_cost_to_target_ratio']),spreads,raw['no_entry_before_close_minutes'],float(raw['max_session_gap_hours']))
    credentials={key:os.getenv(key,'').strip() for key in ('MT5_LOGIN','MT5_PASSWORD','MT5_SERVER')}
    if require_credentials:
        for key,value in credentials.items():
            if not value: raise ValueError(f'Missing {key}; set it in .env before connecting to MT5')
    login=credentials['MT5_LOGIN']
    if login and not login.isdigit(): raise ValueError('MT5_LOGIN must be a numeric account number')
    if not isinstance(config['universe'].get('include_groups'),list) or not isinstance(config['universe'].get('exclude_symbols'),list) or not isinstance(config['universe'].get('symbol_aliases'),dict): raise ValueError('universe include_groups, exclude_symbols and symbol_aliases must be configured')
    if type(config['universe'].get('refresh_minutes')) is not int or config['universe']['refresh_minutes']<=0: raise ValueError('universe.refresh_minutes must be a positive integer')
    if type(config['universe'].get('exclude_dated_futures',True)) is not bool: raise ValueError('universe.exclude_dated_futures must be boolean')
    if not isinstance(config['timeframes'],list) or not config['timeframes']: raise ValueError('timeframes must be a non-empty list')
    if any(tf not in {'M1','M5','M15','H1'} for tf in config['timeframes']): raise ValueError('timeframes may contain only M1, M5, M15 and H1')
    if type(config['magic_number']) is not int or config['magic_number']<=0: raise ValueError('magic_number must be a positive integer')
    if not isinstance(config['executor'],dict) or type(config['executor'].get('deviation_points')) is not int or config['executor']['deviation_points']<0: raise ValueError('executor.deviation_points must be a non-negative integer')
    scanner=config['scanner']
    for key in ('max_seconds_per_cycle','max_candidates','candle_count'):
        if not isinstance(scanner.get(key),(int,float)) or scanner[key]<=0: raise ValueError(f'scanner.{key} must be > 0')
    for key in ('top_movers','movement_timeframe_minutes'):
        if type(scanner.get(key)) is not int or scanner[key]<=0: raise ValueError(f'scanner.{key} must be a positive integer')
    if scanner['top_movers']>100: raise ValueError('scanner.top_movers must not exceed 100')
    if not isinstance(scanner.get('max_spread_volatility_ratio'),(int,float)) or scanner['max_spread_volatility_ratio']<=0: raise ValueError('scanner.max_spread_volatility_ratio must be positive')
    if not isinstance(scanner.get('spread_volatility_horizon_minutes'),(int,float)) or scanner['spread_volatility_horizon_minutes']<=0: raise ValueError('scanner.spread_volatility_horizon_minutes must be positive')
    if not isinstance(scanner.get('min_score'),(int,float)): raise ValueError('scanner.min_score must be numeric')
    if not isinstance(scanner.get('min_confidence'),(int,float)) or not 0<=scanner['min_confidence']<=1: raise ValueError('scanner.min_confidence must be between 0 and 1')
    if scanner['min_confidence']<0.55: raise ValueError('scanner.min_confidence cannot be below the safety floor 0.55')
    for key in ('max_memory_mb','warn_disk_free_gb','stop_downloads_free_gb'):
        if not isinstance(config['health'].get(key),(int,float)) or config['health'][key]<=0: raise ValueError(f'health.{key} must be positive')
    schedule=config['schedule']
    for key in ('hourly_minute_utc','daily_rollover_hour_utc','weekly_day_utc','weekly_hour_utc'):
        maximum=59 if key=='hourly_minute_utc' else 23 if key.endswith('hour_utc') else 6
        if type(schedule.get(key)) is not int or not 0<=schedule[key]<=maximum: raise ValueError(f'schedule.{key} is out of range')
    if type(scanner.get('require_mtf_agreement')) is not bool: raise ValueError('scanner.require_mtf_agreement must be boolean')
    if type(scanner.get('mtf_min_agree')) is not int or not 2<=scanner['mtf_min_agree']<=3: raise ValueError('scanner.mtf_min_agree must be 2 or 3')
    if type(scanner.get('target_open_positions')) is not int or scanner['target_open_positions']<1: raise ValueError('scanner.target_open_positions must be a positive integer')
    for key in ('mtf_ema_fast','mtf_ema_slow'):
        if type(scanner.get(key)) is not int or scanner[key]<=0: raise ValueError(f'scanner.{key} must be a positive integer')
    for key in ('max_spread_vs_median','spread_median_lookback_hours','spread_median_cache_seconds','extend_min_score'):
        if not isinstance(scanner.get(key),(int,float)) or scanner[key]<=0: raise ValueError(f'scanner.{key} must be positive')
    if scanner['max_spread_vs_median']>1.8: raise ValueError('scanner.max_spread_vs_median cannot exceed 1.8')
    if raw['min_risk_reward']<1.2: raise ValueError('risk.min_risk_reward cannot be below the safety floor 1.2')
    if raw['max_cost_to_target_ratio']>0.3: raise ValueError('risk.max_cost_to_target_ratio cannot exceed 0.3')
    point_sizes=scanner.get('timeframe_point_sizes',{})
    if not isinstance(point_sizes,dict) or any(not isinstance(value,(int,float)) or value<=0 for value in point_sizes.values()): raise ValueError('scanner.timeframe_point_sizes must map symbols to positive point sizes')
    if not isinstance(config['loop'].get('interval_seconds'),(int,float)) or config['loop']['interval_seconds']<=0: raise ValueError('loop.interval_seconds must be > 0')
    costs=config['cost_model']
    if type(costs.get('enabled')) is not bool or type(costs.get('one_bar_delay')) is not bool: raise ValueError('cost_model enabled/delay flags must be boolean')
    for key in ('lookback_hours','sample_interval_seconds','min_samples_per_cell','max_stop_ratio'):
        value=costs.get(key)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0: raise ValueError(f'cost_model.{key} must be positive')
    if isinstance(costs.get('slippage_points'),bool) or not isinstance(costs.get('slippage_points'),(int,float)) or not math.isfinite(costs['slippage_points']) or costs['slippage_points']<0: raise ValueError('cost_model.slippage_points must be non-negative')
    if costs['lookback_hours']>24 or costs['max_stop_ratio']>.1: raise ValueError('cost model cannot exceed 24 hours or the 10% stop-cost safety ceiling')
    protection=config['protections']
    if type(protection.get('enabled')) is not bool: raise ValueError('protections.enabled must be boolean')
    if type(protection.get('consecutive_losses')) is not int or protection['consecutive_losses']<=0: raise ValueError('protections.consecutive_losses must be positive')
    for key in ('cooldown_hours','symbol_daily_loss_r','drawdown_window_hours','rolling_drawdown_pct'):
        value=protection.get(key)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0: raise ValueError(f'protections.{key} must be positive')
    _validate_trading_hours(config['universe'].get('trading_hours'))
    history=config['history']
    if not isinstance(history.get('ranges_months'),dict) or any(type(v) is not int or v<=0 for v in history['ranges_months'].values()): raise ValueError('history.ranges_months must map timeframes to positive month counts')
    if not isinstance(history.get('liquid_aliases'),list) or not history['liquid_aliases']: raise ValueError('history.liquid_aliases must be a non-empty list')
    backtest=config['backtest']
    for key in ('start_equity','in_sample_pct','walk_forward_optimize_months','walk_forward_test_months'):
        if not isinstance(backtest.get(key),(int,float)) or backtest[key]<=0: raise ValueError(f'backtest.{key} must be > 0')
    if type(backtest.get('signal_lookback_bars')) is not int or backtest['signal_lookback_bars']<100: raise ValueError('backtest.signal_lookback_bars must be an integer >= 100')
    if backtest['in_sample_pct']>=1: raise ValueError('backtest.in_sample_pct must be < 1')
    for key in ('commission_per_lot_per_side','slippage_points','fallback_spread_points'):
        if not isinstance(backtest.get(key),(int,float)) or backtest[key]<0: raise ValueError(f'backtest.{key} must be >= 0')
    if not isinstance(config['strategies'],dict): raise ValueError('strategies must be a mapping')
    for name,entry in config['strategies'].items():
        if not isinstance(entry,dict) or type(entry.get('enabled')) is not bool or not isinstance(entry.get('groups'),list) or not isinstance(entry.get('timeframes'),list) or not isinstance(entry.get('params'),dict): raise ValueError(f'strategies.{name} must define enabled, groups, timeframes and params')
    live=config['live']
    if live.get('mode') not in {'shadow','trade'}: raise ValueError('live.mode must be shadow or trade')
    if type(live.get('normal_trading_disabled',False)) is not bool: raise ValueError('live.normal_trading_disabled must be boolean')
    if not isinstance(live.get('strategies'),list) or any(not isinstance(name,str) for name in live['strategies']): raise ValueError('live.strategies must be a list of strategy names')
    for key in ('max_cycle_errors','equity_snapshot_seconds','heartbeat_seconds'):
        if type(live.get(key)) is not int or live[key]<=0: raise ValueError(f'live.{key} must be a positive integer')
    if type(live.get('command_ttl_seconds',60)) is not int or live.get('command_ttl_seconds',60)<=0: raise ValueError('live.command_ttl_seconds must be a positive integer')
    supervisor=config['supervisor']
    delays=supervisor.get('restart_backoff_seconds')
    if not isinstance(delays,list) or not delays or any(type(value) is not int or value<=0 for value in delays): raise ValueError('supervisor.restart_backoff_seconds must be a non-empty list of positive integers')
    for key in ('max_restarts_per_hour','restart_window_minutes'):
        if type(supervisor.get(key)) is not int or supervisor[key]<=0: raise ValueError(f'supervisor.{key} must be a positive integer')
    if type(supervisor.get('hang_seconds')) is not int or supervisor['hang_seconds']<=0: raise ValueError('supervisor.hang_seconds must be a positive integer')
    if isinstance(config['mt5'].get('call_timeout_seconds'),bool) or not isinstance(config['mt5'].get('call_timeout_seconds'),(int,float)) or config['mt5']['call_timeout_seconds']<=0: raise ValueError('mt5.call_timeout_seconds must be positive')
    confluence=config['confluence']; rules={'require_with_usd_context','require_with_group_direction','skip_tp_blocked_by_level','skip_against_confirmed_chart_pattern','require_candle_confirmation','skip_off_hours','prefer_fib_reaction'}
    if type(confluence.get('enabled')) is not bool or not isinstance(confluence.get('rules'),list) or any(rule not in rules for rule in confluence['rules']): raise ValueError('confluence.enabled must be boolean and rules must contain supported rule names')
    for section in ('health','notify','db'):
        if not isinstance(config.get(section),dict): raise ValueError(f'{section} must be a mapping')
    health=config['health']
    for key in ('stale_ticks_seconds','reconnect_failures_before_pause'):
        if type(health.get(key)) is not int or health[key]<=0: raise ValueError(f'health.{key} must be a positive integer')
    notify=config['notify']
    if not isinstance(notify.get('big_loss_usd'),(int,float)) or notify['big_loss_usd']<=0: raise ValueError('notify.big_loss_usd must be positive')
    _clock(notify.get('daily_summary_utc'),'daily_summary_utc')
    if type(notify.get('rate_limit_minutes')) is not int or notify['rate_limit_minutes']<=0: raise ValueError('notify.rate_limit_minutes must be positive')
    db=config['db']
    for key in ('signals_retention_days','events_retention_days','equity_retention_days','equity_downsample_minutes','backup_keep_days'):
        if type(db.get(key)) is not int or db[key]<=0: raise ValueError(f'db.{key} must be a positive integer')
    if type(db.get('vacuum_weekday_utc')) is not int or not 0<=db['vacuum_weekday_utc']<=6: raise ValueError('db.vacuum_weekday_utc must be between 0 and 6')
    dashboard=config.get('dashboard',{})
    for key in ('real_balance_default','real_risk_pct_default'):
        if not isinstance(dashboard.get(key),(int,float)) or dashboard[key]<=0: raise ValueError(f'dashboard.{key} must be positive')
    time_cfg = config.get('time', {})
    if time_cfg:
        if not isinstance(time_cfg, dict):
            raise ValueError('time must be a mapping')
        if 'default_server_utc_offset_hours' in time_cfg:
            offset_h = time_cfg['default_server_utc_offset_hours']
            if not isinstance(offset_h, (int, float)) or not (-14 <= offset_h <= 14):
                raise ValueError('time.default_server_utc_offset_hours must be between -14 and 14')
    return Settings(config,int(login) if login else None,credentials['MT5_PASSWORD'],credentials['MT5_SERVER'],os.getenv('MT5_PATH') or None,os.getenv('ALLOW_REAL','false').lower()=='true',path.parent,risk)
