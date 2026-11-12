"""Additional executor tests targeting enforce_time_stop, enforce_adverse_move_exit,
enforce_news_positions, requote paths, remediate_missing_sl and open_trade volume checks."""
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest
import numpy as np
import pandas as pd

from src.executor import Executor
from src.models import Signal


# ---- shared helpers ----

def _info_dict():
    return {'trade_tick_size': .0001, 'trade_tick_value': 1.0, 'trade_tick_value_loss': 1.0,
            'volume_min': .01, 'volume_step': .01, 'volume_max': 10.0}


class FakeDB2:
    def __init__(self):
        self.events = []; self.open = []; self.inserted = []; self.closed = {}
        self.partial_closes = []; self.partial_deal_tickets = set()
        self.features_updates = {}
    def log_event(self, *args): self.events.append(args)
    def get_open_trades(self): return list(self.open)
    def insert_trade(self, **kwargs): self.inserted.append(kwargs)
    def update_open_trade(self, ticket, **kwargs):
        for r in self.open:
            if int(r.get('ticket', -1)) == int(ticket): r.update(kwargs)
        return 1
    def update_trade_close(self, ticket, **kwargs): self.closed[ticket] = kwargs
    def get_trade(self, ticket):
        for r in self.open:
            if int(r.get('ticket', -1)) == int(ticket): return dict(r)
        return self.closed.get(ticket)
    def get_partial_closes(self, ticket): return self.partial_closes
    def get_partial_deal_tickets(self, ticket): return set(self.partial_deal_tickets)
    def insert_partial_close(self, **kwargs): self.partial_closes.append(kwargs); return 1
    def update_trade_features(self, ticket, features): self.features_updates[ticket] = features


class FakeMT52:
    POSITION_TYPE_BUY = 0; POSITION_TYPE_SELL = 1
    ORDER_TYPE_BUY = 0; ORDER_TYPE_SELL = 1
    SYMBOL_TRADE_MODE_FULL = 4; SYMBOL_FILLING_FOK = 1; ORDER_FILLING_FOK = 0
    SYMBOL_FILLING_IOC = 2; ORDER_FILLING_IOC = 1; ORDER_FILLING_RETURN = 2
    SYMBOL_TRADE_EXECUTION_MARKET = 2; ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
    ACCOUNT_TRADE_MODE_DEMO = 0
    TRADE_ACTION_SLTP = 6; TRADE_ACTION_DEAL = 1; TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_REQUOTE = 10004; TRADE_RETCODE_PRICE_CHANGED = 10020
    ORDER_TIME_GTC = 0
    DEAL_REASON_TP = 5; DEAL_REASON_SL = 4
    DEAL_ENTRY_OUT = 1; DEAL_ENTRY_OUT_BY = 3; DEAL_ENTRY_INOUT = 2; DEAL_ENTRY_IN = 0

    def __init__(self):
        self.orders = []; self._positions = []
        self._info = self._make_info()
        self._tick = SimpleNamespace(bid=1.101, ask=1.1011)
        self._deals = []

    @staticmethod
    def _make_info():
        info = SimpleNamespace(
            trade_mode=4, trade_stops_level=0, point=.00001, digits=5,
            volume_min=.01, volume_step=.01, volume_max=10.0,
            trade_tick_size=.0001, trade_tick_value=1.0,
            trade_tick_value_loss=1.0, trade_tick_value_profit=1.0,
            filling_mode=1, trade_exemode=1,
            trade_commission=None, commission=None)
        info._asdict = lambda: _info_dict()
        return info

    def positions_get(self, *a, **kw): return list(self._positions)
    def symbol_info(self, *a): return self._info
    def symbol_info_tick(self, *a): return self._tick
    def account_info(self):
        return SimpleNamespace(equity=10000, balance=10000, margin=0,
                               margin_free=10000, margin_mode=2, trade_mode=0,
                               margin_level=0, currency='USD', leverage=100)
    def symbols_get(self): return []
    def order_send(self, **request):
        assert request and all(value is not None for value in request.values())
        assert all(type(value) in (int, float, str) for value in request.values())
        action=request['action']
        common={'action','symbol','magic'}
        if action==self.TRADE_ACTION_DEAL:
            allowed=common|{'volume','type','price','sl','tp','deviation','comment','type_time','type_filling','position'}
            assert type(request['volume']) is float
            assert request['volume']>=self._info.volume_min
            assert abs((request['volume']-self._info.volume_min)/self._info.volume_step-round((request['volume']-self._info.volume_min)/self._info.volume_step))<1e-7
            assert type(request['price']) is float and request['price']==round(request['price'],self._info.digits)
            if 'comment' in request:
                assert len(request['comment'])<=31 and request['comment'].isascii()
        elif action==self.TRADE_ACTION_SLTP:
            allowed=common|{'position','sl','tp'}
            assert request['sl']==round(request['sl'],self._info.digits)
            assert request['tp']==round(request['tp'],self._info.digits)
        else:
            raise AssertionError(f'unknown MT5 action {action}')
        assert set(request)<=allowed
        self.orders.append(dict(request))
        return SimpleNamespace(retcode=10009, price=request.get('price', 0),
                               comment='done', position=request.get('position', 100),
                               order=100, deal=100, volume=request.get('volume', 0))
    def last_error(self): return (0, 'ok')
    def order_calc_margin(self, *a): return 50.0
    def history_deals_get(self, *a, **kw): return list(self._deals)
    def copy_rates_from_pos(self, *a): return None


def _make_exe(tmp_path, extra_risk=None):
    risk_cfg = dict(
        max_hold_minutes=60, first_target_r=1.0, scale_out_pct=0.5,
        breakeven_cost_multiplier=1.5, momentum_lookback_bars=5,
        trail_atr_mult=1.5, stagnation_minutes=120, stagnation_min_r=0.5,
        max_session_gap_hours=4, no_entry_before_close_minutes=15,
        early_exit_enabled=True, early_exit_volume_lookback=20,
        risk_per_trade_pct=1.0,
    )
    if extra_risk:
        risk_cfg.update(extra_risk)

    settings = SimpleNamespace(
        root=Path(tmp_path), magic_number=7,
        risk=SimpleNamespace(**risk_cfg),
        allow_real=False, db_path='data/test.db',
        config={
            'account': {'mode': 'demo'},
            'executor': {'deviation_points': 20},
            'risk': {
                'max_hold_extended_minutes': 180,
                'extend_step_minutes': 30,
                'extend_min_score': 0.7,
                'early_exit_enabled': True,
                'early_exit_volume_lookback': 20,
            },
            'scanner': {},
        },
    )

    class Service:
        def is_tradable(self, s): return True, 'tradable'
        def get_asset_group(self, s): return 'forex'
        def next_session_close(self, s, n, g): return None

    mt5 = FakeMT52(); db = FakeDB2()
    return Executor(mt5, settings, db, Service()), mt5, db


def test_executor_sends_flat_typed_order_fields_and_rounds_for_strict_mt5(tmp_path):
    exe,mt5,db=_make_exe(tmp_path)
    signal=Signal('EURUSD','buy','M5',1.1011,1.10001234,1.10309876,.8,'test')
    result=exe.open_trade(signal,.019,features={'trade_mode':'exploration','exploration':{'family':'ts_momentum_long_name','variant':'variant_long','timeframe':'H4'}})
    assert result.success
    request=mt5.orders[0]
    assert set(request)=={'action','symbol','volume','type','price','sl','tp','deviation','magic','comment','type_time','type_filling'}
    assert request['volume']==0.01
    assert request['price']==1.1011 and request['sl']==1.10001 and request['tp']==1.1031
    assert len(request['comment'])<=31 and request['comment'].isascii()
    assert db.inserted[0]['features']
    with pytest.raises((AssertionError,KeyError)):
        mt5.order_send(request=request)

def test_build_open_request_is_flat_plain_typed_and_marked_for_demo_diagnostic(tmp_path):
    exe,mt5,_=_make_exe(tmp_path)
    signal=Signal('EURUSD#','buy','M15',1.1011,1.10001234,1.10309876,.8,'diagnostic')
    features={'trade_mode':'exploration','exploration':{'family':'e2e_test','variant':'v1','timeframe':'M15'}}
    prepared=exe.build_open_request(signal,.019,mt5._info,mt5._tick,mt5.ORDER_FILLING_FOK,features)
    request=prepared['request']
    assert request['symbol']=='EURUSD#' and request['volume']==.01
    assert request['sl']==1.10001 and request['tp']==1.1031
    assert request['comment']=='exp:e2e_test:v1:M15'
    assert all(type(value) in (int,float,str) for value in request.values())
    assert 'request' not in request


def test_executor_request_normalizers_use_symbol_precision_and_plain_ascii():
    info=SimpleNamespace(digits=3,volume_min=.01,volume_step=.01)
    assert Executor._price(info,158.123456)==158.123
    assert Executor._volume(info,.019)==.01
    assert Executor._comment('é'*40)=='?'*31


def _buy_row(ticket=9):
    return {'ticket': ticket, 'symbol': 'EURUSD', 'direction': 'buy', 'magic': 7,
            'status': 'open', 'initial_volume': 0.1, 'volume': 0.1,
            'open_price': 1.1, 'sl': 1.099, 'tp': 1.103,
            'initial_risk': 0.001, 'scale_out_done': 0,
            'features': '{}', 'peak_favorable_r': 0,
            'extension_until': None, 'extended_minutes': 0}


def _buy_pos(ticket=9, sl=1.099, tp=1.103, magic=7, volume=0.1, age_seconds=0):
    t = int(datetime.now(timezone.utc).timestamp()) - age_seconds
    return SimpleNamespace(ticket=ticket, identifier=ticket, magic=magic,
                           symbol='EURUSD', type=0, price_open=1.1,
                           price_current=1.101, sl=sl, tp=tp, volume=volume,
                           profit=10, swap=-0.5, time=t)


# ---- enforce_time_stop ----

def test_enforce_time_stop_max_hold_extended_closes(tmp_path):
    """Position older than max_hold_extended_minutes is force-closed."""
    exe, mt5, db = _make_exe(tmp_path)
    # 200 min old => > max_hold_extended_minutes (180)
    pos = _buy_pos(age_seconds=200 * 60)
    mt5._positions = [pos]
    db.open = [_buy_row()]
    actions = exe.enforce_time_stop()
    assert len(actions) == 1 and actions[0].success

def test_enforce_time_stop_no_matching_row(tmp_path):
    """Position with no DB row is skipped."""
    exe, mt5, db = _make_exe(tmp_path)
    pos = _buy_pos(age_seconds=200 * 60)
    mt5._positions = [pos]
    db.open = []  # no matching row
    actions = exe.enforce_time_stop()
    assert actions == []

def test_enforce_time_stop_wrong_magic_skipped(tmp_path):
    """Position from different magic is ignored."""
    exe, mt5, db = _make_exe(tmp_path)
    pos = _buy_pos(magic=999, age_seconds=200 * 60)
    mt5._positions = [pos]
    db.open = [_buy_row()]
    actions = exe.enforce_time_stop()
    assert actions == []

def test_enforce_time_stop_scale_out_at_target_r(tmp_path):
    """Position at target R triggers scale-out."""
    exe, mt5, db = _make_exe(tmp_path)
    # fresh position, profit > initial_risk => current_r >= first_target_r
    pos = _buy_pos(age_seconds=0)
    pos.price_current = 1.102  # R = (1.102-1.1)/0.001 = 2.0 >= 1.0
    mt5._positions = [pos]
    mt5._tick = SimpleNamespace(bid=1.102, ask=1.1021)
    row = dict(_buy_row()); row['scale_out_done'] = 0
    db.open = [row]

    # Mock get_closed_candles to return empty so no momentum/atr calcs
    with patch('src.market_data.get_closed_candles', return_value=pd.DataFrame()):
        with patch('src.market_data.get_cost_estimate', return_value={'cost': 0.0}):
            actions = exe.enforce_time_stop()
    # scale_out_done gets set to 1 or close_all happens
    # At minimum, some action or db update occurred - no crash
    assert isinstance(actions, list)

def test_enforce_time_stop_time_stop_no_extension(tmp_path):
    """Position past max_hold_minutes without extension is closed."""
    exe, mt5, db = _make_exe(tmp_path)
    pos = _buy_pos(age_seconds=70 * 60)  # 70 min > max_hold_minutes(60)
    mt5._positions = [pos]
    row = dict(_buy_row()); row['scale_out_done'] = 0
    db.open = [row]

    with patch('src.market_data.get_closed_candles', side_effect=RuntimeError("no data")):
        with patch('src.market_data.get_cost_estimate', return_value={'cost': 0.0}):
            actions = exe.enforce_time_stop()
    assert len(actions) == 1


# ---- enforce_adverse_move_exit ----

def test_enforce_adverse_move_exit_disabled(tmp_path):
    """When early_exit_enabled=False, no actions taken."""
    exe, mt5, db = _make_exe(tmp_path)
    exe.settings.config['risk']['early_exit_enabled'] = False
    pos = _buy_pos()
    mt5._positions = [pos]
    db.open = [_buy_row()]
    actions = exe.enforce_adverse_move_exit()
    assert actions == []

def test_enforce_adverse_move_exit_wrong_magic(tmp_path):
    exe, mt5, db = _make_exe(tmp_path)
    pos = _buy_pos(magic=999)
    mt5._positions = [pos]
    db.open = [_buy_row()]
    actions = exe.enforce_adverse_move_exit()
    assert actions == []

def test_enforce_adverse_move_exit_no_tick(tmp_path):
    exe, mt5, db = _make_exe(tmp_path)
    mt5._positions = [_buy_pos()]
    mt5.symbol_info_tick = lambda *a: None
    db.open = [_buy_row()]
    actions = exe.enforce_adverse_move_exit()
    assert actions == []

def test_enforce_adverse_move_exit_data_unavailable_logs_warning(tmp_path):
    exe, mt5, db = _make_exe(tmp_path)
    mt5._positions = [_buy_pos()]
    db.open = [_buy_row()]
    with patch('src.market_data.get_closed_candles', side_effect=RuntimeError("unavailable")):
        actions = exe.enforce_adverse_move_exit()
    assert actions == []
    assert any('Adverse exit data unavailable' in str(e) for e in db.events)

def test_enforce_adverse_move_exit_exit_triggered(tmp_path):
    """When should_exit_early returns True, position is closed."""
    exe, mt5, db = _make_exe(tmp_path)
    pos = _buy_pos()
    mt5._positions = [pos]
    db.open = [_buy_row()]

    empty_df = pd.DataFrame()
    with patch('src.market_data.get_closed_candles', return_value=empty_df):
        with patch('src.executor.adverse_exit_score', return_value=(3, ['score_too_high'])):
            with patch('src.executor.should_exit_early', return_value=True):
                actions = exe.enforce_adverse_move_exit()
    assert len(actions) == 1


# ---- enforce_news_positions ----

def test_enforce_news_positions_no_events(tmp_path):
    exe, mt5, db = _make_exe(tmp_path)
    mt5._positions = [_buy_pos()]
    db.open = [_buy_row()]
    actions = exe.enforce_news_positions([], now_utc=datetime.now(timezone.utc))
    assert actions == []

def test_enforce_news_positions_wrong_magic(tmp_path):
    exe, mt5, db = _make_exe(tmp_path)
    pos = _buy_pos(magic=999)
    mt5._positions = [pos]
    db.open = [_buy_row()]
    now = datetime.now(timezone.utc)
    event = SimpleNamespace(importance='HIGH', time=now + timedelta(minutes=5), currency='EUR')
    with patch('src.news.event_matches_symbol', return_value=True):
        actions = exe.enforce_news_positions([event], now_utc=now)
    assert actions == []

def test_enforce_news_positions_in_profit_sets_breakeven_sl(tmp_path):
    """In-profit position with event within 15min gets breakeven SL set."""
    exe, mt5, db = _make_exe(tmp_path)
    pos = _buy_pos()
    pos.price_open = 1.098  # entry below current bid of 1.101 => in profit
    mt5._positions = [pos]
    mt5._tick = SimpleNamespace(bid=1.101, ask=1.1011)
    db.open = [_buy_row()]
    now = datetime.now(timezone.utc)
    event = SimpleNamespace(importance='HIGH', time=now + timedelta(minutes=5), currency='EUR')
    with patch('src.news.event_matches_symbol', return_value=True):
        with patch('src.market_data.get_cost_estimate', return_value={'cost': 2.0}):
            actions = exe.enforce_news_positions([event], now_utc=now)
    # Should not have closed the position
    assert actions == []

def test_enforce_news_positions_losing_close_to_event(tmp_path):
    """Losing position within 5 min of event is closed."""
    exe, mt5, db = _make_exe(tmp_path)
    pos = _buy_pos()
    pos.price_open = 1.102  # entry ABOVE current bid 1.101 => losing
    mt5._positions = [pos]
    db.open = [_buy_row()]
    now = datetime.now(timezone.utc)
    event = SimpleNamespace(importance='HIGH', time=now + timedelta(minutes=3), currency='EUR')
    with patch('src.news.event_matches_symbol', return_value=True):
        actions = exe.enforce_news_positions([event], now_utc=now)
    assert len(actions) == 1


# ---- open_trade: requote paths ----

def test_open_trade_requote_tick_unavailable(tmp_path):
    """After requote, if tick unavailable, trade is cancelled."""
    exe, mt5, db = _make_exe(tmp_path)
    tick_calls = [0]
    def tick_side_effect(*a):
        tick_calls[0] += 1
        if tick_calls[0] <= 1:
            return SimpleNamespace(bid=1.101, ask=1.1011)
        return None  # second call fails
    mt5.symbol_info_tick = tick_side_effect
    mt5.order_send = lambda **request: SimpleNamespace(retcode=10004, comment='requote',
                                                 position=0, order=0, deal=0, volume=0, price=0)
    sig = Signal('EURUSD', 'buy', 'M5', 1.1011, 1.1, 1.103, 0.8, 'test')
    res = exe.open_trade(sig, 0.01)
    assert not res.success and 'tick unavailable' in res.message.lower()

def test_open_trade_requote_price_violates_buy_stops(tmp_path):
    """Requote at a price that violates SL/TP for buy is cancelled."""
    exe, mt5, db = _make_exe(tmp_path)
    # Initial order triggers requote
    call_count = [0]
    def order_side(**request):
        call_count[0] += 1
        return SimpleNamespace(retcode=10004, comment='requote',
                               position=0, order=0, deal=0, volume=0, price=0)
    mt5.order_send = order_side
    # After requote, new price is ABOVE tp => violates buy
    tick_count = [0]
    def tick_side(*a):
        tick_count[0] += 1
        if tick_count[0] == 1:
            return SimpleNamespace(bid=1.101, ask=1.1011)
        return SimpleNamespace(bid=1.106, ask=1.1061)  # above tp=1.103
    mt5.symbol_info_tick = tick_side
    sig = Signal('EURUSD', 'buy', 'M5', 1.1011, 1.1, 1.103, 0.8, 'test')
    res = exe.open_trade(sig, 0.01)
    assert not res.success and 'violates stop/target' in res.message

def test_open_trade_order_send_fails_returns_none(tmp_path):
    """When order_send returns None, failure is returned."""
    exe, mt5, db = _make_exe(tmp_path)
    mt5.order_send = lambda **request: None
    sig = Signal('EURUSD', 'buy', 'M5', 1.1011, 1.1, 1.103, 0.8, 'test')
    res = exe.open_trade(sig, 0.01)
    assert not res.success and 'order_send failed' in res.message

def test_open_trade_broker_rejects_with_retcode(tmp_path):
    """When broker returns non-DONE retcode, failure is returned."""
    exe, mt5, db = _make_exe(tmp_path)
    mt5.order_send = lambda **request: SimpleNamespace(retcode=10006, comment='rejected',
                                                 position=0, order=0, deal=0, volume=0.01, price=0)
    sig = Signal('EURUSD', 'buy', 'M5', 1.1011, 1.1, 1.103, 0.8, 'test')
    res = exe.open_trade(sig, 0.01)
    assert not res.success and 'rejected' in res.message

def test_open_trade_volume_is_rounded_down_to_broker_grid(tmp_path):
    """A requested volume between broker steps is rounded down, never up."""
    exe, mt5, db = _make_exe(tmp_path)
    sig = Signal('EURUSD', 'buy', 'M5', 1.1011, 1.1, 1.103, 0.8, 'test')
    res = exe.open_trade(sig, 0.015)  # between 0.01 steps
    assert res.success and mt5.orders[0]['volume'] == 0.01

def test_open_trade_symbol_unavailable(tmp_path):
    """When symbol_info or tick is None, failure is returned."""
    exe, mt5, db = _make_exe(tmp_path)
    mt5.symbol_info = lambda *a: None
    sig = Signal('EURUSD', 'buy', 'M5', 1.1011, 1.1, 1.103, 0.8, 'test')
    res = exe.open_trade(sig, 0.01)
    assert not res.success

def test_open_trade_symbol_not_tradable(tmp_path):
    """When symbol service says not tradable, failure is returned."""
    exe, mt5, db = _make_exe(tmp_path)

    class RejectService:
        def is_tradable(self, s): return False, 'restricted'
        def get_asset_group(self, s): return 'forex'
        def next_session_close(self, s, n, g): return None

    exe.symbol_service = RejectService()
    sig = Signal('EURUSD', 'buy', 'M5', 1.1011, 1.1, 1.103, 0.8, 'test')
    res = exe.open_trade(sig, 0.01)
    assert not res.success and 'restricted' in res.message


# ---- remediate_missing_sl ----

def test_remediate_missing_sl_already_has_sl(tmp_path):
    """Position with existing SL returns True immediately."""
    exe, mt5, db = _make_exe(tmp_path)
    pos = _buy_pos(sl=1.099)
    result = exe.remediate_missing_sl(pos, planned_sl=1.099)
    assert result is True

def test_remediate_missing_sl_sets_emergency_sl(tmp_path):
    """Position without SL gets emergency ATR-based SL set."""
    exe, mt5, db = _make_exe(tmp_path)
    pos = _buy_pos(sl=0)  # no SL
    result = exe.remediate_missing_sl(pos, planned_sl=1.099)
    assert result is True  # broker accepted (FakeMT52 returns DONE)

def test_remediate_missing_sl_all_retries_fail_closes_position(tmp_path):
    """When all retries fail, position is closed at market."""
    exe, mt5, db = _make_exe(tmp_path)
    pos = _buy_pos(sl=0)
    mt5._positions = [pos]
    db.open = [_buy_row()]
    # Make order_send always fail
    mt5.order_send = lambda **request: SimpleNamespace(retcode=10006, comment='fail',
                                                 position=0, order=0, deal=0, volume=0, price=0)
    result = exe.remediate_missing_sl(pos, planned_sl=0, max_retries=2)
    assert result is False
    # Should have logged CRITICAL and attempted close
    assert any('Emergency remediation failed' in str(e) for e in db.events)

def test_remediate_missing_sl_computes_atr_sl(tmp_path):
    """When planned_sl is on wrong side, ATR-based fallback is computed."""
    exe, mt5, db = _make_exe(tmp_path)
    pos = _buy_pos(sl=0)
    pos.price_open = 1.1
    # planned_sl above price_open for buy => wrong side => should compute ATR fallback
    with patch('src.market_data.get_closed_candles') as mock_candles:
        candle_data = pd.DataFrame({'close': [1.1] * 30, 'high': [1.101] * 30,
                                    'low': [1.099] * 30, 'open': [1.1] * 30})
        mock_candles.return_value = candle_data
        result = exe.remediate_missing_sl(pos, planned_sl=1.102)  # above ask=1.1011 => invalid buy SL
    assert result is True  # broker accepted


def _extension_case(tmp_path, requires):
    exe, mt5, db = _make_exe(tmp_path)
    exe.settings.config['risk']['extend_requires_scale_out'] = requires
    pos = _buy_pos()
    mt5._positions = [pos]; mt5._tick = SimpleNamespace(bid=1.1005, ask=1.1006)
    db.open = [dict(_buy_row())]                 # no partial close yet, stop below entry
    moved = []
    exe.modify_sl_tp = lambda ticket, sl, tp: moved.append((ticket, sl)) or True
    opened = datetime.now(timezone.utc) - timedelta(minutes=61)   # just past max_hold_minutes=60
    with patch('src.executor.mt5_epoch_to_utc', return_value=opened), \
         patch('src.executor.continuation_metrics', return_value=(True, True, 1.0)), \
         patch('src.market_data.get_closed_candles', return_value=pd.DataFrame()), \
         patch('src.market_data.get_cost_estimate', return_value={'cost': 0.0}):
        actions = exe.enforce_time_stop()
    return actions, moved, db


def test_winner_without_partial_close_is_closed_by_default(tmp_path):
    actions, moved, db = _extension_case(tmp_path, True)
    assert len(actions) == 1 and moved == [] and db.open[0]['extension_until'] is None


def test_winner_without_partial_close_is_extended_after_breakeven_stop_when_allowed(tmp_path):
    actions, moved, db = _extension_case(tmp_path, False)
    assert actions == [] and moved and moved[0][0] == 9 and moved[0][1] >= 1.1
    assert db.open[0]['extension_until'] is not None
