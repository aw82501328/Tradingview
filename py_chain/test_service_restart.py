"""Restart tests use disposable servers and storage, never the user's service."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from py_chain import webapp
from py_chain.service_restart import RestartManager


def request(port, path='/api/status', body=None):
    req = Request(f'http://127.0.0.1:{port}{path}',
                  data=json.dumps(body).encode() if body is not None else None,
                  headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=2) as response:
        return json.load(response)


def serve_fixture(port, busy=False, failure=False, hang=False):
    # No ControlApp construction: no production analysis files, presets or jobs touched.
    app = SimpleNamespace(service=None)
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']) if busy else None
    app.status = lambda: {'service': app.service.status(), 'rows': [], 'auto': False,
                          'pid': os.getpid(), 'child': child.pid if child else None}
    class FixtureHandler(webapp.make_handler(app)):
        def do_POST(self):
            if self.path == '/fixture/shutdown':
                self._read_body()
                self.close_connection = True
                self._send_json({'ok': True})
                threading.Thread(target=server.shutdown, daemon=True).start()
                return
            super().do_POST()
    server = webapp.ThreadingHTTPServer(('127.0.0.1', port), FixtureHandler)
    app.service = RestartManager('127.0.0.1', port, (lambda: time.sleep(120)) if hang else server.shutdown)
    if failure == 'occupied':
        app.service.command = [sys.executable, '-c',
            f"import socket; s=socket.socket(); s.bind(('127.0.0.1',{port})); s.listen(); "
            f"from py_chain.test_service_restart import serve_fixture; serve_fixture({port})"]
    else:
        app.service.command = ([sys.executable, '-c', 'raise SystemExit(23)'] if failure else
                              [sys.executable, str(Path(__file__).resolve()), '--serve', str(port)])
    server.serve_forever()
    server.server_close()


class ConnectionTests(unittest.TestCase):
    def test_reset_ends_keepalive_loop(self):
        for exception in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            handler = object.__new__(webapp.make_handler(None))
            handler.close_connection = False
            with patch.object(webapp.BaseHTTPRequestHandler, 'handle_one_request', side_effect=exception) as read:
                handler.handle_one_request()
                self.assertTrue(handler.close_connection)
                self.assertEqual(read.call_count, 1)

    def test_restart_blocks_task_and_chart_acquisition(self):
        with patch.object(webapp, '_service_restarting', True):
            self.assertEqual(webapp.acquire_active('backtest'), '服务重启中')
            self.assertFalse(webapp.ChartLock().acquire())
            self.assertFalse(webapp.ensure_idle()[0])

    def test_duplicate_requests_share_helper(self):
        manager = RestartManager('127.0.0.1', 8123, Mock())
        helper = Mock(); helper.stdout.readline.return_value = 'READY\n'
        with patch('py_chain.service_restart.subprocess.Popen', return_value=helper) as launch:
            with patch('py_chain.service_restart.os.name', 'nt'):
                first = manager.prepare(); second = manager.prepare()
            self.assertEqual(first, second)
            self.assertEqual(launch.call_count, 1)
            manager.commit(); manager.commit()
            self.assertEqual(helper.stdin.write.call_count, 1)

    def test_helper_failure_leaves_service_running(self):
        stop = Mock()
        manager = RestartManager('127.0.0.1', 8123, stop)
        helper = Mock(); helper.stdout.readline.return_value = ''
        with patch('py_chain.service_restart.subprocess.Popen', return_value=helper):
            with self.assertRaises(RuntimeError): manager.prepare()
        self.assertFalse(manager.restarting)
        stop.assert_not_called()


@unittest.skipUnless(os.name == 'nt', 'Windows process lifecycle')
class LifecycleTests(unittest.TestCase):
    def run_server_case(self, busy=False, failure=False, hang=False):
        with tempfile.TemporaryDirectory(prefix='chan-restart-test-') as folder:
            with socket.socket() as reserve:
                reserve.bind(('127.0.0.1', 0)); port = reserve.getsockname()[1]
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            command = [sys.executable, str(Path(__file__).resolve()), '--serve', str(port)]
            if busy: command.append('--busy')
            if failure: command.append('--occupied' if failure == 'occupied' else '--failure')
            if hang: command.append('--hang')
            proc = subprocess.Popen(command, cwd=folder, env=env, creationflags=subprocess.CREATE_NO_WINDOW,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            new_pid = None
            try:
                deadline = time.monotonic() + 10
                while True:
                    try: before = request(port); break
                    except (URLError, OSError):
                        if time.monotonic() > deadline: raise
                        time.sleep(.1)
                started = request(port, '/api/service/restart', {})
                self.assertEqual(started['service']['instanceId'], before['service']['instanceId'])
                self.assertTrue(started['service']['restarting'])
                proc.wait(timeout=15)
                deadline = time.monotonic() + (3 if failure else 15)
                after = None
                while time.monotonic() < deadline:
                    try:
                        candidate = request(port)
                        if candidate['service']['instanceId'] != before['service']['instanceId']:
                            after = candidate; new_pid = after['pid']; break
                    except (URLError, OSError): pass
                    time.sleep(.1)
                if failure:
                    self.assertIsNone(after)
                else:
                    self.assertIsNotNone(after)
                    self.assertEqual(after['rows'], [])
                    self.assertFalse(after['auto'])
                if before['child']:
                    import ctypes
                    kernel = ctypes.WinDLL('kernel32'); kernel.OpenProcess.restype = ctypes.c_void_p
                    handle = kernel.OpenProcess(0x100000, False, before['child'])
                    if handle:
                        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
                        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                        self.assertEqual(kernel.WaitForSingleObject(handle, 1000), 0)
                        kernel.CloseHandle(handle)
            finally:
                if proc.poll() is None: proc.kill(); proc.wait()
                if new_pid:
                    request(port, '/fixture/shutdown', {})
                # The replacement process can still be closing its log handle.
                time.sleep(.8)

    def test_idle_restart(self): self.run_server_case()
    def test_restart_cleans_task_child(self): self.run_server_case(busy=True)
    def test_failed_start_does_not_serve_old_instance(self): self.run_server_case(failure=True)
    def test_occupied_port_is_not_reported_as_recovered(self): self.run_server_case(failure='occupied')
    def test_hung_service_is_replaced_after_grace(self): self.run_server_case(busy=True, hang=True)


if __name__ == '__main__':
    if '--serve' in sys.argv:
        serve_fixture(int(sys.argv[sys.argv.index('--serve') + 1]), '--busy' in sys.argv,
                      'occupied' if '--occupied' in sys.argv else '--failure' in sys.argv,
                      '--hang' in sys.argv)
    else:
        unittest.main()
