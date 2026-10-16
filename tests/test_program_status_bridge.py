"""Opt-in integration tests; all servers and credentials are local and ephemeral.
SIDEPULSE_BRIDGE_BINARY=/path/to/sidepulse-bridge
SIDEPULSE_REDIS_BINARY=/path/to/redis-server (also enables persistence tests)
"""
import contextlib
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sidepulse.program_status import ProgramStore, StatusError
from sidepulse.program_transport import publish_snapshot
from sidepulse.program_adapter import hook_reports

BINARY=os.environ.get('SIDEPULSE_BRIDGE_BINARY')
REDIS=os.environ.get('SIDEPULSE_REDIS_BINARY')
pytestmark=pytest.mark.skipif(not BINARY,reason='set SIDEPULSE_BRIDGE_BINARY for isolated HTTP integration tests')

def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));return s.getsockname()[1]

def http(server,path,body=None):
    data=json.dumps(body).encode() if body is not None else None
    req=urllib.request.Request(server+path,data=data,headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=5) as response:return response.status,json.loads(response.read())
    except urllib.error.HTTPError as error:return error.code,error.read().decode()

@contextlib.contextmanager
def bridge(redis_url=None):
    server='http://127.0.0.1:'+str(port())
    env={'PATH':os.environ['PATH'],'BIND':server.removeprefix('http://'),'HTTP_ONLY':'true','RATE_PER_SEC':'1000'}
    if redis_url:env['REDIS_URL']=redis_url
    child=subprocess.Popen([BINARY],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT)
    try:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if child.poll() is not None:pytest.fail('isolated bridge exited before readiness')
            try:
                with urllib.request.urlopen(server+'/healthz',timeout=.2):break
            except (OSError,urllib.error.URLError):time.sleep(.05)
        else:pytest.fail('isolated bridge did not become ready')
        yield server
    finally:
        child.terminate()
        try:child.wait(timeout=5)
        except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)

@contextlib.contextmanager
def redis_server(directory,listen_port):
    child=subprocess.Popen([REDIS,'--bind','127.0.0.1','--port',str(listen_port),'--save','','--appendonly','yes','--appendfsync','always','--dir',str(directory)],stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT)
    try:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if child.poll() is not None:pytest.fail('isolated Redis exited before readiness')
            try:
                with socket.create_connection(('127.0.0.1',listen_port),timeout=.2) as client:
                    client.sendall(b'*1\r\n$4\r\nPING\r\n')
                    if client.recv(64)==b'+PONG\r\n':break
            except OSError:time.sleep(.05)
        else:pytest.fail('isolated Redis did not become ready')
        yield 'redis://127.0.0.1:'+str(listen_port)
    finally:
        child.terminate()
        try:child.wait(timeout=5)
        except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)

def snapshot(revision=1,publisher='publisher',state='working'):
    return {'v':1,'type':'program_status_snapshot','publisher':publisher,'revision':revision,'scopes':{'s':{'seq':revision,'records':[{'status':{'state':state},'updated_at':1000}]}}}

def read_messages(server, channel, count):
    from sidepulse.links import iter_sse_messages
    with urllib.request.urlopen(server + '/api/leds/' + channel, timeout=3) as response:
        result = []
        for message in iter_sse_messages(response):
            result.append(json.loads(message))
            if len(result) == count: return result
    raise AssertionError('Stream ended before expected messages')


def test_existing_bridge_keeps_five_complete_snapshots(tmp_path):
    receiver = ProgramStore(tmp_path / 'receiver.sqlite3')
    try:
        with bridge() as server:
            for revision in range(1, 8): publish_snapshot(server, 'isolated-snapshots', snapshot(revision))
            values = read_messages(server, 'isolated-snapshots', 5)
            assert [v['revision'] for v in values] == [3, 4, 5, 6, 7]
            for value in reversed(values): receiver.receive('remote', value)
            assert receiver.db.execute("SELECT body FROM snapshots").fetchone()[0] == json.dumps(values[-1], separators=(',', ':'), sort_keys=True)
            cleared = snapshot(8); cleared['scopes'] = {}
            publish_snapshot(server, 'isolated-snapshots', cleared)
            receiver.receive('remote', read_messages(server, 'isolated-snapshots', 1)[0])
            assert receiver.statuses() == ()
            assert not receiver.receive('remote', values[-1])
            assert http(server, '/api/status/isolated-snapshots')[0] == 404
    finally: receiver.close()


def test_existing_phone_queue_carries_snapshot_and_keeps_latest_only():
    with bridge() as server:
        device = 'apns_' + 'a' * 64
        for revision in (1, 2):
            body = {'data': snapshot(revision), 'shared_key': 'forged'}
            # No live push credentials: retention still occurs on the existing route.
            assert http(server, '/api/leds/' + device + '_sender-key', body)[0] == 503
        status, values = http(server, '/api/leds/' + device + '/queued')
        assert status == 200 and len(values) == 1
        assert values[0]['shared_key'] == 'sender-key'
        assert values[0]['data']['revision'] == 2
        assert http(server, '/api/leds/' + device + '/queued')[1] == []
        # Status and direct LED control intentionally share the existing last-message slot.
        http(server, '/api/leds/' + device + '_sender-key', {'data': snapshot(3)})
        http(server, '/api/leds/' + device + '_sender-key', {'leds': 'off'})
        assert http(server, '/api/leds/' + device + '/queued')[1][0]['leds'] == 'off'
        fcm = 'fcm_fake-registration_' + 'b' * 32
        assert http(server, '/api/leds/' + fcm, {'data': snapshot()})[0] == 503
        value = http(server, '/api/leds/fcm_fake-registration/queued')[1][0]
        assert value['data']['type'] == 'program_status_snapshot'
        assert value['shared_key'] == 'b' * 32


@pytest.mark.skipif(not REDIS, reason='set SIDEPULSE_REDIS_BINARY for existing queue persistence coverage')
def test_existing_redis_queue_survives_restart(tmp_path):
    listen_port = port()
    with redis_server(tmp_path, listen_port) as redis_url:
        with bridge(redis_url) as server:
            publish_snapshot(server, 'isolated-redis-snapshots', snapshot(1))
            publish_snapshot(server, 'isolated-redis-snapshots', snapshot(2))
    with redis_server(tmp_path, listen_port) as redis_url:
        with bridge(redis_url) as server:
            assert [v['revision'] for v in read_messages(server, 'isolated-redis-snapshots', 2)] == [1, 2]


def test_osc_cli_daemon_existing_bridge_receiver_ui(tmp_path):
    from sidepulse.cli import sidepulse_main
    from sidepulse.collector import LiveAgentMonitor
    from sidepulse.program_presentation import ProgramMonitor
    from sidepulse.program_transport import ProgramServer, ProgramRelay
    from sidepulse.relay import RelayConfig, listen_for_relay_events
    from sidepulse.service import SidePulseService
    publisher = ProgramStore(tmp_path / 'publisher.sqlite3')
    receiver = ProgramStore(tmp_path / 'receiver.sqlite3')
    try:
        with bridge() as server, tempfile.TemporaryDirectory(prefix='osc-', dir='/tmp') as directory:
            path = Path(directory) / 'status.sock'
            daemon = ProgramServer(publisher, path); daemon.start()
            try:
                config = RelayConfig(server=server, outbound_channel='isolated-osc-flow')
                receiving = RelayConfig(server=server, receiver_channel=config.outbound_channel)
                service = SidePulseService.__new__(SidePulseService)
                service.program_store = receiver
                service.receiver_signature = (server, config.outbound_channel)
                ui = ProgramMonitor(LiveAgentMonitor(sources=(), recovery_sources=(), latest_state_path=None), receiver)
                with patch('sidepulse.program_adapter.hook_reports', side_effect=AssertionError('provider adapter called')), patch('sidepulse.providers.parse_log_line', side_effect=AssertionError('provider parser called')), patch('sidepulse.program_transport.status_socket_path', return_value=path), patch('sidepulse.relay.load_relay_config', return_value=config), patch('sidepulse.links.load_phone_links', return_value=[]):
                    def step(body, expected):
                        assert sidepulse_main(['report', '--scope', 'deployment', '--osc', '\x1b]7501;' + body]) == 0
                        ProgramRelay(publisher).sync()
                        stop = threading.Event()
                        def accept(value):
                            service.handle_remote_status(receiving, value); stop.set()
                        listen_for_relay_events(receiving, stop, lambda *args: pytest.fail('Unexpected raw hook'), accept)
                        assert ui.snapshot().aggregate.mode.value == expected
                    step('state=working:app=deploy', 'working')
                    step('state=blocked:id=approval:kind=permission:msg=QXBwcm92ZT8=', 'waiting_for_input')
                    assert ui.snapshot().aggregate.representative.message == 'Approve?'
                    step('state=clear:id=approval', 'working')
                    step('state=done:app=deploy', 'completed')
                    step('state=clear', 'idle_ready')
                    assert ui.snapshot().statuses == ()
            finally: daemon.stop()
    finally: publisher.close(); receiver.close()
