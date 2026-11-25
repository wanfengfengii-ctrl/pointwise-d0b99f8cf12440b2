from dataclasses import dataclass

from protocol.catalog import require_text


PREFIX = 'prefix'
CONTAINS = 'contains'
_MODES = frozenset((PREFIX, CONTAINS))


@dataclass(frozen=True, slots=True)
class Rule:
    category: str
    mode: str
    codes: tuple[str, ...]


def parse_rule(record) -> Rule:
    if not isinstance(record, (list, tuple)) or len(record) != 3:
        raise ValueError('rule must be a three-field list or tuple')
    category, mode, codes = record
    category = require_text(category, 'category name')
    if not isinstance(mode, str) or mode not in _MODES:
        raise ValueError('rule match mode must be prefix or contains')
    if not isinstance(codes, (list, tuple)) or not codes:
        raise ValueError('rule code sequence must be a nonempty list or tuple')
    return Rule(category, mode, tuple(require_text(code, 'rule code') for code in codes))


def parse_rules(rules, fallback: str) -> tuple[tuple[Rule, ...], str]:
    if not isinstance(rules, (list, tuple)):
        raise ValueError('rules must be an ordered list or tuple')
    fallback = require_text(fallback, 'fallback category')
    parsed = tuple(parse_rule(record) for record in rules)
    names = [rule.category for rule in parsed] + [fallback]
    if len(set(names)) != len(names):
        raise ValueError('category names and the fallback name must be distinct')
    return parsed, fallback


def rule_matches(rule: Rule, codes: tuple[str, ...]) -> bool:
    length = len(rule.codes)
    if rule.mode == PREFIX:
        return tuple(codes[:length]) == rule.codes
    return any(tuple(codes[start:start + length]) == rule.codes
               for start in range(len(codes) - length + 1))


def classify_sequence(rules: tuple[Rule, ...], fallback: str, codes: tuple[str, ...]) -> str:
    for rule in rules:
        if rule_matches(rule, codes):
            return rule.category
    return fallback


def rules_to_json(rules: tuple[Rule, ...]) -> list:
    return [[rule.category, rule.mode, list(rule.codes)] for rule in rules]
