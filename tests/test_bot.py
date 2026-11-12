from datetime import datetime,timezone,timedelta
from pathlib import Path
from types import SimpleNamespace
import pytest
from src.bot import TradingBot,BotLock
from src.db import Database
from src.models import Signal,TradeResult

class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO=0; ORDER_TYPE_BUY=0; ORDER_TYPE_SELL=1
    def __init__(self): self.balance=1000; self.positions=[]
    def account_info(self): return SimpleNamespace(balance=self.balance,equity=self.balance,margin=0,margin_free=self.balance,margin_level=None,currency='USD',leverage=100,trade_mode=0,margin_mode=2,login=123456)
    def positions_get(self): return list(self.positions)
    def order_calc_margin(self,*args): return 0.0
    def last_error(self): return (0,'ok')

class FakeClient:
    def __init__(self,mt5): self.mt5=mt5; self.fail=False; self.closed=False
    def ensure_connected(self):
        if self.fail: raise RuntimeError('terminal unavailable')
    def close(self): self.closed=True

class FakeSymbols:
    def get_asset_group(self,symbol): return 'forex'

class FakeScanner:
    def __init__(self,candidates): self.candidates=candidates; self._strategies=[SimpleNamespace(name='breakout')]; self.symbols=FakeSymbols(); self.last_universe_size=1; self.fail=False; self.calls=0
    def scan_once(self):
        self.calls+=1
        if self.fail: raise RuntimeError('scanner fault')
        return self.candidates

class FakeExecutor:
    def __init__(self): self.opened=[]; self.closed_all=0; self.synced=0
    def sync_positions(self): self.synced+=1
    def enforce_time_stop(self,*args): pass
    def enforce_session_close(self,*args): pass
    def open_trade(self,signal,volume,expected_cost=None,features=None):
        self.opened.append((signal,volume)); return TradeResult(True,77,signal.entry_hint,volume,10009,'done')
    def close_all(self,reason): self.closed_all+=1; return []

def make_settings(root,mode='shadow',strategies=None,max_errors=5):
    risk={'risk_per_trade_pct':.5,'daily_loss_limit_pct':3,'weekly_loss_limit_pct':8,'max_trades_per_day':150,'max_open_positions':10,'max_positions_per_group':4,'max_positions_per_currency':2,'max_positions_per_underlying_group':2,'symbol_cooldown_minutes':30,'max_trades_per_symbol_per_day':5,'max_margin_usage_pct':40,'max_hold_minutes':720,'first_target_r':.7,'scale_out_pct':50,'trail_atr_mult':1.5,'stagnation_minutes':45,'stagnation_min_r':.3,'momentum_lookback_bars':5,'breakeven_cost_multiplier':1,'no_entry_before_close_minutes':60,'max_session_gap_hours':6,'min_risk_reward':1,'max_cost_to_target_ratio':.3}
    return SimpleNamespace(root=root,magic_number=20260001,risk=SimpleNamespace(**risk),db_path=root/'bot.db',config={'live':{'mode':mode,'strategies':strategies or [],'max_cycle_errors':max_errors,'equity_snapshot_seconds':30,'heartbeat_seconds':10},'risk':risk,'loop':{'interval_seconds':5}})

def make_bot(tmp_path,mode='shadow',strategies=None,candidate=True,max_errors=5,clock=None):
    settings=make_settings(tmp_path,mode,strategies,max_errors); db=Database(settings.db_path); mt5=FakeMT5(); client=FakeClient(mt5); executor=FakeExecutor()
    signal=Signal('EURUSD','buy','M1',100,99,102,.8,'breakout','test signal')
    spec={'trade_tick_value':1,'trade_tick_size':1,'volume_min':.01,'volume_step':.01,'volume_max':5}
    row=db.insert_signal(time=datetime.now(timezone.utc).isoformat(),symbol='EURUSD',strategy='breakout',direction='buy',timeframe='M1',confidence=.8,entry=100,sl=99,tp=102,expected_cost=.1,score=5,taken=0,skip_reason='')
    item=SimpleNamespace(symbol='EURUSD',asset_group='forex',strategy='breakout',signal=signal,cost={'cost':.1},symbol_spec=spec,score=5,signal_row_id=row)
    scanner=FakeScanner([item] if candidate else [])
    bot=TradingBot(settings,client,db,scanner,executor,clock=clock)
    bot.notifier=SimpleNamespace(send=lambda *args,**kwargs:False,flush=lambda:None)
    return bot,db,mt5,client,scanner,executor,item

def test_shadow_cycle_logs_skip_and_never_opens(tmp_path):
    bot,db,_,_,_,executor,item=make_bot(tmp_path)
    result=bot.cycle()
    with db.connect() as conn: signal=conn.execute('SELECT taken,skip_reason FROM signals WHERE id=?',(item.signal_row_id,)).fetchone()
    assert result.continue_running and executor.opened==[]
    assert signal['taken']==0 and signal['skip_reason']=='shadow mode'
    with db.connect() as conn:
        assert conn.execute("SELECT 1 FROM bot_events WHERE message LIKE 'Open bot positions by asset group:%'").fetchone()

def test_fresh_markets_without_candidates_are_not_logged_as_closed(tmp_path):
    bot,db,_,_,scanner,_,_=make_bot(tmp_path,candidate=False)
    scanner.last_scan_stats={'scan_complete':True,'groups_open':{'crypto':48},'signals':0}
    bot.cycle()
    with db.connect() as conn:
        messages=[row[0] for row in conn.execute("SELECT message FROM bot_events WHERE source='bot'")]
    assert any('Fresh market quotes are open' in message for message in messages)
    assert not any('markets appear closed' in message for message in messages)

def test_trade_mode_opens_only_live_allowlisted_strategy(tmp_path):
    bot,db,_,_,_,executor,item=make_bot(tmp_path,'trade',['breakout'])
    bot.cycle()
    assert len(executor.opened)==1
    with db.connect() as conn: assert conn.execute('SELECT taken FROM signals WHERE id=?',(item.signal_row_id,)).fetchone()[0]==1
    bot2,db2,_,_,_,executor2,item2=make_bot(tmp_path/'disallowed','trade',['mean_reversion'])
    bot2.cycle()
    assert executor2.opened==[]
    with db2.connect() as conn: assert conn.execute('SELECT skip_reason FROM signals WHERE id=?',(item2.signal_row_id,)).fetchone()[0]=='strategy not allowed live'

def test_demo_exploration_candidate_is_sized_and_tagged_separately(tmp_path):
    bot,db,_,_,_,executor,item=make_bot(tmp_path,'trade',['breakout'])
    bot.settings.config['demo_exploration']={'enabled':True,'risk_per_trade_pct':.1,'max_open_positions':5,'max_trades_per_day':40,'min_confidence':.45,'mtf_min_agree':2,'min_risk_reward':1,'max_cost_to_target_ratio':.45}
    item.features={'exploration':{'eligible':True,'soft_failures':['confidence below scanner.min_confidence']}}
    bot.cycle()
    assert len(executor.opened)==1
    assert item.features['trade_mode']=='exploration'
    assert executor.opened[0][1]<=1.0

def test_case_b_disables_normal_strategy_entries_even_when_allowlisted(tmp_path):
    bot,db,_,_,_,executor,item=make_bot(tmp_path,'trade',['breakout'])
    bot.settings.config['live']['normal_trading_disabled']=True
    bot.cycle()
    assert executor.opened==[]
    with db.connect() as connection:
        assert connection.execute('SELECT skip_reason FROM signals WHERE id=?',(item.signal_row_id,)).fetchone()[0]=='normal strategy entries disabled; no tested edge qualifies'

def test_exploration_can_collect_demo_data_through_adaptive_strategy_gate(tmp_path):
    bot,db,_,_,_,executor,item=make_bot(tmp_path,'trade',[])
    bot.settings.config['demo_exploration']={'enabled':True,'risk_per_trade_pct':.1,'max_open_positions':5,'max_trades_per_day':40,'min_confidence':.45,'mtf_min_agree':2}
    item.features={'exploration':{'eligible':True,'soft_failures':['normal strategies disabled']}}
    now=datetime.now(timezone.utc)
    for i in range(20):
        stamp=(now-timedelta(days=1,seconds=i)).isoformat()
        db.insert_trade(ticket=1000+i,symbol=f'X{i}',asset_group='forex',direction='buy',volume=.01,
            open_time=stamp,close_time=stamp,open_price=10,close_price=9,sl=9,tp=12,profit=-1,
            commission=0,swap=0,strategy='breakout',magic=bot.settings.magic_number,status='closed')
    db.refresh_adaptive_gates(20,24,bot.settings.magic_number,now)
    bot.cycle()
    assert len(executor.opened)==1
    assert item.features['trade_mode']=='exploration'

def test_exploration_still_obeys_daily_loss_limit(tmp_path):
    bot,db,_,_,_,executor,item=make_bot(tmp_path,'trade',[])
    bot.settings.config['demo_exploration']={'enabled':True,'risk_per_trade_pct':.1,'max_open_positions':5,'max_trades_per_day':40,'min_confidence':.45,'mtf_min_agree':2}
    item.features={'exploration':{'eligible':True,'soft_failures':['normal strategies disabled']}}
    stamp=datetime.now(timezone.utc).replace(hour=1).isoformat()
    db.insert_trade(ticket=2001,symbol='GBPUSD',asset_group='forex',direction='buy',volume=.01,
        open_time=stamp,close_time=stamp,open_price=10,close_price=9,sl=9,tp=12,profit=-40,
        commission=0,swap=0,strategy='breakout',magic=bot.settings.magic_number,status='closed')
    bot.cycle()
    assert executor.opened==[]
    with db.connect() as connection:
        assert connection.execute('SELECT skip_reason FROM signals WHERE id=?',(item.signal_row_id,)).fetchone()[0]=='daily loss limit reached'

def test_stop_blocks_entries_and_halt_exits_cleanly(tmp_path):
    bot,db,_,_,scanner,executor,item=make_bot(tmp_path,'trade',['breakout'])
    (tmp_path/'STOP').write_text('PAUSE\n',encoding='utf-8')
    bot.cycle(); assert executor.opened==[]
    with db.connect() as conn: assert conn.execute('SELECT skip_reason FROM signals WHERE id=?',(item.signal_row_id,)).fetchone()[0]=='kill switch active'
    (tmp_path/'STOP').write_text('HALT\n',encoding='utf-8')
    before=scanner.calls; result=bot.cycle()
    assert not result.continue_running and scanner.calls==before

def test_consecutive_errors_disable_new_entries(tmp_path):
    bot,db,_,_,scanner,executor,item=make_bot(tmp_path,'trade',['breakout'],max_errors=2)
    scanner.fail=True
    bot.cycle(); bot.cycle(); assert bot.trading_disabled
    scanner.fail=False; bot.cycle()
    assert executor.opened==[]
    assert 'error limit' in db.get_bot_status()['last_error'] or db.get_bot_status()['status']=='managing_only'

def test_daily_stats_roll_over_at_utc_midnight(tmp_path):
    clock_value=[datetime(2026,11,2,23,59,tzinfo=timezone.utc)]
    bot,db,mt5,_,_,_,_=make_bot(tmp_path,candidate=False,clock=lambda:clock_value[0])
    bot.cycle()
    clock_value[0]=datetime(2026,11,3,0,1,tzinfo=timezone.utc); mt5.balance=1010; bot.cycle()
    first=db.get_daily_stats('2026-11-02'); second=db.get_daily_stats('2026-11-03')
    assert first['start_balance']==1000 and second['start_balance']==1010

def test_command_close_all_is_processed_and_marked_done(tmp_path):
    bot,db,_,_,_,executor,_=make_bot(tmp_path,candidate=False)
    command_id=db.insert_command('close_all'); bot.cycle()
    with db.connect() as conn: row=conn.execute('SELECT status,message FROM commands WHERE id=?',(command_id,)).fetchone()
    assert executor.closed_all==1 and row['status']=='done' and row['message']=='completed'

def test_commands_created_before_bot_start_are_expired(tmp_path):
    bot,db,_,_,_,executor,_=make_bot(tmp_path,candidate=False)
    command_id=db.insert_command('close_all')
    bot.process_started_at=datetime.now(timezone.utc)+timedelta(seconds=1)
    bot._process_commands()
    with db.connect() as conn: row=conn.execute('SELECT status,message FROM commands WHERE id=?',(command_id,)).fetchone()
    assert executor.closed_all==0 and row['status']=='expired'

def test_expired_command_is_ignored_and_marked_expired(tmp_path):
    _,db,_,_,_,_,_=make_bot(tmp_path,candidate=False)
    command_id=db.insert_command('close_all',ttl_seconds=1)
    future=datetime.now(timezone.utc)+timedelta(seconds=2)
    assert db.get_pending_commands(now=future)==[]
    with db.connect() as conn: assert conn.execute('SELECT status FROM commands WHERE id=?',(command_id,)).fetchone()[0]=='expired'

def test_pause_and_resume_commands_manage_stop_file(tmp_path):
    bot,db,_,_,_,executor,item=make_bot(tmp_path,'trade',['breakout'])
    pause=db.insert_command('pause'); bot.cycle()
    assert (tmp_path/'STOP').exists() and executor.opened==[]
    with db.connect() as conn: assert conn.execute('SELECT status FROM commands WHERE id=?',(pause,)).fetchone()[0]=='done'
    resume=db.insert_command('resume'); bot.cycle()
    assert not (tmp_path/'STOP').exists() and len(executor.opened)==1
    with db.connect() as conn: assert conn.execute('SELECT status FROM commands WHERE id=?',(resume,)).fetchone()[0]=='done'

def test_stop_command_exits_after_management_before_scanning(tmp_path):
    bot,db,_,_,scanner,executor,_=make_bot(tmp_path,'trade',['breakout'])
    command=db.insert_command('stop')
    result=bot.cycle()
    assert result.continue_running is False
    assert scanner.calls==0 and executor.closed_all==0
    assert 'dashboard stop command' in (tmp_path/'data'/'supervisor.stop').read_text(encoding='utf-8')
    assert db.get_bot_status()['status']=='stopped'
    with db.connect() as conn: assert conn.execute('SELECT status FROM commands WHERE id=?',(command,)).fetchone()[0]=='done'

def test_close_all_stop_closes_then_exits(tmp_path):
    bot,db,_,_,scanner,executor,_=make_bot(tmp_path,'trade',['breakout'])
    command=db.insert_command('close_all_stop')
    result=bot.cycle()
    assert result.continue_running is False
    assert executor.closed_all==1 and scanner.calls==0
    assert 'dashboard close-all-and-stop command' in (tmp_path/'data'/'supervisor.stop').read_text(encoding='utf-8')
    with db.connect() as conn: assert conn.execute('SELECT status FROM commands WHERE id=?',(command,)).fetchone()[0]=='done'

def test_run_forever_records_exit_reason(tmp_path):
    bot,db,_,client,_,_,_=make_bot(tmp_path,'shadow')
    bot.run_forever(max_cycles=1)
    with db.connect() as conn:
        rows=conn.execute("SELECT message FROM bot_events WHERE message LIKE 'bot exiting reason=%'").fetchall()
    assert any('test cycle limit reached' in row['message'] for row in rows)
    assert client.closed and not (tmp_path/'data'/'bot.lock').exists()

def test_weekly_learning_summary_is_sent_and_idempotent(tmp_path):
    now=datetime(2026,11,8,22,0,tzinfo=timezone.utc)
    bot,db,_,_,_,_,_=make_bot(tmp_path,clock=lambda:now)
    sent=[]; bot.notifier=SimpleNamespace(send=lambda event,message:sent.append((event,message)))
    db.insert_trade(ticket=700,magic=20260001,status='closed',close_time='2026-11-03T12:00:00+00:00',open_time='2026-11-03T11:00:00+00:00',profit=2,commission=-.1,swap=0,strategy='trend_pullback',asset_group='forex')
    bot._weekly_summary(now); bot._weekly_summary(now)
    assert len(sent)==1 and sent[0][0]=='weekly_learning'
    assert 'Bu həftə öyrəndiklərim' in sent[0][1] and '+1.90 $' in sent[0][1]

def test_cycle_reservations_enforce_max_open_when_terminal_positions_lag(tmp_path):
    bot,db,_,_,_,executor,_=make_bot(tmp_path,'trade',['breakout'],candidate=False)
    bot.settings.config['risk']['max_open_positions']=2
    symbols=['GOLD','OIL','US30']
    candidates=[]
    for name in symbols:
        signal=Signal(name,'buy','M1',100,99,102,.8,'breakout','test')
        row=db.insert_signal(symbol=name,strategy='breakout',direction='buy',timeframe='M1',confidence=.8,entry=100,sl=99,tp=102,expected_cost=.1,score=5,taken=0,skip_reason='')
        candidates.append(SimpleNamespace(symbol=name,asset_group='other',strategy='breakout',signal=signal,cost={'cost':.1},symbol_spec={'trade_tick_value':1,'trade_tick_size':1,'volume_min':.01,'volume_step':.01,'volume_max':5},score=5,signal_row_id=row))
    bot.scanner.candidates=candidates
    bot.cycle()
    assert len(executor.opened)==2
    with db.connect() as conn:
        rows=conn.execute("SELECT skip_reason FROM signals WHERE symbol='US30' ORDER BY id DESC LIMIT 1").fetchone()
    assert rows[0]=='maximum open positions reached'

def test_cycle_reservations_enforce_global_daily_trade_cap(tmp_path):
    bot,db,_,_,_,executor,_=make_bot(tmp_path,'trade',['breakout'],candidate=False)
    bot.settings.config['risk']['max_trades_per_day']=1
    candidates=[]
    for symbol in ('AAA','BBB'):
        signal=Signal(symbol,'buy','M1',100,99,102,.8,'breakout','test')
        signal_id=db.insert_signal(symbol=symbol,strategy='breakout',direction='buy',timeframe='M1',confidence=.8,entry=100,sl=99,tp=102,expected_cost=.1,taken=0,skip_reason='')
        candidates.append(SimpleNamespace(symbol=symbol,asset_group='other',strategy='breakout',signal=signal,cost={'cost':.1},symbol_spec={'trade_tick_value':1,'trade_tick_size':1,'volume_min':.01,'volume_step':.01,'volume_max':5},score=5,signal_row_id=signal_id))
    bot.scanner.candidates=candidates; bot.cycle()
    assert len(executor.opened)==1
    with db.connect() as conn:
        skipped=conn.execute("SELECT skip_reason FROM signals WHERE symbol='BBB'").fetchone()[0]
    assert skipped=='maximum trades today reached'

def test_cycle_reservations_enforce_total_stop_risk_cap(tmp_path):
    bot,db,_,_,_,executor,_=make_bot(tmp_path,'trade',['breakout'],candidate=False)
    bot.settings.config['risk']['max_total_open_risk_pct']=.75
    candidates=[]
    for symbol in ('AAA','BBB'):
        signal=Signal(symbol,'buy','M1',100,99,102,.8,'breakout','test')
        signal_id=db.insert_signal(symbol=symbol,strategy='breakout',direction='buy',timeframe='M1',confidence=.8,entry=100,sl=99,tp=102,expected_cost=.1,taken=0,skip_reason='')
        candidates.append(SimpleNamespace(symbol=symbol,asset_group='other',strategy='breakout',signal=signal,cost={'cost':.1},symbol_spec={'trade_tick_value':1,'trade_tick_size':1,'volume_min':.01,'volume_step':.01,'volume_max':5},score=5,signal_row_id=signal_id))
    bot.scanner.candidates=candidates; bot.cycle()
    assert len(executor.opened)==1
    with db.connect() as conn:
        skipped=conn.execute("SELECT skip_reason FROM signals WHERE symbol='BBB'").fetchone()[0]
    assert skipped=='total open stop risk cap reached'

def test_terminal_trade_permission_blocks_new_entries(tmp_path):
    bot,db,mt5,_,_,executor,item=make_bot(tmp_path,'trade',['breakout'])
    mt5.terminal_info=lambda:SimpleNamespace(connected=True,trade_allowed=False)
    bot.cycle()
    assert executor.opened==[]
    assert db.get_trade(77) is None
    with db.connect() as conn:
        assert conn.execute('SELECT skip_reason FROM signals ORDER BY id DESC LIMIT 1').fetchone()[0]=='MT5 health or algorithmic trading permission blocks new entries'
    assert db.get_bot_status()['trade_allowed']==0

def test_position_without_stop_loss_blocks_entries_and_alerts_once(tmp_path):
    bot,db,mt5,_,_,_,_=make_bot(tmp_path,candidate=False)
    mt5.terminal_info=lambda:SimpleNamespace(connected=True,trade_allowed=True)
    mt5.positions=[SimpleNamespace(magic=bot.settings.magic_number,symbol='EURUSD',sl=0)]
    mt5.symbol_select=lambda *args:True
    now=datetime.now(timezone.utc)
    stamp=now.timestamp()
    mt5.symbol_info_tick=lambda symbol:SimpleNamespace(time=int(stamp),time_msc=int(stamp*1000))
    assert bot._health(now) is False
    assert bot._entries_health_blocked
    assert bot._health(now) is False
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bot_events WHERE level='CRITICAL' AND message LIKE '%stop-loss yoxdur%'").fetchone()[0]==1

def test_low_margin_level_blocks_new_entries_and_recovers(tmp_path):
    bot,db,mt5,_,_,_,_=make_bot(tmp_path,candidate=False)
    mt5.terminal_info=lambda:SimpleNamespace(connected=True,trade_allowed=True)
    mt5.account_info=lambda:SimpleNamespace(margin_level=120)
    assert bot._health(datetime.now(timezone.utc)) is False
    mt5.account_info=lambda:SimpleNamespace(margin_level=200)
    assert bot._health(datetime.now(timezone.utc)) is True
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bot_events WHERE level='CRITICAL' AND message LIKE '%Margin səviyyəsi%'").fetchone()[0]==1

def test_repeated_order_rejections_raise_one_alert(tmp_path):
    bot,db,_,_,_,executor,_=make_bot(tmp_path,'trade',['breakout'])
    executor.open_trade=lambda *args,**kwargs:TradeResult(False,None,None,0,10030,'broker rejected request')
    bot.settings.config.setdefault('health',{})['order_rejections_before_alert']=2
    bot.cycle(); bot.cycle()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bot_events WHERE level='CRITICAL' AND message LIKE '%əməliyyat sorğusu rədd edildi%'").fetchone()[0]==1

def test_three_consecutive_order_rejections_auto_pause_new_entries(tmp_path):
    bot,db,_,_,_,executor,item=make_bot(tmp_path,'trade',['breakout'])
    executor.open_trade=lambda *args,**kwargs:TradeResult(False,None,None,0,10030,'broker rejected request')
    bot.settings.config.setdefault('health',{})['order_rejections_before_alert']=3
    for _ in range(3): bot.cycle()
    assert (tmp_path/'STOP').read_text(encoding='utf-8').strip()=='PAUSE'
    bot.cycle()
    assert bot._order_rejection_count==3
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bot_events WHERE level='CRITICAL' AND message LIKE '%STOP=PAUSE%'").fetchone()[0]==1

def test_stale_open_symbol_ticks_block_entries_after_three_reconnect_attempts(tmp_path):
    bot,db,mt5,client,_,_,_=make_bot(tmp_path,candidate=False)
    mt5.terminal_info=lambda:SimpleNamespace(connected=True,trade_allowed=True)
    mt5.positions=[SimpleNamespace(magic=bot.settings.magic_number,symbol='EURUSD',sl=1.0)]
    now=datetime.now(timezone.utc)
    for _ in range(3): assert bot._health(now) is False
    assert bot._entries_health_blocked
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bot_events WHERE level='CRITICAL' AND message LIKE '%bərpa cəhdi uğursuz oldu%' ").fetchone()[0]==1

def test_closed_market_does_not_trigger_stale_tick_reconnect(tmp_path):
    bot,_,mt5,client,_,_,_=make_bot(tmp_path,candidate=False)
    mt5.terminal_info=lambda:SimpleNamespace(connected=True,trade_allowed=True)
    mt5.symbol_select=lambda *args:True
    old_stamp=datetime(2026,1,1,tzinfo=timezone.utc).timestamp()
    mt5.symbol_info_tick=lambda symbol:SimpleNamespace(time=int(old_stamp),time_msc=int(old_stamp*1000))
    mt5.positions=[SimpleNamespace(magic=bot.settings.magic_number,symbol='EURUSD',sl=1.0)]
    bot.scanner.symbols.market_should_be_open=lambda symbol,now:False
    assert bot._health(datetime.now(timezone.utc)) is True
    assert not client.closed and bot._health_failures==0

def test_weekly_drawdown_closes_and_stops_bot(tmp_path):
    from datetime import timedelta
    bot,db,mt5,_,_,executor,_=make_bot(tmp_path,'trade',['breakout'],candidate=False)
    mt5.account_info=lambda:SimpleNamespace(balance=1000,equity=900,margin=0,margin_free=1000,margin_level=None,currency='USD',leverage=100,trade_mode=0,margin_mode=2,login=123456)
    now=datetime.now(timezone.utc); monday=(now-timedelta(days=now.weekday())).date().isoformat()
    db.get_or_create_week_start(monday,1000)
    result=bot.cycle()
    assert not result.continue_running and executor.closed_all==1
    assert 'weekly drawdown stop' in (tmp_path/'data'/'supervisor.stop').read_text(encoding='utf-8')
    assert db.get_or_create_week_start(monday,1000)['stopped']==1

def test_run_forever_releases_lock_and_shuts_down_client(tmp_path):
    bot,_,_,client,_,_,_=make_bot(tmp_path,candidate=False)
    bot.run_forever(max_cycles=1)
    assert client.closed and not (tmp_path/'data'/'bot.lock').exists()

def test_bot_lock_refuses_second_instance(tmp_path):
    path=tmp_path/'data'/'bot.lock'; first=BotLock(path); second=BotLock(path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError): second.acquire()
    finally: first.release()
    assert not path.exists()

def test_bot_lock_release_tolerates_external_cleanup(tmp_path):
    lock=BotLock(tmp_path/'data'/'bot.lock'); lock.acquire(); lock.path.unlink()
    lock.release()
    assert not lock.held

def test_weekend_management_uses_configured_horizon_and_extension(tmp_path):
    settings=SimpleNamespace(config={'live':{'mode':'shadow'},'notify':{},'weekend':{'enabled':True,'move_horizon_minutes':60,'max_hold_extended_minutes':120},'risk':{'max_hold_extended_minutes':60},'scanner':{}},root=tmp_path,risk=SimpleNamespace(max_hold_minutes=20))
    bot=TradingBot(settings,client=SimpleNamespace(mt5=None),db=Database(tmp_path/'weekend.db'))
    sunday=datetime(2026,11,8,12,tzinfo=timezone.utc)
    result=bot._management_settings(sunday)
    assert result.risk.max_hold_minutes==60
    assert result.config['risk']['max_hold_extended_minutes']==120
    monday=bot._management_settings(datetime(2026,11,9,12,tzinfo=timezone.utc))
    assert monday is settings

def test_restart_command_requests_supervisor_restart_without_stop(tmp_path):
    bot,db,_,_,scanner,_,_=make_bot(tmp_path,'trade',['breakout'])
    bot._data_dir=tmp_path/'data'; bot._data_dir.mkdir(exist_ok=True)
    command=db.insert_command('restart'); result=bot.cycle()
    assert result.continue_running is False and scanner.calls==0
    assert (tmp_path/'data'/'restart.request').exists() and not (tmp_path/'data'/'supervisor.stop').exists()
    with db.connect() as conn: assert conn.execute('SELECT status FROM commands WHERE id=?',(command,)).fetchone()[0]=='done'

def test_close_position_command_closes_one_ticket(tmp_path):
    bot,db,_,_,_,executor,_=make_bot(tmp_path,candidate=False)
    closed=[]; executor.close_position=lambda ticket,reason: closed.append((ticket,reason)) or TradeResult(True,ticket,0,0,10009,'done')
    command=db.insert_command('close_position:42'); bot.cycle()
    assert closed==[(42,'manual')]
    with db.connect() as conn: assert conn.execute('SELECT status FROM commands WHERE id=?',(command,)).fetchone()[0]=='done'

def test_failed_close_position_command_is_marked_failed(tmp_path):
    bot,db,_,_,_,executor,_=make_bot(tmp_path,candidate=False)
    executor.close_position=lambda ticket,reason: TradeResult(False,ticket,0,0,10006,'rejected')
    command=db.insert_command('close_position:7'); bot.cycle()
    with db.connect() as conn: assert conn.execute('SELECT status FROM commands WHERE id=?',(command,)).fetchone()[0]=='failed'

def test_fast_watch_rechecks_early_exit_between_cycles_only_with_open_positions(tmp_path):
    bot,db,mt5,_,_,executor,_=make_bot(tmp_path,candidate=False)
    bot.settings.config['dashboard']={'chart_refresh_seconds':2}; bot.settings.config['risk']['early_exit_enabled']=True
    calls=[]; executor.enforce_adverse_move_exit=lambda settings: calls.append(1) or []
    bot._started=True; bot.sleep_fn=lambda s: None
    bot._heartbeat_sleep(10)
    assert calls==[]
    mt5.positions=[SimpleNamespace(magic=bot.settings.magic_number,ticket=1,symbol='EURUSD',type=0,volume=.1,price_open=1.0,sl=0,tp=0,time=0,profit=0)]
    bot._heartbeat_sleep(10)
    assert len(calls)==4   # spans at 2,4,6,8 s (not after the last one: the cycle runs then)
