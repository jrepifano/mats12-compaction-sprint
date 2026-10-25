"""Desktop portability and active native guards; no Docker/model calls."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from compaction import claude_headless as H
from compaction.s2.native import claude_code as CC
from compaction.s2.native import common as N


def test_managed_policy_uses_platform_roots():
    if sys.platform == 'darwin':
        assert str(H.MANAGED_DIR) == '/Library/Application Support/ClaudeCode'
        assert H.MANAGED_DEFAULTS_CMD
    else:
        assert str(H.MANAGED_DIR) == '/etc/claude-code'
        assert H.MANAGED_DEFAULTS_CMD is None and not H.MANAGED_PLIST_PATHS


def test_local_unix_memory_guard_reads_memavailable(tmp_path, monkeypatch):
    mem = tmp_path / 'meminfo'
    mem.write_text('MemTotal: 99999999 kB\nMemAvailable: 8388608 kB\n')
    monkeypatch.setattr(N, 'Path', lambda path: mem if str(path) == '/proc/meminfo' else Path(path))
    assert N.available_gb('unix:///var/run/docker.sock') == 8
    assert N.wait_for_memory('unix:///var/run/docker.sock', 6, wait_s=0) == 8


@pytest.mark.parametrize('util,expected',[(.89,False),(.9,True),(.99,True)])
def test_native_stream_stops_at_weekly_hold(util,expected):
    state, stop = {}, threading.Event()
    CC._stream_watch(state,stop,CC.CCConfig(max_window_utilization=.90))(json.dumps({
        'type':'rate_limit_event','rate_limit_info':{'unifiedWindows':{
            'seven_day':{'utilization':util,'resetsAt':time.time()+100}}}}))
    assert stop.is_set() is expected
    assert bool(state.get('policy_stop')) is expected


def test_native_guard_withholds_expired_call(monkeypatch):
    monkeypatch.setattr(N,'detached_exec',lambda *a,**k: pytest.fail('must not launch'))
    with pytest.raises(RuntimeError,match='deadline'):
        CC.guarded_exec(None,cfg=CC.CCConfig(deadline_epoch=time.time()-1),state={},
                        stop=threading.Event(),cmd='never')


def test_native_guard_stops_silent_command(tmp_path,monkeypatch):
    flag=tmp_path/'stop'; state={}; stop=threading.Event()
    def executing(sb,*,stop,**kwargs):
        flag.touch()
        assert stop.wait(2), 'silent native call did not observe operator stop'
        return 'stopped'
    monkeypatch.setattr(N,'detached_exec',executing)
    assert CC.guarded_exec(None,cfg=CC.CCConfig(stop_file=str(flag)),state=state,
                           stop=stop,cmd='fake') == 'stopped'
    assert state['policy_stop'] == 'operator_stop'


def test_local_sidecar_mounts_only_private_socket_directory():
    egress = N.Egress('claude_code','127.0.0.1',0,sim_socket='/tmp/private-socket/server.sock')
    args = egress.proxy_run_args('fixture')
    assert 'type=bind,src=/tmp/private-socket,dst=/run/emx-sim,readonly' in args
    assert 'UNIX_FORWARD=8000=/run/emx-sim/server.sock' in args
    assert not any(x.startswith('FORWARD=') for x in args)


def test_unix_forward_transports_bytes(short_socket_dir):
    import socket

    from compaction.s2.native import egress_proxy as EP
    path = str(short_socket_dir/'endpoint.sock')
    listener = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)
    left,right=socket.socketpair()
    worker=threading.Thread(target=EP.handle_unix_forward,args=(right,('fixture',0),path,8000))
    worker.start()
    upstream,_=listener.accept()
    try:
        left.settimeout(2)
        upstream.settimeout(2)
        left.sendall(b'authenticated HTTP bytes')
        assert upstream.recv(100) == b'authenticated HTTP bytes'
        upstream.sendall(b'response')
        assert left.recv(100) == b'response'
    finally:
        left.close(); upstream.close(); listener.close()
        worker.join(2)
    assert not worker.is_alive()


def test_simserver_unix_socket_preserves_authentication(short_socket_dir):
    import socket
    pytest.importorskip('fastmcp')
    from compaction.s2 import interact as I
    path = str(short_socket_dir/'http.sock')
    server=N.SimServer(I.UserSim('fixture',CC.NoSimulatorModel(),lambda *a:None),unix_socket=path)
    server.on_audit=lambda payload:{'continue':False,'stopReason':'fixture'}
    server.start()
    try:
        def request(token=''):
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
                sock.settimeout(3); sock.connect(path)
                header=f'{N.TOKEN_HEADER}: {token}\r\n' if token else ''
                sock.sendall((f'POST {N.AUDIT_PATH} HTTP/1.1\r\nHost: localhost\r\n'
                              f'{header}Content-Length: 2\r\nContent-Type: application/json\r\n'
                              'Connection: close\r\n\r\n{}').encode())
                chunks=[]
                while chunk:=sock.recv(4096): chunks.append(chunk)
                return b''.join(chunks)
        assert b'403 Forbidden' in request()
        assert b'200 OK' in request(server.token)
        assert not server.rows
    finally:
        cleanup=server.stop()
    assert not cleanup


@pytest.fixture
def short_socket_dir():
    import tempfile
    with tempfile.TemporaryDirectory(prefix='planted-uds-',dir='/tmp') as directory:
        yield Path(directory)
