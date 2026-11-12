from pathlib import Path

import yaml

from src.dashboard import ui
from src.dashboard.overview import brain_overview, risk_overview, runtime_label, stop_state

ROOT = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((Path(__file__).resolve().parent / 'fixtures' / 'default_config.yaml').read_text(encoding='utf-8'))


def test_demo_risk_overview_matches_bot_profile():
    view = risk_overview(CONFIG, 'DEMO', 10000, -300, -300, -200)
    assert view['risk_pct'] == 1.0 and view['risk_money'] == 100
    assert view['daily']['limit_money'] == 1000 and view['daily']['loss_money'] == 500
    assert view['daily']['used_share'] == .5 and view['daily']['left_money'] == 500
    assert view['weekly']['limit_money'] == 2500


def test_real_and_unknown_accounts_use_real_limits():
    real = risk_overview(CONFIG, 'REAL', 1000, 0, 0, 0)
    assert real['risk_pct'] == .25 and real['daily']['limit_pct'] == 3.0
    unknown = risk_overview(CONFIG, 'UNKNOWN', 1000, 0, 0, 0)
    assert not unknown['known'] and unknown['risk_pct'] == .25


def test_profit_never_counts_as_used_limit():
    assert risk_overview(CONFIG, 'DEMO', 10000, 500, 500, 0)['daily']['used_share'] == 0


def test_runtime_label_warns_when_config_points_elsewhere():
    assert runtime_label({'runtime': {'location': 'local'}}, {})['state'] == 'good'
    server = runtime_label({'runtime': {'location': 'local'}}, {'BOT_LOCATION': 'server'})
    assert server['place'] == 'Linux server' and server['state'] == 'warn'


def test_brain_overview_explains_shadow_mode(tmp_path):
    view = brain_overview(CONFIG, tmp_path)
    assert view['state'] == 'neutral' and any('brain.enabled' in reason for reason in view['reasons'])


def test_stop_state(tmp_path):
    assert stop_state(tmp_path) == 'none'
    (tmp_path / 'STOP').write_text('PAUSE\n', encoding='utf-8')
    assert stop_state(tmp_path) == 'pause'
    (tmp_path / 'STOP').write_text('HALT\n', encoding='utf-8')
    assert stop_state(tmp_path) == 'halt'


def test_ui_escapes_text_and_clamps_bars():
    assert '<script>' not in ui.kpis([('<script>', '1', 'good', '')])
    assert 'width:100.0%' in ui.limit_bar('x', 5, 'y')
    assert 'width:0.0%' in ui.limit_bar('x', -1, 'y')


def test_dashboard_renders_in_phone_view():
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(str(ROOT / 'src' / 'dashboard' / 'app.py'))
    app.query_params['view'] = 'phone'
    app.run(timeout=30)
    assert not app.exception
    assert app.toggle(key='phone_view').value is True


# ---------------------------------------------------------------- position charts
class _ChartMT5:
    POSITION_TYPE_BUY = 0; TIMEFRAME_M1 = 1; TIMEFRAME_M5 = 5
    def __init__(self, now):
        import numpy as np
        self.now = now
        self.dtype = [('time', 'i8'), ('open', 'f8'), ('high', 'f8'), ('low', 'f8'), ('close', 'f8'), ('tick_volume', 'i8'), ('spread', 'i8'), ('real_volume', 'i8')]
        self.np = np
    def symbol_select(self, symbol, enable): return True
    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        import math
        step = 60 * timeframe; end = int(self.now.timestamp())
        rows = []
        for i in range(count):
            mid = 1.10 + .002 * math.sin(i / 9)
            rows.append((end - (count - i) * step, mid, mid + .0004, mid - .0004, mid + (.0002 if i % 3 else -.0002), 10, 1, 0))
        return self.np.array(rows, dtype=self.dtype)
    def symbol_info_tick(self, symbol):
        from types import SimpleNamespace
        return SimpleNamespace(bid=1.1012, ask=1.1013, time=int(self.now.timestamp()))
    def last_error(self): return (0, 'ok')


def _chart_positions(now):
    from types import SimpleNamespace
    return [SimpleNamespace(ticket=11, symbol='EURUSD#', type=0, volume=.5, time=int(now.timestamp()) - 1800, price_open=1.1, sl=1.098, tp=1.104, profit=60.0),
            SimpleNamespace(ticket=12, symbol='GOLD#', type=1, volume=.1, time=int(now.timestamp()) - 600, price_open=1.103, sl=1.105, tp=1.099, profit=-20.0)]


def test_bot_writes_chart_snapshots_and_removes_closed(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from src import position_charts, time_utils
    monkeypatch.setattr(time_utils, 'server_utc_offset', lambda *a, **k: timedelta(0))
    import src.market_data as market_data
    monkeypatch.setattr(market_data, 'server_utc_offset', lambda *a, **k: timedelta(0))
    now = datetime.now(timezone.utc); mt5 = _ChartMT5(now)
    (tmp_path / 'charts').mkdir(); (tmp_path / 'charts' / '999.json').write_text('{}', encoding='utf-8')
    assert position_charts.write_position_charts(mt5, _chart_positions(now), tmp_path) == 2
    payload = position_charts.read_position_chart(tmp_path, 11)
    assert payload['direction'] == 'buy' and payload['sl'] == 1.098 and payload['current'] == 1.1012
    assert len(payload['bars']['M1']) == 120 and len(payload['bars']['M5']) == 120
    assert not (tmp_path / 'charts' / '999.json').exists()
    assert position_charts.read_position_chart(tmp_path, 12)['current'] == 1.1013


def test_position_chart_builds_and_r_multiple():
    from datetime import datetime, timedelta, timezone
    from src.dashboard.charts import level_rows, position_chart, r_multiple
    now = datetime.now(timezone.utc)
    bars = [{'t': (now - timedelta(minutes=60 - i)).isoformat(), 'o': 1.1, 'h': 1.101, 'l': 1.099, 'c': 1.1005} for i in range(60)]
    payload = {'direction': 'buy', 'open_price': 1.1, 'sl': 1.098, 'tp': 1.104, 'current': 1.101, 'open_time': (now - timedelta(minutes=30)).isoformat(), 'bars': {'M1': bars}}
    assert position_chart(payload, 'M1') is not None
    assert position_chart(payload, 'M5') is None
    assert [row['name'] for row in level_rows(payload)] == ['Giriş', 'SL', 'TP', 'Cari']
    assert abs(r_multiple(payload) - .5) < 1e-9
    assert abs(r_multiple({**payload, 'direction': 'sell', 'sl': 1.102}) + .5) < 1e-9


def test_bot_chart_writer_never_raises(tmp_path):
    from types import SimpleNamespace
    from src.bot import TradingBot
    class Broken:
        def positions_get(self): raise RuntimeError('MT5 gone')
    bot = SimpleNamespace(settings=SimpleNamespace(config={'dashboard': {'chart_refresh_seconds': 15}}, magic_number=7),
                          monotonic=lambda: 1e6, _charts_written=-1e9, client=SimpleNamespace(mt5=Broken()), db=SimpleNamespace(get_open_trades=lambda: []),
                          _data_dir=tmp_path)
    bot._owned_positions = lambda: TradingBot._owned_positions(bot)
    TradingBot._write_position_charts(bot)      # must swallow the error
    assert bot._charts_written == 1e6
