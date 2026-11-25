from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from intake.receive import PieceConflict, receive_piece
from issue.envelope import encode_envelope
from issue.freeze import issue_revision, issue_shift_summary, register_shift
from issue.register import IssueRegister
from issue.retrieve import retrieve_piece
from issue.shift_body import decode_shift_body
from protocol.catalog import build_catalog
from protocol.revisions import RevisionStore, seal_revision
from protocol.transitions import Transition, TransitionGraph, transition_from_record
from review.history import read_report, read_shift_report
from review.publish import review_pair, review_shift
from runs.history import RunHistory
from runs.import_run import record_run
from runs.observations import SupportStatus
from tests.fixtures import LedgerTestCase, seal, serial_graph


SAMPLES = Path(__file__).resolve().parent.parent / 'samples'


def loop_graph():
    quarter = Fraction(1, 4)
    return TransitionGraph(('S', 'T'), 'S', (
        Transition('S', 'silent', quarter, 'S'),
        Transition('S', 'public', quarter, 'S', 'X'),
        Transition('S', 'public', quarter, 'T', 'Y'),
        Transition('S', 'end', quarter),
        Transition('T', 'end', 1),
    ))


def overlap_graph():
    half = Fraction(1, 2)
    return TransitionGraph(('S', 'A', 'B', 'C', 'D', 'T'), 'S', (
        Transition('S', 'public', half, 'A', 'X'),
        Transition('S', 'public', half, 'B', 'X'),
        Transition('A', 'public', 1, 'C', 'X'),
        Transition('B', 'public', 1, 'C', 'X'),
        Transition('C', 'public', 1, 'D', 'X'),
        Transition('D', 'public', 1, 'T', 'Y'),
        Transition('T', 'end', 1),
    ))


def geometric_graph():
    half = Fraction(1, 2)
    return TransitionGraph(('S',), 'S', (
        Transition('S', 'public', half, 'S', 'X'),
        Transition('S', 'end', half),
    ))


class ShiftCase(LedgerTestCase):
    def issue_protocol(self, site='甲站', graph=None, protocol='回路巡检'):
        source = self.station(site)
        revision_id = seal(source, graph or loop_graph(), protocol=protocol)
        register = IssueRegister(source, site)
        piece = issue_revision(register, revision_id)
        return source, register, revision_id, piece

    def close_shift(self, register, history, revision_id, protocol_issue_id, rules, fallback, runs):
        shift_id = register_shift(register, protocol_issue_id, rules, fallback)
        recorded = [record_run(history, revision_id, codes) for codes in runs]
        summary = issue_shift_summary(register, shift_id, [run.run_id for run in recorded])
        return shift_id, recorded, summary

    def prepare(self, rules, runs, fallback='其他', graph=None, site='甲站'):
        source, register, revision_id, piece = self.issue_protocol(site=site, graph=graph)
        history = RunHistory(source)
        shift_id, recorded, summary = self.close_shift(
            register, history, revision_id, piece.issue_id, rules, fallback, runs)
        center, intake, reports = self.center()
        # The summary piece is accepted before its protocol piece; the binding
        # is only checked when the shift report is signed.
        summary_accepted = receive_piece(intake, summary.raw)
        protocol_accepted = receive_piece(intake, piece.raw)
        return SimpleNamespace(
            source=source, register=register, history=history, revision_id=revision_id,
            piece=piece, shift_id=shift_id, runs=recorded, summary_envelope=summary,
            center=center, intake=intake, reports=reports,
            protocol=protocol_accepted, summary=summary_accepted,
        )

    def shift_report_counts(self, connection):
        return tuple(connection.execute(query).fetchone()[0] for query in (
            'SELECT COUNT(*) FROM shift_reports',
            'SELECT COUNT(*) FROM shift_report_parents',
            'SELECT COUNT(*) FROM shift_report_receipts',
        ))


class ShiftRegistrationTests(ShiftCase):
    def test_register_returns_shift_ids(self):
        source, register, _, piece = self.issue_protocol()
        first = register_shift(register, piece.issue_id, [['含Y', 'contains', ['Y']]], '其他')
        second = register_shift(register, piece.issue_id, [], '其他')
        self.assertEqual((first, second), (1, 2))

    def test_register_unknown_protocol_issue(self):
        _, register, _, piece = self.issue_protocol()
        with self.assertRaises(KeyError):
            register_shift(register, piece.issue_id + 1, [], '其他')

    def test_register_against_summary_piece(self):
        source, register, revision_id, piece = self.issue_protocol()
        _, _, summary = self.close_shift(register, RunHistory(source), revision_id,
                                         piece.issue_id, [], '其他', [['X']])
        with self.assertRaisesRegex(ValueError, 'V1'):
            register_shift(register, summary.issue_id, [], '其他')

    def test_register_invalid_rules(self):
        source, register, _, piece = self.issue_protocol()
        bad_rules = [
            None,
            'not-a-list',
            [['含Y', 'contains']],
            [['含Y', 'contains', ['Y'], 'extra']],
            [['含Y', 'startswith', ['Y']]],
            [['含Y', 'contains', []]],
            [['含Y', 'contains', ['']]],
            [['含Y', 'contains', ['Y', 2]]],
            [[1, 'contains', ['Y']]],
            [['含Y', 'contains', ['Y']], ['含Y', 'prefix', ['X']]],
            [['其他', 'contains', ['Y']]],
        ]
        for rules in bad_rules:
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                register_shift(register, piece.issue_id, rules, '其他')
        with self.assertRaises(ValueError):
            register_shift(register, piece.issue_id, [], '')
        self.assertEqual(source.execute('SELECT COUNT(*) FROM shifts').fetchone(), (0,))

    def test_rules_may_use_codes_absent_from_model(self):
        _, register, _, piece = self.issue_protocol()
        shift_id = register_shift(register, piece.issue_id, [['未见', 'contains', ['Q', 'Q']]], '其他')
        self.assertEqual(shift_id, 1)


class ShiftSummaryTests(ShiftCase):
    def test_summary_envelope_and_body(self):
        source, register, revision_id, piece = self.issue_protocol()
        history = RunHistory(source)
        rules = [['含Y', 'contains', ['Y']], ['首X', 'prefix', ['X']]]
        shift_id = register_shift(register, piece.issue_id, rules, '其他')
        runs = [record_run(history, revision_id, codes) for codes in (['X', 'Y'], ['X'], [])]
        summary = issue_shift_summary(register, shift_id, [run.run_id for run in runs])
        self.assertEqual(summary.version, 2)
        self.assertEqual(summary.site, '甲站')
        self.assertEqual(summary.revision_id, revision_id)
        self.assertEqual(summary.issue_id, piece.issue_id + 1)
        body = decode_shift_body(summary.body)
        self.assertEqual(body.shift_id, shift_id)
        self.assertEqual(body.protocol_issue_id, piece.issue_id)
        self.assertEqual(body.protocol_body_sha256, piece.body_sha256)
        self.assertEqual(tuple(rule.category for rule in body.rules), ('含Y', '首X'))
        self.assertEqual(body.fallback, '其他')
        self.assertEqual(body.counts, (1, 1, 1))
        self.assertEqual(body.total, 3)
        self.assertEqual(retrieve_piece(register, summary.issue_id).raw, summary.raw)
        # Device numbers, run numbers and original sequences stay at the station.
        self.assertEqual(set(json.loads(summary.body)),
                         {'shift_id', 'protocol_issue_id', 'protocol_body_sha256',
                          'rules', 'fallback', 'counts', 'total'})
        self.assertNotIn('bench-private', summary.raw.decode('utf-8'))

    def test_unknown_shift_or_run(self):
        source, register, revision_id, piece = self.issue_protocol()
        history = RunHistory(source)
        shift_id = register_shift(register, piece.issue_id, [], '其他')
        with self.assertRaises(KeyError):
            issue_shift_summary(register, shift_id + 1, [1])
        run = record_run(history, revision_id, ['X'])
        with self.assertRaises(KeyError):
            issue_shift_summary(register, shift_id, [run.run_id + 1])
        self.assertEqual(source.execute('SELECT COUNT(*) FROM issued_pieces').fetchone(), (1,))

    def test_selection_validation_and_shift_stays_open(self):
        source, register, revision_id, piece = self.issue_protocol()
        history = RunHistory(source)
        early = record_run(history, revision_id, ['X'])
        shift_id = register_shift(register, piece.issue_id, [], '其他')
        late = record_run(history, revision_id, ['X'])
        for bad in ([], (), [late.run_id, late.run_id], [early.run_id], ['1'], [True], [0]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                issue_shift_summary(register, shift_id, bad)
        self.assertEqual(source.execute('SELECT COUNT(*) FROM issued_pieces').fetchone(), (1,))
        summary = issue_shift_summary(register, shift_id, [late.run_id])
        self.assertEqual(decode_shift_body(summary.body).counts, (1,))

    def test_wrong_revision_run(self):
        source, register, _, piece = self.issue_protocol()
        history = RunHistory(source)
        other_revision = seal(source, serial_graph('Y'), protocol='回路巡检')
        shift_id = register_shift(register, piece.issue_id, [], '其他')
        foreign = record_run(history, other_revision, ['Y'])
        with self.assertRaisesRegex(ValueError, 'revision'):
            issue_shift_summary(register, shift_id, [foreign.run_id])
        self.assertEqual(source.execute('SELECT COUNT(*) FROM issued_pieces').fetchone(), (1,))

    def test_refix_rejected_and_later_runs_do_not_change_summary(self):
        source, register, revision_id, piece = self.issue_protocol()
        history = RunHistory(source)
        shift_id = register_shift(register, piece.issue_id, [], '其他')
        first_run = record_run(history, revision_id, ['X'])
        summary = issue_shift_summary(register, shift_id, [first_run.run_id])
        later = record_run(history, revision_id, ['X', 'Y'])
        with self.assertRaisesRegex(ValueError, 'already fixed'):
            issue_shift_summary(register, shift_id, [later.run_id])
        self.assertEqual(retrieve_piece(register, summary.issue_id).raw, summary.raw)
        self.assertEqual(source.execute('SELECT COUNT(*) FROM issued_pieces').fetchone(), (2,))


class ShiftIntakeTests(ShiftCase):
    def valid_summary_raw(self, **overrides):
        document = {
            'shift_id': 1, 'protocol_issue_id': 1, 'protocol_body_sha256': 'a' * 64,
            'rules': [['含Y', 'contains', ['Y']]], 'fallback': '其他',
            'counts': [1, 1], 'total': 2,
        }
        document.update(overrides)
        body = json.dumps(document, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')).encode('utf-8')
        return encode_envelope('甲站', 5, 3, body, version=2)

    def test_receive_summary_roundtrip(self):
        center, intake, _ = self.center()
        raw = self.valid_summary_raw()
        first = receive_piece(intake, raw)
        self.assertEqual(first.envelope.version, 2)
        self.assertEqual(receive_piece(intake, raw), first)
        self.assertEqual(center.execute('SELECT COUNT(*) FROM accepted_pieces').fetchone(), (1,))

    def test_conflicting_summary(self):
        center, intake, _ = self.center()
        raw = self.valid_summary_raw()
        first = receive_piece(intake, raw)
        with self.assertRaises(PieceConflict):
            receive_piece(intake, self.valid_summary_raw(counts=[2, 0]))
        self.assertEqual(receive_piece(intake, raw), first)
        self.assertEqual(center.execute('SELECT COUNT(*) FROM intake_conflicts').fetchone(), (1,))

    def test_invalid_summary_pieces_leave_no_receipt(self):
        center, intake, _ = self.center()
        cases = [
            self.valid_summary_raw(total=3),
            self.valid_summary_raw(counts=[0, 0], total=0),
            self.valid_summary_raw(counts=[2], total=2),
            self.valid_summary_raw(counts=[-1, 3], total=2),
            self.valid_summary_raw(rules=[['含Y', 'startswith', ['Y']]]),
            self.valid_summary_raw(rules=[['含Y', 'contains', []]]),
            self.valid_summary_raw(rules=[['含Y', 'contains', ['Y']], ['含Y', 'prefix', ['X']]]),
            self.valid_summary_raw(rules=[['其他', 'contains', ['Y']]]),
            self.valid_summary_raw(protocol_body_sha256='A' * 64),
            self.valid_summary_raw(protocol_body_sha256='abc'),
            self.valid_summary_raw(shift_id=0),
            self.valid_summary_raw(protocol_issue_id=True),
        ]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                receive_piece(intake, raw)
        bool_body = json.dumps({'shift_id': 1, 'protocol_issue_id': 1,
                                'protocol_body_sha256': 'a' * 64, 'rules': [], 'fallback': '其他',
                                'counts': [True], 'total': 1},
                               ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        with self.assertRaises(ValueError):
            receive_piece(intake, encode_envelope('甲站', 5, 3, bool_body, version=2))
        duplicate = ('{"counts":[1,1],"counts":[1,1],"fallback":"其他","protocol_body_sha256":"'
                     + 'a' * 64 + '","protocol_issue_id":1,'
                     '"rules":[["含Y","contains",["Y"]]],"shift_id":1,"total":2}')
        with self.assertRaises(ValueError):
            receive_piece(intake, encode_envelope('甲站', 5, 3, duplicate.encode('utf-8'), version=2))
        spaced = json.dumps({'shift_id': 1, 'protocol_issue_id': 1,
                             'protocol_body_sha256': 'a' * 64, 'rules': [], 'fallback': '其他',
                             'counts': [1], 'total': 1},
                            ensure_ascii=False, sort_keys=True).encode('utf-8')
        with self.assertRaises(ValueError):
            receive_piece(intake, encode_envelope('甲站', 5, 3, spaced, version=2))
        with self.assertRaises(ValueError):
            receive_piece(intake, self.manual_envelope(3, spaced))
        self.assertEqual(center.execute('SELECT COUNT(*) FROM accepted_pieces').fetchone(), (0,))
        self.assertEqual(center.execute('SELECT COUNT(*) FROM intake_receipts').fetchone(), (0,))

    def manual_envelope(self, version, body):
        document = {'version': version, 'site': '甲站', 'issue_id': 5, 'revision_id': 3,
                    'body_sha256': sha256(body).hexdigest(), 'body': body.decode('utf-8')}
        return json.dumps(document, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')).encode('utf-8')


class ShiftReviewTests(ShiftCase):
    def test_loop_shift_rows(self):
        env = self.prepare([['含Y', 'contains', ['Y']], ['首X', 'prefix', ['X']]],
                           [['X', 'Y'], ['X'], []])
        self.assertEqual(env.runs[2].status, SupportStatus.SUPPORTED)
        signed = review_shift(env.reports, env.protocol.intake_id, env.summary.intake_id)
        self.assertEqual(signed.rows, (
            ('含Y', 1, Fraction(3, 2), Fraction(-1, 2)),
            ('首X', 1, Fraction(1, 2), Fraction(1, 2)),
            ('其他', 1, Fraction(1), Fraction(0)),
        ))
        for category, measured, expected, difference in signed.rows:
            self.assertIsInstance(measured, int)
            self.assertIsInstance(expected, Fraction)
            self.assertIsInstance(difference, Fraction)
            self.assertEqual(difference, measured - expected)
        self.assertEqual(signed.shift_id, env.shift_id)
        self.assertEqual(signed.receipt, f'SR-{signed.report_id}')
        self.assertEqual([parent.intake_id for parent in signed.parents],
                         [env.protocol.intake_id, env.summary.intake_id])
        self.assertEqual(signed.parents[0].issue_id, env.piece.issue_id)
        self.assertEqual(signed.parents[1].issue_id, env.summary_envelope.issue_id)
        self.assertEqual(signed.parents[0].revision_id, env.revision_id)
        self.assertEqual(read_shift_report(env.reports, signed.report_id), signed)
        with self.assertRaises(KeyError):
            read_report(env.reports, signed.report_id)

    def test_swapped_rule_order(self):
        env = self.prepare([['首X', 'prefix', ['X']], ['含Y', 'contains', ['Y']]],
                           [['X', 'Y'], ['X'], []])
        signed = review_shift(env.reports, env.protocol.intake_id, env.summary.intake_id)
        self.assertEqual(signed.rows, (
            ('首X', 2, Fraction(1), Fraction(1)),
            ('含Y', 0, Fraction(1), Fraction(-1)),
            ('其他', 1, Fraction(1), Fraction(0)),
        ))

    def test_overlap_same_code_branches(self):
        env = self.prepare([['末段', 'contains', ['X', 'X', 'Y']], ['规程外码', 'contains', ['Z']]],
                           [['X', 'X', 'X', 'Y'], ['Z']], graph=overlap_graph())
        self.assertEqual(env.runs[0].status, SupportStatus.SUPPORTED)
        self.assertEqual(env.runs[1].status, SupportStatus.OUTSIDE_PROTOCOL)
        signed = review_shift(env.reports, env.protocol.intake_id, env.summary.intake_id)
        self.assertEqual(signed.rows, (
            ('末段', 1, Fraction(2), Fraction(-1)),
            ('规程外码', 1, Fraction(0), Fraction(1)),
            ('其他', 0, Fraction(0), Fraction(0)),
        ))

    def test_multicode_prefix(self):
        # Only records whose first two codes are exactly X, Y match; among the
        # loop records X^k Y that is just XY itself, with probability 1/9.
        env = self.prepare([['首XY', 'prefix', ['X', 'Y']]], [['X', 'Y'], ['X', 'X'], ['Y']])
        signed = review_shift(env.reports, env.protocol.intake_id, env.summary.intake_id)
        self.assertEqual(signed.rows, (
            ('首XY', 1, Fraction(1, 3), Fraction(2, 3)),
            ('其他', 2, Fraction(8, 3), Fraction(-2, 3)),
        ))

    def test_contains_overlapping_pattern(self):
        env = self.prepare([['双X', 'contains', ['X', 'X']]],
                           [['X'], ['X', 'X'], ['X', 'X', 'X'], []], graph=geometric_graph())
        signed = review_shift(env.reports, env.protocol.intake_id, env.summary.intake_id)
        self.assertEqual(signed.rows, (
            ('双X', 2, Fraction(1), Fraction(1)),
            ('其他', 2, Fraction(3), Fraction(-1)),
        ))

    def test_empty_rules_all_fallback(self):
        env = self.prepare([], [['X', 'Y'], ['Z'], []])
        self.assertEqual(decode_shift_body(env.summary_envelope.body).counts, (3,))
        signed = review_shift(env.reports, env.protocol.intake_id, env.summary.intake_id)
        self.assertEqual(signed.rows, (('其他', 3, Fraction(3), Fraction(0)),))

    def test_rule_with_code_absent_from_model(self):
        env = self.prepare([['未见', 'contains', ['Q']]], [['X']])
        signed = review_shift(env.reports, env.protocol.intake_id, env.summary.intake_id)
        self.assertEqual(signed.rows, (
            ('未见', 0, Fraction(0), Fraction(0)),
            ('其他', 1, Fraction(1), Fraction(0)),
        ))

    def test_binding_rejects_other_issuance(self):
        env = self.prepare([['含Y', 'contains', ['Y']]], [['Y']])
        other = issue_revision(env.register, env.revision_id)
        other_accepted = receive_piece(env.intake, other.raw)
        self.assertNotEqual(other_accepted.intake_id, env.protocol.intake_id)
        self.assertEqual(other.body, env.piece.body)
        with self.assertRaisesRegex(ValueError, 'protocol issue'):
            review_shift(env.reports, other_accepted.intake_id, env.summary.intake_id)
        self.assertEqual(self.shift_report_counts(env.center), (0, 0, 0))

    def test_binding_rejects_other_site(self):
        env = self.prepare([['含Y', 'contains', ['Y']]], [['Y']])
        _, _, _, other_piece = self.issue_protocol(site='乙站')
        other_accepted = receive_piece(env.intake, other_piece.raw)
        self.assertEqual(other_piece.issue_id, env.piece.issue_id)
        with self.assertRaisesRegex(ValueError, 'site'):
            review_shift(env.reports, other_accepted.intake_id, env.summary.intake_id)
        self.assertEqual(self.shift_report_counts(env.center), (0, 0, 0))

    def test_binding_rejects_other_revision(self):
        env = self.prepare([['含Y', 'contains', ['Y']]], [['Y']])
        corrected = seal(env.source, serial_graph('Y'), protocol='回路巡检')
        other = issue_revision(env.register, corrected)
        other_accepted = receive_piece(env.intake, other.raw)
        with self.assertRaisesRegex(ValueError, 'protocol issue'):
            review_shift(env.reports, other_accepted.intake_id, env.summary.intake_id)
        self.assertEqual(self.shift_report_counts(env.center), (0, 0, 0))

    def test_positions_require_matching_versions(self):
        env = self.prepare([['含Y', 'contains', ['Y']]], [['Y']])
        with self.assertRaisesRegex(ValueError, 'V2'):
            review_shift(env.reports, env.protocol.intake_id, env.protocol.intake_id)
        with self.assertRaisesRegex(ValueError, 'V1'):
            review_shift(env.reports, env.summary.intake_id, env.summary.intake_id)
        with self.assertRaisesRegex(ValueError, 'V1'):
            review_pair(env.reports, env.protocol.intake_id, env.summary.intake_id)
        with self.assertRaisesRegex(ValueError, 'V1'):
            review_pair(env.reports, env.summary.intake_id, env.summary.intake_id)
        self.assertEqual(self.shift_report_counts(env.center), (0, 0, 0))

    def test_review_missing_pieces(self):
        env = self.prepare([['含Y', 'contains', ['Y']]], [['Y']])
        for ids in ((99, env.summary.intake_id), (env.protocol.intake_id, 99),
                    (True, env.summary.intake_id)):
            with self.subTest(ids=ids), self.assertRaisesRegex(ValueError, 'valid accepted piece'):
                review_shift(env.reports, *ids)
        self.assertEqual(self.shift_report_counts(env.center), (0, 0, 0))

    def test_unknown_shift_report(self):
        _, _, reports = self.center()
        with self.assertRaises(KeyError):
            read_shift_report(reports, 1)

    def test_shift_report_survives_reopen_and_correction(self):
        env = self.prepare([['含Y', 'contains', ['Y']], ['首X', 'prefix', ['X']]],
                           [['X', 'Y'], ['X'], []])
        signed = review_shift(env.reports, env.protocol.intake_id, env.summary.intake_id)
        seal(env.source, serial_graph('Y'), protocol='回路巡检')
        env.source.close()
        env.center.close()
        _, _, reports = self.center()
        self.assertEqual(read_shift_report(reports, signed.report_id), signed)

    def test_shift_report_rollback(self):
        env = self.prepare([['含Y', 'contains', ['Y']]], [['Y']])
        env.center.execute('''CREATE TABLE shift_commit_fault (
            report_id INTEGER PRIMARY KEY,
            invalid_intake INTEGER NOT NULL REFERENCES accepted_pieces(intake_id)
                DEFERRABLE INITIALLY DEFERRED
        )''')
        env.center.execute('''CREATE TRIGGER fail_at_shift_commit
            AFTER INSERT ON shift_report_receipts BEGIN
            INSERT INTO shift_commit_fault VALUES (NEW.report_id, -1);
        END''')
        with self.assertRaises(sqlite3.IntegrityError):
            review_shift(env.reports, env.protocol.intake_id, env.summary.intake_id)
        self.assertFalse(env.center.in_transaction)
        self.assertEqual(self.shift_report_counts(env.center), (0, 0, 0))
        with self.assertRaises(KeyError):
            read_shift_report(env.reports, 1)
        env.center.execute('DROP TRIGGER fail_at_shift_commit')
        retried = review_shift(env.reports, env.protocol.intake_id, env.summary.intake_id)
        self.assertEqual(retried.rows, (
            ('含Y', 1, Fraction(1, 2), Fraction(1, 2)),
            ('其他', 0, Fraction(1, 2), Fraction(-1, 2)),
        ))
        self.assertEqual(self.shift_report_counts(env.center), (1, 2, 1))
        self.assertEqual(read_shift_report(env.reports, retried.report_id), retried)


class SampleShiftFlowTests(LedgerTestCase):
    def test_sample_shifts_match_expected_rows(self):
        sample = json.loads((SAMPLES / 'shift_review.json').read_text(encoding='utf-8'))
        center, intake, reports = self.center()
        stations = {}
        issued = {}
        for name, model in sample['models'].items():
            site = model['site']
            if site not in stations:
                stations[site] = self.station(site)
            source = stations[site]
            revisions, history = RevisionStore(source), RunHistory(source)
            register = IssueRegister(source, site)
            graph = TransitionGraph(tuple(model['states']), model['initial'],
                                    tuple(transition_from_record(row) for row in model['transitions']))
            revision_id = seal_revision(revisions, model['protocol_id'],
                                        build_catalog(model['catalog']), graph,
                                        device_id=model['device_id'])
            piece = issue_revision(register, revision_id)
            issued[name] = (register, history, revision_id, piece)
        for shift in sample['shifts']:
            register, history, revision_id, piece = issued[shift['model']]
            with self.subTest(shift=shift['model'], rules=shift['rules']):
                shift_id = register_shift(register, piece.issue_id, shift['rules'], shift['fallback'])
                runs = [record_run(history, revision_id, codes) for codes in shift['runs']]
                summary = issue_shift_summary(register, shift_id, [run.run_id for run in runs])
                summary_accepted = receive_piece(intake, summary.raw)
                protocol_accepted = receive_piece(intake, piece.raw)
                signed = review_shift(reports, protocol_accepted.intake_id,
                                      summary_accepted.intake_id)
                actual = [[category, measured, str(expected), str(difference)]
                          for category, measured, expected, difference in signed.rows]
                self.assertEqual(actual, shift['expected'])
                self.assertEqual(read_shift_report(reports, signed.report_id), signed)
        last_shift_runs = stations['甲站'] and RunHistory(stations['甲站']).for_revision(
            issued['overlap'][2])
        self.assertEqual(last_shift_runs[-1].status, SupportStatus.OUTSIDE_PROTOCOL)
