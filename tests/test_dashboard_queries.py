from src.db import Database
from src.dashboard import queries
import pytest

def test_dashboard_queries_return_database_only_views_and_queue_commands(tmp_path):
    path=tmp_path/'dashboard.db'; db=Database(path)
    db.insert_trade(ticket=1,symbol='EURUSD',asset_group='forex',direction='buy',volume=.1,open_time='2026-11-05T10:00:00+00:00',close_time='2026-11-05T10:10:00+00:00',open_price=1.1,close_price=1.11,sl=1.09,tp=1.12,profit=2,commission=-.2,swap=0,magic=20260001,status='closed',close_reason='tp',strategy='breakout')
    db.insert_trade(ticket=2,symbol='XAUUSD',asset_group='metals',direction='sell',volume=.01,open_time='2026-11-05T10:05:00+00:00',open_price=2000,profit=-.5,commission=0,swap=0,magic=20260001,status='open',strategy='trend_pullback')
    db.insert_partial_close(ticket=2,symbol='XAUUSD',volume=.01,price=1999,profit=.3,commission=-.02,swap=0,reason='partial_tp',time='2026-11-05T10:08:00+00:00')
    db.insert_signal(time='2026-11-05T10:00:00+00:00',symbol='EURUSD',strategy='breakout',direction='buy',timeframe='M5',confidence=.8,entry=1.1,sl=1.09,tp=1.12,expected_cost=.1,score=1.5,taken=1,skip_reason='')
    db.upsert_daily_stats(date='2026-11-05',start_balance=100,end_balance=102,pnl=1.5,trades_count=1,wins=1,losses=0,max_drawdown=0,invested_margin_peak=12)
    db.insert_equity_snapshot(time='2026-11-05T10:00:00+00:00',balance=102,equity=101.5,margin=12,free_margin=89.5,open_positions=1)
    db.log_event('WARNING','bot','mock warning')
    db.upsert_bot_status(time='2026-11-05T10:00:00+00:00',cycle=3,mode='shadow',status='running',account_type='DEMO',equity=101.5,last_error='')
    start='2026-11-05T00:00:00+00:00'; end='2026-11-06T00:00:00+00:00'
    summary=queries.get_day_summary(start,end,path)
    assert summary['closed_pnl']==2.08 and summary['floating_pnl']==-.5 and summary['net_pnl']==1.58
    assert summary['wins']==1 and summary['taken']==2.08
    assert queries.get_open_positions(path)[0]['symbol']=='XAUUSD'
    assert queries.get_closed_trades(start,end,path)[0]['net_profit']==1.8
    assert queries.get_closed_trades(start,end,path)[0]['sl']==1.09
    assert queries.get_latest_signals(path)[0]['score']==1.5
    db.insert_signal(time='2026-11-05T10:11:00+00:00',symbol='GBPUSD',strategy='trend_pullback',direction='sell',timeframe='M5',confidence=.7,entry=1.2,sl=1.21,tp=1.18,expected_cost=.1,taken=0,skip_reason='spread too wide')
    assert queries.get_top_skip_reasons_since('2026-11-05T10:00:00+00:00',path)[0]['skip_reason']=='spread too wide'
    assert queries.get_recent_events(path)[0]['level']=='WARNING'
    with db.connect() as conn:
        conn.execute("INSERT INTO bot_events(time,level,source,message) VALUES (?,?,?,?)",('2026-11-05T12:00:00+00:00','WARNING','supervisor','Restarting bot after exit code 1; restart 1/5 in window, delay=10s'))
    assert queries.get_supervisor_restart_count(start,end,path)==1
    assert queries.get_equity_curve(start,end,path)[0]['equity']==101.5
    assert queries.get_daily_pnl(path)[0]['pnl']==1.5
    assert queries.get_bot_status(path)['status']=='running'
    command_id=queries.enqueue_command('close_all',path)
    assert db.get_pending_commands()[0]['id']==command_id
    assert queries.get_recent_commands(path)[0]['command']=='close_all'
    import pytest
    with pytest.raises(ValueError,match='already pending or running'):
        queries.enqueue_command('close_all',path)

def test_performance_report_includes_commissions_partial_closes_and_breakdowns(tmp_path):
    path=tmp_path/'metrics.db'; db=Database(path)
    db.insert_trade(ticket=11,symbol='EURUSD',asset_group='forex',direction='buy',volume=.1,open_time='2026-11-01T00:00:00+00:00',close_time='2026-11-02T00:00:00+00:00',profit=2,commission=-.2,swap=0,estimated_spread_cost=.1,magic=1,status='closed',close_reason='trail',strategy='trend_pullback')
    db.insert_partial_close(ticket=11,symbol='EURUSD',volume=.05,price=1.2,profit=.5,commission=-.05,swap=0,reason='partial_tp',time='2026-11-02T00:00:00+00:00')
    db.insert_trade(ticket=12,symbol='GOLD',asset_group='metals',direction='sell',volume=.01,open_time='2026-11-01T00:00:00+00:00',close_time='2026-11-03T00:00:00+00:00',profit=-1,commission=-.1,swap=0,magic=1,status='closed',close_reason='sl',strategy='mean_reversion')
    db.insert_equity_snapshot(time='2026-11-01T00:00:00+00:00',equity=100, balance=100,margin=0,free_margin=100,open_positions=0)
    db.insert_equity_snapshot(time='2026-11-02T00:00:00+00:00',equity=90, balance=90,margin=0,free_margin=90,open_positions=0)
    result=queries.get_performance_report('2026-11-01T00:00:00+00:00','2026-11-04T00:00:00+00:00',path)
    assert result['overall']['trades']==2
    assert result['overall']['expectancy']==pytest.approx(.575)
    assert result['overall']['average_win']==pytest.approx(2.25)
    assert result['overall']['average_loss']==pytest.approx(1.1)
    assert result['overall']['max_drawdown_pct']==pytest.approx(10)
    assert result['overall']['estimated_costs']==pytest.approx(.45)
    assert set(result['by_asset_group'])=={'forex','metals'}

def test_dashboard_feature_impact_reads_persisted_closed_trade_features(tmp_path):
    import json
    path=tmp_path/'features.db'; db=Database(path)
    db.insert_trade(ticket=99,status='closed',profit=1,commission=0,swap=0,features=json.dumps({'observations':{'with_usd_context':True}}))
    result=queries.get_feature_impact(path)
    assert result and result[0]['feature']=='observations.with_usd_context=true' and result[0]['trades']==1

def test_exploration_is_separate_from_main_dashboard_metrics(tmp_path):
    path=tmp_path/'modes.db'; db=Database(path)
    base={'asset_group':'forex','direction':'buy','volume':.01,'open_time':'2026-11-05T10:00:00+00:00','close_time':'2026-11-05T10:10:00+00:00','profit':1,'commission':0,'swap':0,'magic':1,'status':'closed','strategy':'test'}
    db.insert_trade(ticket=1,symbol='EURUSD',**base)
    db.insert_trade(ticket=2,symbol='GBPUSD',**base,mode='exploration')
    start='2026-11-05T00:00:00+00:00'; end='2026-11-06T00:00:00+00:00'
    assert queries.get_performance_report(start,end,path)['overall']['trades']==1
    assert len(queries.get_exploration_trades(start,end,path))==1

def test_dashboard_open_positions_includes_current_account_exploration_hash_symbol_and_separates_external(tmp_path):
    path=tmp_path/'open-positions.db'; db=Database(path)
    db.set_active_account_id('108765323')
    db.insert_trade(ticket=990053392,symbol='USDCAD#',asset_group='forex',direction='buy',volume=.02,
        open_time='2026-11-10T17:02:19+00:00',open_price=1.4201,sl=1.4144,tp=1.43154,profit=.25,
        commission=0,swap=0,magic=20260001,status='open',strategy='donchian_breakout',mode='exploration')
    db.insert_trade(ticket=42,symbol='EURUSD#',asset_group='forex',direction='buy',volume=.01,
        open_time='2026-11-10T17:00:00+00:00',open_price=1.1,sl=1.09,tp=1.12,profit=0,
        commission=0,swap=0,magic=20260001,status='open',strategy='breakout',mode='main',account_id='other-account')
    db.sync_external_positions('108765323',[{'ticket':7,'symbol':'GBPUSD#','magic':0,'direction':'sell','volume':.03,
        'open_time':'2026-11-10T17:03:00+00:00','open_price':1.3,'sl':1.31,'tp':1.28,'profit':-.1,'swap':0,'comment':'manual'}])
    owned=queries.get_open_positions(path)
    external=queries.get_external_open_positions(path)
    assert [(row['ticket'],row['symbol'],row['volume'],row['sl'],row['tp'],row['mode']) for row in owned]==[(990053392,'USDCAD#',.02,1.4144,1.43154,'exploration')]
    assert [(row['ticket'],row['symbol'],row['magic'],row['volume'],row['sl'],row['tp']) for row in external]==[(7,'GBPUSD#',0,.03,1.31,1.28)]


def test_enqueue_accepts_restart_and_single_close_only(tmp_path):
    import pytest
    from src.db import Database
    from src.dashboard import queries as q
    path = tmp_path / 'bot.db'; Database(path)
    assert q.enqueue_command('restart', path) and q.enqueue_command('close_position:123', path)
    for bad in ('close_position:abc', 'close_position:', 'rm -rf', 'restart;stop'):
        with pytest.raises(ValueError): q.enqueue_command(bad, path)
