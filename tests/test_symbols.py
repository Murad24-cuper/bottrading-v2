from datetime import datetime, timezone
from types import SimpleNamespace

from src.symbols import SymbolService, is_dated_future, underlying_group, underlying_key


class FakeMT5:
    SYMBOL_TRADE_MODE_FULL = 4
    TIMEFRAME_H1 = 16385

    def __init__(self, names):
        now = datetime.now(timezone.utc).timestamp()
        self.rows = [SimpleNamespace(name=name) for name in names]
        self.ticks = {name: SimpleNamespace(time=int(now), time_msc=int(now * 1000), bid=1.0, ask=1.001) for name in names}

    def symbols_get(self):
        return self.rows

    def symbol_info(self, name):
        return SimpleNamespace(name=name, path='Indices\\US', trade_mode=4, point=0.001, select=False)

    def symbol_select(self, name, enable):
        return True

    def symbol_info_tick(self, name):
        return self.ticks.get(name, next(iter(self.ticks.values())))

    def last_error(self):
        return (0, 'ok')

    def copy_rates_from_pos(self,name,timeframe,start,count):
        import time
        stamp=int(time.time())
        return [{'time':stamp-3600*(count-i+start),'open':1,'high':1.1,'low':.9,'close':1,'tick_volume':1,'spread':1} for i in range(count)]


def make_service(names, exclude_dated=True):
    mt5 = FakeMT5(names)
    config = {'universe': {'include_groups': ['indices'], 'exclude_symbols': [], 'exclude_dated_futures': exclude_dated, 'symbol_aliases': {}, 'refresh_minutes':5}, 'history': {'liquid_aliases': []}}
    settings = SimpleNamespace(config=config, risk=SimpleNamespace(max_spread_points={}))
    return SymbolService(mt5, settings)


def test_underlying_normalizes_cash_and_expiry_suffixes():
    assert underlying_key('US500Cash') == 'US500'
    assert underlying_key('US500-DEC26') == 'US500'
    assert underlying_key('EURUSDm') == 'EURUSD'

def test_broker_path_with_spelled_out_indices_is_grouped_as_index():
    service=make_service(['Altcoins_INDX'])
    service.mt5.symbol_info=lambda name: SimpleNamespace(path='Thematic Indices\\Standard\\Altcoins_INDX')
    assert service.get_asset_group('Altcoins_INDX')=='indices'


def test_dated_futures_are_excluded_from_universe_by_default():
    service = make_service(['US500Cash', 'US500-DEC26', 'US400Cash'])
    assert service.get_universe() == ['US500Cash', 'US400Cash']
    assert is_dated_future('US500-DEC26')
    assert underlying_group('US400Cash', 'indices') == 'us_indices'


def test_dated_futures_can_be_included_when_explicitly_configured():
    service = make_service(['US500-DEC26'], exclude_dated=False)
    assert service.get_universe() == ['US500-DEC26']

def test_focus_universe_resolves_only_ten_configured_logical_symbols():
    names=['EURUSD','GBPUSD','USDJPY','AUDUSD','USDCAD','US500Cash','US100Cash','US30Cash','GER40Cash','GOLD','BTCUSD']
    service=make_service(names)
    service.settings.config['universe'].update({
        'focus_symbols':['EURUSD','GBPUSD','USDJPY','AUDUSD','USDCAD','US500','NAS100','US30','GER40','GOLD'],
        'symbol_aliases':{'US500':['US500'],'NAS100':['US100'],'US30':['US30'],'GER40':['GER40']},
    })
    found=service.active_logical_universe(datetime(2026,11,9,12,tzinfo=timezone.utc))
    assert found==['EURUSD','GBPUSD','USDJPY','AUDUSD','USDCAD','US500Cash','US100Cash','US30Cash','GER40Cash','GOLD']

def test_main_profile_prefers_micro_names_for_all_focus_forex_and_gold():
    import yaml
    from pathlib import Path
    config=yaml.safe_load((Path(__file__).resolve().parent / 'fixtures' / 'default_config.yaml').read_text(encoding='utf-8'))
    logicals=['EURUSD','GBPUSD','USDJPY','AUDUSD','USDCAD','EURJPY','GBPJPY','GOLD']
    aliases=config['universe']['symbol_aliases']
    names=[f'{name}micro' for name in logicals]
    service=make_service(names)
    service.settings.config['universe'].update(config['universe'])
    assert {name:service.resolve_alias(name) for name in logicals}==dict(zip(logicals,names))

def test_alias_prefers_enabled_suffix_when_exact_broker_symbol_is_disabled():
    service=make_service(['EURUSD','EURUSD#'])
    service.settings.config['universe']['symbol_aliases']={'EURUSD':['EURUSD']}
    modes={'EURUSD':0,'EURUSD#':4}
    service.mt5.symbol_info=lambda name: SimpleNamespace(trade_mode=modes[name],point=0.00001)

    assert service.resolve_alias('EURUSD')=='EURUSD#'

def test_weekend_universe_is_crypto_gold247_plus_fresh_open_indices(monkeypatch):
    service=make_service(['BTCUSD','ETHUSD','GOLD24-7','US500Cash','US30Cash'])
    service.settings.config['universe'].update({
        'weekend_symbols':['BTCUSD','ETHUSD','GOLD24-7'],
        'symbol_aliases':{'BTCUSD':['BTCUSD'],'ETHUSD':['ETHUSD'],'GOLD24-7':['GOLD24-7']},
        'weekend_include_open_indices':True,
    })
    service.market_should_be_open=lambda symbol,now: symbol=='US500Cash'
    now=datetime(2026,11,8,12,tzinfo=timezone.utc)
    import src.time_utils
    monkeypatch.setattr(src.time_utils,'mt5_epoch_to_utc',lambda stamp,mt5:now)
    found=service.active_logical_universe(now)
    assert found==['BTCUSD','ETHUSD','GOLD24-7','US500Cash']

def test_configured_liquid_symbols_are_selected_first_for_rotation():
    service=make_service(['US500Cash','US400Cash'])
    service.settings.config['history']['liquid_aliases']=['US400Cash']
    assert service.get_candidates()[:2]==['US400Cash','US500Cash']

def test_missing_session_history_falls_back_to_recent_bar_activity(monkeypatch):
    import pandas as pd
    from src import market_data
    service=make_service(['US500Cash']); service.session_history_available=lambda symbol:False
    service.settings.config['universe']['tick_freshness_seconds_by_group']={'indices':180,'other':60}
    now=pd.Timestamp.now(tz='UTC')
    monkeypatch.setattr(market_data,'get_closed_candles',lambda *args,**kwargs:pd.DataFrame({'close':[1]},index=[now-pd.Timedelta(minutes=1)]))
    assert service.is_tradable('US500Cash')==(True,'tradable')

def test_missing_recent_bar_activity_rejects_when_session_history_is_unavailable(monkeypatch):
    import pandas as pd
    from src import market_data
    service=make_service(['US500Cash']); service.session_history_available=lambda symbol:False
    service.settings.config['universe']['tick_freshness_seconds_by_group']={'indices':180,'other':60}
    monkeypatch.setattr(market_data,'get_closed_candles',lambda *args,**kwargs:pd.DataFrame())
    assert service.is_tradable('US500Cash')==(False,'no recent bar activity')

def test_session_close_is_inferred_in_utc_from_repeated_hourly_gaps():
    import pandas as pd
    from datetime import timedelta
    from src.sessions import next_session_close
    stamps=[]
    start=datetime(2026,10,19,tzinfo=timezone.utc)
    for day in range(15):
        current=start+timedelta(days=day)
        if current.weekday()>=5: continue
        last_hour=20 if current.weekday()==4 else 23
        stamps.extend(current.replace(hour=hour) for hour in range(last_hour+1))
    frame=pd.DataFrame({'close':range(len(stamps))},index=pd.DatetimeIndex(stamps))
    now=datetime(2026,10,30,20,30,tzinfo=timezone.utc)
    upcoming=next_session_close(frame,now,6)
    assert upcoming is not None

def test_market_open_check_uses_recurring_weekend_closures():
    import pandas as pd
    from datetime import timedelta
    service=make_service(['EURUSD'])
    stamps=[]; start=datetime(2026,10,19,tzinfo=timezone.utc)
    for day in range(15):
        current=start+timedelta(days=day)
        if current.weekday()>=5: continue
        last_hour=20 if current.weekday()==4 else 23
        stamps.extend(current.replace(hour=hour) for hour in range(last_hour+1))
    frame=pd.DataFrame({'close':range(len(stamps))},index=pd.DatetimeIndex(stamps))
    service._session_frame=lambda symbol:frame
    assert service.market_should_be_open('EURUSD',datetime(2026,10,30,20,30,tzinfo=timezone.utc))
    assert not service.market_should_be_open('EURUSD',datetime(2026,10,30,22,00,tzinfo=timezone.utc))

def test_forex_reopen_requires_fresh_tradable_quote():
    service=make_service(['EURUSD'])
    service.market_should_be_open=lambda symbol,now:True
    service.is_tradable=lambda symbol,now_utc=None:(False,'stale tick / market closed')
    assert not service.forex_session_open(datetime(2026,11,8,19,tzinfo=timezone.utc))

def test_session_close_is_inferred_in_utc_from_repeated_hourly_gaps():
    import pandas as pd
    from datetime import timedelta
    from src.sessions import next_session_close
    stamps=[]
    start=datetime(2026,10,19,tzinfo=timezone.utc)
    for day in range(15):
        current=start+timedelta(days=day)
        if current.weekday()>=5: continue
        last_hour=20 if current.weekday()==4 else 23
        stamps.extend(current.replace(hour=hour) for hour in range(last_hour+1))
    frame=pd.DataFrame({'close':range(len(stamps))},index=pd.DatetimeIndex(stamps))
    now=datetime(2026,10,30,20,30,tzinfo=timezone.utc)
    upcoming=next_session_close(frame,now,6)
    assert upcoming is not None
    assert upcoming[0]==datetime(2026,10,30,21,0,tzinfo=timezone.utc)
    assert upcoming[1]>6
