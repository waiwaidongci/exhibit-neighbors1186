from sqlalchemy import Column,Integer,String,Float,JSON,ForeignKey,UniqueConstraint,Index
from ..config.database import Base

class Terminal(Base):
    __tablename__='terminals'
    id=Column(Integer,primary_key=True)
    code=Column(String(60),unique=True,nullable=False)
    venue=Column(String(120),nullable=False)
    name=Column(String(160),default='')
    slots=Column(JSON,nullable=False)
    created_at=Column(String(40),nullable=False)

class TerminalEvent(Base):
    """First received copy of one terminal record.

    (terminal_id, record_uid) is the natural key. A retry or a second offline
    upload of the same record never creates another row and never overwrites
    the first received payload.
    """
    __tablename__='terminal_events'
    __table_args__=(UniqueConstraint('terminal_id','record_uid',name='uq_terminal_event_uid'),Index('ix_terminal_event_match','venue','visitor','exhibit','occurred_at'))
    id=Column(Integer,primary_key=True)
    terminal_id=Column(Integer,ForeignKey('terminals.id'),nullable=False)
    record_uid=Column(String(120),nullable=False)
    kind=Column(String(20),nullable=False)              # exposure | rating
    venue=Column(String(120),nullable=False)
    visitor=Column(String(120),nullable=False)
    exhibit=Column(String(120),nullable=False)
    occurred_at=Column(String(40),nullable=False)       # normalized UTC ISO text
    slot=Column(Integer,nullable=False)
    rating=Column(Float,nullable=True)
    received_at=Column(String(40),nullable=False)
    payload_hash=Column(String(64),nullable=False)

class AccountVersion(Base):
    __tablename__='account_versions'
    id=Column(Integer,primary_key=True)
    version_no=Column(Integer,unique=True,nullable=False)
    closed_at=Column(String(40),nullable=False)
    closed_by=Column(String(120),nullable=False)
    snapshot=Column(JSON,nullable=False)
    checksum=Column(String(64),nullable=False)

class VersionEvent(Base):
    """The one and only version in which an event was first received.

    event_id is the primary key, so even a concurrent double close or a buggy
    retry cannot put the same record into two first-inclusion versions.
    """
    __tablename__='account_version_events'
    event_id=Column(Integer,ForeignKey('terminal_events.id'),primary_key=True)
    version_id=Column(Integer,ForeignKey('account_versions.id'),nullable=False,index=True)

class AccountLock(Base):
    """Singleton row used to serialize close against concurrent supplements."""
    __tablename__='account_lock'
    id=Column(Integer,primary_key=True)
