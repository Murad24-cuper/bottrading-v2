"""Small cross-platform process checks for PID locks and health reports."""
from __future__ import annotations

import os


def pid_is_running(pid: int | str | None, *, windows: bool | None = None,
                   kernel32=None, get_last_error=None) -> bool:
    """Check whether a PID exists without treating Windows error 87 as death."""
    try:
        value = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    if windows is None:
        windows = os.name == "nt"
    if windows:
        import ctypes
        if kernel32 is None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            from ctypes import wintypes
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            get_last_error = ctypes.get_last_error
        handle = kernel32.OpenProcess(0x1000, False, value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return bool(get_last_error and get_last_error() == 5)  # Access denied: assume it exists.
        try:
            from ctypes import wintypes
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True  # Could not inspect an opened process; fail closed for lock safety.
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(value, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False


def is_process_named_running(names: tuple[str, ...] | list[str] | set[str]) -> bool:
    """Check whether any process matching the given names (case-insensitive) is currently running."""
    target_names = {n.lower() for n in names}
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        # TH32CS_SNAPPROCESS = 0x00000002
        h_snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        invalid_handle = wintypes.HANDLE(-1).value
        if not h_snapshot or h_snapshot == invalid_handle:
            return False
        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ('dwSize', wintypes.DWORD),
                ('cntUsage', wintypes.DWORD),
                ('th32ProcessID', wintypes.DWORD),
                ('th32DefaultHeapID', ctypes.c_size_t),
                ('th32ModuleID', wintypes.DWORD),
                ('cntThreads', wintypes.DWORD),
                ('th32ParentProcessID', wintypes.DWORD),
                ('pcPriClassBase', wintypes.LONG),
                ('dwFlags', wintypes.DWORD),
                ('szExeFile', ctypes.c_char * 260)
            ]
        kernel32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
        kernel32.Process32First.restype = wintypes.BOOL
        kernel32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
        kernel32.Process32Next.restype = wintypes.BOOL
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        try:
            if not kernel32.Process32First(h_snapshot, ctypes.byref(pe)):
                return False
            while True:
                exe_name = pe.szExeFile.decode('mbcs', errors='ignore').lower()
                if exe_name in target_names:
                    return True
                if not kernel32.Process32Next(h_snapshot, ctypes.byref(pe)):
                    break
            return False
        finally:
            kernel32.CloseHandle(h_snapshot)
    else:
        try:
            import subprocess
            out = subprocess.check_output(['ps', '-A'], text=True, stderr=subprocess.DEVNULL)
            return any(name.lower() in out.lower() for name in names)
        except Exception:
            return False


class SingleInstanceLock:
    """OS-level single-instance lock: a named mutex on Windows (also under Wine)
    and an ``flock`` on POSIX. Both are released by the OS if the process dies,
    so no stale lock can block a restart."""
    def __init__(self, name: str, lock_file_path=None):
        import hashlib
        from pathlib import Path
        self.lock_file_path = lock_file_path
        norm_path = str(Path(lock_file_path).resolve()).lower() if lock_file_path else str(name).lower()
        path_hash = hashlib.sha256(norm_path.encode('utf-8')).hexdigest()[:16]
        safe_name = str(name).replace('\\', '_').replace('/', '_')
        self.name = f"{safe_name}_{path_hash}"
        self._mutex_handle = None
        self._fd = None
        self.held = False

    def _posix_lock_path(self):
        from pathlib import Path
        if self.lock_file_path:
            return Path(self.lock_file_path)
        import tempfile
        return Path(tempfile.gettempdir()) / f"TradingBotLock_{self.name}.lock"

    def acquire(self) -> bool:
        if self.held:
            return True
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            # Local namespace restricts to current desktop session; path hash isolates per directory
            mutex_name = f"Local\\TradingBotLock_{self.name}"
            handle = kernel32.CreateMutexW(None, True, mutex_name)
            if not handle:
                return False
            last_error = ctypes.get_last_error()
            if last_error == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                return False
            self._mutex_handle = handle
            if self.lock_file_path:
                try:
                    from pathlib import Path
                    p = Path(self.lock_file_path)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(f"{os.getpid()}\n", encoding='utf-8')
                except OSError:
                    pass
        else:
            import fcntl
            path = self._posix_lock_path()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
            except OSError:
                return False
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode('utf-8'))
            self._fd = fd
        self.held = True
        return True

    def release(self) -> None:
        if not self.held:
            return
        if os.name == 'nt' and self._mutex_handle:
            import ctypes
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.CloseHandle(self._mutex_handle)
            self._mutex_handle = None
        if self._fd is not None:
            try:
                path = self._posix_lock_path()
                try:
                    if path.read_text(encoding='utf-8').strip() == str(os.getpid()):
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
            finally:
                import fcntl
                try: fcntl.flock(self._fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._fd)
                    self._fd = None
        elif self.lock_file_path:
            try:
                from pathlib import Path
                p = Path(self.lock_file_path)
                if p.exists() and p.read_text(encoding='utf-8').strip() == str(os.getpid()):
                    p.unlink(missing_ok=True)
            except OSError:
                pass
        self.held = False

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"Another instance of '{self.name}' is already running")
        return self

    def __exit__(self, *args):
        self.release()

