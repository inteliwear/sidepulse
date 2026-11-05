from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import pytest
from sidepulse.program_status import ProgramState, bridge_update, phone_update, fits_phone_push, validate_update


def update(scope='session', seq=1, state='working', publisher='mac', **fields):
    return dict(v=1, type='program_status', publisher=publisher, scope=scope, seq=seq,
                updated_at=2_000_000, status=dict(state=state, **fields))

def test_abandoned_sessions_never_block_new_events(tmp_path):
    store = ProgramState()
    try:
        for i in range(1000):
            store.submit(update(scope=f's-{i}', seq=1))
        with patch('sidepulse.program_status.datetime') as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(days=30)
            store.submit(update(scope='new'))
        assert {s.display_name for s in store.statuses()} == {*(f's-{i}' for i in range(937, 1000)), 'new'}
        assert len(store.pending_events('phone')) == 64
        assert len(store._sequences) <= 128
    finally:
        store.close()


@pytest.mark.parametrize('msg', ['🟢' * 512, '\\"' * 1024, 'a' * 2048])
@pytest.mark.parametrize('prepare', [bridge_update, phone_update])
def test_one_message_budget_clips_only_message(msg, prepare):
    event = update(scope='s'*128, publisher='p'*128, msg=msg, id='a'*32+'/'+ 'b'*32+'/'+ 'c'*32, title='t'*192, app='a'*32)
    clipped = prepare(event)
    assert len(clipped['status']['msg'].encode('utf-8')) <= 1024
    if prepare is phone_update:
        assert fits_phone_push(clipped)
    assert clipped['status']['msg'] == msg[:len(clipped['status']['msg'])]
    assert {k:v for k,v in event['status'].items() if k != 'msg'} == {k:v for k,v in clipped['status'].items() if k != 'msg'}
    assert event['status']['msg'] == msg
    assert validate_update(clipped) == clipped


@pytest.mark.parametrize('prepare', [bridge_update, phone_update])
@pytest.mark.parametrize(('message', 'expected'), [
    ('a' * 1024, 'a' * 1024),
    ('a' * 1025, 'a' * 1024),
    ('a' * 1023 + '🙂', 'a' * 1023),
])
def test_outbound_message_one_kib_boundary(prepare, message, expected):
    assert prepare(update(msg=message))['status']['msg'] == expected


def test_capacity_is_shared_by_local_and_remote(tmp_path):
    store = ProgramState()
    try:
        for i in range(64):
            store.submit(update(scope=f's-{i}'))
        assert store.receive_update('remote', update())
        assert 's-0' not in {s.display_name for s in store.statuses()}
        assert len(store.statuses()) == 64
        assert len(store.pending_events('phone')) == 64
        # Clearing frees a place; the next update need not evict another session.
        assert store.receive_update('remote', update(seq=2, state='clear'))
        store.submit(update(scope='new'))
        assert 's-1' in {s.display_name for s in store.statuses()}
        assert len(store.statuses()) == 64
    finally: store.close()
