"""Opt-in integration tests; all servers and credentials are local and ephemeral.
SIDEPULSE_BRIDGE_BINARY=/path/to/sidepulse-bridge
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
from sidepulse.program_status import ProgramState, StatusError
from sidepulse.program_transport import publish_update

BINARY=os.environ.get('SIDEPULSE_BRIDGE_BINARY')
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
def bridge(queue_ttl=None):
    server='http://127.0.0.1:'+str(port())
    env={'PATH':os.environ['PATH'],'BIND':server.removeprefix('http://'),'HTTP_ONLY':'true','RATE_PER_SEC':'1000'}
    if queue_ttl is not None: env['QUEUE_TTL_SECS']=str(queue_ttl)
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


def update(seq=1,publisher='publisher',state='working'):
    return {'v':1,'type':'program_status','publisher':publisher,'scope':'session','seq':seq,
            'status':{'state':state},'updated_at':1000}

def read_messages(server, channel, count):
    from sidepulse.links import iter_sse_messages
    with urllib.request.urlopen(server + '/api/leds/' + channel, timeout=3) as response:
        result = []
        for message in iter_sse_messages(response):
            result.append(json.loads(message))
            if len(result) == count: return result
    raise AssertionError('Stream ended before expected messages')


def test_bridge_keeps_only_the_newest_value_per_session(tmp_path):
    receiver = ProgramState()
    try:
        with bridge() as server:
            for revision in range(1, 8): publish_update(server, 'isolated-snapshots', update(revision))
            values = read_messages(server, 'isolated-snapshots', 1)
            assert [v['seq'] for v in values] == [7]
            for value in reversed(values): receiver.receive_update('remote', value)
            assert len(receiver.statuses()) == 1
            cleared = update(8); cleared['status'] = {'state':'clear'}
            publish_update(server, 'isolated-snapshots', cleared)
            receiver.receive_update('remote', read_messages(server, 'isolated-snapshots', 1)[0])
            assert receiver.statuses() == ()
            assert not receiver.receive_update('remote', values[-1])
            assert http(server, '/api/status/isolated-snapshots')[0] == 404
    finally: receiver.close()


@pytest.mark.parametrize('message', ['🙂' * 512, '\\"' * 1024, 'a' * 2048])
def test_sender_clips_before_bridge_including_phone_recovery_metadata(message):
    from sidepulse.program_status import encoded
    value = update()
    value['publisher'] = 'p' * 128
    value['scope'] = 's' * 128
    value['status'] = {'state': 'done', 'msg': message, 'title': 't' * 192}
    original = copy.deepcopy(value)
    with bridge() as server:
        publish_update(server, 'bounded-status', value)
        received = read_messages(server, 'bounded-status', 1)[0]
        assert len(encoded(received).encode()) <= 2048
        assert len(received['status']['msg'].encode()) <= 1024
        assert received['status']['msg'] and original['status']['msg'].startswith(received['status']['msg'])
        for device, key in [('apns_' + 'a' * 64, 'k' * 128), ('fcm_fake-registration', 'b' * 32)]:
            # No provider credentials: 503 proves size validation and retention
            # succeeded; an oversized request would instead fail with 413.
            with pytest.raises(StatusError) as failure:
                publish_update(server, device + '_' + key, value, phone=True)
            assert failure.value.status_code == 503
            queued = http(server, '/api/leds/' + device + '_' + key + '/queued')[1][0]
            assert len(encoded(queued).encode()) <= 2048
            delivered = queued['data']
            assert len(delivered['status']['msg'].encode()) <= 1024
            assert delivered['status']['msg'] and message.startswith(delivered['status']['msg'])
            assert {**delivered, 'status': {**delivered['status'], 'msg': message}} == original
        assert value == original


def test_phone_queue_keeps_latest_session_and_manual_push_independently():
    with bridge() as server:
        device = 'apns_' + 'a' * 64
        for revision in (1, 2):
            body = {'data': update(revision), 'shared_key': 'forged'}
            # No live push credentials: retention still occurs on the existing route.
            assert http(server, '/api/leds/' + device + '_sender-key', body)[0] == 503
        status, values = http(server, '/api/leds/' + device + '_sender-key/queued')
        assert status == 200 and len(values) == 1
        assert values[0]['shared_key'] == 'sender-key'
        assert values[0]['data']['seq'] == 2
        assert http(server, '/api/leds/' + device + '_sender-key/queued')[1] == []
        # Manual pushes do not discard queued session states.
        http(server, '/api/leds/' + device + '_sender-key', {'data': update(3)})
        http(server, '/api/leds/' + device + '_sender-key', {'leds': 'off'})
        queued = http(server, '/api/leds/' + device + '_sender-key/queued')[1]
        assert len(queued) == 2 and queued[-1]['leds'] == 'off'
        fcm = 'fcm_fake-registration_' + 'b' * 32
        assert http(server, '/api/leds/' + fcm, {'data': update()})[0] == 503
        value = http(server, '/api/leds/' + fcm + '/queued')[1][0]
        assert value['data']['type'] == 'program_status'
        assert value['shared_key'] == 'b' * 32


def test_bridge_queue_keeps_ten_recent_sessions_and_isolates_sources():
    with bridge() as server:
        for n in range(10):
            value = {**update(), 'scope':f's{n}'}
            publish_update(server,'ten-sessions',value)
        publish_update(server,'ten-sessions',{**update(2),'scope':'s0'})
        publish_update(server,'ten-sessions',{**update(),'scope':'s10'})
        values = read_messages(server,'ten-sessions',10)
        assert [v['scope'] for v in values] == [f's{n}' for n in range(2,10)]+['s0','s10']
        device = 'apns_'+'a'*64
        for sender in ['source-a','source-b']:
            assert http(server,'/api/leds/'+device+'_'+sender,{'data':update()})[0] == 503
        assert http(server,'/api/leds/'+device+'/queued')[0] == 400
        for sender in ['source-a','source-b']:
            status,values = http(server,'/api/leds/'+device+'_'+sender+'/queued')
            assert status == 200 and len(values) == 1
            assert values[0]['shared_key'] == sender


def test_phone_recovery_expires_with_configured_shorter_ttl():
    with bridge(queue_ttl=1) as server:
        device = 'apns_'+'a'*64
        assert http(server,'/api/leds/'+device+'_source',{'data':update()})[0] == 503
        time.sleep(1.1)
        assert http(server,'/api/leds/'+device+'_source/queued')[1] == []


def test_android_recovery_keeps_latest_state_per_authorized_source():
    with bridge() as server:
        for sender in ['a'*32,'b'*32]:
            for seq in [1,2,1]:
                assert http(server,'/api/leds/fcm_test-registration_'+sender,{'data':update(seq)})[0] == 503
        assert http(server,'/api/leds/fcm_test-registration/queued')[0] == 400
        for sender in ['a'*32,'b'*32]:
            status,values = http(server,'/api/leds/fcm_test-registration_'+sender+'/queued')
            assert status == 200 and len(values) == 1
            assert values[0]['shared_key'] == sender and values[0]['data']['seq'] == 2


def test_osc_cli_daemon_existing_bridge_receiver_ui(tmp_path):
    from sidepulse.cli import sidepulse_main
    from sidepulse.collector import LiveAgentMonitor
    from sidepulse.program_presentation import ProgramMonitor
    from sidepulse.program_transport import ProgramServer, ProgramRelay
    from sidepulse.relay import RelayConfig, listen_for_status_updates
    from sidepulse.service import SidePulseService
    publisher = ProgramState()
    receiver = ProgramState()
    try:
        with bridge() as server, tempfile.TemporaryDirectory(prefix='osc-', dir='/tmp') as directory:
            path = Path(directory) / 'status.sock'
            daemon = ProgramServer(publisher, path); daemon.start()
            try:
                config = RelayConfig(server=server, outbound_channel='isolated-osc-flow')
                receiving = RelayConfig(server=server, receiver_channel=config.outbound_channel)
                service = SidePulseService.__new__(SidePulseService)
                service.program_state = receiver
                service.receiver_signature = (server, config.outbound_channel)
                ui = ProgramMonitor(receiver)
                with patch('sidepulse.program_adapter.hook_report', side_effect=AssertionError('provider adapter called')), patch('sidepulse.providers.parse_log_line', side_effect=AssertionError('provider parser called')), patch('sidepulse.program_transport.status_socket_path', return_value=path), patch('sidepulse.relay.load_relay_config', return_value=config), patch('sidepulse.links.load_phone_links', return_value=[]):
                    relay = ProgramRelay(publisher); relay.sync()
                    def step(body, expected):
                        assert sidepulse_main(['report', '--scope', 'deployment', '--osc', '\x1b]7501;' + body]) == 0
                        relay.sync()
                        stop = threading.Event()
                        def accept(value):
                            service.handle_remote_status(receiving, value); stop.set()
                        listen_for_status_updates(receiving, stop, accept)
                        assert ui.snapshot().aggregate.mode.value == expected
                    step('state=working:app=deploy', 'working')
                    step('state=blocked:id=approval:kind=permission:msg=QXBwcm92ZT8=', 'waiting_for_input')
                    assert ui.snapshot().aggregate.representative.message == 'Approve?'
                    step('state=clear:id=approval', 'idle_ready')
                    step('state=done:app=deploy', 'completed')
                    step('state=clear', 'idle_ready')
                    assert ui.snapshot().statuses == ()
                    relay.stop()
            finally: daemon.stop()
    finally: publisher.close(); receiver.close()
