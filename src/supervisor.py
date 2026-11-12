"""Crash restart policy kept independent from process and MT5 details."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
import time
from datetime import datetime, timezone
import sqlite3
import faulthandler
from .notify import TelegramNotifier

_notifier=None

def detached_python_executable(current_executable: str|Path) -> str:
    """Prefer pythonw beside a venv interpreter so detached jobs have no console to inherit."""
    current=Path(current_executable); pythonw=current.with_name('pythonw.exe')
    return str(pythonw if pythonw.is_file() else current)

def heartbeat_is_stale(db_path: str | Path, hang_seconds: int, now: datetime | None = None) -> bool:
    """Return true when the persisted live bot heartbeat exceeds its limit."""
    try:
        with sqlite3.connect(db_path, timeout=5) as connection:
            connection.execute('PRAGMA busy_timeout=5000')
            row=connection.execute("SELECT time,status FROM bot_status WHERE id=1").fetchone()
        if not row or row[1] != 'running': return False
        stamp=datetime.fromisoformat(str(row[0]).replace('Z','+00:00'))
        if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=timezone.utc)
        return ((now or datetime.now(timezone.utc))-stamp.astimezone(timezone.utc)).total_seconds()>hang_seconds
    except (OSError,sqlite3.Error,TypeError,ValueError):
        return False

def hang_watchdog_should_terminate(heartbeat_stale: bool, child_runtime_seconds: float, hang_seconds: int) -> bool:
    """Ignore a stale persisted heartbeat until the newly launched child has had time to start."""
    return bool(heartbeat_stale and float(child_runtime_seconds)>=int(hang_seconds))

def request_child_traceback(child_pid: int) -> bool:
    """Signal child process to dump all thread tracebacks to crash.log before termination."""
    import os
    import signal
    try:
        if hasattr(signal, 'CTRL_BREAK_EVENT'):
            os.kill(child_pid, signal.CTRL_BREAK_EVENT)
            return True
        elif hasattr(signal, 'SIGUSR1'):
            os.kill(child_pid, signal.SIGUSR1)
            return True
    except (OSError, ProcessLookupError):
        pass
    return False

def write_hang_report(log_dir: str | Path, reason: str, child_pid: int | None = None) -> None:
    """Capture recent bot log lines, request child thread traceback, and record supervisor stack."""
    root=Path(log_dir); root.mkdir(parents=True,exist_ok=True)
    if child_pid:
        request_child_traceback(child_pid)
        time.sleep(0.5)
    with (root/'crash.log').open('a',encoding='utf-8') as out:
        out.write(f"\nHANG WATCHDOG {datetime.now(timezone.utc).isoformat()} {reason}\n")
        if child_pid:
            out.write(f"--- requested child stack dump for PID={child_pid} ---\n")
        out.write("--- last bot.log lines ---\n")
        try: out.writelines((root/'bot.log').read_text(encoding='utf-8',errors='replace').splitlines(keepends=True)[-50:])
        except OSError as exc: out.write(f'bot.log unavailable: {exc}\n')
        out.write('--- supervisor faulthandler stack ---\n')
        faulthandler.dump_traceback(file=out,all_threads=True)


def clear_stale_pid_lock(path: str | Path, pid_is_alive: Callable[[int], bool]) -> bool:
    """Remove a bot lock only when its recorded owner process is confirmed dead."""
    lock=Path(path)
    if not lock.exists(): return True
    try: pid=int(lock.read_text(encoding='utf-8').split()[0])
    except (OSError,ValueError,IndexError): return False
    if pid<=0 or pid_is_alive(pid): return False
    try: lock.unlink(missing_ok=True)
    except OSError: return False
    return True


def run_restart_supervisor(
    run_child: Callable[[], int],
    stop_requested: Callable[[], bool],
    log_event: Callable[[str, str], None],
    backoff_seconds: Sequence[int] = (10, 30, 60),
    max_restarts: int = 5,
    window_seconds: int = 3600,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    restart_requested: Callable[[], bool] = lambda: False,
) -> int:
    """Restart unexpected exits with bounded backoff and a sliding hourly cap."""
    global _notifier
    restarts: deque[float] = deque()
    while True:
        if stop_requested():
            log_event('INFO', 'bot exiting reason=stop requested')
            return 0
        try:
            return_code = int(run_child())
        except BaseException as exc:
            return_code = 1
            log_event('ERROR', f'Child launch/wait failed: {type(exc).__name__}: {exc}')
        if stop_requested():
            log_event('INFO', 'bot exiting reason=dashboard stop command')
            return 0
        if restart_requested():
            # Dashboard "save settings": restart at once, not counted as a crash.
            log_event('INFO', 'Restarting bot to apply new settings')
            continue
        if return_code != 0:
            if _notifier is None: _notifier=TelegramNotifier()
            _notifier.send('crash',f'💥 Bot prosesi gözlənilmədən dayandı · çıxış kodu {return_code}.')
        now = clock()
        while restarts and now - restarts[0] >= window_seconds:
            restarts.popleft()
        if len(restarts) >= max_restarts:
            log_event('CRITICAL', f'Supervisor restart limit reached: {max_restarts} restarts within {window_seconds} seconds; last child exit={return_code}')
            if _notifier is None: _notifier=TelegramNotifier()
            _notifier.send('supervisor_limit','🚨 Nəzarətçi son bir saatda çoxsaylı xətalara görə botu dayandırdı.')
            _notifier.flush()
            log_event('INFO', 'bot exiting reason=supervisor restart limit exceeded')
            return 1
        delay = int(backoff_seconds[min(len(restarts), len(backoff_seconds) - 1)])
        restarts.append(now)
        message=f'Restarting bot after exit code {return_code}; restart {len(restarts)}/{max_restarts} in window, delay={delay}s'
        log_event('WARNING', message)
        if _notifier is None: _notifier=TelegramNotifier()
        _notifier.send('supervisor_restart',f'🔄 Nəzarətçi botu yenidən başladır · cəhd {len(restarts)}/{max_restarts}.')
        sleep(delay)
