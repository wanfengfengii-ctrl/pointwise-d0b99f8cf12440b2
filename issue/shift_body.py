from dataclasses import dataclass
import json

from issue.envelope import _unique_fields
from issue.shift_rules import Rule, parse_rules, rules_to_json
from ledger import record_id
from protocol.catalog import require_text


_HEX_DIGITS = frozenset('0123456789abcdef')


@dataclass(frozen=True, slots=True)
class ShiftSummaryBody:
    shift_id: int
    protocol_issue_id: int
    protocol_body_sha256: str
    rules: tuple[Rule, ...]
    fallback: str
    counts: tuple[int, ...]
    total: int


def _check_digest(value: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in _HEX_DIGITS for char in value):
        raise ValueError('protocol body digest must be 64 lowercase hexadecimal characters')
    return value


def _check_counts(counts, rule_count: int) -> tuple[int, ...]:
    if not isinstance(counts, (list, tuple)) or len(counts) != rule_count + 1:
        raise ValueError('counts must cover every rule in order plus the fallback')
    checked = tuple(counts)
    if any(type(count) is not int or count < 0 for count in checked):
        raise ValueError('counts must be nonnegative integers')
    return checked


def _check_total(total: int, counts: tuple[int, ...]) -> int:
    if type(total) is not int or total < 1 or total != sum(counts):
        raise ValueError('total must be the positive sum of the counts')
    return total


def encode_shift_body(body: ShiftSummaryBody) -> bytes:
    if not isinstance(body.rules, tuple) or any(not isinstance(rule, Rule) for rule in body.rules):
        raise ValueError('summary rules must be parsed Rule instances')
    counts = _check_counts(list(body.counts), len(body.rules))
    document = {
        'shift_id': record_id(body.shift_id),
        'protocol_issue_id': record_id(body.protocol_issue_id),
        'protocol_body_sha256': _check_digest(body.protocol_body_sha256),
        'rules': rules_to_json(body.rules),
        'fallback': require_text(body.fallback, 'fallback category'),
        'counts': list(counts),
        'total': _check_total(body.total, counts),
    }
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def decode_shift_body(raw: bytes) -> ShiftSummaryBody:
    if type(raw) is not bytes:
        raise ValueError('shift summary body must be bytes')
    document = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_fields)
    fields = {'shift_id', 'protocol_issue_id', 'protocol_body_sha256', 'rules', 'fallback', 'counts', 'total'}
    if not isinstance(document, dict) or document.keys() != fields:
        raise ValueError('invalid shift summary body fields')
    rules, fallback = parse_rules(document['rules'], document['fallback'])
    counts = _check_counts(document['counts'], len(rules))
    body = ShiftSummaryBody(
        record_id(document['shift_id']), record_id(document['protocol_issue_id']),
        _check_digest(document['protocol_body_sha256']), rules, fallback, counts,
        _check_total(document['total'], counts),
    )
    if encode_shift_body(body) != raw:
        raise ValueError('shift summary body is not in canonical encoding')
    return body
