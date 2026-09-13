"""Run explicitly on an installed server; validates a real idle menu via PTY."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import time

paths = [Path('/root/3xui-dual/manager.py'), Path('/usr/local/bin/3xui-relay')]
before = [p.stat().st_mtime_ns for p in paths]
pid, fd = pty.fork()
if pid == 0:
    os.execv('/usr/local/bin/3xui-relay', ['3xui-relay'])
output = b''
reaped = False
try:
    deadline = time.monotonic() + 15
    while '选择操作'.encode() not in output:
        if time.monotonic() >= deadline:
            raise RuntimeError('Menu did not become ready')
        if select.select([fd], [], [], 1)[0]:
            output += os.read(fd, 65536)
    with open('/run/lock/3xui-dual.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock, fcntl.LOCK_UN)
    assert '9. 更新管理脚本'.encode() in output
    assert '下次输入'.encode() not in output
    os.write(fd, b'0\n')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        child, status = os.waitpid(pid, os.WNOHANG)
        if child:
            reaped = True
            assert os.waitstatus_to_exitcode(status) == 0
            break
        time.sleep(0.1)
    assert reaped
    assert before == [p.stat().st_mtime_ns for p in paths]
    report = {'idle_menu_releases_lock': True, 'startup_does_not_rewrite_files': True,
              'update_menu_visible': True, 'exit_ok': True}
    Path('/root/3xui-dual/menu-validation-1.1.2.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
finally:
    if not reaped:
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)
    os.close(fd)
