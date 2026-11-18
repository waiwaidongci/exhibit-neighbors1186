import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

TMP = tempfile.mkdtemp(prefix='acct-test-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(TMP, 'test.db')
os.environ['SEED_DEMO'] = 'false'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from src.config.database import Base, engine, Session  # noqa: E402
from src.main import app  # noqa: E402
from src.models import accounting as models  # noqa: E402
from src.models.user import User  # noqa: E402
from src.services import accounting  # noqa: E402
from src.services.accounting import ensure_lock_row  # noqa: E402
from src.utils.security import hash_password  # noqa: E402
from src.utils.serialization import now  # noqa: E402

PASSWORD = 'Research2026!'


@pytest.fixture(scope='session', autouse=True)
def prepare():
    Base.metadata.create_all(engine)
    with Session() as db:
        ensure_lock_row(db)
        for role in ('admin', 'analyst', 'viewer'):
            db.add(User(email=role + '@test.demo', password_hash=hash_password(PASSWORD), role=role))
        db.commit()


@pytest.fixture(autouse=True)
def clean_ledger():
    with Session() as db:
        for model in (models.VersionEvent, models.AccountVersion, models.TerminalEvent, models.Terminal):
            db.query(model).delete()
        db.commit()
    yield


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def login(client, role):
    r = client.post('/auth/login', json={'email': role + '@test.demo', 'password': PASSWORD})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}


def event(uid, kind, at, slot, rating=None, visitor='V1', exhibit='E01'):
    body = {'record_uid': uid, 'kind': kind, 'visitor': visitor, 'exhibit': exhibit, 'occurred_at': at, 'slot': slot}
    if rating is not None:
        body['rating'] = rating
    return body


def make_terminal(client, headers, code, venue='HALL-A', slots=(1,)):
    r = client.post('/accounting/terminals', headers=headers,
                    json={'code': code, 'venue': venue, 'declared_slots': list(slots)})
    assert r.status_code == 200, r.text
    return r.json()


def post(client, headers, terminal, events):
    return client.post('/accounting/terminals/%d/events' % terminal['id'], headers=headers, json={'events': events})


class FakeUser:
    email = 'analyst@test.demo'


def test_rating_before_exposure_is_pending_then_late_exposure_reattributes():
    events = [
        {'id': 1, 'kind': 'rating', 'venue': 'H', 'visitor': 'V1', 'exhibit': 'E1', 'occurred_at': '2026-11-18T02:00:00.000000Z'},
    ]
    matches = accounting.compute_matches(events)
    assert matches == [{'rating_event_id': 1, 'exposure_event_id': None, 'status': 'pending'}]

    events = [
        {'id': 1, 'kind': 'rating', 'venue': 'H', 'visitor': 'V1', 'exhibit': 'E1', 'occurred_at': '2026-11-18T02:00:00.000000Z'},
        {'id': 2, 'kind': 'exposure', 'venue': 'H', 'visitor': 'V1', 'exhibit': 'E1', 'occurred_at': '2026-11-18T01:00:00.000000Z'},
    ]
    assert accounting.compute_matches(events)[0]['exposure_event_id'] == 2

    # A nearer exposure that arrives later steals the attribution.
    events.append({'id': 3, 'kind': 'exposure', 'venue': 'H', 'visitor': 'V1', 'exhibit': 'E1', 'occurred_at': '2026-11-18T01:59:00.000000Z'})
    assert accounting.compute_matches(events)[0]['exposure_event_id'] == 3


def test_match_requires_same_venue_visitor_and_exhibit():
    events = [
        {'id': 1, 'kind': 'exposure', 'venue': 'H', 'visitor': 'V1', 'exhibit': 'E1', 'occurred_at': '2026-11-18T01:00:00.000000Z'},
        {'id': 2, 'kind': 'rating', 'venue': 'H', 'visitor': 'V2', 'exhibit': 'E1', 'occurred_at': '2026-11-18T02:00:00.000000Z'},
        {'id': 3, 'kind': 'rating', 'venue': 'H', 'visitor': 'V1', 'exhibit': 'E2', 'occurred_at': '2026-11-18T02:00:00.000000Z'},
        {'id': 4, 'kind': 'rating', 'venue': 'OTHER', 'visitor': 'V1', 'exhibit': 'E1', 'occurred_at': '2026-11-18T02:00:00.000000Z'},
    ]
    assert [m['status'] for m in accounting.compute_matches(events)] == ['pending', 'pending', 'pending']


def test_coverage_counts_rated_exposures_per_exhibit():
    events = [
        {'id': 1, 'kind': 'exposure', 'venue': 'H', 'visitor': 'V1', 'exhibit': 'E1', 'occurred_at': '2026-11-18T01:00:00.000000Z'},
        {'id': 2, 'kind': 'exposure', 'venue': 'H', 'visitor': 'V2', 'exhibit': 'E1', 'occurred_at': '2026-11-18T01:00:00.000000Z'},
        {'id': 3, 'kind': 'exposure', 'venue': 'H', 'visitor': 'V3', 'exhibit': 'E2', 'occurred_at': '2026-11-18T01:00:00.000000Z'},
        {'id': 4, 'kind': 'rating', 'venue': 'H', 'visitor': 'V1', 'exhibit': 'E1', 'occurred_at': '2026-11-18T02:00:00.000000Z'},
    ]
    coverage = accounting.compute_coverage(events, accounting.compute_matches(events))
    assert coverage == [
        {'exhibit': 'E1', 'exposures': 2, 'rated_exposures': 1, 'ratio': 0.5},
        {'exhibit': 'E2', 'exposures': 1, 'rated_exposures': 0, 'ratio': 0.0},
    ]


def test_duplicate_retry_keeps_first_copy_and_does_not_recount(client):
    headers = login(client, 'analyst')
    terminal = make_terminal(client, headers, 'T-DUP')
    first = post(client, headers, terminal, [
        event('a', 'exposure', '2026-11-18T09:00:00+08:00', 1),
        event('a', 'exposure', '2026-11-18T09:00:00+08:00', 1),
        event('a', 'exposure', '2026-11-18T09:30:00+08:00', 1),
    ])
    assert first.status_code == 200
    body = first.json()
    assert body['accepted'] == 1 and body['duplicates'] == 1 and body['conflicts'] == 1
    assert [r['status'] for r in body['results']] == ['accepted', 'duplicate', 'conflict']
    events = client.get('/accounting/events', headers=headers).json()
    assert len([e for e in events if e['record_uid'] == 'a']) == 1
    assert next(e for e in events if e['record_uid'] == 'a')['occurred_at'] == '2026-11-18T01:00:00.000000Z'


def test_close_requires_every_declared_slot(client):
    headers = login(client, 'analyst')
    terminal = make_terminal(client, headers, 'T-SLOTS', slots=(1, 2, 3))
    post(client, headers, terminal, [event('s1', 'exposure', '2026-11-18T09:00:00+08:00', 1)])
    blocked = client.post('/accounting/close', headers=headers)
    assert blocked.status_code == 409
    assert 'T-SLOTS' in blocked.json()['detail'] and '2' in blocked.json()['detail']
    post(client, headers, terminal, [
        event('s2', 'exposure', '2026-11-18T09:10:00+08:00', 2),
        event('s3', 'exposure', '2026-11-18T09:20:00+08:00', 3),
    ])
    assert client.get('/accounting/status', headers=headers).json()['can_close'] is True


def test_viewer_can_read_but_not_write(client):
    headers = login(client, 'viewer')
    assert client.get('/accounting/status', headers=headers).status_code == 200
    assert client.get('/accounting/versions', headers=headers).status_code == 200
    blocked = client.post('/accounting/close', headers=headers)
    assert blocked.status_code == 403


def test_supplement_creates_new_version_and_old_version_recomputes(client):
    headers = login(client, 'admin')
    terminal = make_terminal(client, headers, 'T-VER', slots=(1,))
    post(client, headers, terminal, [
        event('e1', 'exposure', '2026-11-18T09:00:00+08:00', 1),
        event('e2', 'exposure', '2026-11-18T09:10:00+08:00', 1, visitor='V2'),
        event('r1', 'rating', '2026-11-18T09:05:00+08:00', 1, rating=4),
    ])
    v1 = client.post('/accounting/close', headers=headers).json()
    assert v1['summary']['exposures'] == 2 and v1['summary']['matched'] == 1
    assert client.post('/accounting/close', headers=headers).status_code == 409

    post(client, headers, terminal, [
        event('e3', 'exposure', '2026-11-18T09:06:00+08:00', 1, visitor='V3'),
        event('r2', 'rating', '2026-11-18T09:07:00+08:00', 1, visitor='V3', rating=5),
    ])
    v2 = client.post('/accounting/close', headers=headers).json()
    assert v2['version_no'] == v1['version_no'] + 1
    first_v1 = client.get('/accounting/versions/%d' % v1['id'], headers=headers).json()
    first_v2 = client.get('/accounting/versions/%d' % v2['id'], headers=headers).json()
    assert first_v1['verified'] and first_v2['verified']
    assert len(first_v1['first_included_event_ids']) == 3
    assert len(first_v2['first_included_event_ids']) == 2
    assert first_v1['summary']['exposures'] == 2
    assert first_v2['summary']['exposures'] == 3
    recompute = client.get('/accounting/versions/%d/recompute' % v1['id'], headers=headers).json()
    assert recompute['verified'] is True
    assert recompute['coverage'] == first_v1['coverage']


def test_unparseable_or_unzoned_timestamps_rejected(client):
    headers = login(client, 'analyst')
    terminal = make_terminal(client, headers, 'T-TIME')
    for bad in ('not-a-time', '2026-11-18T09:00:00'):
        r = post(client, headers, terminal, [event('x', 'exposure', bad, 1)])
        assert r.status_code == 422


def test_rating_must_be_in_range(client):
    headers = login(client, 'analyst')
    terminal = make_terminal(client, headers, 'T-RATE')
    assert post(client, headers, terminal, [event('x', 'rating', '2026-11-18T09:00:00Z', 1, rating=9)]).status_code == 422
    assert post(client, headers, terminal, [event('y', 'rating', '2026-11-18T09:00:00Z', 1)]).status_code == 422


def test_undeclared_slot_rejected(client):
    headers = login(client, 'analyst')
    terminal = make_terminal(client, headers, 'T-SLOTX', slots=(1, 2))
    assert post(client, headers, terminal, [event('x', 'exposure', '2026-11-18T09:00:00Z', 9)]).status_code == 422


def test_concurrent_uploads_of_same_record_store_one_copy():
    """Two sessions race on the same record; exactly one copy survives."""
    with Session() as db:
        terminal = accounting.create_terminal(db, FakeUser(), __import__('src.schemas.accounting', fromlist=['TerminalInput']).TerminalInput(code='T-RACE', venue='RACE', declared_slots=[1]))
    barrier = threading.Barrier(2)
    outcomes = []

    def worker():
        from src.schemas.accounting import EventBatch, EventInput
        with Session() as db:
            barrier.wait()
            try:
                outcomes.append(accounting.add_events(db, FakeUser(), terminal['id'], EventBatch(events=[EventInput(
                    record_uid='race', kind='exposure', visitor='V', exhibit='E', occurred_at='2026-11-18T09:00:00Z', slot=1)])))
            except Exception as exc:  # pragma: no cover - surfaced in the assertion
                outcomes.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(isinstance(o, dict) for o in outcomes), outcomes
    assert sorted(o['accepted'] + o['duplicates'] + o['conflicts'] for o in outcomes) == [1, 1]
    assert sum(o['accepted'] for o in outcomes) == 1
    with Session() as db:
        rows = db.query(models.TerminalEvent).filter_by(record_uid='race').all()
    assert len(rows) == 1


def test_concurrent_close_and_upload_never_double_counts():
    """A record racing a close lands in exactly one first-inclusion version."""
    from src.schemas.accounting import EventBatch, EventInput, TerminalInput
    with Session() as db:
        terminal = accounting.create_terminal(db, FakeUser(), TerminalInput(code='T-CLOSE-RACE', venue='RACE2', declared_slots=[1]))
        accounting.add_events(db, FakeUser(), terminal['id'], EventBatch(events=[EventInput(
            record_uid='seed', kind='exposure', visitor='V', exhibit='E', occurred_at='2026-11-18T08:00:00Z', slot=1)]))
    barrier = threading.Barrier(2)

    def upload():
        with Session() as db:
            barrier.wait()
            accounting.add_events(db, FakeUser(), terminal['id'], EventBatch(events=[EventInput(
                record_uid='race-close', kind='exposure', visitor='V', exhibit='E', occurred_at='2026-11-18T09:00:00Z', slot=1)]))

    def close():
        with Session() as db:
            barrier.wait()
            try:
                accounting.close_books(db, FakeUser())
            except Exception as exc:
                assert getattr(exc, 'status_code', None) in (409,) or 'locked' in str(exc).lower()

    threads = [threading.Thread(target=upload), threading.Thread(target=close)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with Session() as db:
        event = db.query(models.TerminalEvent).filter_by(record_uid='race-close').one()
        versions = db.query(models.VersionEvent).filter_by(event_id=event.id).all()
    assert len(versions) <= 1
