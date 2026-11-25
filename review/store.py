from dataclasses import dataclass
from fractions import Fraction
import json
import sqlite3

from intake.selection import AcceptedPair
from intake.store import AcceptedPiece
from ledger import record_id, write_transaction
from protocol.catalog import require_text
from review.length import AgreementStatistics


@dataclass(frozen=True, slots=True)
class ReportParent:
    intake_id: int
    site: str
    issue_id: int
    revision_id: int
    body_sha256: str

    @classmethod
    def from_piece(cls, piece: AcceptedPiece) -> 'ReportParent':
        envelope = piece.envelope
        return cls(piece.intake_id, envelope.site, envelope.issue_id, envelope.revision_id, envelope.body_sha256)


@dataclass(frozen=True, slots=True)
class SignedReport:
    report_id: int
    statistics: AgreementStatistics
    parents: tuple[ReportParent, ReportParent]
    receipt: str


@dataclass(frozen=True, slots=True)
class SignedShiftReport:
    report_id: int
    shift_id: int
    rows: tuple[tuple[str, int, Fraction, Fraction], ...]
    parents: tuple[ReportParent, ReportParent]
    receipt: str


def _check_shift_row(row) -> tuple[str, int, Fraction, Fraction]:
    if not isinstance(row, (list, tuple)) or len(row) != 4:
        raise ValueError('a shift report row must hold category, measured, expected, difference')
    category, measured, expected, difference = row
    require_text(category, 'category name')
    if type(measured) is not int or measured < 0:
        raise ValueError('measured count must be a nonnegative integer')
    if not isinstance(expected, Fraction) or not isinstance(difference, Fraction):
        raise ValueError('expected and difference must be exact Fraction values')
    if measured - expected != difference:
        raise ValueError('difference must equal measured minus expected')
    return (category, measured, expected, difference)


class ReportStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def initialize(self) -> None:
        self.connection.execute('''CREATE TABLE IF NOT EXISTS reports (
            report_id INTEGER PRIMARY KEY,
            statistics_json TEXT NOT NULL
        )''')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS report_parents (
            report_id INTEGER NOT NULL REFERENCES reports(report_id),
            position INTEGER NOT NULL CHECK(position IN (0, 1)),
            intake_id INTEGER NOT NULL REFERENCES accepted_pieces(intake_id),
            site TEXT NOT NULL,
            issue_id INTEGER NOT NULL,
            revision_id INTEGER NOT NULL,
            body_sha256 TEXT NOT NULL,
            PRIMARY KEY(report_id, position)
        )''')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS report_receipts (
            report_id INTEGER PRIMARY KEY REFERENCES reports(report_id),
            receipt TEXT NOT NULL UNIQUE
        )''')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS shift_reports (
            report_id INTEGER PRIMARY KEY,
            shift_id INTEGER NOT NULL,
            rows_json TEXT NOT NULL
        )''')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS shift_report_parents (
            report_id INTEGER NOT NULL REFERENCES shift_reports(report_id),
            position INTEGER NOT NULL CHECK(position IN (0, 1)),
            intake_id INTEGER NOT NULL REFERENCES accepted_pieces(intake_id),
            site TEXT NOT NULL,
            issue_id INTEGER NOT NULL,
            revision_id INTEGER NOT NULL,
            body_sha256 TEXT NOT NULL,
            PRIMARY KEY(report_id, position)
        )''')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS shift_report_receipts (
            report_id INTEGER PRIMARY KEY REFERENCES shift_reports(report_id),
            receipt TEXT NOT NULL UNIQUE
        )''')

    def commit_report(self, statistics: AgreementStatistics, pair: AcceptedPair) -> SignedReport:
        parents = (ReportParent.from_piece(pair.left), ReportParent.from_piece(pair.right))
        with write_transaction(self.connection):
            cursor = self.connection.execute('INSERT INTO reports(statistics_json) VALUES (?)',
                                             (json.dumps(statistics.to_record()),))
            report_id = cursor.lastrowid
            self.connection.executemany('''INSERT INTO report_parents(
                report_id, position, intake_id, site, issue_id, revision_id, body_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?)''', [
                (report_id, position, parent.intake_id, parent.site, parent.issue_id,
                 parent.revision_id, parent.body_sha256)
                for position, parent in enumerate(parents)
            ])
            receipt = f'R-{report_id}'
            self.connection.execute('INSERT INTO report_receipts(report_id, receipt) VALUES (?, ?)',
                                    (report_id, receipt))
        return SignedReport(report_id, statistics, parents, receipt)

    def read(self, report_id: int) -> SignedReport:
        row = self.connection.execute('''SELECT r.statistics_json, s.receipt
            FROM reports AS r JOIN report_receipts AS s USING(report_id)
            WHERE r.report_id = ?''', (record_id(report_id),)).fetchone()
        if row is None:
            raise KeyError(report_id)
        statistics, receipt = row
        parents = tuple(ReportParent(*record) for record in self.connection.execute('''
            SELECT intake_id, site, issue_id, revision_id, body_sha256
            FROM report_parents WHERE report_id = ? ORDER BY position''', (report_id,)))
        if len(parents) != 2:
            raise sqlite3.DatabaseError('signed report must have two stored parent positions')
        return SignedReport(report_id, AgreementStatistics.from_record(json.loads(statistics)), parents, receipt)

    def commit_shift_report(self, shift_id: int, rows, protocol_piece: AcceptedPiece,
                            summary_piece: AcceptedPiece) -> SignedShiftReport:
        record_id(shift_id)
        checked = tuple(_check_shift_row(row) for row in rows)
        parents = (ReportParent.from_piece(protocol_piece), ReportParent.from_piece(summary_piece))
        record = [[category, measured, str(expected), str(difference)]
                  for category, measured, expected, difference in checked]
        with write_transaction(self.connection):
            cursor = self.connection.execute('INSERT INTO shift_reports(shift_id, rows_json) VALUES (?, ?)',
                                             (shift_id, json.dumps(record, ensure_ascii=False)))
            report_id = cursor.lastrowid
            self.connection.executemany('''INSERT INTO shift_report_parents(
                report_id, position, intake_id, site, issue_id, revision_id, body_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?)''', [
                (report_id, position, parent.intake_id, parent.site, parent.issue_id,
                 parent.revision_id, parent.body_sha256)
                for position, parent in enumerate(parents)
            ])
            receipt = f'SR-{report_id}'
            self.connection.execute('INSERT INTO shift_report_receipts(report_id, receipt) VALUES (?, ?)',
                                    (report_id, receipt))
        return SignedShiftReport(report_id, shift_id, checked, parents, receipt)

    def read_shift_report(self, report_id: int) -> SignedShiftReport:
        row = self.connection.execute('''SELECT r.shift_id, r.rows_json, s.receipt
            FROM shift_reports AS r JOIN shift_report_receipts AS s USING(report_id)
            WHERE r.report_id = ?''', (record_id(report_id),)).fetchone()
        if row is None:
            raise KeyError(report_id)
        shift_id, rows_json, receipt = row
        parents = tuple(ReportParent(*record) for record in self.connection.execute('''
            SELECT intake_id, site, issue_id, revision_id, body_sha256
            FROM shift_report_parents WHERE report_id = ? ORDER BY position''', (report_id,)))
        if len(parents) != 2:
            raise sqlite3.DatabaseError('signed shift report must have two stored parent positions')
        rows = tuple((category, measured, Fraction(expected), Fraction(difference))
                     for category, measured, expected, difference in json.loads(rows_json))
        return SignedShiftReport(report_id, shift_id, rows, parents, receipt)
