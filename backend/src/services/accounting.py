import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..models.accounting import AccountLock, AccountVersion, Terminal, TerminalEvent, VersionEvent
from ..utils.serialization import now
from .audit import log

MATCH_RULE = 'nearest_prior_exposure_v1'
KINDS = ('exposure', 'rating')


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _norm_time(raw):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('发生时间不能为空')
    txt = raw.strip()
    if txt.endswith(('Z', 'z')):
        txt = txt[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(txt)
    except ValueError:
        raise ValueError('发生时间需为ISO8601格式')
    if parsed.tzinfo is None:
        raise ValueError('发生时间必须包含时区，例如 +08:00')
    return parsed.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _text(value, label, limit=120):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + '不能为空')
    value = value.strip()
    if len(value) > limit:
        raise ValueError(label + '最多' + str(limit) + '字符')
    return value


def _payload_hash(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    ).hexdigest()


def _checksum(snapshot):
    body = {k: v for k, v in snapshot.items() if k != 'checksum'}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    ).hexdigest()


def _ensure_lock(db):
    if db.get(AccountLock, 1) is not None:
        return
    try:
        with db.begin_nested():
            db.add(AccountLock(id=1))
            db.flush()
    except IntegrityError:
        pass


def ensure_lock_row(db):
    _ensure_lock(db)
    db.commit()


def _acquire_write(db):
    """Serialize intake and close on the singleton lock row.

    Every insert and every close passes through here, so a record is either
    visible to a close or deferred to the next version, never in both. The
    locking read is a current read, so it neither pins a MySQL REPEATABLE READ
    snapshot nor needs the row to pre-exist.
    """
    locked = db.execute(select(AccountLock.id).where(AccountLock.id == 1).with_for_update()).first()
    if locked is None:
        try:
            with db.begin_nested():
                db.add(AccountLock(id=1))
                db.flush()
        except IntegrityError:
            pass
        db.execute(select(AccountLock.id).where(AccountLock.id == 1).with_for_update())


def _terminal_out(t, events):
    received = sorted({e['slot'] for e in events})
    declared = sorted(set(t.slots or []))
    missing = [s for s in declared if s not in received]
    return {
        'id': t.id,
        'code': t.code,
        'name': t.name,
        'venue': t.venue,
        'declared_slots': declared,
        'received_slots': received,
        'missing_slots': missing,
        'complete': not missing,
        'event_count': len(events),
    }


def _event_dict(e, terminal_code=None):
    return {
        'id': e.id,
        'terminal_id': e.terminal_id,
        'terminal': terminal_code,
        'record_uid': e.record_uid,
        'kind': e.kind,
        'venue': e.venue,
        'visitor': e.visitor,
        'exhibit': e.exhibit,
        'occurred_at': e.occurred_at,
        'slot': e.slot,
        'rating': e.rating,
        'received_at': e.received_at,
        'payload_hash': e.payload_hash,
    }


def compute_matches(events):
    """Assign every rating to its nearest earlier exposure of the same venue,
    visitor and exhibit (occurrence time, then event id, decides ties).

    A rating with no earlier exposure is pending. Because nothing is consumed,
    a late-arriving exposure can become the nearer match and change a rating's
    attribution on the next recomputation."""
    exposures = defaultdict(list)
    for event in events:
        if event['kind'] == 'exposure':
            exposures[(event['venue'], event['visitor'], event['exhibit'])].append(event)
    ratings = sorted(
        (e for e in events if e['kind'] == 'rating'),
        key=lambda e: (e['occurred_at'], e['id']),
    )
    matches = []
    for rating in ratings:
        key = (rating['venue'], rating['visitor'], rating['exhibit'])
        chosen = None
        for exposure in exposures.get(key, ()):
            if exposure['occurred_at'] > rating['occurred_at']:
                continue
            if chosen is None or (exposure['occurred_at'], exposure['id']) > (chosen['occurred_at'], chosen['id']):
                chosen = exposure
        matches.append({
            'rating_event_id': rating['id'],
            'exposure_event_id': chosen['id'] if chosen else None,
            'status': 'matched' if chosen else 'pending',
        })
    return matches


def compute_coverage(events, matches):
    exposure_by_exhibit = defaultdict(int)
    for event in events:
        if event['kind'] == 'exposure':
            exposure_by_exhibit[event['exhibit']] += 1
    event_by_id = {e['id']: e for e in events}
    rated_by_exhibit = defaultdict(set)
    for match in matches:
        if match['exposure_event_id'] is None:
            continue
        exposure = event_by_id.get(match['exposure_event_id'])
        if exposure:
            rated_by_exhibit[exposure['exhibit']].add(exposure['id'])
    rows = []
    for exhibit in sorted(exposure_by_exhibit):
        total = exposure_by_exhibit[exhibit]
        rated = len(rated_by_exhibit.get(exhibit, ()))
        rows.append({
            'exhibit': exhibit,
            'exposures': total,
            'rated_exposures': rated,
            'ratio': round(rated / total, 6) if total else 0.0,
        })
    return rows


def _summary(events, matches, coverage, terminal_count):
    exposures = sum(1 for e in events if e['kind'] == 'exposure')
    ratings = sum(1 for e in events if e['kind'] == 'rating')
    matched = sum(1 for m in matches if m['status'] == 'matched')
    rated = sum(row['rated_exposures'] for row in coverage)
    return {
        'events': len(events),
        'exposures': exposures,
        'ratings': ratings,
        'matched': matched,
        'pending': ratings - matched,
        'rated_exposures': rated,
        'coverage_ratio': round(rated / exposures, 6) if exposures else 0.0,
        'terminals': terminal_count,
    }


def _load_events(db, with_lock=False):
    stmt = select(TerminalEvent).order_by(TerminalEvent.id)
    if with_lock:
        # A locking read is always a current read, so MySQL REPEATABLE READ cannot
        # return a snapshot taken before the exclusive lock was granted.
        stmt = stmt.with_for_update()
    return list(db.scalars(stmt))


def _load_terminals(db, with_lock=False):
    stmt = select(Terminal).order_by(Terminal.id)
    if with_lock:
        stmt = stmt.with_for_update()
    return list(db.scalars(stmt))


def list_terminals(db):
    terminals = _load_terminals(db)
    events = [_event_dict(e) for e in _load_events(db)]
    return [_terminal_out(t, [e for e in events if e['terminal_id'] == t.id]) for t in terminals]


def create_terminal(db, user, data):
    code = data.code.strip()
    if db.scalar(select(Terminal.id).where(Terminal.code == code)):
        raise HTTPException(409, '终端编号已存在：' + code)
    slots = sorted({int(s) for s in data.declared_slots})
    if not slots or any(s < 1 for s in slots):
        raise HTTPException(422, '申报时段序号需为大于0的整数')
    terminal = Terminal(
        code=code,
        venue=data.venue.strip(),
        name=(data.name or '').strip(),
        slots=slots,
        created_at=now(),
    )
    try:
        with db.begin_nested():
            db.add(terminal)
            db.flush()
    except IntegrityError:
        raise HTTPException(409, '终端编号已存在：' + code)
    log(db, user, 'terminal.created', terminal.id, code + ' · 时段 ' + ','.join(map(str, slots)))
    db.commit()
    return row_terminal(terminal)


def row_terminal(terminal):
    return {
        'id': terminal.id,
        'code': terminal.code,
        'name': terminal.name,
        'venue': terminal.venue,
        'declared_slots': sorted(set(terminal.slots or [])),
        'created_at': terminal.created_at,
    }


def _normalize_event(db, terminal, event):
    kind = event.kind
    if kind not in KINDS:
        raise ValueError('记录类型需为 exposure 或 rating')
    venue = event.venue.strip() if isinstance(event.venue, str) and event.venue.strip() else terminal.venue
    if venue != terminal.venue:
        raise ValueError('场地与终端登记不一致：' + terminal.venue)
    rating = None
    if kind == 'rating':
        if event.rating is None or isinstance(event.rating, bool):
            raise ValueError('评分记录必须填写评分')
        try:
            rating = float(event.rating)
        except (TypeError, ValueError):
            raise ValueError('评分必须为数值')
        if not math.isfinite(rating) or not 1 <= rating <= 5:
            raise ValueError('评分需在1–5之间')
    elif event.rating is not None:
        raise ValueError('曝光记录不应填写评分')
    if isinstance(event.slot, bool) or not isinstance(event.slot, int):
        raise ValueError('时段序号需为整数')
    slot = event.slot
    declared = sorted(set(terminal.slots or []))
    if slot not in declared:
        raise ValueError('时段序号 ' + str(slot) + ' 未由该终端申报')
    payload = {
        'record_uid': _text(event.record_uid, '记录编号'),
        'kind': kind,
        'venue': venue,
        'visitor': _text(event.visitor, '匿名访客'),
        'exhibit': _text(event.exhibit, '展品代号'),
        'occurred_at': _norm_time(event.occurred_at),
        'slot': slot,
        'rating': rating,
    }
    payload['payload_hash'] = _payload_hash(payload)
    return payload


def _first_version_no(db, event_id):
    link = db.get(VersionEvent, event_id)
    if not link:
        return None
    version = db.get(AccountVersion, link.version_id)
    return version.version_no if version else None


def add_events(db, user, terminal_id, data):
    _acquire_write(db)
    terminal = db.get(Terminal, terminal_id)
    if not terminal:
        raise HTTPException(404, '未找到终端')
    normalized = []
    for event in data.events:
        try:
            normalized.append(_normalize_event(db, terminal, event))
        except ValueError as exc:
            raise HTTPException(422, str(exc))
    results = []
    accepted = 0
    duplicates = 0
    conflicts = 0
    received_at = _now_iso()

    def existing_for(uid):
        # Locking read sees the latest committed row even under MySQL REPEATABLE READ.
        return db.scalar(
            select(TerminalEvent)
            .where(TerminalEvent.terminal_id == terminal.id, TerminalEvent.record_uid == uid)
            .with_for_update()
        )

    for payload in normalized:
        existing = existing_for(payload['record_uid'])
        if existing:
            if existing.payload_hash == payload['payload_hash']:
                duplicates += 1
                results.append({
                    'record_uid': payload['record_uid'],
                    'status': 'duplicate',
                    'event_id': existing.id,
                    'first_version': _first_version_no(db, existing.id),
                })
            else:
                conflicts += 1
                results.append({
                    'record_uid': payload['record_uid'],
                    'status': 'conflict',
                    'event_id': existing.id,
                    'first_version': _first_version_no(db, existing.id),
                    'detail': '同编号记录已按首次收件留存，本次内容未覆盖',
                })
            continue
        event = TerminalEvent(
            terminal_id=terminal.id,
            record_uid=payload['record_uid'],
            kind=payload['kind'],
            venue=payload['venue'],
            visitor=payload['visitor'],
            exhibit=payload['exhibit'],
            occurred_at=payload['occurred_at'],
            slot=payload['slot'],
            rating=payload['rating'],
            received_at=received_at,
            payload_hash=payload['payload_hash'],
        )
        try:
            with db.begin_nested():
                db.add(event)
                db.flush()
        except IntegrityError:
            # A concurrent upload of the same record won; keep its first copy.
            existing = existing_for(payload['record_uid'])
            if existing and existing.payload_hash == payload['payload_hash']:
                duplicates += 1
                results.append({
                    'record_uid': payload['record_uid'],
                    'status': 'duplicate',
                    'event_id': existing.id,
                    'first_version': _first_version_no(db, existing.id),
                })
            else:
                conflicts += 1
                results.append({
                    'record_uid': payload['record_uid'],
                    'status': 'conflict',
                    'event_id': existing.id if existing else None,
                    'first_version': _first_version_no(db, existing.id) if existing else None,
                    'detail': '并发补传已按首次收件留存，本次内容未覆盖',
                })
            continue
        accepted += 1
        results.append({
            'record_uid': event.record_uid,
            'status': 'accepted',
            'event_id': event.id,
            'first_version': None,
        })
    log(db, user, 'terminal.events.received', terminal.id,
        '新增 ' + str(accepted) + ' · 重复 ' + str(duplicates) + ' · 冲突 ' + str(conflicts))
    db.commit()
    return {
        'accepted': accepted,
        'duplicates': duplicates,
        'conflicts': conflicts,
        'results': results,
    }


def _terminal_snapshot(terminals, events):
    out = []
    for terminal in terminals:
        out.append(_terminal_out(
            terminal,
            [e for e in events if e['terminal_id'] == terminal.id],
        ))
    return out


def _snapshot(version_no, closed_at, closed_by, events, first_ids, terminals):
    terminal_rows = _terminal_snapshot(terminals, events)
    matches = compute_matches(events)
    coverage = compute_coverage(events, matches)
    snapshot = {
        'version_no': version_no,
        'closed_at': closed_at,
        'closed_by': closed_by,
        'rule': MATCH_RULE,
        'terminals': terminal_rows,
        'events': events,
        'first_included_event_ids': sorted(first_ids),
        'matches': matches,
        'coverage': coverage,
        'summary': _summary(events, matches, coverage, len(terminals)),
    }
    snapshot['checksum'] = _checksum(snapshot)
    return snapshot


def _missing_summary(terminals):
    parts = []
    for terminal in terminals:
        if terminal['missing_slots']:
            parts.append(terminal['code'] + ' 缺 ' + ','.join(map(str, terminal['missing_slots'])))
    return '时段未收齐：' + '；'.join(parts)


def close_books(db, user):
    _acquire_write(db)
    terminals = _load_terminals(db, with_lock=True)
    events = [_event_dict(e) for e in _load_events(db, with_lock=True)]
    terminal_rows = _terminal_snapshot(terminals, events)
    if not terminals:
        raise HTTPException(409, '还没有登记任何终端')
    if any(not row['complete'] for row in terminal_rows):
        raise HTTPException(409, _missing_summary(terminal_rows))
    assigned = set(db.scalars(select(VersionEvent.event_id)))
    fresh = [event for event in events if event['id'] not in assigned]
    if not fresh:
        raise HTTPException(409, '没有新的收录记录，无需封账')
    latest = db.scalar(select(func.max(AccountVersion.version_no))) or 0
    closed_at = _now_iso()
    snapshot = _snapshot(latest + 1, closed_at, getattr(user, 'email', str(user)), events,
                         [event['id'] for event in fresh], terminals)
    version = AccountVersion(
        version_no=latest + 1,
        closed_at=closed_at,
        closed_by=getattr(user, 'email', str(user)),
        snapshot=snapshot,
        checksum=snapshot['checksum'],
    )
    db.add(version)
    db.flush()
    for event in fresh:
        db.add(VersionEvent(event_id=event['id'], version_id=version.id))
    log(db, user, 'account.closed', version.id,
        '版本 ' + str(version.version_no) + ' · 首次收录 ' + str(len(fresh)) + ' 条')
    db.commit()
    return row_version(version)


def verify_snapshot(snapshot):
    events = snapshot.get('events') or []
    matches = compute_matches(events)
    coverage = compute_coverage(events, matches)
    return (
        matches == snapshot.get('matches')
        and coverage == snapshot.get('coverage')
        and _checksum(snapshot) == snapshot.get('checksum')
    )


def row_version(version, include_snapshot=False):
    snapshot = version.snapshot or {}
    out = {
        'id': version.id,
        'version_no': version.version_no,
        'closed_at': version.closed_at,
        'closed_by': version.closed_by,
        'checksum': version.checksum,
        'summary': snapshot.get('summary'),
        'coverage': snapshot.get('coverage', []),
        'first_included_event_ids': snapshot.get('first_included_event_ids', []),
    }
    if include_snapshot:
        out['snapshot'] = snapshot
        out['verified'] = verify_snapshot(snapshot)
    return out


def list_versions(db):
    return [
        row_version(version)
        for version in db.scalars(select(AccountVersion).order_by(AccountVersion.version_no.desc()))
    ]


def get_version(db, id):
    version = db.get(AccountVersion, id)
    if not version:
        raise HTTPException(404, '未找到账册版本')
    return row_version(version, include_snapshot=True)


def recompute_version(db, id):
    version = db.get(AccountVersion, id)
    if not version:
        raise HTTPException(404, '未找到账册版本')
    snapshot = version.snapshot or {}
    matches = compute_matches(snapshot.get('events') or [])
    coverage = compute_coverage(snapshot.get('events') or [], matches)
    return {
        'id': version.id,
        'version_no': version.version_no,
        'verified': verify_snapshot(snapshot),
        'matches': matches,
        'coverage': coverage,
    }


def _pending_rows(events, matches):
    by_id = {e['id']: e for e in events}
    rows = []
    for match in matches:
        if match['status'] != 'pending':
            continue
        event = by_id.get(match['rating_event_id'])
        if event:
            rows.append({
                'event_id': event['id'],
                'record_uid': event['record_uid'],
                'terminal': event.get('terminal'),
                'visitor': event['visitor'],
                'exhibit': event['exhibit'],
                'occurred_at': event['occurred_at'],
                'received_at': event['received_at'],
                'slot': event['slot'],
                'rating': event['rating'],
            })
    return sorted(rows, key=lambda row: (row['occurred_at'], row['event_id']))


def status(db):
    terminals = _load_terminals(db)
    raw_events = _load_events(db)
    terminal_codes = {t.id: t.code for t in terminals}
    events = [_event_dict(e, terminal_codes.get(e.terminal_id)) for e in raw_events]
    terminal_rows = _terminal_snapshot(terminals, events)
    matches = compute_matches(events)
    coverage = compute_coverage(events, matches)
    assigned = set(db.scalars(select(VersionEvent.event_id)))
    unclosed = [e for e in events if e['id'] not in assigned]
    latest = db.scalar(select(func.max(AccountVersion.version_no)))
    return {
        'terminals': terminal_rows,
        'all_slots_received': all(row['complete'] for row in terminal_rows) if terminals else False,
        'unclosed_events': len(unclosed),
        'pending_ratings': _pending_rows(events, matches),
        'coverage': coverage,
        'summary': _summary(events, matches, coverage, len(terminals)),
        'latest_version_no': latest,
        'can_close': bool(terminals) and all(row['complete'] for row in terminal_rows) and bool(unclosed),
    }


def list_events(db, limit=50):
    codes = {t.id: t.code for t in _load_terminals(db)}
    raw_events = _load_events(db)
    events = [_event_dict(e, codes.get(e.terminal_id)) for e in raw_events]
    matches = compute_matches(events)
    rating_status = {m['rating_event_id']: m for m in matches}
    rated_exposures = {m['exposure_event_id'] for m in matches if m['exposure_event_id'] is not None}
    assigned = set(db.scalars(select(VersionEvent.event_id)))
    out = []
    for event in sorted(events, key=lambda e: e['id'], reverse=True)[:max(1, min(limit, 200))]:
        item = dict(event)
        item['rated'] = event['id'] in rated_exposures
        item['match_status'] = rating_status.get(event['id'], {}).get('status') if event['kind'] == 'rating' else None
        item['first_version'] = _first_version_no(db, event['id'])
        item['included'] = event['id'] in assigned
        out.append(item)
    return out
