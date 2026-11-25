from dataclasses import dataclass
import json
import sqlite3

from issue.envelope import IssuedEnvelope, decode_envelope, encode_envelope
from issue.shift_rules import Rule, parse_rules, rules_to_json
from ledger import record_id, write_transaction
from protocol.catalog import require_text


@dataclass(frozen=True, slots=True)
class ShiftRegistration:
    shift_id: int
    protocol_issue_id: int
    revision_id: int
    protocol_body_sha256: str
    rules: tuple[Rule, ...]
    fallback: str
    run_boundary: int
    summary_issue_id: int | None


class IssueRegister:
    def __init__(self, connection: sqlite3.Connection, site: str):
        self.connection = connection
        self.site = require_text(site, 'issuing site')

    def initialize(self) -> None:
        self.connection.execute('''CREATE TABLE IF NOT EXISTS issued_pieces (
            site TEXT NOT NULL,
            issue_id INTEGER NOT NULL,
            revision_id INTEGER NOT NULL REFERENCES revisions(revision_id),
            raw BLOB NOT NULL,
            PRIMARY KEY(site, issue_id)
        )''')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS shifts (
            shift_id INTEGER PRIMARY KEY,
            protocol_issue_id INTEGER NOT NULL,
            revision_id INTEGER NOT NULL,
            protocol_body_sha256 TEXT NOT NULL,
            rules_json TEXT NOT NULL,
            fallback TEXT NOT NULL,
            run_boundary INTEGER NOT NULL,
            summary_issue_id INTEGER
        )''')

    def _issue_locked(self, revision_id: int, body: bytes, version: int) -> IssuedEnvelope:
        last_id, = self.connection.execute('''SELECT COALESCE(MAX(issue_id), 0)
            FROM issued_pieces WHERE site = ?''', (self.site,)).fetchone()
        issue_id = record_id(last_id + 1)
        raw = encode_envelope(self.site, issue_id, revision_id, body, version=version)
        envelope = decode_envelope(raw)
        self.connection.execute('''INSERT INTO issued_pieces(site, issue_id, revision_id, raw)
            VALUES (?, ?, ?, ?)''', (self.site, issue_id, revision_id, raw))
        return envelope

    def create(self, revision_id: int, body: bytes) -> IssuedEnvelope:
        record_id(revision_id)
        with write_transaction(self.connection):
            return self._issue_locked(revision_id, body, 1)

    def read(self, issue_id: int) -> IssuedEnvelope:
        row = self.connection.execute('''SELECT raw FROM issued_pieces
            WHERE site = ? AND issue_id = ?''', (self.site, record_id(issue_id))).fetchone()
        if row is None:
            raise KeyError(issue_id)
        return decode_envelope(row[0])

    def add_shift(self, protocol: IssuedEnvelope, rules: tuple[Rule, ...], fallback: str) -> int:
        rules_json = json.dumps(rules_to_json(rules), ensure_ascii=False)
        boundary, = self.connection.execute(
            'SELECT COALESCE(MAX(run_id), 0) FROM observed_runs').fetchone()
        with write_transaction(self.connection):
            cursor = self.connection.execute('''INSERT INTO shifts(
                protocol_issue_id, revision_id, protocol_body_sha256, rules_json, fallback, run_boundary
            ) VALUES (?, ?, ?, ?, ?, ?)''', (protocol.issue_id, protocol.revision_id,
                                             protocol.body_sha256, rules_json, fallback, boundary))
            return record_id(cursor.lastrowid)

    def read_shift(self, shift_id: int) -> ShiftRegistration:
        row = self.connection.execute('''SELECT protocol_issue_id, revision_id, protocol_body_sha256,
            rules_json, fallback, run_boundary, summary_issue_id FROM shifts
            WHERE shift_id = ?''', (record_id(shift_id),)).fetchone()
        if row is None:
            raise KeyError(shift_id)
        issue_id, revision_id, digest, rules_json, fallback, boundary, summary_issue_id = row
        rules, fallback = parse_rules(json.loads(rules_json), fallback)
        return ShiftRegistration(shift_id, issue_id, revision_id, digest, rules, fallback,
                                 boundary, summary_issue_id)

    def fix_shift_summary(self, shift_id: int, revision_id: int, body: bytes) -> IssuedEnvelope:
        record_id(shift_id)
        record_id(revision_id)
        with write_transaction(self.connection):
            row = self.connection.execute('''SELECT summary_issue_id FROM shifts
                WHERE shift_id = ?''', (shift_id,)).fetchone()
            if row is None:
                raise KeyError(shift_id)
            if row[0] is not None:
                raise ValueError(f'shift {shift_id} is already fixed')
            envelope = self._issue_locked(revision_id, body, 2)
            self.connection.execute('''UPDATE shifts SET summary_issue_id = ?
                WHERE shift_id = ?''', (envelope.issue_id, shift_id))
            return envelope
