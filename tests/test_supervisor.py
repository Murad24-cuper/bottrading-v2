from src.supervisor import clear_stale_pid_lock, heartbeat_is_stale, hang_watchdog_should_terminate, run_restart_supervisor, write_hang_report, detached_python_executable
import faulthandler
import sys
import threading
from types import SimpleNamespace


def test_supervisor_restarts_failed_child_with_backoff():
    outcomes=iter([1,0]); stop={'value':False}; calls=[]; delays=[]; events=[]
    def child():
        value=next(outcomes); calls.append(value)
        if len(calls)==2: stop['value']=True
        return value
    result=run_restart_supervisor(child,lambda:stop['value'],lambda level,msg:events.append((level,msg)),
                                  (10,30,60),5,3600,delays.append,lambda:0)
    assert result==0 and calls==[1,0] and delays==[10]
    assert any('Restarting bot after exit code 1' in msg for _,msg in events)


def test_supervisor_does_not_restart_after_stop_command():
    calls=[]; events=[]; stop={'value':False}
    def child(): calls.append('run'); stop['value']=True; return 0
    result=run_restart_supervisor(child,lambda:stop['value'],lambda level,msg:events.append((level,msg)),sleep=lambda _:None)
    assert result==0 and calls==['run']

def test_watchdog_grants_new_child_a_startup_grace_before_reusing_old_heartbeat():
    assert not hang_watchdog_should_terminate(True,1,90)
    assert not hang_watchdog_should_terminate(True,89.9,90)
    assert hang_watchdog_should_terminate(True,90,90)
    assert not hang_watchdog_should_terminate(False,120,90)

def test_detached_supervisor_prefers_pythonw_and_falls_back(tmp_path):
    python=tmp_path/'python.exe'; python.write_text('')
    assert detached_python_executable(python)==str(python)
    pythonw=tmp_path/'pythonw.exe'; pythonw.write_text('')
    assert detached_python_executable(python)==str(pythonw)

def test_supervisor_health_check_interval():
    from scripts.run_forever import health_check_due,utc_now
    assert not health_check_due(10,69.9)
    assert health_check_due(10,70)
    assert utc_now().utcoffset().total_seconds()==0

def test_detached_console_handlers_rebind_to_utf8_log_stream():
    import io,logging
    from scripts.run_forever import bind_console_handlers
    logger=logging.getLogger('test.detached.console')
    logger.setLevel(logging.INFO)
    handler=logging.StreamHandler(None); logger.addHandler(handler)
    stream=io.StringIO()
    try:
        assert bind_console_handlers(stream)>=1
        logger.info('açıq')
        assert handler.stream is stream
        assert 'açıq' in stream.getvalue()
    finally:
        logger.removeHandler(handler)


def test_supervisor_stops_after_restart_limit_and_logs_critical():
    calls=[]; events=[]; now=[0]
    def child(): calls.append('crash'); return 1
    def sleep(seconds): now[0]+=seconds
    result=run_restart_supervisor(child,lambda:False,lambda level,msg:events.append((level,msg)),
                                  (10,30,60),2,3600,sleep,lambda:now[0])
    assert result==1 and len(calls)==3
    assert [level for level,msg in events].count('WARNING')==2
    assert any(level=='CRITICAL' and 'restart limit' in msg for level,msg in events)


def test_supervisor_removes_only_confirmed_dead_bot_lock(tmp_path):
    lock=tmp_path/'bot.lock'; lock.write_text('1234 token',encoding='utf-8')
    assert not clear_stale_pid_lock(lock,lambda pid:True) and lock.exists()
    assert clear_stale_pid_lock(lock,lambda pid:False) and not lock.exists()
    lock.write_text('damaged',encoding='utf-8')
    assert not clear_stale_pid_lock(lock,lambda pid:False) and lock.exists()

def test_watchdog_detects_stale_running_heartbeat_and_writes_crash_report(tmp_path):
    import sqlite3
    from datetime import datetime,timezone,timedelta
    db=tmp_path/'bot.db'
    with sqlite3.connect(db) as con:
        con.execute('CREATE TABLE bot_status(id INTEGER PRIMARY KEY,time TEXT,status TEXT)')
        now=datetime.now(timezone.utc)
        con.execute('INSERT INTO bot_status VALUES(1,?,?)',((now-timedelta(seconds=91)).isoformat(),'running'))
    assert heartbeat_is_stale(db,90,now)
    logdir=tmp_path/'logs'; logdir.mkdir(); (logdir/'bot.log').write_text('\n'.join(f'line {i}' for i in range(70)),encoding='utf-8')
    write_hang_report(logdir,'heartbeat stale')
    report=(logdir/'crash.log').read_text(encoding='utf-8')
    assert 'line 69' in report and '\nline 20\n' in report and '\nline 19\n' not in report
    assert 'heartbeat stale' in report and 'Current thread' in report


def test_crash_handlers_capture_main_and_thread_tracebacks(tmp_path,monkeypatch):
    from scripts import run_bot
    old_sys=sys.excepthook; old_thread=threading.excepthook
    monkeypatch.setattr(run_bot,'ROOT',tmp_path)
    run_bot.install_crash_handlers()
    try:
        try: raise RuntimeError('main crash test')
        except RuntimeError: sys.excepthook(*sys.exc_info())
        try: raise ValueError('thread crash test')
        except ValueError as exc:
            threading.excepthook(SimpleNamespace(exc_type=type(exc),exc_value=exc,exc_traceback=exc.__traceback__,thread=SimpleNamespace(name='test-thread')))
        assert faulthandler.is_enabled()
        text=(tmp_path/'logs'/'crash.log').read_text(encoding='utf-8')
        assert 'main crash test' in text and 'thread crash test' in text and 'Traceback' in text
    finally:
        faulthandler.disable()
        run_bot._crash_file.close()
        sys.excepthook=old_sys; threading.excepthook=old_thread


def test_supervisor_restarts_clean_exit_when_restart_requested():
    calls=[]; events=[]; requests=iter([True,False]); stop={'value':False}
    def child():
        calls.append('run')
        if len(calls)==2: stop['value']=True
        return 0
    result=run_restart_supervisor(child,lambda:stop['value'],lambda level,msg:events.append((level,msg)),sleep=lambda _:None,restart_requested=lambda:next(requests))
    assert result==0 and calls==['run','run']
    assert any('apply new settings' in msg for _,msg in events)
