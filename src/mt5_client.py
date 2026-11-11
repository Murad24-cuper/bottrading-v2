"""Timeout-protected connection to the local MetaTrader 5 terminal."""
from __future__ import annotations
import queue
import threading
from .logger import get_logger

log=get_logger(__name__)

class MT5CallTimeoutError(TimeoutError):
    """Raised when an MT5 extension call does not return in time."""

class MT5TerminalNotRunningError(RuntimeError):
    """Raised when terminal64.exe is closed and auto_start_terminal is disabled."""

class MT5ShutdownRequestedError(RuntimeError):
    """Raised when an MT5 operation is rejected because bot stop was requested."""

class MT5CallProxy:
    """Run package calls on a single dedicated worker thread with a queue so the bot loop can time out safely."""
    def __init__(self, module, timeout: float, unhealthy=None):
        self._module = module
        self._timeout = float(timeout)
        self._unhealthy = unhealthy
        self._is_unhealthy = False
        self._queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, name='mt5-worker', daemon=True)
        self._worker.start()

    def is_single_worker(self) -> bool:
        return True

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            fn, args, kwargs, res_q = item
            try:
                res = fn(*args, **kwargs)
                res_q.put((True, res))
            except BaseException as exc:
                res_q.put((False, exc))

    def __getattr__(self, name):
        value = getattr(self._module, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            if self._is_unhealthy:
                raise MT5CallTimeoutError(f"MT5 session is marked unhealthy; call '{name}' rejected")
            res_q = queue.Queue(maxsize=1)
            self._queue.put((value, args, kwargs, res_q))
            try:
                ok, payload = res_q.get(timeout=self._timeout)
            except queue.Empty as exc:
                self._is_unhealthy = True
                if self._unhealthy:
                    self._unhealthy(name, self._timeout)
                raise MT5CallTimeoutError(
                    f'MT5 call {name} exceeded {self._timeout:g}s timeout; terminal marked unhealthy'
                ) from exc
            if ok:
                return payload
            raise payload

        return call

class MT5Client:
    def __init__(self,settings):
        self.settings=settings; self.mt5=None; self._module=None; self._unhealthy=False
        self.call_timeout=float(settings.config.get('mt5',{}).get('call_timeout_seconds',20))
        self.stop_requested=False
    def _mark_unhealthy(self,name,timeout):
        self._unhealthy=True
        log.error('MT5 call %s timed out after %.1fs; marking connection unhealthy',name,timeout)
    def request_stop(self):
        """Signal that the bot is shutting down; prevent any future initialize calls."""
        self.stop_requested=True
        self.close()
    def connect(self):
        if self.stop_requested:
            raise MT5ShutdownRequestedError("Bot stop requested; skipping MT5 connect")
        try: import MetaTrader5 as module
        except ImportError as exc: raise RuntimeError('MetaTrader5 is not installed; install requirements on Windows') from exc
        self._module=module
        auto_start=bool(self.settings.config.get('mt5',{}).get('auto_start_terminal',False))
        is_mock=not hasattr(module,'__file__') or type(module).__name__=='SimpleNamespace'
        if not auto_start and not is_mock:
            from .process_utils import is_process_named_running
            if not is_process_named_running(('terminal64.exe','terminal.exe')):
                log.warning('MT5 terminal process (terminal64.exe) is not running and mt5.auto_start_terminal=false; connect aborted')
                raise MT5TerminalNotRunningError('MT5 terminal is closed and auto_start_terminal is false')
        self.mt5=MT5CallProxy(module,self.call_timeout,self._mark_unhealthy)
        init_timeout_ms=int(self.settings.config.get('mt5',{}).get('init_timeout_seconds',30))*1000
        init_kwargs={'timeout':init_timeout_ms}
        if self.settings.mt5_path: init_kwargs['path']=str(self.settings.mt5_path)
        if self.settings.mt5_login: init_kwargs['login']=int(self.settings.mt5_login)
        if self.settings.mt5_password: init_kwargs['password']=str(self.settings.mt5_password)
        if self.settings.mt5_server: init_kwargs['server']=str(self.settings.mt5_server)
        max_attempts=int(self.settings.config.get('mt5',{}).get('max_reconnect_attempts',5))
        last_error=None
        for attempt in range(1,max_attempts+1):
            if self.stop_requested:
                raise MT5ShutdownRequestedError("Bot stop requested during initialize retry")
            try:
                initialized=self.mt5.initialize(**init_kwargs)
            except TypeError:
                initialized=self.mt5.initialize(self.settings.mt5_path) if self.settings.mt5_path else self.mt5.initialize()
            if initialized: break
            last_error=self.mt5.last_error()
            try: self.mt5.shutdown()
            except Exception: pass
            if attempt<max_attempts and not self.stop_requested:
                delay=min(2**attempt,15)
                log.warning('MT5 initialize attempt %d/%d failed: %s; retrying in %ds',attempt,max_attempts,last_error,delay)
                import time
                slept=0.0
                while slept<delay and not self.stop_requested:
                    time.sleep(min(1.0,delay-slept))
                    slept+=1.0
        else: raise RuntimeError(f'MT5 initialize failed after {max_attempts} attempts: {last_error}')
        account = self.mt5.account_info()
        # Use existing terminal session if already logged into an account; otherwise log in with credentials
        if account is None or getattr(account, 'login', 0) == 0:
            if not self.mt5.login(self.settings.mt5_login,password=self.settings.mt5_password,server=self.settings.mt5_server):
                err=self.mt5.last_error(); self.mt5.shutdown(); raise RuntimeError(f'MT5 login failed: {err}')
            account=self.mt5.account_info()
        if account is None:
            err=self.mt5.last_error(); self.mt5.shutdown(); raise RuntimeError(f'MT5 account_info failed: {err}')
        expected=int(self.settings.mt5_login) if self.settings.mt5_login else None
        if expected is not None and int(getattr(account,'login',0) or 0)!=expected:
            # The terminal was logged into another account: switch to the .env account or refuse to trade.
            log.warning('MT5 terminal is on account ***%s, .env expects ***%s; switching',str(getattr(account,'login',''))[-3:],str(expected)[-3:])
            switched=self.mt5.login(expected,password=self.settings.mt5_password,server=self.settings.mt5_server)
            account=self.mt5.account_info() if switched else None
            if account is None or int(getattr(account,'login',0) or 0)!=expected:
                err=self.mt5.last_error(); self.mt5.shutdown()
                raise RuntimeError(f'MT5 is not on the .env account (MT5_LOGIN ***{str(expected)[-3:]}); refusing to trade on another account: {err}')
        demo_mode=getattr(self.mt5,'ACCOUNT_TRADE_MODE_DEMO',0)
        configured_mode=self.settings.config['account']['mode']
        if account.trade_mode!=demo_mode and (not self.settings.allow_real or configured_mode!='real'):
            self.mt5.shutdown(); raise RuntimeError('Real account refused: set account.mode=real in config.yaml and ALLOW_REAL=true in .env to enable trading')
        terminal=self.mt5.terminal_info()
        if terminal is None:
            err=self.mt5.last_error(); self.mt5.shutdown(); raise RuntimeError(f'MT5 terminal_info failed: {err}')
        if not terminal.trade_allowed: log.warning('MT5 algorithmic trading is disabled in terminal settings')
        mode='hedging' if account.margin_mode==getattr(self.mt5,'ACCOUNT_MARGIN_MODE_RETAIL_HEDGING',2) else 'netting'
        self.account_login=int(getattr(account,'login',0) or 0) or None
        log.info('Connected to account ***%s; margin mode=%s',str(account.login)[-3:],mode)
        self._unhealthy=False
        return self
    def ensure_connected(self):
        if self.stop_requested:
            return False
        terminal=None
        if self.mt5 is not None and not self._unhealthy:
            try: terminal=self.mt5.terminal_info()
            except Exception: terminal=None
        if self.mt5 is None or terminal is None or not bool(getattr(terminal,'connected',True)) or self._unhealthy:
            self.close(); self.connect()
        return True
    def close(self):
        if self.mt5 is not None:
            try: self.mt5.shutdown()
            except Exception: pass
            finally: self.mt5=None; self._module=None; self._unhealthy=False
    def __enter__(self): return self.connect()
    def __exit__(self,*exc): self.close()
