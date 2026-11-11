from datetime import datetime, timedelta, timezone

from src.news import (CalendarEvent, entry_block_reason, event_matches_symbol,
                      parse_calendar, minutes_to_next_high_impact, parse_ff_json,
                      parse_ff_xml, load_calendar)


def test_high_impact_event_blocks_mapped_instrument_only_inside_window():
    now=datetime(2026,11,9,13,45,tzinfo=timezone.utc)
    event=CalendarEvent(now+timedelta(minutes=10),'USD','HIGH','Inflation')
    assert 'Inflation' in entry_block_reason('EURUSD',now,[event],now)
    assert entry_block_reason('GER40Cash',now,[event],now)==''


def test_stale_calendar_uses_conservative_currency_time_window():
    now=datetime(2026,11,9,13,0,tzinfo=timezone.utc)
    assert entry_block_reason('US30Cash',now,[],now-timedelta(hours=25))
    assert entry_block_reason('BTCUSD',now,[],now-timedelta(hours=25))==''


def test_calendar_parser_normalizes_utc_and_skips_invalid_rows(tmp_path):
    path=tmp_path/'calendar.csv'
    path.write_text('time_utc,currency,importance,name,actual,forecast,previous\n2026-11-09T13:00:00+00:00,USD,HIGH,CPI,1,2,3\nbad,USD,HIGH,broken,,,\n',encoding='utf-8')
    events,updated=parse_calendar(path)
    assert len(events)==1 and events[0].time.tzinfo==timezone.utc
    assert event_matches_symbol(events[0],'US500Cash')

def test_minutes_until_next_high_impact_uses_mapped_currency():
    now=datetime(2026,11,9,13,0,tzinfo=timezone.utc)
    events=[CalendarEvent(now+timedelta(minutes=23),'USD','HIGH','CPI'),CalendarEvent(now+timedelta(minutes=2),'GBP','LOW','Survey')]
    assert minutes_to_next_high_impact('US500Cash',now,events)==23
    assert minutes_to_next_high_impact('GER40Cash',now,events) is None


def test_forexfactory_json_offsets_are_converted_to_utc():
    events=parse_ff_json('[{"title":"CPI","country":"USD","date":"2026-11-09T08:30:00-05:00","impact":"High"}]')
    assert len(events)==1
    assert events[0].time==datetime(2026,11,9,13,30,tzinfo=timezone.utc)
    assert events[0].importance=='HIGH'


def test_forexfactory_xml_wall_time_converts_from_eastern_to_utc():
    xml='<weeklyevents><event><title>Payrolls</title><country>USD</country><date>11-09-2026</date><time>8:30am</time><impact>High</impact></event></weeklyevents>'
    events=parse_ff_xml(xml)
    assert len(events)==1 and events[0].time==datetime(2026,11,9,13,30,tzinfo=timezone.utc)


def test_calendar_prefers_fresh_csv_and_uses_download_cache_when_csv_stale(tmp_path):
    now=datetime(2026,11,9,15,tzinfo=timezone.utc)
    csv_path=tmp_path/'calendar.csv'
    csv_path.write_text('time_utc,currency,importance,name\n2026-11-09T16:00:00+00:00,USD,HIGH,CSV\n',encoding='utf-8')
    import os
    os.utime(csv_path,(now.timestamp(),now.timestamp()))
    cache=tmp_path/'ff.json'
    feed=b'[{"title":"Feed","country":"USD","date":"2026-11-09T12:30:00-05:00","impact":"High"}]'
    events,updated,source,age=load_calendar(csv_path,cache,now,fetch=lambda:feed)
    assert source=='MT5 CSV' and events[0].name=='CSV' and age==0
    os.utime(csv_path,((now-timedelta(days=2)).timestamp(),)*2)
    events,updated,source,age=load_calendar(csv_path,cache,now,fetch=lambda:feed)
    assert source=='ForexFactory cache' and events[0].name=='Feed' and age==0


def test_calendar_keeps_recent_cache_if_download_fails(tmp_path):
    now=datetime(2026,11,9,15,tzinfo=timezone.utc); cache=tmp_path/'ff.json'
    cache.write_text('[{"title":"Cached","country":"USD","date":"2026-11-09T12:30:00-05:00","impact":"High"}]',encoding='utf-8')
    import os
    os.utime(cache,((now-timedelta(hours=7)).timestamp(),)*2)
    events,updated,source,age=load_calendar(None,cache,now,fetch=lambda: (_ for _ in ()).throw(OSError('offline')))
    assert source=='ForexFactory cache' and events[0].name=='Cached' and age==7


def test_calendar_uses_blanket_fallback_only_when_both_sources_fail(tmp_path):
    now=datetime(2026,11,9,13,tzinfo=timezone.utc)
    events,updated,source,age=load_calendar(None,tmp_path/'missing.json',now,fetch=lambda: (_ for _ in ()).throw(OSError('offline')))
    assert events==[] and updated is None and source=='fallback window'
    assert entry_block_reason('US30Cash',now,events,updated,24)


def test_cross_pairs_and_all_majors_are_matched():
    from datetime import datetime, timezone
    from src.news import CalendarEvent, event_matches_symbol, symbol_currencies
    assert symbol_currencies('EURJPY#') == {'EUR', 'JPY'}
    assert symbol_currencies('GBPJPYmicro') == {'GBP', 'JPY'}
    assert symbol_currencies('USDCHF') == {'USD', 'CHF'} and symbol_currencies('NZDUSD#') == {'NZD', 'USD'}
    assert symbol_currencies('SILVER') == {'USD'} and symbol_currencies('GOLD#') == {'USD'}
    assert symbol_currencies('GER40Cash') == {'EUR'} and symbol_currencies('UK100Cash') == {'GBP'}
    assert symbol_currencies('BTCUSD') == set()
    now = datetime(2026, 11, 11, 13, 30, tzinfo=timezone.utc)
    assert event_matches_symbol(CalendarEvent(now, 'JPY', 'HIGH', 'BoJ'), 'EURJPY#')
    assert not event_matches_symbol(CalendarEvent(now, 'CHF', 'HIGH', 'SNB'), 'EURJPY#')


def test_blackout_minutes_come_from_config():
    from datetime import datetime, timedelta, timezone
    from src.news import CalendarEvent, entry_block_reason
    event_time = datetime(2026, 11, 11, 13, 30, tzinfo=timezone.utc)
    events = [CalendarEvent(event_time, 'USD', 'HIGH', 'CPI')]
    early = event_time - timedelta(minutes=25)
    assert entry_block_reason('EURUSD', early, events, early) == ''
    assert 'CPI' in entry_block_reason('EURUSD', early, events, early, minutes_before=30)
    late = event_time + timedelta(minutes=20)
    assert entry_block_reason('EURUSD', late, events, late, minutes_after=10) == ''
    assert 'CPI' in entry_block_reason('EURUSD', late, events, late, minutes_after=30)
