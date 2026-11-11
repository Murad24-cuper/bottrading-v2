import sys
from types import SimpleNamespace

import pytest

from src.mt5_client import MT5CallProxy, MT5CallTimeoutError, MT5Client


def client_settings(mode,allow_real):
    return SimpleNamespace(mt5_path=None,mt5_login=123456,mt5_password='private-test-value',mt5_server='demo-server',allow_real=allow_real,config={'account':{'mode':mode}})

def fake_mt5(trade_mode):
    return SimpleNamespace(ACCOUNT_TRADE_MODE_DEMO=0,ACCOUNT_MARGIN_MODE_RETAIL_HEDGING=2,initialize=lambda *a:True,login=lambda *a,**k:True,account_info=lambda:SimpleNamespace(trade_mode=trade_mode,margin_mode=2,login=123456),terminal_info=lambda:SimpleNamespace(trade_allowed=True),shutdown=lambda:None,last_error=lambda:(0,'ok'))

def test_real_login_requires_both_real_mode_and_explicit_allow_real(monkeypatch):
    module=fake_mt5(1); monkeypatch.setitem(sys.modules,'MetaTrader5',module)
    with pytest.raises(RuntimeError,match='account.mode=real'):
        MT5Client(client_settings('demo',True)).connect()

def test_real_login_allowed_only_with_both_explicit_guards(monkeypatch,caplog):
    module=fake_mt5(1); monkeypatch.setitem(sys.modules,'MetaTrader5',module)
    client=MT5Client(client_settings('real',True)).connect()
    assert client.mt5._module is module
    assert 'private-test-value' not in caplog.text

def test_mt5_call_timeout_marks_connection_unhealthy():
    import time
    from types import SimpleNamespace
    marked=[]
    def slow(): time.sleep(.08); return True
    proxy=MT5CallProxy(SimpleNamespace(slow=slow),.01,lambda name,timeout:marked.append((name,timeout)))
    with pytest.raises(MT5CallTimeoutError,match='slow'):
        proxy.slow()
    assert marked==[('slow',.01)]


def test_connect_switches_from_other_terminal_account_to_env_account(monkeypatch):
    state={'login':999999}
    module=fake_mt5(0)
    module.account_info=lambda:SimpleNamespace(trade_mode=0,margin_mode=2,login=state['login'])
    def login(value,**kwargs): state['login']=value; return True
    module.login=login
    monkeypatch.setitem(sys.modules,'MetaTrader5',module)
    MT5Client(client_settings('demo',False)).connect()
    assert state['login']==123456


def test_connect_refuses_when_terminal_stays_on_another_account(monkeypatch):
    module=fake_mt5(0)
    module.account_info=lambda:SimpleNamespace(trade_mode=0,margin_mode=2,login=999999)
    module.login=lambda *a,**k:False
    monkeypatch.setitem(sys.modules,'MetaTrader5',module)
    with pytest.raises(RuntimeError,match='refusing to trade on another account'):
        MT5Client(client_settings('demo',False)).connect()
