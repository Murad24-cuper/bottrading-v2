from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import yaml
from pathlib import Path

from src.exit_review import replay, review_exits
from src.position_watch import analyze

ROOT = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((Path(__file__).resolve().parent / 'fixtures' / 'default_config.yaml').read_text(encoding='utf-8'))


def frame(closes, start=None, volume=100):
    start = start or datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc)
    index = pd.date_range(start, periods=len(closes), freq='1min', tz='UTC')
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({'open': closes + .05, 'high': closes + .1, 'low': closes - .1, 'close': closes,
                         'tick_volume': volume, 'spread': 1}, index=index)


def run(closes, current, age=5, row=None):
    candles = {'M1': frame(closes), 'M5': frame(closes[-80:]), 'M15': frame(closes[-80:])}
    return analyze('buy', 100.0, current, row or {'initial_risk': 1.0}, CONFIG['risk'], CONFIG['scanner'], candles,
                   age, 30, 10, .2)


def test_falling_market_in_loss_band_is_flagged_for_early_exit():
    closes = list(np.linspace(101, 100, 66)) + [99.9, 99.8, 99.7, 99.4]
    result = run(closes, 99.6)
    assert result['danger']['score'] >= result['danger']['threshold']
    assert result['danger']['in_band'] and result['tone'] == 'bad' and 'Erkən' in result['plan']
    assert result['current_r'] == pytest.approx(-.4)


def test_rising_market_near_time_limit_is_an_extension_candidate():
    closes = list(np.linspace(99, 101, 70)) + list(np.linspace(101, 103, 10))
    result = run(closes, 103.0, age=27, row={'initial_risk': 1.0, 'peak_favorable_r': 3})
    assert result['danger']['score'] == 0 and result['continuation']['mtf']
    assert result['tone'] == 'good'


def test_missing_data_degrades_gracefully():
    result = analyze('sell', 100.0, None, {}, CONFIG['risk'], CONFIG['scanner'], {}, 1, 30, 10, .2)
    assert result['current_r'] is None and result['danger']['score'] == 0 and result['plan']


def test_replay_counts_stop_first_when_one_bar_hits_both():
    bars = pd.DataFrame({'open': [100], 'high': [102.5], 'low': [98.5], 'close': [100]})
    assert replay('buy', 100, 1, 102, bars) == ('sl', -1.0)
    bars = pd.DataFrame({'open': [100, 101], 'high': [100.5, 102.2], 'low': [99.5, 100.8], 'close': [100.2, 102]})
    assert replay('buy', 100, 1, 102, bars) == ('tp', 2.0)
    assert replay('sell', 100, 1, 98, bars)[0] == 'sl'


class ReviewDB:
    def __init__(self, rows): self.rows = rows; self.saved = {}
    def get_exits_to_review(self, reasons, before, after, limit): return [r for r in self.rows if r['close_reason'] in reasons]
    def set_exit_review(self, ticket, outcome, review_r=None, delta=None): self.saved[ticket] = (outcome, review_r, delta)


def test_review_exits_marks_a_good_early_exit_as_money_saved(monkeypatch):
    closed = datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc)
    row = {'ticket': 5, 'symbol': 'GOLD', 'direction': 'buy', 'open_price': 100.0, 'close_price': 99.6, 'initial_risk': 1.0,
           'tp': 101.5, 'volume': 1.0, 'close_time': closed.isoformat(), 'close_reason': 'early_exit'}
    db = ReviewDB([row])
    after = frame(list(np.linspace(99.6, 98.0, 40)), start=closed - timedelta(minutes=2))
    import src.market_data as md
    monkeypatch.setattr(md, 'get_candles', lambda *a, **k: after)
    mt5 = SimpleNamespace(symbol_info=lambda s: SimpleNamespace(trade_tick_value=1.0, trade_tick_size=.01))
    assert review_exits(db, mt5, now=closed + timedelta(minutes=40)) == 1
    outcome, review_r, delta = db.saved[5]
    assert outcome == 'sl' and review_r == -1.0 and delta == pytest.approx(-60.0)


def test_exit_review_summary_counts_help_and_harm():
    from src.dashboard.history import exit_review_summary
    rows = [{'close_reason': 'early_exit', 'review_outcome': 'sl', 'review_delta': -60},
            {'close_reason': 'early_exit', 'review_outcome': 'tp', 'review_delta': 40},
            {'close_reason': 'time', 'review_outcome': 'open', 'review_delta': None}]
    groups = {g['reason']: g for g in exit_review_summary(rows)}
    assert groups['early_exit'] == {'reason': 'early_exit', 'trades': 2, 'helped': 1, 'hurt': 1, 'saved': 20.0, 'would_sl': 1, 'would_tp': 1}
    assert groups['time']['trades'] == 1
