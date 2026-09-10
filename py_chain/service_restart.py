"""Windows service replacement; never terminates TradingView or unrelated processes."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import traceback
import uuid


class RestartManager:
    def __init__(self, host, port, shutdown):
        self.instance_id = uuid.uuid4().hex
        self.command = [sys.executable, '-m', 'py_chain.webapp', '--host', host, '--port', str(port)]
        self.shutdown = shutdown
        self.lock = threading.Lock()
        self.helper = None
        self.restarting = False
        self.committed = False

    def status(self):
        return {'instanceId': self.instance_id, 'restarting': self.restarting,
                'startCommand': subprocess.list2cmdline(self.command)}

    def prepare(self):
        with self.lock:
            if self.restarting:
                return self.status()
            if os.name != 'nt':
                raise RuntimeError('页面重启目前仅支持 Windows')
            command = [sys.executable, str(Path(__file__).resolve()), str(os.getpid()),
                       json.dumps(self.command)]
            self.helper = subprocess.Popen(command, cwd=os.getcwd(), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
            # Helper acquires process handles before acknowledging readiness.
            ready = []
            reader = threading.Thread(target=lambda: ready.append(self.helper.stdout.readline().strip()), daemon=True)
            reader.start()
            reader.join(5)
            if ready != ['READY']:
                self.helper.kill()
                self.helper.wait(timeout=5)
                reader.join(1)
                self.helper.stdin.close()
                self.helper.stdout.close()
                raise RuntimeError('无法启动重启辅助进程，原服务继续运行')
            self.restarting = True
            return self.status()

    def commit(self):
        with self.lock:
            if self.committed:
                return
            self.committed = True
            self.helper.stdin.write('RESTART\n')
            self.helper.stdin.flush()
            self.helper.stdin.close()
            self.helper.stdout.close()
            threading.Thread(target=self.shutdown, daemon=True, name='service-shutdown').start()


def process_handles(parent_pid):
    """Snapshot only Python/Node task descendants; hold handles against PID reuse."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('pid', wintypes.DWORD), ('heap', ctypes.c_size_t),
                    ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                    ('parent', wintypes.DWORD), ('priority', wintypes.LONG),
                    ('flags', wintypes.DWORD), ('exe', wintypes.WCHAR * 260)]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    parent = kernel.OpenProcess(0x100001, False, parent_pid)
    if not parent:
        raise ctypes.WinError(ctypes.get_last_error())
    snap = kernel.CreateToolhelp32Snapshot(2, 0)
    if snap == ctypes.c_void_p(-1).value:
        kernel.CloseHandle(parent)
        raise ctypes.WinError(ctypes.get_last_error())
    entry = Entry(); entry.dwSize = ctypes.sizeof(entry)
    rows = []
    ok = kernel.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        rows.append((entry.pid, entry.parent, entry.exe.lower()))
        ok = kernel.Process32NextW(snap, ctypes.byref(entry))
    kernel.CloseHandle(snap)
    descendants = {parent_pid}
    handles = []
    for _ in range(len(rows)):
        added = False
        for pid, ppid, name in rows:
            if ppid in descendants and pid not in descendants and pid != os.getpid() and name in ('python.exe', 'pythonw.exe', 'node.exe'):
                descendants.add(pid); added = True
                handle = kernel.OpenProcess(0x100001, False, pid)
                if handle:
                    handles.append(handle)
        if not added:
            break
    return kernel, parent, handles


def replace_service(parent_pid, command):
    kernel, parent, children = process_handles(parent_pid)
    print('READY', flush=True)
    if sys.stdin.readline().strip() != 'RESTART':
        return
    # Normal shutdown gets ten seconds; handles refer to the original processes.
    kernel.WaitForSingleObject(parent, 10000)
    if kernel.WaitForSingleObject(parent, 0) == 258:
        if not kernel.TerminateProcess(parent, 1):
            raise ctypes.WinError(ctypes.get_last_error())
    if kernel.WaitForSingleObject(parent, 5000) != 0:
        raise RuntimeError('旧服务未退出，取消启动新服务')
    # Also capture tasks started between the initial snapshot and shutdown.
    # Keeping the original parent handle open prevents its PID being reused.
    _, extra_parent, extra_children = process_handles(parent_pid)
    kernel.CloseHandle(extra_parent)
    children.extend(extra_children)
    for handle in reversed(children):
        if kernel.WaitForSingleObject(handle, 0) == 258:
            if not kernel.TerminateProcess(handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            if kernel.WaitForSingleObject(handle, 5000) != 0:
                raise RuntimeError('任务子进程未退出，取消启动新服务')
        kernel.CloseHandle(handle)
    kernel.CloseHandle(parent)
    folder = Path.cwd() / '.cache'
    folder.mkdir(exist_ok=True)
    with (folder / 'service-restart.log').open('ab') as output:
        subprocess.Popen(command, cwd=os.getcwd(), stdin=subprocess.DEVNULL,
                         stdout=output, stderr=output, creationflags=subprocess.CREATE_NO_WINDOW)


if __name__ == '__main__':
    try:
        replace_service(int(sys.argv[1]), json.loads(sys.argv[2]))
    except Exception:
        folder = Path.cwd() / '.cache'
        folder.mkdir(exist_ok=True)
        with (folder / 'service-restart.log').open('a', encoding='utf-8') as output:
            traceback.print_exc(file=output)
        raise
