from types import SimpleNamespace
from unittest.mock import MagicMock
from datetime import datetime, timezone

from src.models import Signal
from src.executor import Executor
from src.trade_rules import rebase_signal_levels


def test_exploration_keeps_family_stop_and_target_distances():
    assert rebase_signal_levels(150.0, 149.0, 152.0, 'buy', 150.25) == (149.25, 152.25)
    assert rebase_signal_levels(150.0, 151.5, 147.0, 'sell', 149.75) == (151.25, 146.75)


def test_exploration_signal_comment_format(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        magic_number=20260001,
        root=tmp_path,
        risk=SimpleNamespace(risk_per_trade_pct=0.25),
        config={
            'executor': {'deviation_points': 20, 'filling_modes': {}},
            'risk': {'risk_per_trade_pct': 0.25},
            'demo_exploration': {'risk_per_trade_pct': 0.1}
        }
    )
    mt5 = MagicMock()
    mt5.account_info.return_value = SimpleNamespace(equity=1000.0, trade_mode=0, margin_mode=2)
    info = SimpleNamespace(
        point=0.001, volume_min=0.01, volume_step=0.01, volume_max=100.0,
        trade_contract_size=1000.0, trade_tick_size=0.001, trade_tick_value_loss=0.00634,
        filling_mode=1, select=True, trade_mode=4, trade_stops_level=0,
        path='Forex\\USDJPYmicro', _asdict=lambda: {
            'point': 0.001, 'volume_min': 0.01, 'volume_step': 0.01, 'volume_max': 100.0,
            'trade_contract_size': 1000.0, 'trade_tick_size': 0.001,
            'trade_tick_value_loss': 0.00634
        }
    )
    mt5.symbol_info.return_value = info
    mt5.symbol_info_tick.return_value = SimpleNamespace(ask=150.0, bid=149.999, time=1)
    mt5.order_send.return_value = SimpleNamespace(retcode=10009, price=150.0, position=12345, comment='Done')
    mt5.TRADE_RETCODE_DONE = 10009
    mt5.TRADE_ACTION_DEAL = 1
    mt5.ORDER_TYPE_BUY = 0
    mt5.ORDER_TIME_GTC = 0
    mt5.SYMBOL_TRADE_MODE_FULL = 4
    mt5.SYMBOL_FILLING_FOK = 1
    mt5.ORDER_FILLING_FOK = 0
    mt5.ACCOUNT_TRADE_MODE_DEMO = 0
    mt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
    mt5.positions_get.return_value = [SimpleNamespace(ticket=12345, magic=20260001, sl=149.0, volume=0.01)]

    monkeypatch.setattr('src.symbols.SymbolService.is_tradable', lambda *a, **k: (True, 'tradable'))

    db = MagicMock()
    executor = Executor(mt5, settings, db)

    sig = Signal(
        symbol='USDJPYmicro',
        direction='buy',
        timeframe='H4',
        entry_hint=150.0,
        sl_price=149.0,
        tp_price=152.0,
        confidence=0.55,
        strategy_name='donchian_breakout',
        reason='test'
    )
    features = {
        'trade_mode': 'exploration',
        'exploration': {'family': 'donchian_breakout', 'variant': 'v1', 'timeframe': 'H4'}
    }

    res = executor.open_trade(sig, volume=0.01, features=features)
    assert res.success
    # Check that comment passed to order_send matched exp:{fam[:10]}:{var[:4]}:{tf[:3]}
    sent_req = mt5.order_send.call_args.kwargs
    assert mt5.order_send.call_args.args == ()
    assert sent_req['comment'] == 'exp:donchian_b:v1:H4'
    mt5.account_info.return_value.trade_mode = 1
    res = executor.open_trade(sig, volume=0.01, features=features)
    assert not res.success
    assert res.message == 'Refused open: exploration trades are DEMO only'
    assert mt5.order_send.call_count == 1
    mt5.account_info.return_value.trade_mode = 0
    res = executor.open_trade(sig, volume=0.3, features=features)
    assert not res.success
    assert 'requested volume exceeds stop-risk size' in res.message
    assert mt5.order_send.call_count == 1


def test_exploration_caps_enforced(tmp_path, monkeypatch):
    from src.bot import TradingBot

    settings = SimpleNamespace(
        magic_number=20260001,
        root=tmp_path,
        risk=SimpleNamespace(risk_per_trade_pct=0.25, no_entry_before_close_minutes=60),
        config={
            'live': {'mode': 'trade', 'normal_trading_disabled': True, 'strategies': ['donchian_breakout'], 'max_cycle_errors': 5},
            'demo_exploration': {'enabled': True, 'risk_per_trade_pct': 0.1, 'max_open_positions': 3, 'max_trades_per_day': 40},
            'risk': {'max_open_positions': 4, 'risk_per_trade_pct': 0.25, 'move_horizon_minutes': 60, 'max_total_open_risk_pct': 5.0},
            'weekend': {'enabled': False},
            'protections': {'enabled': False},
            'cost_model': {'slippage_points': 0}
        }
    )
    bot = TradingBot.__new__(TradingBot)
    bot.settings = settings
    bot._last_account_type = 'DEMO'
    bot.db = MagicMock()
    bot.db.count_mode_trades_since.return_value = 0
    bot.db.count_trades_since.return_value = 0
    bot.db.get_active_adaptive_gates.return_value = []
    bot.consecutive_errors = 0
    bot.trading_disabled = False
    bot._entries_health_blocked = False
    bot._trade_allowed = True
    bot._cycle_reservations = []
    bot._cycle_funnel = None
    bot.symbols = MagicMock()
    bot.symbols.forex_session_open.return_value = True
    bot.scanner = SimpleNamespace(symbols=bot.symbols)

    spec = {
        'symbol': 'AUDUSDmicro', 'point': 0.00001, 'volume_min': 0.01, 'volume_step': 0.01,
        'volume_max': 100.0, 'trade_contract_size': 1000.0, 'trade_tick_size': 0.00001,
        'trade_tick_value_loss': 0.01
    }
    sig = Signal(symbol='AUDUSDmicro', direction='buy', timeframe='H4', entry_hint=0.65, sl_price=0.645, tp_price=0.66, confidence=0.55, strategy_name='donchian_breakout', reason='test')
    candidate = SimpleNamespace(
        symbol='AUDUSDmicro',
        signal=sig,
        direction='buy',
        strategy='donchian_breakout',
        asset_group='forex',
        features={'exploration': {'eligible': True}},
        cost={'cost': 0.05},
        symbol_spec=spec,
        score=1.0,
        signal_row_id=None
    )

    bot.mt5 = MagicMock()
    bot.client = MagicMock()
    bot.client.mt5 = bot.mt5
    bot._protection_notified = set()

    state = SimpleNamespace(equity=1000.0, margin=100.0, margin_free=900.0, balance=1000.0)
    monkeypatch.setattr('src.bot.get_account_state', lambda client: state)
    bot.mt5.symbol_info.return_value = SimpleNamespace(
        point=0.00001, volume_min=0.01, volume_step=0.01, volume_max=100.0,
        trade_contract_size=1000.0, trade_tick_size=0.00001, trade_tick_value_loss=0.01,
        trade_mode=4, select=True, _asdict=lambda: spec
    )
    bot._signal_update = MagicMock()
    bot._get_day_and_week_pnl = lambda now: (0.0, 0.0)

    # Case 1: 3 positions already open -> rejected with cap reached
    open_3 = [
        SimpleNamespace(ticket=1, symbol='EURUSDmicro', comment='exp:donchian_b:v1:H4', magic=20260001),
        SimpleNamespace(ticket=2, symbol='GBPUSDmicro', comment='exp:ts_momentum:v1:H4', magic=20260001),
        SimpleNamespace(ticket=3, symbol='USDJPYmicro', comment='exp:session_br:v1:M15', magic=20260001),
    ]
    bot.mt5.positions_get.return_value = open_3
    bot._owned_positions = lambda: open_3

    res = bot._candidate(candidate, datetime.now(timezone.utc), True)
    assert res is False
    assert any('max open exploration positions cap' in str(arg) for call in bot._signal_update.call_args_list for arg in call[0])

    # Case 2: position on same symbol already open -> rejected with max 1 per symbol
    open_1 = [
        SimpleNamespace(ticket=1, symbol='AUDUSDmicro', comment='exp:donchian_b:v1:H4', magic=20260001),
    ]
    bot.mt5.positions_get.return_value = open_1
    bot._owned_positions = lambda: open_1
    bot._signal_update.reset_mock()
    res = bot._candidate(candidate, datetime.now(timezone.utc), True)
    assert res is False
    assert any('max 1 open exploration trade for symbol' in str(arg) for call in bot._signal_update.call_args_list for arg in call[0])

    # Case 3: several positions per symbol allowed in config -> the per-symbol rule does not apply
    settings.config['risk']['allow_multiple_positions_per_symbol'] = True
    bot._signal_update.reset_mock()
    bot._candidate(candidate, datetime.now(timezone.utc), True)
    assert not any('max 1 open exploration trade for symbol' in str(arg) for call in bot._signal_update.call_args_list for arg in call[0])
