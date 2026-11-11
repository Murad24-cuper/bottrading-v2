"""Resilient single-process live loop; shadow mode never submits orders."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timezone,timedelta
from pathlib import Path
import logging,os,signal,time,re,json
from collections import Counter
from types import SimpleNamespace
from .config import Settings
from .db import Database
from .executor import Executor
from .parquet_compat import assert_fastparquet_available
from .logger import get_logger
from .market_data import get_account_state
from .mt5_client import MT5Client
from .risk import calc_volume,calc_volume_stressed,check_trade_allowed
from .scanner import MarketScanner
from .symbols import SymbolService
from .symbols import underlying_group,underlying_key
from .time_utils import mt5_epoch_to_utc, server_clock_drift_seconds
from .trade_rules import weekend_mode_active
from .notify import TelegramNotifier,TelegramCommandPoller

log=get_logger(__name__)
RECONNECT_BACKOFF=(5,15,60)

class BotLock:
    """Exclusive OS-level and file-backed lock for one bot process per project directory."""
    def __init__(self,path):
        self.path=Path(path)
        from .process_utils import SingleInstanceLock
        self._lock=SingleInstanceLock(f"xm_bot_{self.path.parent.name}",lock_file_path=self.path)
        self.held=False
    def acquire(self):
        if not self._lock.acquire():
            raise RuntimeError(f'Bot lock already active: {self.path}; another instance is running')
        self.held=True
    def release(self):
        if not self.held: return
        try: self._lock.release()
        finally: self.held=False
    def __enter__(self): self.acquire(); return self
    def __exit__(self,*args): self.release()

@dataclass
class CycleResult:
    continue_running:bool=True
    sleep_seconds:float=5.0
    market_closed:bool=False

class TradingBot:
    def __init__(self,settings:Settings,client=None,db=None,scanner=None,executor=None,clock=None,sleep=None,monotonic=None):
        assert_fastparquet_available()
        self.settings=settings; self.client=client or MT5Client(settings); self.db=db or Database(settings.db_path)
        self.scanner=scanner; self.executor=executor; self.clock=clock or (lambda:datetime.now(timezone.utc)); self.sleep_fn=sleep or time.sleep; self.monotonic=monotonic or time.monotonic
        self.process_started_at=self._now()
        self.cycle_number=0; self.consecutive_errors=0; self.trading_disabled=False; self.last_error=''; self._critical_reported=False
        self._market_closed_logged=False; self._market_filter_logged=False; self._last_snapshot=0.0; self._started=False; self._last_account_type='unknown'; self._daily_peak_equity=0.0
        data_dir=getattr(settings,'data_dir',settings.root/'data')
        self.stop_event=False; self._connection_down=False; self.lock=BotLock(data_dir/'bot.lock')
        self._stop_after_cycle=False
        self._cycle_reservations: list = []
        self.exit_reason=''
        cfg=settings.config
        notify_cfg=cfg.get('notify',{})
        self.notifier=TelegramNotifier(rate_limit_minutes=notify_cfg.get('rate_limit_minutes',5))
        self.telegram_commands=TelegramCommandPoller(self.db,ttl_seconds=int(cfg.get('live',{}).get('command_ttl_seconds',60)))
        self._health_failures=0; self._entries_health_blocked=False; self._trade_allowed=False; self._last_daily_summary=''; self._last_maintenance_day=''
        self._data_dir=data_dir; self._brain_drift_checked=0.0; self._charts_written=-1e9; self._backup_day=None
        self._protection_notified: set = set()
        self._history_refresh_cycle=0
        self._last_notified_trade_id=self.db.latest_trade_id() if hasattr(self.db,'latest_trade_id') else 0
        self._observed_utc_day=self._now().date()-timedelta(days=1)
        self._daily_limit_alert_day=''; self._weekly_limit_alert_week=''
        self._weekend_gate_notified_until=''
        self._clock_checked=False
        self._missing_sl_signature=()
        self._low_margin_alerted=False
        self._order_rejection_count=0
        self._order_rejection_alerted=False
        self._cycle_funnel=None
        self._calendar_failure_alerted=False
    @property
    def live(self): return self.settings.config['live']
    def _now(self):
        value=self.clock()
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    def _weekend_mode(self,now):
        forex_open=False
        symbols=getattr(self.scanner,'symbols',None)
        if symbols is not None:
            try:
                forex_open=symbols.forex_session_open(now)
            except Exception: forex_open=False
        return weekend_mode_active(now,self.settings.config,forex_open)
    def _set_status(self,status='running',equity=None):
        stats=getattr(self.scanner,'last_scan_stats',{}) if self.scanner else {}
        self.db.upsert_bot_status(time=self._now().isoformat(),cycle=self.cycle_number,mode=self.live['mode'],status=status,account_type=self._last_account_type,equity=equity,last_error=self.last_error,mt5_connected=int(not self._connection_down),trade_allowed=int(self._trade_allowed),scan_total=stats.get('total_symbols',0),scan_scanned=stats.get('symbols_scanned',0),scan_complete=int(bool(stats.get('scan_complete',False))),scan_stage1_passed=stats.get('stage1_passed',0),scan_top_n=stats.get('top_movers',0),scan_signals=stats.get('signals',0),scan_seconds=stats.get('scan_seconds',0),scan_rejections=json.dumps(stats.get('stage1_rejections',{}),sort_keys=True),strategy_diagnostics=json.dumps(stats.get('strategy_diagnostics',{}),sort_keys=True),scan_groups_open=json.dumps(stats.get('groups_open',{}),sort_keys=True))
    def _event(self,level,message):
        log.log(getattr(logging,level.upper(),logging.INFO),message)
        self.db.log_event(level.upper(),'bot',message)
    def _notify(self,event_type,message):
        try: self.notifier.send(event_type,message)
        except Exception as exc: log.warning('Telegram notification queue failed: %s',exc)
    def _check_brain_drift(self):
        brain=self.settings.config.get('brain',{})
        if not brain.get('enabled',False) or self.monotonic()-self._brain_drift_checked<60: return
        self._brain_drift_checked=self.monotonic()
        from .brain.drift import assess_live_drift
        learning=self.settings.config.get('self_learning',{})
        # Drift settings live under self_learning in config.yaml; brain.* overrides when present.
        def drift_setting(key,default): return brain.get(key,learning.get(key,default))
        window=int(drift_setting('live_drift_window',100))
        rows=self.db.get_recent_brain_outcomes(window,self.settings.magic_number)
        result=assess_live_drift(rows,window,float(drift_setting('live_max_calibration_error',.1)),float(drift_setting('live_min_expectancy_r',0)))
        if not result['trigger']: return
        brain['enabled']=False; brain['shadow']=True
        path=self._data_dir/'brain'/'live_gate.json'; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({'mode':'shadow','checked_at':self._now().isoformat(),'result':result},ensure_ascii=False,indent=2),encoding='utf-8')
        message='Brain live rejimindən müşahidə rejiminə keçirildi: '+', '.join(result['reasons'])
        self._event('CRITICAL',message); self._notify('brain_drift',message)
    def _health(self,now):
        """Check terminal permissions and stale ticks before evaluating entries."""
        mt5=self.client.mt5
        try: terminal=mt5.terminal_info() if hasattr(mt5,'terminal_info') else None
        except Exception: terminal=None
        connected=bool(getattr(terminal,'connected',True)) if terminal is not None else not hasattr(mt5,'terminal_info')
        trade_allowed=bool(getattr(terminal,'trade_allowed',False)) if terminal is not None else not hasattr(mt5,'terminal_info')
        self._trade_allowed=trade_allowed
        self._connection_down=not connected
        if not connected:
            self._health_failures+=1
            self._connection_down=True
            if self._health_failures==1: self._notify('mt5_disconnected','🔴 MT5 ilə əlaqə kəsildi; yeni əməliyyatlar dayandırıldı.')
            try: self.client.ensure_connected()
            except Exception as exc:
                if self._health_failures>=int(self.settings.config.get('health',{}).get('reconnect_failures_before_pause',3)):
                    self._entries_health_blocked=True
                    self._event('CRITICAL',f'MT5 əlaqəsi {self._health_failures} dəfə bərpa olunmadı; yeni əməliyyatlar sağlamlıq bərpa edilənədək bloklandı: {exc}')
                return False
            terminal=mt5.terminal_info(); connected=bool(terminal and getattr(terminal,'connected',True)); trade_allowed=bool(terminal and getattr(terminal,'trade_allowed',False))
            self._trade_allowed=trade_allowed
            self._connection_down=not connected
            if not connected and self._health_failures>=int(self.settings.config.get('health',{}).get('reconnect_failures_before_pause',3)) and not self._entries_health_blocked:
                self._entries_health_blocked=True
                self._event('CRITICAL',f'MT5 bağlantısı {self._health_failures} bərpa cəhdindən sonra yoxdur; yeni əməliyyatlar bloklandı')
                self._notify('mt5_reconnect_critical','🚨 MT5 bağlantısı üç dəfə bərpa olunmadı; yeni əməliyyatlar bağlantı bərpa edilənədək bloklandı.')
        stale_limit=int(self.settings.config.get('health',{}).get('stale_ticks_seconds',120))
        positions=self._owned_positions() if hasattr(mt5,'positions_get') else []
        stale=[]
        if connected:
            for position in positions:
                try:
                    mt5.symbol_select(position.symbol,True)
                    tick=mt5.symbol_info_tick(position.symbol)
                    stamp=getattr(tick,'time_msc',0)/1000 if tick and getattr(tick,'time_msc',0) else getattr(tick,'time',0) if tick else 0
                    tick_age=(now-mt5_epoch_to_utc(stamp,mt5)).total_seconds() if stamp else float('inf')
                    if tick_age>stale_limit:
                        service=getattr(self.scanner,'symbols',None)
                        market_open=service.market_should_be_open(position.symbol,now) if service and hasattr(service,'market_should_be_open') else True
                        if market_open: stale.append(position.symbol)
                except Exception: stale.append(position.symbol)
        if connected and stale:
            self._health_failures+=1
            try:
                self.client.close(); self.client.ensure_connected()
                terminal=mt5.terminal_info(); connected=bool(terminal and getattr(terminal,'connected',True))
                if not connected: raise RuntimeError('terminal remains disconnected after reconnect')
                still_stale=[]
                for symbol in stale:
                    tick=mt5.symbol_info_tick(symbol)
                    stamp=getattr(tick,'time_msc',0)/1000 if tick and getattr(tick,'time_msc',0) else getattr(tick,'time',0) if tick else 0
                    if not stamp or (now-mt5_epoch_to_utc(stamp,mt5)).total_seconds()>stale_limit: still_stale.append(symbol)
                if still_stale: raise RuntimeError('ticks remain stale for '+', '.join(still_stale))
            except Exception as exc:
                if self._health_failures>=int(self.settings.config.get('health',{}).get('reconnect_failures_before_pause',3)):
                    self._entries_health_blocked=True
                    self._event('CRITICAL',f'MT5 qiymətləri köhnə qaldı; {self._health_failures} bərpa cəhdi uğursuz oldu: {exc}')
                    self._notify('mt5_stale_critical','🚨 MT5 qiymət axını üç bərpa cəhdindən sonra da yenilənmədi; yeni əməliyyatlar bloklandı.')
            return False
        if connected and not stale:
            if self._connection_down or self._health_failures:
                self._notify('mt5_reconnected','🟢 MT5 bağlantısı bərpa olundu.')
                self._event('INFO','MT5 bağlantısı və qiymət axını bərpa olundu.')
            self._health_failures=0; self._connection_down=False; self._entries_health_blocked=False
        missing_sl=tuple(sorted(
            str(getattr(position,'symbol','?'))
            for position in positions
            if not float(getattr(position,'sl',0) or 0)
        ))
        if missing_sl and self.executor and hasattr(self.executor,'remediate_missing_sl'):
            for p in positions:
                if not float(getattr(p,'sl',0) or 0):
                    self.executor.remediate_missing_sl(p)
            positions=self._owned_positions() if hasattr(mt5,'positions_get') else []
            missing_sl=tuple(sorted(
                str(getattr(position,'symbol','?'))
                for position in positions
                if not float(getattr(position,'sl',0) or 0)
            ))
        if missing_sl:
            self._entries_health_blocked=True
            if missing_sl!=self._missing_sl_signature:
                message='Bot mövqelərində stop-loss yoxdur; yeni əməliyyatlar bloklandı: '+', '.join(missing_sl)
                self._event('CRITICAL',message)
                self._notify('position_without_sl',message)
            self._missing_sl_signature=missing_sl
        elif self._missing_sl_signature:
            self._event('INFO','Bot mövqelərində stop-loss yoxlaması bərpa olundu.')
            self._missing_sl_signature=()
            self._entries_health_blocked=False
        if connected:
            try: account=mt5.account_info()
            except Exception: account=None
            margin_level=getattr(account,'margin_level',None) if account is not None else None
            threshold=float(self.settings.config.get('health',{}).get('min_margin_level_pct',150))
            if margin_level is not None and float(margin_level)>0 and float(margin_level)<threshold:
                self._entries_health_blocked=True
                if not self._low_margin_alerted:
                    message=f'Margin səviyyəsi {float(margin_level):.1f}% həddən aşağıdır ({threshold:.1f}%); yeni əməliyyatlar bloklandı.'
                    self._event('CRITICAL',message); self._notify('low_margin_level',message)
                    self._low_margin_alerted=True
            elif self._low_margin_alerted:
                self._event('INFO','Margin səviyyəsi təhlükəsiz həddə qayıtdı.')
                self._low_margin_alerted=False
        if not trade_allowed:
            self._notify('trading_disallowed','⚠️ MT5 terminalında avtomatik ticarət icazəsi bağlıdır; yeni əməliyyatlar açılmır.')
            self._event('WARNING','MT5 trade_allowed=false; new entries disabled')
        return connected and trade_allowed and not stale and not self._entries_health_blocked
    def _request_supervisor_stop(self,reason):
        self.exit_reason=str(reason)
        path=getattr(self.settings,'data_dir',self.settings.root/'data')/'supervisor.stop'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(self.exit_reason+'\n',encoding='utf-8')
    def _components(self):
        mt5=self.client.mt5
        service=SymbolService(mt5,self.settings)
        if self.scanner is None: self.scanner=MarketScanner(mt5,self.settings,self.db,service)
        if self.executor is None: self.executor=Executor(mt5,self.settings,self.db,service)
    def _management_settings(self,now):
        """Use the longer weekend holding horizon only while weekend policy is active."""
        if not self._weekend_mode(now): return self.settings
        weekend=self.settings.config.get('weekend',{})
        risk=SimpleNamespace(**self.settings.risk.__dict__)
        risk.max_hold_minutes=int(weekend.get('move_horizon_minutes',60))
        config=dict(self.settings.config); risk_config=dict(config['risk'])
        risk_config['max_hold_extended_minutes']=int(weekend.get('max_hold_extended_minutes',120))
        config['risk']=risk_config
        return SimpleNamespace(config=config,risk=risk,root=self.settings.root)
    def _account_type(self):
        account=self.client.mt5.account_info()
        if account is None: raise RuntimeError(f'account_info failed: {self.client.mt5.last_error()}')
        if hasattr(self.db,'set_active_account_id'): self.db.set_active_account_id(account.login)
        self._last_account_type='DEMO' if account.trade_mode==getattr(self.client.mt5,'ACCOUNT_TRADE_MODE_DEMO',0) else 'REAL'
        return account
    def _startup(self,state):
        if self._started: return
        if self.executor:
            if hasattr(self.executor,'sync_positions'):
                self.executor.sync_positions()
            for p in self._owned_positions():
                if not float(getattr(p,'sl',0) or 0):
                    if hasattr(self.executor,'remediate_missing_sl'):
                        self.executor.remediate_missing_sl(p)
        enabled=[s.name for s in getattr(self.scanner,'_strategies',[])] if self.scanner else []
        allowed=[name for name in enabled if name in self.live['strategies']]
        self._event('INFO',f"Bot started mode={self.live['mode']} account={self._last_account_type} equity={state.equity:.2f} enabled={enabled} allowed_live={allowed}")
        self._notify('started',f"🟢 Bot başladı · rejim: {self.live['mode']} · hesab: {self._last_account_type} · kapital: {state.equity:.2f} $")
        self._started=True
    def _check_clock(self,now):
        if self._clock_checked: return
        self._clock_checked=True
        try:
            service=getattr(self.scanner,'symbols',None)
            symbol=service.resolve_alias('EURUSD') if service and hasattr(service,'resolve_alias') else 'EURUSD'
            if not symbol: return
            mt5=self.client.mt5; tick=mt5.symbol_info_tick(symbol); stamp=getattr(tick,'time_msc',0)/1000 if tick and getattr(tick,'time_msc',0) else getattr(tick,'time',0) if tick else 0
            if not stamp: return
            age=(now-mt5_epoch_to_utc(stamp,mt5)).total_seconds()
            if age>int(self.settings.config.get('health',{}).get('stale_ticks_seconds',120)): return
            drift=server_clock_drift_seconds(mt5,symbol,now)
            if drift is not None and drift>30:
                self._event('WARNING',f'PC UTC saatı MT5 server vaxtından təxminən {drift:.0f} saniyə fərqlənir')
                self._notify('clock_drift','⚠️ Kompüterin UTC saatı MT5 vaxtı ilə 30 saniyədən çox fərqlənir.')
        except Exception as exc: self._event('WARNING',f'Saat uyğunluğu yoxlanıla bilmədi: {exc}')
    def _halt_requested(self):
        for name in ('STOP','STOP.txt'):
            path=self.settings.root/name
            if not path.exists():
                continue
            try: content=path.read_text(encoding='utf-8')
            except OSError: content=''
            if re.search(r'\bHALT\b',content,re.IGNORECASE):
                self._event('WARNING','STOP contains HALT; exiting cleanly without closing positions')
                self._request_supervisor_stop('HALT kill switch')
                self._set_status('stopped')
                if hasattr(self.client,'request_stop'):
                    self.client.request_stop()
                self.stop_event=True
                return True
        return False
    def _process_commands(self):
        for command in self.db.get_pending_commands(now=self._now(),bot_started_at=self.process_started_at):
            command_id=int(command['id']); action=command['command']
            self.db.update_command(command_id,'running')
            try:
                if action=='close_all':
                    results=self.executor.close_all('manual')
                    failed=[r.message for r in results if not r.success]
                    if failed: raise RuntimeError('; '.join(failed))
                elif action=='stop':
                    self._stop_after_cycle=True
                    self._request_supervisor_stop('dashboard stop command')
                elif action=='close_all_stop':
                    self._stop_after_cycle=True
                    self._request_supervisor_stop('dashboard close-all-and-stop command')
                    results=self.executor.close_all('kill')
                    failed=[r.message for r in results if not r.success]
                    if failed: raise RuntimeError('; '.join(failed))
                elif action=='pause':
                    (self.settings.root/'STOP').write_text('PAUSE\n',encoding='utf-8')
                elif action=='resume':
                    (self.settings.root/'STOP').unlink(missing_ok=True)
                elif action=='diagnose_order':
                    pause_path=self.settings.root/'STOP'
                    if not pause_path.is_file() or 'PAUSE' not in pause_path.read_text(encoding='utf-8').upper():
                        raise RuntimeError('Order diagnostic requires STOP=PAUSE; no requests were checked.')
                    mt5=self.client.mt5; symbol='USDJPY#'; info=mt5.symbol_info(symbol); tick=mt5.symbol_info_tick(symbol)
                    account=mt5.account_info()
                    if account is None or account.trade_mode!=getattr(mt5,'ACCOUNT_TRADE_MODE_DEMO',0):
                        raise RuntimeError('Order diagnostic refused: live account is not DEMO.')
                    if info is None or tick is None: raise RuntimeError(f'Order diagnostic quote unavailable: {mt5.last_error()}')
                    digits=int(getattr(info,'digits',3)); point=float(info.point); pip=point*(10 if digits in (3,5) else 1)
                    volume=float(info.volume_min); entry=float(tick.ask); stop=round(entry-35*pip,digits); target=round(entry+70*pip,digits)
                    filling=self.executor._filling_mode(info)
                    if filling is None: raise RuntimeError('Order diagnostic refused: no supported filling mode.')
                    base={'action':mt5.TRADE_ACTION_DEAL,'symbol':symbol,'volume':volume,'type':mt5.ORDER_TYPE_BUY,'price':entry,'sl':stop,
                          'deviation':int(self.settings.config['executor']['deviation_points']),'magic':int(self.settings.magic_number),
                          'type_time':mt5.ORDER_TIME_GTC,'type_filling':int(filling),'comment':'diag:USDJPY'}
                    def probe(label,request):
                        typed={key:{'value':value,'type':type(value).__name__} for key,value in request.items()}
                        result=mt5.order_check(**request)
                        checked=getattr(result,'request',None)
                        checked_dict=checked._asdict() if checked is not None and hasattr(checked,'_asdict') else checked
                        outcome={'retcode':getattr(result,'retcode',None),'comment':getattr(result,'comment',None),'last_error':mt5.last_error(),'checked_request':checked_dict}
                        self._event('INFO',f'order_check {label} account={{mode:{account.trade_mode},margin_mode:{account.margin_mode},currency:{getattr(account,"currency",None)!r}}} symbol_info={{visible:{getattr(info,"visible",None)},trade_mode:{info.trade_mode},digits:{digits},point:{point},volume_min:{info.volume_min},volume_step:{info.volume_step},filling_mode:{info.filling_mode},trade_exemode:{info.trade_exemode},trade_stops_level:{info.trade_stops_level}}} request={json.dumps(typed,sort_keys=True)} result={json.dumps(outcome,default=str)}')
                        return outcome
                    outcome=probe('minimal',dict(base))
                    positional_result=mt5.order_check(dict(base))
                    self._event('INFO',f'order_check minimal_positional result={{retcode:{getattr(positional_result,"retcode",None)},comment:{getattr(positional_result,"comment",None)!r},last_error:{mt5.last_error()!r}}}')
                    unpacked_result=mt5.order_check(**dict(base))
                    unpacked_request=getattr(unpacked_result,'request',None)
                    unpacked_request=unpacked_request._asdict() if unpacked_request is not None and hasattr(unpacked_request,'_asdict') else unpacked_request
                    self._event('INFO',f'order_check minimal_unpacked result={{retcode:{getattr(unpacked_result,"retcode",None)},comment:{getattr(unpacked_result,"comment",None)!r},request:{unpacked_request!r},last_error:{mt5.last_error()!r}}}')
                    request=dict(base); request['tp']=target
                    outcome=probe('plus_tp',request)
                    tagged=dict(request); tagged['comment']='exp:ts_mome:v1:H4'
                    tagged_outcome=probe('executor_tag',tagged)
                    for key in ('comment','tp','type_time','deviation','magic','price','sl'):
                        variant=dict(tagged); variant.pop(key,None); probe(f'without_{key}',variant)
                    for fill_name in ('ORDER_FILLING_FOK','ORDER_FILLING_IOC','ORDER_FILLING_RETURN'):
                        if hasattr(mt5,fill_name):
                            variant=dict(tagged); variant['type_filling']=int(getattr(mt5,fill_name)); probe(f'fill_{fill_name}',variant)
                    variant=dict(tagged); variant.pop('type_filling',None); probe('without_type_filling',variant)
                    self._event('INFO',f'Order-check diagnostic complete; executor-shaped retcode={tagged_outcome.get("retcode")}, comment={tagged_outcome.get("comment")!r}. No order was opened.')
                elif action=='demo_executor_e2e':
                    pause_path=self.settings.root/'STOP'
                    if not pause_path.is_file() or 'PAUSE' not in pause_path.read_text(encoding='utf-8').upper():
                        raise RuntimeError('DEMO executor E2E requires STOP=PAUSE before the guarded test.')
                    mt5=self.client.mt5; symbol='EURUSD#'; account=mt5.account_info(); info=mt5.symbol_info(symbol); tick=mt5.symbol_info_tick(symbol)
                    if account is None or account.trade_mode!=getattr(mt5,'ACCOUNT_TRADE_MODE_DEMO',0):
                        raise RuntimeError('DEMO executor E2E refused: live terminal is not DEMO.')
                    terminal=mt5.terminal_info()
                    if terminal is None or not bool(getattr(terminal,'trade_allowed',False)):
                        raise RuntimeError('DEMO executor E2E refused: terminal trade_allowed is false.')
                    if info is None or tick is None or info.trade_mode!=getattr(mt5,'SYMBOL_TRADE_MODE_FULL',4):
                        raise RuntimeError(f'DEMO executor E2E refused: {symbol} is unavailable or not fully tradable.')
                    if mt5.positions_get(symbol=symbol) or any(int(p.magic)==self.settings.magic_number for p in (mt5.positions_get() or [])):
                        raise RuntimeError('DEMO executor E2E refused: an existing position could conflict with the controlled test.')
                    from .models import Signal
                    digits=int(info.digits); pip=float(info.point)*(10 if digits in (3,5) else 1); volume=float(info.volume_min)
                    entry=float(tick.ask); sl=round(entry-35*pip,digits); tp=round(entry+70*pip,digits)
                    signal=Signal(symbol,'buy','M15',entry,sl,tp,1.0,'e2e_diagnostic','controlled DEMO executor verification')
                    features={'trade_mode':'exploration','exploration':{'family':'e2e_test','variant':'v1','timeframe':'M15','diagnostic_only':True}}
                    filling=self.executor._filling_mode(info)
                    if filling is None: raise RuntimeError('DEMO executor E2E refused: no supported filling mode.')
                    prepared=self.executor.build_open_request(signal,volume,info,tick,filling,features)
                    checked=mt5.order_check(**prepared['request'])
                    self._event('INFO',f'DEMO E2E order_check: symbol={symbol} volume={volume} entry={prepared["price"]} SL={prepared["sl"]} TP={prepared["tp"]} retcode={getattr(checked,"retcode",None)} comment={getattr(checked,"comment",None)!r} last_error={mt5.last_error()!r}')
                    if checked is None or getattr(checked,'retcode',None)!=0:
                        raise RuntimeError(f'DEMO executor E2E order_check rejected request: {getattr(checked,"comment",None)!r}')
                    ticket=None; close_ok=False
                    try:
                        pause_path.unlink(missing_ok=True)
                        self._event('WARNING','DEMO E2E temporarily cleared STOP inside serialized bot command; opening only the guarded EURUSD# test position.')
                        opened=self.executor.open_trade(signal,volume,features=features)
                        if not opened.success: raise RuntimeError(f'DEMO executor E2E open failed: {opened.message}')
                        ticket=int(opened.ticket); positions=mt5.positions_get(symbol=symbol) or []
                        position=next((p for p in positions if int(p.ticket)==ticket and int(p.magic)==self.settings.magic_number),None)
                        if position is None or float(getattr(position,'sl',0) or 0)<=0:
                            raise RuntimeError(f'DEMO executor E2E ticket {ticket} did not have a server-side SL.')
                        self._event('INFO',f'DEMO E2E opened server position ticket={ticket} symbol={symbol} volume={position.volume} entry={position.price_open} SL={position.sl} TP={position.tp}')
                        tightened=round(float(position.sl)+pip,digits)
                        if not self.executor.modify_sl_tp(ticket,tightened,float(position.tp)):
                            raise RuntimeError(f'DEMO executor E2E failed to tighten server SL for ticket {ticket}.')
                        position=next((p for p in (mt5.positions_get(symbol=symbol) or []) if int(p.ticket)==ticket),None)
                        if position is None or float(position.sl)<tightened-10**(-digits):
                            raise RuntimeError(f'DEMO executor E2E modified SL was not confirmed server-side for ticket {ticket}.')
                        self._event('INFO',f'DEMO E2E modified server SL ticket={ticket} new_SL={position.sl}')
                    finally:
                        if ticket is not None:
                            for attempt in range(1,4):
                                if not any(int(p.ticket)==ticket for p in (mt5.positions_get(symbol=symbol) or [])):
                                    close_ok=True; break
                                result=self.executor.close_position(ticket,'manual')
                                self._event('INFO' if result.success else 'WARNING',f'DEMO E2E close attempt={attempt} ticket={ticket} success={result.success} message={result.message}')
                            if not any(int(p.ticket)==ticket for p in (mt5.positions_get(symbol=symbol) or [])):
                                close_ok=True
                            if not close_ok:
                                self._event('CRITICAL',f'DEMO E2E could not close ticket={ticket}; protective server SL remains active.')
                        pause_path.write_text('PAUSE\n',encoding='utf-8')
                        self._event('INFO','DEMO E2E restored STOP=PAUSE.')
                    if ticket is not None and not close_ok:
                        raise RuntimeError(f'DEMO executor E2E ticket {ticket} remains open; STOP=PAUSE restored and server SL retained.')
                elif action in {'status','health','today','positions'}:
                    status=self.db.get_bot_status() or {}; positions=self._owned_positions()
                    if action in {'status','health'}:
                        text=f"Bot: {status.get('status','naməlum')} · hesab: {status.get('account_type','naməlum')} · MT5: {'qoşulub' if status.get('mt5_connected') else 'əlaqə yoxdur'} · heartbeat: {status.get('time','naməlum')}"
                    elif action=='positions':
                        text='Açıq bot mövqeləri: '+(', '.join(f"{p.symbol} {'alış' if p.type==0 else 'satış'} {p.volume:g}" for p in positions) if positions else 'yoxdur')
                    else:
                        rows=self.db.get_trades_for_day(self._now().date()); closed=[r for r in rows if r.get('status')=='closed']
                        pnl=sum(float(r.get('profit') or 0)+float(r.get('commission') or 0)+float(r.get('swap') or 0) for r in closed)
                        text=f"Bu gün: {len(closed)} bağlanan əməliyyat · xalis nəticə {pnl:+.2f} $"
                    self._notify(f'telegram_command_{command_id}',text)
                else: raise ValueError(f'Unknown bot command: {action}')
                self.db.update_command(command_id,'done','completed')
                self._event('INFO',f'Processed command {command_id}: {action}')
            except Exception as exc:
                self.db.update_command(command_id,'failed',str(exc))
                self._event('ERROR',f'Command {command_id} ({action}) failed: {exc}')
    def _signal_update(self,candidate,taken,reason,stage='risk'):
        if not taken and reason and self._cycle_funnel is not None:
            key='order_rejections' if stage=='order' else 'risk_rejections'
            self._cycle_funnel[key][str(reason)]+=1
        row_id=getattr(candidate,'signal_row_id',None)
        if row_id is not None: self.db.update_signal(row_id,taken=int(taken),skip_reason=reason or '')
        else:
            signal=candidate.signal
            self.db.insert_signal(time=self._now().isoformat(),symbol=signal.symbol,strategy=signal.strategy_name,direction=signal.direction,timeframe=signal.timeframe,confidence=signal.confidence,entry=signal.entry_hint,sl=signal.sl_price,tp=signal.tp_price,expected_cost=float(candidate.cost.get('cost',0)),score=float(candidate.score),taken=int(taken),skip_reason=reason or '')
    def _backup_database(self,now):
        """Once per UTC day copy bot.db to data/backups (keeps backup.keep_days copies)."""
        cfg=self.settings.config.get('backup',{})
        if not cfg.get('enabled',True) or self._backup_day==now.date(): return
        self._backup_day=now.date()
        try:
            from .db import backup_database
            path=getattr(self.db,'path',None)
            if path is None: return
            made=backup_database(path,Path(self._data_dir)/'backups',int(cfg.get('keep_days',7)),now.date())
            if made: log.info('Database backup written: %s',made.name)
        except Exception as exc:
            log.warning('Database backup failed: %s',exc)
    def _write_position_charts(self):
        """Best-effort price snapshots for dashboard charts; never interrupts trading."""
        interval=float(self.settings.config.get('dashboard',{}).get('chart_refresh_seconds',15))
        if interval<=0 or self.monotonic()-self._charts_written<interval: return
        self._charts_written=self.monotonic()
        try:
            from .position_charts import write_position_charts
            rows={int(row['ticket']):row for row in self.db.get_open_trades()} if hasattr(self.db,'get_open_trades') else {}
            write_position_charts(self.client.mt5,self._owned_positions(),self._data_dir,rows)
        except Exception as exc:
            log.debug('Position chart snapshot skipped: %s',exc)
    def _owned_positions(self):
        mt5=self.client.mt5
        return [p for p in (mt5.positions_get() or []) if int(p.magic)==self.settings.magic_number]
    def _pnl_periods(self,now):
        day=now.replace(hour=0,minute=0,second=0,microsecond=0)
        monday=(now-timedelta(days=now.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)
        magic=getattr(self.settings,'magic_number',None)
        return self.db.get_pnl_since(day,magic=magic),self.db.get_pnl_since(monday,magic=magic)
    def _candidate(self,candidate,now,allow_entries):
        live_mode=self.live['mode']=='trade'
        exploration_cfg=self.settings.config.get('demo_exploration',{})
        candidate_features=getattr(candidate,'features',{})
        exploration_eligible=bool(exploration_cfg.get('enabled',False) and self._last_account_type=='DEMO' and candidate_features.get('exploration',{}).get('eligible',False))
        allowed_strategy=candidate.strategy in self.live['strategies']
        active_gates=self.db.get_active_adaptive_gates(now) if hasattr(self.db,'get_active_adaptive_gates') else []
        gate=next((entry for entry in active_gates if (entry['scope']=='global' and entry['name']=='all') or (entry['scope']=='symbol' and entry['name']==candidate.symbol)),None)
        if gate is None: gate=next((entry for entry in active_gates if (entry['scope']=='strategy' and entry['name']==candidate.strategy) or (entry['scope']=='group' and entry['name']==candidate.asset_group)),None)
        if gate and (gate['scope'] in {'symbol','global'} or not exploration_eligible): reason=f'adaptive gate active: {gate["scope"]}={gate["name"]} until {gate["disabled_until"]}'
        elif (self.settings.root/'STOP').exists(): reason='kill switch active'
        elif self.consecutive_errors>=int(self.live['max_cycle_errors']) or self.trading_disabled: reason='consecutive cycle error limit reached'
        elif not live_mode: reason='shadow mode'
        elif self.settings.config.get('live',{}).get('normal_trading_disabled',False) and not exploration_eligible: reason='normal strategy entries disabled; no tested edge qualifies'
        elif not allowed_strategy and not exploration_eligible: reason='strategy not allowed live'
        elif self._entries_health_blocked or not self._trade_allowed: reason='MT5 health or algorithmic trading permission blocks new entries'
        elif not allow_entries: reason='new trades disabled after cycle errors'
        else: reason=''
        if reason:
            self._signal_update(candidate,False,reason); return False
        protection_cfg=self.settings.config.get('protections',{})
        if protection_cfg.get('enabled',False):
            from .protections import check_protections
            cutoff=now-timedelta(hours=float(protection_cfg.get('drawdown_window_hours',24)))
            trades=self.db.get_recent_symbol_trades(candidate.symbol,100,self.settings.magic_number) if hasattr(self.db,'get_recent_symbol_trades') else []
            login=getattr(self.client,'account_login',None)
            # Only this account's equity: switching accounts must never look like a drawdown.
            equity=(self.db.get_equity_snapshots_since(cutoff,login) if login else self.db.get_equity_snapshots_since(cutoff)) if hasattr(self.db,'get_equity_snapshots_since') else []
            if self._last_account_type=='DEMO' and 'demo_risk' in self.settings.config:
                from .risk_profile import account_risk_profile
                protection_cfg={**protection_cfg,'rolling_drawdown_pct':account_risk_profile(self.settings.config,True)['rolling_drawdown_pct']}
            blocked,protection_reason=check_protections(trades,equity,candidate.symbol,now,protection_cfg)
            key=(candidate.symbol,protection_reason)
            if blocked:
                if protection_reason.startswith('rolling drawdown'):
                    scope,name='global','all'; until=now+timedelta(hours=float(protection_cfg.get('drawdown_window_hours',24)))
                elif 'daily loss limit' in protection_reason:
                    scope,name='symbol',candidate.symbol; until=now.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)
                else:
                    scope,name='symbol',candidate.symbol; until=now+timedelta(hours=float(protection_cfg.get('cooldown_hours',24)))
                created=self.db.activate_protection_gate(scope,name,until,protection_reason,now) if hasattr(self.db,'activate_protection_gate') else False
                if created: self._notify(f'protection_{scope}_{name}',f'🛑 {candidate.symbol}: {protection_reason}. Yeni girişlər fasilə verildi.')
                self._protection_notified.add(key)
                self._signal_update(candidate,False,protection_reason); return False
            self._protection_notified={item for item in self._protection_notified if item[0]!=candidate.symbol}
        from .confluence import apply_confluence
        signal=candidate.signal
        allowed,confluence_reason,adjusted_confidence=apply_confluence(getattr(candidate,'features',{}),signal.direction,signal.confidence,self.settings.config.get('confluence',{}))
        if not allowed:
            self._signal_update(candidate,False,confluence_reason); return False
        if adjusted_confidence!=signal.confidence:
            signal.confidence=adjusted_confidence; candidate.confidence=adjusted_confidence
            if candidate.signal_row_id is not None:
                with self.db.connect() as connection: connection.execute('UPDATE signals SET confidence=? WHERE id=?',(adjusted_confidence,candidate.signal_row_id))
        state=get_account_state(self.client.mt5); positions=self._owned_positions()
        spec=candidate.symbol_spec
        weekend=self._weekend_mode(now)
        weekend_cfg=self.settings.config.get('weekend',{})
        if exploration_eligible:
            max_exp=min(int(exploration_cfg.get('max_open_positions',3)),3)
            exp_positions=[p for p in positions if str(getattr(p,'comment','')).startswith('exp:')]
            live_open_exp=max(self.db.count_mode_trades_since('exploration',now,open_only=True),len(exp_positions))
            if live_open_exp>=max_exp:
                self._signal_update(candidate,False,f'max open exploration positions cap ({max_exp}) reached'); return False
            if any(str(getattr(p,'symbol',''))==candidate.symbol for p in exp_positions):
                self._signal_update(candidate,False,f'max 1 open exploration trade for symbol {candidate.symbol} reached'); return False
        weekend_gate=self.db.weekend_gate(now,20,7) if weekend and hasattr(self.db,'weekend_gate') else {}
        if weekend_gate.get('disabled_until') and weekend_gate['disabled_until']>now.isoformat():
            self._signal_update(candidate,False,'weekend mode disabled after negative 20-trade expectancy')
            if self._weekend_gate_notified_until!=weekend_gate['disabled_until']:
                self._weekend_gate_notified_until=weekend_gate['disabled_until']
                message=f"Weekend trading disabled until {weekend_gate['disabled_until']} after negative expectancy across {weekend_gate['sample_size']} trades"
                self._event('WARNING',message); self._notify('weekend_disabled','Həftəsonu rejimi son 20 əməliyyatın mənfi nəticəsinə görə 7 gün söndürüldü.')
            return False
        trade_mode=('weekend_exploration' if weekend else 'exploration') if exploration_eligible else ('weekend' if weekend else 'main')
        from .risk_profile import applied_risk_pct,is_demo_account
        account_demo=is_demo_account(state,self.client.mt5)
        if 'demo_risk' in self.settings.config:
            risk_pct=applied_risk_pct(self.settings.config,account_demo,trade_mode,getattr(candidate,'features',None))
        else:
            risk_pct=float(exploration_cfg.get('risk_per_trade_pct',.1)) if exploration_eligible else self.settings.risk.risk_per_trade_pct
            if weekend: risk_pct=min(risk_pct,float(weekend_cfg.get('risk_per_trade_pct',risk_pct)))
        gate_settings=self.settings
        if weekend:
            risk_config=dict(self.settings.config['risk']); risk_config['max_open_positions']=min(int(risk_config['max_open_positions']),int(weekend_cfg.get('max_open_positions',3)))
            gate_settings=SimpleNamespace(config={**self.settings.config,'risk':risk_config},root=self.settings.root)
        if exploration_eligible:
            risk_config=dict(gate_settings.config['risk'])
            risk_config['max_open_positions']=min(int(risk_config['max_open_positions']),int(exploration_cfg.get('max_open_positions',3)),3)
            gate_settings=SimpleNamespace(config={**gate_settings.config,'risk':risk_config},root=self.settings.root)
        planned_volume,planned_reason=calc_volume(state.equity,risk_pct,signal.entry_hint,signal.sl_price,spec)
        if planned_volume<=0:
            self._signal_update(candidate,False,planned_reason); return False
        tick_size=float(spec.get('trade_tick_size') or spec.get('tick_size') or 0)
        tick_value=float(spec.get('trade_tick_value_loss') or spec.get('trade_tick_value') or spec.get('tick_value') or 0)
        point=float(spec.get('point') or spec.get('point_size') or 0)
        cost_per_lot=float(candidate.cost.get('cost',0) or 0)/max(float(getattr(candidate,'volume',planned_volume) or planned_volume),1e-12)
        cost_per_lot+=2*float(self.settings.config.get('cost_model',{}).get('slippage_points',0))*point/tick_size*tick_value if tick_size>0 else float('inf')
        volume,volume_reason=calc_volume_stressed(state.equity,risk_pct,signal.entry_hint,signal.sl_price,spec,cost_per_lot)
        min_lot=(getattr(candidate,'features',None) or {}).get('min_lot',{})
        if isinstance(min_lot,dict) and min_lot.get('active') and volume>0:
            # Small-balance trade: only ever the broker minimum lot, re-checked with stressed costs.
            from .risk import min_lot_risk_money
            from .risk_profile import min_lot_cap_pct
            needed=min_lot_risk_money(signal.entry_hint,signal.sl_price,spec,cost_per_lot)
            cap=min_lot_cap_pct(self.settings.config,account_demo)
            if needed is None or cap<=0 or needed>float(state.equity)*cap/100:
                volume,volume_reason=0,'min lot exceeds small-account cap after costs'
            else: volume=float(spec.get('volume_min'))
        if volume<=0:
            self._signal_update(candidate,False,volume_reason); return False
        risk_limit_pct=float(self.settings.config.get('risk',{}).get('max_total_open_risk_pct',5.0))
        def _risk_money(distance,trade_volume,trade_spec):
            tick_size=float(trade_spec.get('trade_tick_size') or trade_spec.get('tick_size') or trade_spec.get('point_size') or trade_spec.get('point') or 0)
            tick_value=float(trade_spec.get('trade_tick_value_loss') or trade_spec.get('trade_tick_value') or trade_spec.get('tick_value') or 0)
            if tick_size<=0 or tick_value<=0: return float('inf')
            return abs(float(distance))/tick_size*tick_value*float(trade_volume)
        current_open_risk=0.0
        for position in positions:
            stop=float(getattr(position,'sl',0.0) or 0.0)
            if stop<=0:
                self._signal_update(candidate,False,'existing bot position has no stop-loss; total open risk cannot be verified'); return False
            info=self.client.mt5.symbol_info(position.symbol) if hasattr(self.client.mt5,'symbol_info') else None
            position_spec=info._asdict() if info is not None and hasattr(info,'_asdict') else (dict(info) if isinstance(info,dict) else {})
            is_buy=getattr(position,'type',0)==getattr(getattr(self.client,'mt5',None),'POSITION_TYPE_BUY',0)
            risk_dist=max(0.0,float(position.price_open)-stop) if is_buy else max(0.0,stop-float(position.price_open))
            current_open_risk+=_risk_money(risk_dist,float(position.volume),position_spec)
        reserved_risk=sum(float(row.get('risk_money',0.0)) for row in self._cycle_reservations)
        new_risk=_risk_money(abs(float(signal.entry_hint)-float(signal.sl_price)),volume,spec)
        candidate_features.setdefault('sizing',{}).update({'planned_volume':planned_volume,'stressed_volume':volume,
            'planned_stop_risk_money':_risk_money(abs(float(signal.entry_hint)-float(signal.sl_price)),planned_volume,spec),
            'stressed_risk_money':new_risk+cost_per_lot*volume,'estimated_cost_per_lot':cost_per_lot})
        if current_open_risk+reserved_risk+new_risk>float(state.equity)*risk_limit_pct/100:
            self._signal_update(candidate,False,'total open stop risk cap reached'); return False
        order_type=self.client.mt5.ORDER_TYPE_BUY if signal.direction=='buy' else self.client.mt5.ORDER_TYPE_SELL
        margin=self.client.mt5.order_calc_margin(order_type,candidate.symbol,volume,signal.entry_hint)
        if margin is None:
            reason=f'order_calc_margin failed: {self.client.mt5.last_error()}'; self._signal_update(candidate,False,reason); return False
        service=getattr(self.scanner,'symbols',None)
        open_positions=[]
        db_open={int(row['ticket']):row for row in self.db.get_open_trades()}
        for p in positions:
            info=self.client.mt5.symbol_info(p.symbol); group=service.get_asset_group(p.symbol) if service else candidate.asset_group
            row=db_open.get(int(p.ticket),{})
            open_positions.append({'symbol':p.symbol,'asset_group':group,'exposure_group':underlying_group(p.symbol,group),'underlying':underlying_key(p.symbol),'direction':'buy' if p.type==self.client.mt5.POSITION_TYPE_BUY else 'sell','currency_base':getattr(info,'currency_base','') if info else '','currency_profit':getattr(info,'currency_profit','') if info else '','profit':float(p.profit)+float(getattr(p,'swap',0) or 0)+float(row.get('commission') or 0)})
        visible_symbols={str(position['symbol']) for position in open_positions}
        reserved_margin=0.0
        for reservation in self._cycle_reservations:
            if reservation['position']['symbol'] not in visible_symbols:
                open_positions.append(reservation['position']); reserved_margin+=reservation['margin']
        pnl_today,pnl_week=self._pnl_periods(now)
        day_start=now.replace(hour=0,minute=0,second=0,microsecond=0)
        cooldown_active=self.db.underlying_closed_since(candidate.symbol,now-timedelta(minutes=self.settings.risk.symbol_cooldown_minutes),self.settings.magic_number) if hasattr(self.db,'underlying_closed_since') else self.db.symbol_closed_since(candidate.symbol,now-timedelta(minutes=self.settings.risk.symbol_cooldown_minutes),self.settings.magic_number)
        symbol_trades_today=self.db.count_underlying_trades_since(candidate.symbol,day_start,self.settings.magic_number) if hasattr(self.db,'count_underlying_trades_since') else self.db.count_symbol_trades_since(candidate.symbol,day_start,self.settings.magic_number)
        session_closing=False
        if service and hasattr(service,'next_session_close'):
            upcoming=service.next_session_close(candidate.symbol,now,0.0)
            session_closing=bool(upcoming and 0<=(upcoming[0]-now).total_seconds()/60<=self.settings.risk.no_entry_before_close_minutes)
        trades_today=self.db.count_trades_since(day_start,self.settings.magic_number)+len(self._cycle_reservations)
        group_trades_today=(self.db.count_group_trades_since(day_start,candidate.asset_group,self.settings.magic_number) if hasattr(self.db,'count_group_trades_since') else 0)+sum(r['position'].get('asset_group')==candidate.asset_group for r in self._cycle_reservations)
        allowed,why=check_trade_allowed(signal,state,open_positions,pnl_today,pnl_week,gate_settings,candidate.cost,candidate.asset_group,now,margin+reserved_margin,volume,spec,cooldown_active,symbol_trades_today,session_closing,trades_today,group_trades_today)
        if not allowed:
            if why=='daily loss limit reached': self._notify('daily_loss','🛑 Gündəlik zərər limiti doldu; yeni əməliyyatlar dayandırıldı.')
            if why=='weekly loss limit reached': self._notify('weekly_loss','🛑 Həftəlik zərər limiti doldu; yeni əməliyyatlar dayandırıldı.')
            if not allowed: self._signal_update(candidate,False,why); return False
        if exploration_eligible:
            count_today=self.db.count_mode_trades_since('exploration',day_start)
            if count_today>=int(exploration_cfg.get('max_trades_per_day',40)):
                self._signal_update(candidate,False,'exploration trade limit reached'); return False
        candidate_features.setdefault('trade_mode',trade_mode)
        candidate_features['process_review']={'good_process':True,'grade':'good',
            'checks':{'stop_and_target_present':bool(signal.sl_price and signal.tp_price),
                      'stressed_risk_sized':bool(new_risk>0),'risk_gate_passed':True,
                      'cost_gate_passed':bool(candidate_features.get('cost_map',{}).get('median_cost_stop_ratio',0)<.10 and candidate_features.get('cost_map',{}).get('commission_known',True))},
            'outcome_r':None,'outcome_grade':'pending'}
        if self._cycle_funnel is not None: self._cycle_funnel['passed_risk']+=1
        result=self.executor.open_trade(signal,volume,expected_cost=candidate.cost,features=getattr(candidate,'features',{}))
        if not result.success:
            self._order_rejection_count+=1
            threshold=max(1,int(self.settings.config.get('health',{}).get('order_rejections_before_alert',3)))
            if self._order_rejection_count>=threshold and not self._order_rejection_alerted:
                message=f'Ardıcıl {self._order_rejection_count} əməliyyat sorğusu rədd edildi; yeni girişlər STOP=PAUSE ilə dayandırıldı. Son cavab: {result.message[:180]}'
                try: (self.settings.root/'STOP').write_text('PAUSE\n',encoding='utf-8')
                except OSError as exc: self._event('CRITICAL',f'Repeated order rejection auto-pause could not write STOP: {exc}')
                self._event('CRITICAL',message); self._notify('repeated_order_rejections',message)
                self._order_rejection_alerted=True
            self._signal_update(candidate,False,result.message,stage='order'); return False
        if self._cycle_funnel is not None: self._cycle_funnel['orders']+=1
        self._order_rejection_count=0; self._order_rejection_alerted=False
        self._cycle_reservations.append({'position':{'symbol':candidate.symbol,'asset_group':candidate.asset_group,'exposure_group':underlying_group(candidate.symbol,candidate.asset_group),'underlying':underlying_key(candidate.symbol),'direction':signal.direction,'currency_base':spec.get('currency_base',''),'currency_profit':spec.get('currency_profit',''),'profit':0.0},'margin':float(margin),'risk_money':float(new_risk)})
        self._signal_update(candidate,True,'')
        # Refresh portfolio state before the next ranked candidate.
        get_account_state(self.client.mt5); self._owned_positions()
        return True
    def _daily_stats(self,state,positions,now):
        day=now.date().isoformat(); previous=self.db.get_daily_stats(day)
        closed=self.db.get_closed_trades_for_day(day)
        net=lambda row:float(row.get('profit') or 0)+float(row.get('commission') or 0)+float(row.get('swap') or 0)
        closed_pnl=sum(net(row) for row in closed)+self.db.get_partial_pnl_since(datetime.fromisoformat(now.date().isoformat()+'T00:00:00+00:00'))
        open_rows={int(row['ticket']):row for row in self.db.get_open_trades()}
        floating=sum(float(p.profit)+float(getattr(p,'swap',0) or 0)+float(open_rows.get(int(p.ticket),{}).get('commission') or 0) for p in positions)
        start=previous['start_balance'] if previous else state.balance
        if not self._daily_peak_equity or not previous: self._daily_peak_equity=max(float(start),float(state.equity))
        self._daily_peak_equity=max(self._daily_peak_equity,float(state.equity))
        peak_margin=max(float(previous.get('invested_margin_peak') or 0) if previous else 0,float(state.margin))
        self.db.upsert_daily_stats(date=day,start_balance=float(start),end_balance=float(state.balance),pnl=closed_pnl+floating,trades_count=len(closed),wins=sum(net(x)>0 for x in closed),losses=sum(net(x)<0 for x in closed),max_drawdown=max(float(previous.get('max_drawdown') or 0) if previous else 0,self._daily_peak_equity-float(state.equity)),invested_margin_peak=peak_margin)
    def _notify_closed_losses(self):
        if not hasattr(self.db,'get_closed_trades_after_id'): return
        threshold=float(self.settings.config.get('notify',{}).get('big_loss_usd',5))
        for row in self.db.get_closed_trades_after_id(self._last_notified_trade_id):
            self._last_notified_trade_id=max(self._last_notified_trade_id,int(row['id']))
            net=float(row.get('profit') or 0)+float(row.get('commission') or 0)+float(row.get('swap') or 0)
            if net<=-threshold:
                self._notify('big_trade_loss',f'⚠️ Böyük zərər: {row["symbol"]} · {row["direction"]} · {net:.2f} $.')
    def _daily_summary(self,now):
        target=self.settings.config.get('notify',{}).get('daily_summary_utc','21:00')
        if now.strftime('%H:%M')<target or self._last_daily_summary==now.date().isoformat(): return
        day=now.date().isoformat(); rows=self.db.get_closed_trades_for_day(day)
        nets=[float(r.get('profit') or 0)+float(r.get('commission') or 0)+float(r.get('swap') or 0) for r in rows]
        wins=[x for x in nets if x>0]; losses=[x for x in nets if x<0]
        best=max(nets,default=0); worst=min(nets,default=0)
        stats=self.db.get_daily_stats(day) or {}
        message=(f'📊 Günlük yekun · balans: {float(stats.get("end_balance") or 0):.2f} $ · '
                 f'nəticə: {float(stats.get("pnl") or 0):+.2f} $ · əməliyyat: {len(rows)} · '
                 f'qazanma: {len(wins)/len(nets):.1%}' if nets else '📊 Günlük yekun · əməliyyat yoxdur.')
        if nets: message+=f' · orta qazanc: {sum(wins)/len(wins):.2f} $ · orta zərər: {sum(losses)/len(losses):.2f} $' if losses else f' · orta qazanc: {sum(wins)/len(wins):.2f} $ · orta zərər: 0.00 $'
        message+=f' · ən yaxşı: {best:+.2f} $ · ən zəif: {worst:+.2f} $'
        self._notify('daily_summary',message); self._last_daily_summary=day
    def _weekly_summary(self,now):
        if now.weekday()!=6: return
        target=self.settings.config.get('notify',{}).get('weekly_summary_utc','21:00')
        if now.strftime('%H:%M')<target: return
        week_key=now.strftime('%G-W%V')
        if self.db.maintenance_done('weekly_learning',week_key): return
        monday=now.date()-timedelta(days=6)
        start=datetime.combine(monday,datetime.min.time(),tzinfo=timezone.utc)
        end=start+timedelta(days=7)
        rows=self.db.get_closed_trades_between(start,end,self.settings.magic_number)
        def net(row): return float(row.get('profit') or 0)+float(row.get('commission') or 0)+float(row.get('swap') or 0)
        by_strategy=Counter(); by_group=Counter()
        good_process=0; reviewed_r=[]
        for row in rows:
            value=net(row); by_strategy[str(row.get('strategy') or 'Naməlum')]+=value; by_group[str(row.get('asset_group') or 'digər')]+=value
            try:
                review=json.loads(row.get('features') or '{}').get('process_review',{})
                good_process+=bool(review.get('good_process'))
                if review.get('outcome_r') is not None: reviewed_r.append(float(review['outcome_r']))
            except (TypeError,ValueError): pass
        pnl=sum(net(row) for row in rows)
        lessons=[]
        if by_strategy:
            best=max(by_strategy,key=by_strategy.get); worst=min(by_strategy,key=by_strategy.get)
            lessons.append(f'strategiya üzrə yaxşı: {best} {by_strategy[best]:+.2f} $, zəif: {worst} {by_strategy[worst]:+.2f} $')
        if by_group:
            best_group=max(by_group,key=by_group.get); worst_group=min(by_group,key=by_group.get)
            lessons.append(f'qrup üzrə yaxşı: {best_group} {by_group[best_group]:+.2f} $, zəif: {worst_group} {by_group[worst_group]:+.2f} $')
        mean_r=sum(reviewed_r)/len(reviewed_r) if reviewed_r else None
        review_summary=f'proses qaydasına uyğun {good_process}/{len(rows)} · orta nəticə {mean_r:+.2f}R' if mean_r is not None else f'proses qaydasına uyğun {good_process}/{len(rows)} · R nəticəsi məlum deyil'
        message=f"🧠 Bu həftə öyrəndiklərim · {start.date().isoformat()}–{(end-timedelta(days=1)).date().isoformat()} · {len(rows)} bağlanan əməliyyat · xalis {pnl:+.2f} $ · {review_summary}."
        if lessons: message+=' '+' · '.join(lessons)+'.'
        else: message+=' Əməliyyat olmayıb; strategiya nəticəsi çıxarmaq üçün nümunə yoxdur.'
        self.db.log_event('INFO','bot',message); self._notify('weekly_learning',message)
        self.db.mark_maintenance_done('weekly_learning',week_key,now)
    def _daily_maintenance(self,now):
        day=now.date().isoformat()
        if self._last_maintenance_day==day or self.db.maintenance_done('daily',day):
            self._last_maintenance_day=day; return
        cfg=self.settings.config.get('db',{})
        self.db.maintain(now,cfg.get('signals_retention_days',7),cfg.get('events_retention_days',30),cfg.get('equity_retention_days',30),cfg.get('equity_downsample_minutes',5))
        backups=getattr(self.settings,'data_dir',self.settings.root/'data')/'backups'
        self.db.backup(backups/f'bot_{day.replace("-","")}.db')
        keep=int(cfg.get('backup_keep_days',14))
        files=sorted(backups.glob('bot_????????.db'))
        for old in files[:-keep]: old.unlink(missing_ok=True)
        self.db.mark_maintenance_done('daily',day,now)
        self._last_maintenance_day=day
    def _daily_rollover_report(self,now):
        if now.date()<=self._observed_utc_day: return
        previous=self._observed_utc_day.isoformat(); stats=self.db.get_daily_stats(previous)
        if stats: self.db.save_daily_report(previous,stats,now.isoformat())
        self._observed_utc_day=now.date()
    def _connected_cycle(self,now):
        self._cycle_reservations=[]
        self._components(); account=self._account_type(); state=get_account_state(self.client.mt5); self._startup(state)
        self._check_clock(now)
        if hasattr(self.scanner,'_load_news_calendar'): self.scanner._load_news_calendar(now)
        calendar_unavailable=getattr(self.scanner,'_news_source','')=='fallback window'
        if calendar_unavailable and not self._calendar_failure_alerted:
            message='Təqvim mənbələrinin ikisi də əlçatan deyil; ehtiyat xəbər filtri aktivdir.'
            self._notify('calendar_sources_failed',message); self._calendar_failure_alerted=True
        elif not calendar_unavailable:
            self._calendar_failure_alerted=False
        health_ok=self._health(now)
        self.executor.sync_positions(); getattr(self.executor,'enforce_adverse_move_exit',lambda *_:None)(self.settings); getattr(self.executor,'enforce_news_positions',lambda *_:[])(getattr(self.scanner,'_news_events',[]),now); self.executor.enforce_time_stop(self._management_settings(now)); self.executor.enforce_session_close(self.settings,now)
        self._notify_closed_losses()
        self._write_position_charts()
        self._backup_database(now)
        pnl_today,pnl_week=self._pnl_periods(now)
        open_rows={int(row['ticket']):row for row in self.db.get_open_trades()}
        floating=sum(float(p.profit)+float(getattr(p,'swap',0) or 0)+float(open_rows.get(int(p.ticket),{}).get('commission') or 0) for p in self._owned_positions())
        from .risk_profile import is_demo_account,loss_limits
        daily_limit_pct,weekly_limit_pct=loss_limits(self.settings.config,is_demo_account(state,self.client.mt5)) if 'demo_risk' in self.settings.config else (float(self.settings.config['risk']['daily_loss_limit_pct']),float(self.settings.config['risk']['weekly_loss_limit_pct']))
        if pnl_today+floating<=-float(state.balance)*daily_limit_pct/100 and self._daily_limit_alert_day!=now.date().isoformat():
            self._notify('daily_loss','🛑 Gündəlik zərər limiti doldu; yeni əməliyyatlar dayandırıldı.'); self._daily_limit_alert_day=now.date().isoformat()
        week_key=(now-timedelta(days=now.weekday())).date().isoformat()
        if pnl_week+floating<=-float(state.balance)*weekly_limit_pct/100 and self._weekly_limit_alert_week!=week_key:
            self._notify('weekly_loss','🛑 Həftəlik zərər limiti doldu; yeni əməliyyatlar dayandırıldı.'); self._weekly_limit_alert_week=week_key
        risk_cfg=self.settings.config['risk']
        self._check_brain_drift()
        if hasattr(self.db,'refresh_adaptive_gates'):
            disabled=self.db.refresh_adaptive_gates(risk_cfg.get('gating_window',20),risk_cfg.get('gating_disable_hours',24),self.settings.magic_number,now)
            for gate in disabled: self._notify(f'adaptive_{gate["scope"]}_{gate["name"]}',f'⚠️ {gate["scope"]}: {gate["name"]} strategiyası/qrupu mənfi nəticəyə görə 24 saat söndürüldü.')
        self._process_commands()
        if self._stop_after_cycle:
            self._set_status('stopped',state.equity)
            return CycleResult(False,0)
        history_cfg=self.settings.config.get('history',{})
        refresh_every=max(1,int(history_cfg.get('incremental_refresh_every_cycles',4)))
        self._history_refresh_cycle+=1
        if history_cfg.get('incremental_refresh_enabled',False) and self._history_refresh_cycle%refresh_every==0:
            try:
                from .history import refresh_incremental_batch
                refresh_started=self.monotonic()
                refreshed=refresh_incremental_batch(self.client.mt5,self.settings,now)
                elapsed=self.monotonic()-refresh_started
                budget=float(history_cfg.get('incremental_budget_seconds',1.5))
                if elapsed>budget: self._event('WARNING',f'Incremental history request exceeded the cycle budget ({elapsed:.2f}s > {budget:.2f}s); one request ran sequentially and no cycles overlapped')
                elif refreshed: self._event('INFO',f"Incremental history updated {refreshed['symbol']} {refreshed['timeframe']} bars={refreshed['bars']} elapsed={elapsed:.2f}s")
            except Exception as exc:
                self._event('WARNING',f'Incremental history batch failed: {type(exc).__name__}: {exc}')
        scan=getattr(self.scanner,'scan',None) or getattr(self.scanner,'scan_once')
        candidates=scan()
        scan_stats=getattr(self.scanner,'last_scan_stats',{})
        signal_rejections=Counter()
        for reasons in (scan_stats.get('strategy_diagnostics') or {}).values():
            signal_rejections.update(reasons)
        self._cycle_funnel={'passed_risk':0,'orders':0,'signal_rejections':signal_rejections,
            'risk_rejections':Counter(),'order_rejections':Counter()}
        groups_open=scan_stats.get('groups_open',{})
        market_closed=bool(scan_stats.get('scan_complete',False)) and not groups_open
        if market_closed and not self._market_closed_logged:
            self._event('INFO','No tradable symbols: markets appear closed; scan interval extended')
            self._market_closed_logged=True
        elif not market_closed and self._market_closed_logged:
            self._event('INFO','Tradable market symbols are available again')
            self._market_closed_logged=False
        if not market_closed and not candidates and groups_open and not self._market_filter_logged:
            self._event('INFO',f'Fresh market quotes are open in {dict(groups_open)}; no candidate passed current scan and risk filters')
            self._market_filter_logged=True
        elif candidates or market_closed:
            self._market_filter_logged=False
        allow_entries=health_ok and self.consecutive_errors<int(self.live['max_cycle_errors']) and not self.trading_disabled
        week_start=week_key
        weekly=self.db.get_or_create_week_start(week_start,state.balance)
        weekly_limit=float(weekly['start_balance'])*(1-weekly_limit_pct/100)
        if float(state.equity)<=weekly_limit and not int(weekly.get('stopped') or 0):
            self.executor.close_all('weekly_stop'); self.db.mark_week_stopped(week_start)
            self._notify('weekly_drawdown_stop','🛑 Həftəlik kapital limiti aşıldı; bot mövqeləri bağladı və dayandı.')
            self._event('CRITICAL',f'Weekly drawdown stop reached: equity={state.equity:.2f}, week_start_balance={weekly["start_balance"]:.2f}')
            self._request_supervisor_stop('weekly drawdown stop'); self.stop_event=True
            self._set_status('stopped',state.equity); return CycleResult(False,0)
        for candidate in candidates:
            if self._halt_requested(): return CycleResult(False,0,market_closed)
            self._candidate(candidate,now,allow_entries)
        if hasattr(self.db,'record_scan_funnel'):
            self.db.record_scan_funnel(time=now.isoformat(),symbols_scanned=int(scan_stats.get('symbols_scanned',0)),
                filters_passed=int(scan_stats.get('stage1_passed',0)),signals=int(scan_stats.get('signals',0)),
                passed_risk=int(self._cycle_funnel['passed_risk'] if self._cycle_funnel else 0),
                orders=int(self._cycle_funnel['orders'] if self._cycle_funnel else 0),
                filter_rejections=scan_stats.get('stage1_rejections',{}),
                signal_rejections=self._cycle_funnel['signal_rejections'] if self._cycle_funnel else {},
                risk_rejections=dict(self._cycle_funnel['risk_rejections']) if self._cycle_funnel else {},
                order_rejections=dict(self._cycle_funnel['order_rejections']) if self._cycle_funnel else {})
        self._cycle_funnel=None
        state=get_account_state(self.client.mt5); positions=self._owned_positions()
        open_by_group=Counter((self.scanner.symbols.get_asset_group(p.symbol) if self.scanner and getattr(self.scanner,'symbols',None) else 'other') for p in positions)
        self._event('INFO',f'Open bot positions by asset group: {dict(open_by_group)}')
        self._daily_stats(state,positions,now)
        self._daily_rollover_report(now)
        self._daily_maintenance(now)
        self._daily_summary(now)
        self._weekly_summary(now)
        vacuum_key=now.strftime('%G-W%V')
        db_cfg=self.settings.config.get('db',{})
        if now.weekday()==int(db_cfg.get('vacuum_weekday_utc',6)) and not positions and not self.db.maintenance_done('vacuum',vacuum_key):
            self.db.vacuum(); self.db.mark_maintenance_done('vacuum',vacuum_key,now)
        if self.monotonic()-self._last_snapshot>=int(self.live['equity_snapshot_seconds']):
            self.db.insert_equity_snapshot(time=now.isoformat(),balance=state.balance,equity=state.equity,margin=state.margin,free_margin=state.free_margin,open_positions=len(positions),login=getattr(self.client,'account_login',None))
            self._last_snapshot=self.monotonic()
        if not self.trading_disabled: self.last_error=''
        self._set_status('managing_only' if self.trading_disabled else 'running',state.equity)
        return CycleResult(True,60.0 if market_closed else float(self.settings.config['loop']['interval_seconds']),market_closed)
    def _record_error(self,exc):
        self.consecutive_errors+=1; self.last_error=str(exc)[:500]
        self._event('ERROR',f'Cycle {self.cycle_number} failed ({self.consecutive_errors}/{self.live["max_cycle_errors"]}): {self.last_error}')
        if self.consecutive_errors>=int(self.live['max_cycle_errors']):
            self.trading_disabled=True
            if not self._critical_reported:
                self._event('CRITICAL','Maximum consecutive cycle errors reached; new entries disabled, position management continues')
                self._critical_reported=True
            self.exit_reason='error limit reached'
            self.stop_event=True
    def cycle(self):
        self.cycle_number+=1; now=self._now()
        if self._halt_requested(): return CycleResult(False,0)
        try:
            self.client.ensure_connected()
        except Exception as exc:
            self._connection_down=True
            self._health_failures+=1
            threshold=int(self.settings.config.get('health',{}).get('reconnect_failures_before_pause',3))
            if self._health_failures>=threshold and not self._entries_health_blocked:
                self._entries_health_blocked=True
                self._event('CRITICAL',f'MT5 bağlantısı {self._health_failures} bərpa cəhdindən sonra yoxdur; yeni əməliyyatlar bloklandı: {exc}')
                self._notify('mt5_reconnect_critical','🚨 MT5 bağlantısı üç dəfə bərpa olunmadı; yeni əməliyyatlar bağlantı bərpa edilənədək bloklandı.')
            self._record_error(exc)
            try: self._set_status('error')
            except Exception: pass
            idx=min(max(self.consecutive_errors-1,0),len(RECONNECT_BACKOFF)-1)
            return CycleResult(True,RECONNECT_BACKOFF[idx])
        now=self._now()
        try:
            result=self._connected_cycle(now)
            self.consecutive_errors=0
            return result
        except Exception as exc:
            self._record_error(exc)
            try: self._set_status('error')
            except Exception: pass
            return CycleResult(True,float(self.settings.config['loop']['interval_seconds']))
    def _heartbeat_sleep(self,seconds):
        remaining=float(seconds); heartbeat=max(1,int(self.live['heartbeat_seconds']))
        while remaining>0 and not self.stop_event:
            span=min(remaining,heartbeat); self.sleep_fn(span); remaining-=span
            if not self.stop_event:
                try: self._set_status('error' if self._connection_down or (self.last_error and not self.trading_disabled) else ('managing_only' if self.trading_disabled else 'running'))
                except Exception: pass
    def run_forever(self,max_cycles=None):
        cycles=0; previous_handlers={}
        def request_stop(signum,*_):
            try: label=signal.Signals(signum).name
            except (ValueError,AttributeError): label='process signal'
            self.exit_reason=f'signal {label}'
            self.stop_event=True
            if hasattr(self.client,'request_stop'):
                self.client.request_stop()
            try: self._request_supervisor_stop(self.exit_reason)
            except OSError: pass
        locked=False
        try:
            self.lock.acquire(); locked=True
            self.telegram_commands.start()
            for name in ('SIGINT','SIGTERM','SIGBREAK'):
                signum=getattr(signal,name,None)
                if signum is None: continue
                try: previous_handlers[signum]=signal.signal(signum,request_stop)
                except (ValueError,OSError): pass
            while not self.stop_event:
                cycle_started=self.monotonic(); result=self.cycle(); cycles+=1
                if not result.continue_running:
                    self.exit_reason=self.exit_reason or 'bot requested normal stop'
                    break
                if max_cycles is not None and cycles>=max_cycles:
                    self.exit_reason='test cycle limit reached'
                    break
                elapsed=max(0.0,self.monotonic()-cycle_started)
                self._heartbeat_sleep(max(0.0,result.sleep_seconds-elapsed))
        finally:
            for signum,handler in previous_handlers.items():
                try: signal.signal(signum,handler)
                except (ValueError,OSError): pass
            if not self.exit_reason: self.exit_reason='stop event received' if self.stop_event else 'normal loop exit'
            try: self._event('INFO',f'bot exiting reason={self.exit_reason}')
            except Exception: log.exception('Could not persist bot exiting event')
            self._notify('stopped',f'🔴 Bot dayandı · səbəb: {self.exit_reason}')
            if locked:
                try:
                    if hasattr(self.client,'request_stop'): self.client.request_stop()
                    elif self.client.mt5 is not None: self.client.close()
                finally:
                    try: self._set_status('stopped')
                    finally: self.lock.release()
            try: self.notifier.flush()
            except Exception: log.exception('Telegram bildirişləri dayandırılarkən tamamlanmadı')
            try: self.telegram_commands.stop()
            except Exception: log.exception('Telegram əmr polleri dayandırılarkən xəta')
