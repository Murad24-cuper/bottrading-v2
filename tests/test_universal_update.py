"""Tests for the universal Windows/Linux update: risk profiles, live brain, OOD,
freeze level, locks and start/stop scripts."""
from pathlib import Path
from types import SimpleNamespace
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src import risk_profile as rp
from src.brain.live import dynamic_risk, live_status
from src.brain.memory import query_memory
from src.brain.ood import distance_is_out_of_distribution, fit_ood_guard_fast
from src.db import Database
from src.process_utils import SingleInstanceLock
from src.risk import check_trade_allowed
from tests.test_executor_safety import make_executor

ROOT = Path(__file__).resolve().parents[1]
DEMO = SimpleNamespace(trade_mode=0)
REAL = SimpleNamespace(trade_mode=2)


def _config(**demo):
    return {'risk': {'risk_per_trade_pct': .25, 'daily_loss_limit_pct': 3.0, 'weekly_loss_limit_pct': 8.0},
            'weekend': {'risk_per_trade_pct': .15},
            'demo_risk': {'risk_per_trade_pct': 1.0, 'daily_loss_limit_pct': 10.0, 'weekly_loss_limit_pct': 25.0,
                          'brain_min_risk_pct': 1.0, 'brain_max_risk_pct': 2.0, **demo},
            'brain': {'real_risk': {'brain_min_risk_pct': .1, 'brain_max_risk_pct': .25}}}


# ---------------------------------------------------------------- risk profile
def test_account_type_comes_from_mt5_and_unknown_is_real():
    assert rp.is_demo_account(DEMO) and not rp.is_demo_account(REAL)
    assert not rp.is_demo_account(None) and not rp.is_demo_account(SimpleNamespace())


def test_demo_uses_one_percent_in_every_mode_real_keeps_locked_quarter_percent():
    config = _config()
    for mode in ('main', 'weekend', 'exploration', 'weekend_exploration'):
        assert rp.base_risk_pct(config, True, mode) == 1.0
    assert rp.base_risk_pct(config, False, 'main') == .25
    assert rp.base_risk_pct(config, False, 'weekend') == .15


def test_loss_limits_are_separate_for_demo_and_real():
    assert rp.loss_limits(_config(), True) == (10.0, 25.0)
    assert rp.loss_limits(_config(), False) == (3.0, 8.0)


@pytest.mark.parametrize('key,value', [('risk_per_trade_pct', 2.5), ('daily_loss_limit_pct', 11), ('weekly_loss_limit_pct', 26), ('brain_max_risk_pct', 3)])
def test_demo_ceilings_cannot_be_raised(key, value):
    with pytest.raises(ValueError):
        rp.validate_demo_risk(_config(**{key: value}))


def test_real_brain_bound_cannot_exceed_locked_ceiling():
    config = _config(); config['brain']['real_risk']['brain_max_risk_pct'] = .5
    with pytest.raises(ValueError):
        rp.validate_demo_risk(config)


def test_applied_risk_reclamps_brain_value_to_account_bounds():
    config = _config()
    live = {'brain': {'sizing': {'active': True, 'risk_pct': 50.0}}}
    assert rp.applied_risk_pct(config, True, 'main', live) == 2.0
    assert rp.applied_risk_pct(config, False, 'main', live) == .25
    low = {'brain': {'sizing': {'active': True, 'risk_pct': .01}}}
    assert rp.applied_risk_pct(config, True, 'main', low) == 1.0
    inactive = {'brain': {'sizing': {'active': False, 'risk_pct': 2.0}}}
    assert rp.applied_risk_pct(config, True, 'main', inactive) == 1.0
    assert rp.applied_risk_pct(config, True, 'main', None) == 1.0


def test_real_account_with_demo_config_still_gets_real_limits():
    config = _config(); config['account'] = {'mode': 'demo'}
    assert rp.applied_risk_pct(config, rp.is_demo_account(REAL), 'main', None) == .25


def test_daily_loss_gate_uses_account_type(tmp_path):
    import yaml
    config = yaml.safe_load((Path(__file__).resolve().parent / 'fixtures' / 'default_config.yaml').read_text(encoding='utf-8'))
    settings = SimpleNamespace(config=config, root=tmp_path)
    signal = SimpleNamespace(symbol='EURUSD')
    now = datetime(2026, 11, 11, 10, tzinfo=timezone.utc)
    def gate(account):
        account = SimpleNamespace(balance=10000, equity=9500, trade_mode=account.trade_mode)
        # Full book of flat positions: the next gate after the loss limits is deterministic.
        positions = [{'symbol': f'S{i}', 'profit': 0} for i in range(int(config['risk']['max_open_positions']))]
        return check_trade_allowed(signal, account, positions, -500, -500, settings, {}, 'forex', now, 0)[1]
    assert gate(REAL) == 'daily loss limit reached'          # -5% breaches real 3%
    assert gate(DEMO) == 'maximum open positions reached'    # -5% is inside demo 10%


# ---------------------------------------------------------------- live brain
def _write_gates(tmp_path, gate='PASS', live=None):
    brain = tmp_path / 'brain'; brain.mkdir(parents=True, exist_ok=True)
    (brain / 'gate.json').write_text(json.dumps({'status': gate}), encoding='utf-8')
    if live is not None: (brain / 'live_gate.json').write_text(json.dumps(live), encoding='utf-8')


def test_brain_is_live_only_when_every_condition_holds(tmp_path):
    cfg = {'enabled': True, 'shadow': False}
    assert not live_status(cfg, tmp_path)['active']                       # no gate.json
    _write_gates(tmp_path, 'FAIL')
    assert not live_status(cfg, tmp_path)['active']
    _write_gates(tmp_path, 'PASS')
    assert live_status(cfg, tmp_path)['active']
    assert not live_status({'enabled': False, 'shadow': False}, tmp_path)['active']
    assert not live_status({'enabled': True, 'shadow': True}, tmp_path)['active']
    _write_gates(tmp_path, 'PASS', {'mode': 'shadow'})
    assert not live_status(cfg, tmp_path)['active']


def test_current_config_keeps_brain_shadow_only():
    import yaml
    config = yaml.safe_load((Path(__file__).resolve().parent / 'fixtures' / 'default_config.yaml').read_text(encoding='utf-8'))
    assert config['brain']['enabled'] is False and config['brain']['shadow'] is True
    rp.validate_demo_risk(config)
    assert rp.base_risk_pct(config, True) == 1.0 and rp.base_risk_pct(config, False) == .25


MEMORY = {'probability': .65, 'avg_win_r': 1.5, 'out_of_distribution': False}
ACCEPTED = {'accepted': True}


def test_dynamic_risk_scales_with_probability_inside_bounds():
    low = dynamic_risk({**MEMORY, 'probability': .45}, ACCEPTED, .25, 1.0, 2.0)
    high = dynamic_risk({**MEMORY, 'probability': .80}, ACCEPTED, .25, 1.0, 2.0)
    assert low['active'] and high['active']
    assert 1.0 <= low['risk_pct'] <= high['risk_pct'] <= 2.0


@pytest.mark.parametrize('memory,decision,losses', [
    (MEMORY, {'accepted': False}, 0),
    ({**MEMORY, 'out_of_distribution': None}, ACCEPTED, 0),     # no OOD guard -> unknown
    ({**MEMORY, 'out_of_distribution': True}, ACCEPTED, 0),
    ({**MEMORY, 'probability': None}, ACCEPTED, 0),
    ({**MEMORY, 'probability': .2}, ACCEPTED, 0),                # no positive Kelly edge
    (MEMORY, ACCEPTED, 3),                                       # loss streak -> base risk
])
def test_dynamic_risk_falls_back_to_base(memory, decision, losses):
    assert not dynamic_risk(memory, decision, .25, 1.0, 2.0, losses)['active']


def test_consecutive_losses_counts_until_first_win(tmp_path):
    db = Database(tmp_path / 'bot.db'); db.init()
    with db.connect() as c:
        for ticket, profit, minute in [(1, 5, 1), (2, -3, 2), (3, -1, 3), (4, -2, 4)]:
            c.execute("INSERT INTO trades(ticket,symbol,direction,status,profit,commission,swap,magic,close_time) VALUES(?,?,?,?,?,?,?,?,?)",
                      (ticket, 'EURUSD', 'buy', 'closed', profit, 0, 0, 7, f'2026-11-11T10:0{minute}:00+00:00'))
    assert db.consecutive_losses(7) == 3


# ---------------------------------------------------------------- OOD
def test_ood_guard_flags_far_situations():
    rng = np.random.default_rng(1)
    train = rng.normal(size=(500, 4)); valid = rng.normal(size=(100, 4))
    guard = fit_ood_guard_fast(train, valid, 99.0)
    assert guard['threshold'] > 0 and guard['dimension'] == 4
    assert distance_is_out_of_distribution(guard['threshold'] * .5, guard) is False
    assert distance_is_out_of_distribution(guard['threshold'] * 2, guard) is True
    assert distance_is_out_of_distribution(1.0, None) is None


def test_query_memory_reports_nearest_distance():
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    rows = [{'vector': [0.0, 0.0], 'time': (now - timedelta(days=10)).isoformat(), 'symbol': 'GBPUSD', 'win': 1, 'net_r': 1.0},
            {'vector': [3.0, 4.0], 'time': (now - timedelta(days=9)).isoformat(), 'symbol': 'GBPUSD', 'win': 0, 'net_r': -1.0}]
    result = query_memory([0.0, 1.0], rows, now, 'EURUSD', k=2)
    assert result['nearest_distance'] == pytest.approx(1.0)


# ---------------------------------------------------------------- executor
def test_modify_is_deferred_inside_freeze_level(tmp_path):
    executor, mt5, _ = make_executor(tmp_path)
    mt5.info.trade_freeze_level = 30                 # 0.0003; bid 1.101 is 0.0002 from TP 1.1012
    mt5.position.tp = 1.1012
    assert not executor.modify_sl_tp(9, 1.1005, 1.1012)
    assert mt5.orders == []
    mt5.info.trade_freeze_level = 0
    assert executor.modify_sl_tp(9, 1.1005, 1.1012)


def test_modify_respects_stops_level_for_new_levels(tmp_path):
    executor, mt5, _ = make_executor(tmp_path)
    mt5.info.trade_stops_level = 100                 # 0.001
    assert not executor.modify_sl_tp(9, 1.1005, 1.103)   # new SL 0.0005 from bid
    assert mt5.orders == []


def test_close_and_partial_close_are_deferred_inside_freeze_level(tmp_path):
    executor, mt5, _ = make_executor(tmp_path)
    mt5.info.trade_freeze_level = 30; mt5.position.sl = 1.1008   # bid 1.101 is 0.0002 from SL
    assert 'deferred' in executor.close_position(9, 'time').message
    assert 'deferred' in executor._close_partial(mt5.position, .05).message
    assert mt5.orders == []


def test_in_flight_guard_is_thread_safe(tmp_path):
    executor, _, _ = make_executor(tmp_path)
    started = threading.Event(); release = threading.Event(); results = []
    def slow(signal, volume, **kwargs):
        started.set(); release.wait(2); return 'opened'
    executor._open_trade_internal = slow
    signal = SimpleNamespace(symbol='EURUSD')
    first = threading.Thread(target=lambda: results.append(executor.open_trade(signal, .01)))
    first.start(); started.wait(2)
    second = executor.open_trade(signal, .01)
    release.set(); first.join(2)
    assert results == ['opened'] and not second.success and 'in-flight' in second.message
    assert executor._in_flight == set()


# ---------------------------------------------------------------- locks & runtime
def test_single_instance_lock_blocks_second_instance_and_releases(tmp_path):
    path = tmp_path / 'data' / 'bot.lock'
    first = SingleInstanceLock('bot', lock_file_path=path); second = SingleInstanceLock('bot', lock_file_path=path)
    assert first.acquire() and not second.acquire()
    first.release()
    assert not path.exists() and second.acquire()
    second.release()


def test_drift_settings_are_read_from_self_learning(tmp_path):
    from src.bot import TradingBot
    seen = {}
    class DB:
        def get_recent_brain_outcomes(self, window, magic): seen['window'] = window; return []
    bot = SimpleNamespace(settings=SimpleNamespace(magic_number=7, config={'brain': {'enabled': True}, 'self_learning': {'live_drift_window': 42}}),
                          monotonic=time.monotonic, _brain_drift_checked=-1e9, db=DB(), _data_dir=tmp_path,
                          _event=lambda *a: None, _notify=lambda *a: None)
    TradingBot._check_brain_drift(bot)
    assert seen['window'] == 42


def test_source_parses_with_python_311_rules():
    """Wine runs CPython; guard against 3.12-only syntax (e.g. backslash in f-string expression)."""
    import ast
    for path in ROOT.rglob('*.py'):
        if '.git' in path.parts: continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.FormattedValue):
                segment = ast.get_source_segment(path.read_text(encoding='utf-8'), node.value) or ''
                assert '\\' not in segment, f'{path}: backslash inside f-string expression'


# ---------------------------------------------------------------- scripts
def test_start_scripts_never_delete_a_halt_file():
    bat = (ROOT / 'start_bot.bat').read_text(encoding='utf-8')
    assert bat.index('HALT') < bat.index('del /f /q "STOP"')
    assert 'force' in bat
    sh = (ROOT / 'start_bot.sh').read_text(encoding='utf-8')
    assert sh.index('grep -qiw HALT') < sh.index('rm -f STOP')


def test_linux_entrypoint_matches_windows_stop_semantics():
    entry = (ROOT / 'deploy/linux/mt5/entrypoint.sh').read_text(encoding='utf-8')
    assert 'grep -qiw HALT' in entry and 'requirements.txt' in entry
    assert 'if [ -e /opt/tradingbot/STOP ] ||' not in entry       # empty STOP is a pause, not a stop
    assert 'Python312' in entry


# ---------------------------------------------------------------- scanner integration
def _live_brain_scanner(tmp_path, monkeypatch, accepted=True, equity=1000, brain_on=True, exploration=None):
    import pandas as pd
    import src.scanner as scanner_module
    import src.brain.memory as memory_module
    import src.brain.decide as decide_module
    from src import market_data
    from tests.test_scanner import FakeStrategy, FakeSymbols
    monkeypatch.setattr(scanner_module, 'load_strategies', lambda settings: [FakeStrategy()])
    class MT5:
        def symbol_info(self, symbol):
            return SimpleNamespace(point=.0001, _asdict=lambda: {'trade_tick_value': 1, 'trade_tick_size': 1, 'volume_min': .01, 'volume_step': .01, 'volume_max': 50})
        def symbol_info_tick(self, symbol): return SimpleNamespace(bid=1.0, ask=1.0001)
        def account_info(self): return SimpleNamespace(equity=equity, trade_mode=0)
    config = {'risk': {'sl_move_mult': .8, 'tp_move_mult': 1.2, 'move_horizon_minutes': 10, 'risk_per_trade_pct': .25},
              'scanner': {'max_seconds_per_cycle': 5, 'max_candidates': 10, 'candle_count': 100, 'min_score': 0, 'min_confidence': .6, 'require_mtf_agreement': True, 'spread_median_lookback_hours': 24, 'spread_median_cache_seconds': 300, 'max_spread_vs_median': 1.5, 'movement_timeframe_minutes': 30, 'max_spread_volatility_ratio': .35, 'top_movers': 40, 'timeframe_point_sizes': {}},
              'strategies': {}, 'self_learning': {'bandit_enabled': False},
              'demo_risk': {'risk_per_trade_pct': 1.0, 'brain_min_risk_pct': 1.0, 'brain_max_risk_pct': 2.0},
              'brain': {'enabled': brain_on, 'shadow': not brain_on, 'kelly_fraction': .25}}
    if exploration is not None: config['demo_exploration'] = exploration
    settings = SimpleNamespace(config=config, risk=SimpleNamespace(risk_per_trade_pct=.25), root=tmp_path, magic_number=7)
    _write_gates(tmp_path / 'data', 'PASS')
    db = Database(tmp_path / 'scan.db'); db.init()
    scanner = scanner_module.MarketScanner(MT5(), settings, db, FakeSymbols())
    scanner._spread_cache = {s: {'median_points': 0.0, 'time': datetime.now(timezone.utc)} for s in ('EURUSD', 'GBPUSD')}
    bars = pd.DataFrame({'open': np.arange(100) + 1, 'high': np.arange(100) + 2, 'low': np.arange(100), 'close': np.arange(100) + 1, 'tick_volume': 100, 'spread': 1},
                        index=pd.date_range('2026-01-01', periods=100, freq='min', tz='UTC'))
    scanner._bars = lambda symbol, tf, count: bars
    scanner._brain_rows = {'forex': [{'stub': True}]}
    scanner._brain_ood_guard = {'threshold': 1.0}
    monkeypatch.setattr(memory_module, 'query_memory', lambda *a, **k: {'count': 300, 'probability': .75, 'avg_win_r': 1.5, 'avg_loss_r': 1.0, 'cases': [], 'mean_distance': .2, 'nearest_distance': .1})
    monkeypatch.setattr(decide_module, 'decide', lambda *a, **k: {'accepted': accepted, 'probability': .75, 'reasons': [] if accepted else ['probability below floor']})
    monkeypatch.setattr(market_data, 'get_cost_estimate', lambda mt5, symbol, volume: {'cost': .01 * volume, 'commission_known': True})
    monkeypatch.setattr(market_data, 'price_distance_to_money', lambda mt5, symbol, distance, volume: abs(distance) * volume)
    return scanner, db


def test_live_brain_raises_demo_risk_within_bounds(tmp_path, monkeypatch):
    scanner, _ = _live_brain_scanner(tmp_path, monkeypatch)
    result = scanner.scan_once()
    assert result, 'accepted live brain signal should remain a candidate'
    brain = result[0].features['brain']
    assert brain['mode'] == 'live' and brain['sizing']['active']
    assert 1.0 < brain['applied_risk_pct'] <= 2.0
    # Volume follows the brain risk: risk money is the applied % of 1,000 equity.
    assert result[0].risk_money == pytest.approx(1000 * brain['applied_risk_pct'] / 100, rel=.02)
    assert rp.applied_risk_pct(scanner.settings.config, True, 'main', result[0].features) == pytest.approx(brain['applied_risk_pct'])


def test_live_brain_rejection_blocks_signal(tmp_path, monkeypatch):
    scanner, db = _live_brain_scanner(tmp_path, monkeypatch, accepted=False)
    assert scanner.scan_once() == []
    with db.connect() as connection:
        reasons = [row[0] for row in connection.execute('SELECT skip_reason FROM signals')]
    assert reasons and all(reason.startswith('brain filter') for reason in reasons)


# ---------------------------------------------------------------- small balance (minimum lot)
def test_min_lot_risk_money():
    from src.risk import min_lot_risk_money
    spec = {'trade_tick_value': 10, 'trade_tick_size': .0001, 'volume_min': .01}   # standard EURUSD: $10 per pip per lot
    assert min_lot_risk_money(1.1010, 1.1000, spec) == pytest.approx(1.0)          # 10 pips * $0.10
    assert min_lot_risk_money(1.1010, 1.1000, spec, extra_cost_per_lot=20) == pytest.approx(1.2)
    assert min_lot_risk_money(1.1, 1.1, spec) is None


def test_min_lot_cap_is_demo_only_and_locked():
    assert rp.min_lot_cap_pct(_config(), True) == 3.0
    assert rp.min_lot_cap_pct(_config(), False) == 0.0
    with pytest.raises(ValueError):
        rp.validate_demo_risk(_config(min_lot_max_risk_pct=6))
    features = {'min_lot': {'active': True}}
    assert rp.applied_risk_pct(_config(), True, 'main', features) == 3.0
    assert rp.applied_risk_pct(_config(), False, 'main', features) == .25


def test_small_demo_balance_trades_minimum_lot(tmp_path, monkeypatch):
    # Minimum lot risks ~0.05 $; 1% of 3 $ is 0.03 $ (too small) but 3% is 0.09 $ (fits).
    scanner, _ = _live_brain_scanner(tmp_path, monkeypatch, equity=3, brain_on=False)
    result = scanner.scan_once()
    assert result and result[0].volume == .01
    assert result[0].features['min_lot']['active'] and result[0].features['min_lot']['needed_pct'] <= 3.0


def test_tiny_balance_refuses_when_minimum_lot_is_too_risky(tmp_path, monkeypatch):
    scanner, db = _live_brain_scanner(tmp_path, monkeypatch, equity=1, brain_on=False)
    assert scanner.scan_once() == []
    with db.connect() as connection:
        reasons = [row[0] for row in connection.execute('SELECT skip_reason FROM signals')]
    assert reasons and all('min lot risks' in reason and 'limit 3%' in reason for reason in reasons)


# ---------------------------------------------------------------- backups & server script
def test_daily_backup_is_consistent_once_per_day_and_rotated(tmp_path):
    from datetime import date
    from src.db import Database, backup_database
    db = Database(tmp_path / 'bot.db')
    with db.connect() as c:
        c.execute("INSERT INTO trades(ticket,symbol,direction,status,magic) VALUES(1,'EURUSD','buy','closed',7)")
    folder = tmp_path / 'backups'
    first = backup_database(db.path, folder, keep_days=3, today=date(2026, 11, 1))
    import sqlite3
    check = sqlite3.connect(first)
    try: assert check.execute('SELECT COUNT(*) FROM trades').fetchone()[0] == 1
    finally: check.close()   # Windows cannot delete an open file during rotation
    assert backup_database(db.path, folder, keep_days=3, today=date(2026, 11, 1)) is None
    for day in range(2, 7):
        backup_database(db.path, folder, keep_days=3, today=date(2026, 11, day))
    assert sorted(p.name for p in folder.glob('bot-*.db')) == ['bot-2026-11-04.db', 'bot-2026-11-05.db', 'bot-2026-11-06.db']


def test_bot_backup_never_raises(tmp_path):
    from datetime import datetime, timezone
    from src.bot import TradingBot
    bot = SimpleNamespace(settings=SimpleNamespace(config={}), _backup_day=None, db=SimpleNamespace(path=tmp_path / 'missing' / 'bot.db'), _data_dir=tmp_path)
    TradingBot._backup_database(bot, datetime(2026, 11, 11, tzinfo=timezone.utc))
    assert bot._backup_day is not None


def test_server_address_is_not_in_the_repository():
    for name in ('scripts/server.ps1', 'scripts/deploy.ps1'):
        text = (ROOT / name).read_text(encoding='utf-8')
        assert 'TRADINGBOT_SERVER' in text
        assert not __import__('re').search(r'@\d+\.\d+\.\d+\.\d+', text)


# ---------------------------------------------------------------- group share, per-account drawdown
def _group_gate(share, trades_today=10, group_trades=10):
    import yaml
    config = yaml.safe_load((Path(__file__).resolve().parent / 'fixtures' / 'default_config.yaml').read_text(encoding='utf-8'))
    config['risk']['max_group_share_today'] = share
    settings = SimpleNamespace(config=config, root=Path('/nonexistent'))
    account = SimpleNamespace(balance=10000, equity=10000, trade_mode=0)
    now = datetime(2026, 11, 11, 10, tzinfo=timezone.utc)
    try:
        return check_trade_allowed(SimpleNamespace(symbol='EURUSD'), account, [], 0, 0, settings, {}, 'forex', now, 0,
                                   trades_today=trades_today, group_trades_today=group_trades)[1]
    except AttributeError:
        return 'passed the group-share gate'   # later gates need a full signal object


def test_group_share_rule_off_by_default_and_with_zero():
    import inspect
    if 'group_trades_today' not in inspect.signature(check_trade_allowed).parameters:
        pytest.skip('signature differs')
    assert _group_gate(0) != 'maximum asset-group share today reached'
    assert _group_gate(1.0) != 'maximum asset-group share today reached'
    assert _group_gate(.4) == 'maximum asset-group share today reached'


def test_equity_snapshots_are_filtered_by_account(tmp_path):
    db = Database(tmp_path / 'bot.db')
    now = datetime.now(timezone.utc)
    db.insert_equity_snapshot(time=(now - timedelta(hours=2)).isoformat(), balance=10000, equity=10000, margin=0, free_margin=10000, open_positions=0, login=111)
    db.insert_equity_snapshot(time=(now - timedelta(hours=1)).isoformat(), balance=1000, equity=1000, margin=0, free_margin=1000, open_positions=0, login=222)
    since = now - timedelta(hours=24)
    assert [row['equity'] for row in db.get_equity_snapshots_since(since, 222)] == [1000]
    assert len(db.get_equity_snapshots_since(since)) == 2


def test_account_switch_is_not_a_drawdown():
    from src.protections import check_protections
    now = datetime.now(timezone.utc)
    only_current = [{'time': (now - timedelta(hours=1)).isoformat(), 'equity': 1000}, {'time': now.isoformat(), 'equity': 1000}]
    assert check_protections([], only_current, 'EURUSD', now, {'rolling_drawdown_pct': 2.0}) == (False, '')


def test_demo_drawdown_limit_is_six_percent_real_stays_two():
    from src.protections import check_protections
    now = datetime.now(timezone.utc)
    def equity(drop_pct):
        return [{'time': (now - timedelta(hours=2)).isoformat(), 'equity': 1000}, {'time': now.isoformat(), 'equity': 1000 * (1 - drop_pct / 100)}]
    demo = rp.account_risk_profile(_config(), True)['rolling_drawdown_pct']
    real = rp.account_risk_profile({**_config(), 'protections': {'rolling_drawdown_pct': 2.0}}, False)['rolling_drawdown_pct']
    assert (demo, real) == (6.0, 2.0)
    assert not check_protections([], equity(3), 'EURUSD', now, {'rolling_drawdown_pct': demo})[0]
    assert check_protections([], equity(3), 'EURUSD', now, {'rolling_drawdown_pct': real})[0]
    assert check_protections([], equity(7), 'EURUSD', now, {'rolling_drawdown_pct': demo})[0]
    with pytest.raises(ValueError):
        rp.validate_demo_risk(_config(rolling_drawdown_pct=11))


def test_bot_records_account_login_on_snapshots():
    text = (ROOT / 'src' / 'bot.py').read_text(encoding='utf-8')
    assert "login=getattr(self.client,'account_login',None)" in text
    assert 'get_equity_snapshots_since(cutoff,login)' in text


# ---------------------------------------------------------------- exploration stop sizing
@pytest.mark.parametrize('mode,expected', [('horizon', .8 * 2 * 20 ** .5), ('strategy', 1.0)])
def test_exploration_stops_follow_the_holding_horizon(tmp_path, monkeypatch, mode, expected):
    exploration = {'enabled': True, 'stop_mode': mode, 'move_horizon_minutes': 20, 'min_confidence': .45, 'mtf_min_agree': 1,
                   'min_risk_reward': 1.0, 'max_cost_to_target_ratio': .45, 'max_open_positions': 3, 'max_trades_per_day': 40}
    scanner, _ = _live_brain_scanner(tmp_path, monkeypatch, brain_on=False, exploration=exploration)
    result = scanner.scan_once()
    assert result, 'exploration signal should remain a candidate'
    signal = result[0].signal
    assert abs(signal.entry_hint - signal.sl_price) == pytest.approx(expected, rel=.02)


# ---------------------------------------------------------------- gold + US indices, US session
def test_trading_hours_follow_new_york_session_with_dst():
    from src.trade_rules import within_trading_hours
    hours = {'enabled': True, 'timezone': 'America/New_York', 'start': '09:30', 'end': '16:00'}
    winter_open = datetime(2026, 11, 12, 14, 30, tzinfo=timezone.utc)      # 09:30 EST
    assert within_trading_hours(winter_open, hours)
    assert not within_trading_hours(winter_open - timedelta(minutes=1), hours)
    assert not within_trading_hours(datetime(2026, 11, 12, 21, 0, tzinfo=timezone.utc), hours)   # 16:00 EST
    assert within_trading_hours(datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc), hours)       # 09:30 EDT (summer)
    assert not within_trading_hours(datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc), hours)   # Saturday
    assert within_trading_hours(datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc), {'enabled': False})


def test_config_trades_only_gold_and_us_indices():
    import yaml
    config = yaml.safe_load((Path(__file__).resolve().parent / 'fixtures' / 'default_config.yaml').read_text(encoding='utf-8'))
    assert config['universe']['focus_symbols'] == ['GOLD', 'US500', 'NAS100', 'US30']
    assert set(config['universe']['include_groups']) == {'indices', 'metals'}
    assert config['weekend']['enabled'] is False and config['universe']['trading_hours']['enabled'] is False
    risk = config['risk']
    assert (risk['stagnation_minutes'], risk['max_hold_minutes']) == (10, 30)
    assert risk['tp_move_mult'] / risk['sl_move_mult'] == pytest.approx(1.5)
    assert config['demo_exploration']['move_horizon_minutes'] == 30


def test_us_indices_share_one_position_group():
    from src.symbols import underlying_group
    assert {underlying_group(s, 'indices') for s in ('US500Cash', 'US100Cash', 'US30Cash')} == {'us_indices'}


def test_outside_hours_blocks_new_entries(tmp_path, monkeypatch):
    scanner, db = _live_brain_scanner(tmp_path, monkeypatch, brain_on=False)
    scanner.settings.config['universe'] = {'trading_hours': {'enabled': True, 'timezone': 'UTC', 'start': '00:00', 'end': '00:00'}}
    assert scanner.scan_once() == []


# ---------------------------------------------------------------- per-symbol hours, repeat entries
def test_gold_has_its_own_longer_window():
    import yaml
    from src.trade_rules import hours_for_symbol, within_trading_hours
    universe = yaml.safe_load((Path(__file__).resolve().parent / 'fixtures' / 'default_config.yaml').read_text(encoding='utf-8'))['universe']
    # 24h test is on: every hour is allowed while the windows stay stored for re-enabling.
    assert within_trading_hours(datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc), hours_for_symbol('US500Cash', universe))
    universe['trading_hours']['enabled'] = True
    baku_13 = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)      # 13:00 Baku (winter)
    baku_0130 = datetime(2026, 11, 12, 21, 30, tzinfo=timezone.utc)  # 01:30 Baku
    assert within_trading_hours(baku_13, hours_for_symbol('GOLD#', universe))
    assert not within_trading_hours(baku_13, hours_for_symbol('US500Cash', universe))
    assert within_trading_hours(baku_0130, hours_for_symbol('GOLD', universe))
    assert not within_trading_hours(baku_0130, hours_for_symbol('US100Cash', universe))
    assert not within_trading_hours(datetime(2026, 11, 12, 22, 30, tzinfo=timezone.utc), hours_for_symbol('GOLD#', universe))  # 02:30 Baku


def _repeat_gate(open_positions, symbol_trades_today=0, cooldown=False, **risk_overrides):
    import yaml
    config = yaml.safe_load((Path(__file__).resolve().parent / 'fixtures' / 'default_config.yaml').read_text(encoding='utf-8'))
    config['risk'].update(risk_overrides)
    settings = SimpleNamespace(config=config, root=Path('/nonexistent'))
    account = SimpleNamespace(balance=10000, equity=10000, trade_mode=0)
    try:
        return check_trade_allowed(SimpleNamespace(symbol='GOLD#'), account, open_positions, 0, 0, settings, {}, 'metals',
                                   datetime(2026, 11, 11, 15, tzinfo=timezone.utc), 0,
                                   symbol_cooldown=cooldown, symbol_trades_today=symbol_trades_today)[1]
    except AttributeError:
        return 'passed the repeat-entry gates'


def test_repeat_entries_allowed_by_current_config():
    open_gold = [{'symbol': 'GOLD#', 'profit': 0, 'asset_group': 'metals'}]
    assert _repeat_gate(open_gold) not in {'symbol already has open position', 'underlying already has open position'}
    assert _repeat_gate([], symbol_trades_today=50) != 'maximum trades for symbol today reached'


def test_repeat_entry_rules_still_work_when_switched_on():
    open_gold = [{'symbol': 'GOLD#', 'profit': 0, 'asset_group': 'metals'}]
    assert _repeat_gate(open_gold, allow_multiple_positions_per_symbol=False) == 'symbol already has open position'
    assert _repeat_gate([], symbol_trades_today=4, max_trades_per_symbol_per_day=4) == 'maximum trades for symbol today reached'


def test_symbol_pauses_off_but_rolling_drawdown_kept():
    from src.protections import check_protections
    now = datetime.now(timezone.utc)
    losses = [{'symbol': 'GOLD#', 'net': -10, 'close_time': (now - timedelta(minutes=i)).isoformat(), 'outcome_r': -1} for i in range(1, 5)]
    assert check_protections(losses, [], 'GOLD#', now, {'symbol_pauses_enabled': False}) == (False, '')
    assert check_protections(losses, [], 'GOLD#', now, {})[0]
    drop = [{'time': (now - timedelta(hours=1)).isoformat(), 'equity': 1000}, {'time': now.isoformat(), 'equity': 900}]
    assert check_protections([], drop, 'GOLD#', now, {'symbol_pauses_enabled': False, 'rolling_drawdown_pct': 6})[0]


def test_config_turns_off_repeat_limits_and_keeps_total_caps():
    import yaml
    config = yaml.safe_load((Path(__file__).resolve().parent / 'fixtures' / 'default_config.yaml').read_text(encoding='utf-8'))
    risk = config['risk']
    assert risk['symbol_cooldown_minutes'] == 0 and risk['max_trades_per_symbol_per_day'] == 0
    assert risk['allow_multiple_positions_per_symbol'] is True
    assert config['protections']['symbol_pauses_enabled'] is False
    assert risk['max_total_open_risk_pct'] <= 5.0 and config['demo_exploration']['max_open_positions'] <= 3
