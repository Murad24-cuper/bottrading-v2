from zoneinfo import ZoneInfo

from src.dashboard import history


def rows():
    return [
        {'symbol': 'GOLD', 'net': 15.0, 'est_cost': 1.0, 'close_reason': 'tp', 'open_time': '2026-11-10T14:00:00+00:00', 'close_time': '2026-11-10T14:20:00+00:00'},
        {'symbol': 'GOLD', 'net': -10.0, 'est_cost': 1.0, 'close_reason': 'sl', 'open_time': '2026-11-10T15:00:00+00:00', 'close_time': '2026-11-10T15:10:00+00:00'},
        {'symbol': 'US500', 'net': -4.0, 'est_cost': .5, 'close_reason': 'stagnation', 'open_time': '2026-11-11T19:00:00+00:00', 'close_time': '2026-11-11T19:10:00+00:00'},
    ]


def test_summary_metrics():
    s = history.summary(rows())
    assert s['trades'] == 3 and s['wins'] == 1 and s['total'] == 1.0
    assert s['profit_factor'] == 15 / 14 and s['best'] == 15 and s['worst'] == -10
    assert round(s['average_minutes'], 1) == 13.3 and s['costs'] == 2.5


def test_summary_of_nothing_is_zero():
    s = history.summary([])
    assert s['trades'] == 0 and s['total'] == 0 and s['win_rate'] == 0


def test_group_by_and_daily_totals():
    groups = {g['name']: g for g in history.group_by(rows(), 'symbol')}
    assert groups['GOLD']['trades'] == 2 and groups['GOLD']['total'] == 5.0
    days = history.daily_totals(rows(), ZoneInfo('Asia/Baku'))
    assert [d['pnl'] for d in days] == [5.0, -4.0]


def test_insights_are_text():
    assert all(isinstance(line, str) and line for line in history.insights(rows()))


def test_by_hour_uses_local_open_hour():
    result = history.by_hour(rows(), ZoneInfo('Asia/Baku'))
    assert [(h['hour'], h['trades'], h['total']) for h in result] == [(18, 1, 15.0), (19, 1, -10.0), (23, 1, -4.0)]
