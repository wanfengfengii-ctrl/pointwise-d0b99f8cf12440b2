from collections import deque
from fractions import Fraction

from issue.shift_rules import CONTAINS, PREFIX, Rule
from protocol.transitions import TransitionGraph
from review.linear_system import RationalSystem
from review.next_events import _next_events


_MATCHED = 'matched'
_FAILED = 'failed'


def _kmp_table(pattern: tuple[str, ...]) -> list[int]:
    table = [0] * len(pattern)
    length = 0
    for index in range(1, len(pattern)):
        while length and pattern[index] != pattern[length]:
            length = table[length - 1]
        if pattern[index] == pattern[length]:
            length += 1
        table[index] = length
    return table


class _Matcher:
    """Tracks one rule against the codes emitted so far."""

    __slots__ = ('codes', 'mode', 'table')

    def __init__(self, rule: Rule):
        self.codes = rule.codes
        self.mode = rule.mode
        self.table = _kmp_table(rule.codes) if rule.mode == CONTAINS else None

    def advance(self, state, code: str):
        pattern = self.codes
        if self.mode == PREFIX:
            if state is _MATCHED or state is _FAILED:
                return state
            if code == pattern[state]:
                return _MATCHED if state + 1 == len(pattern) else state + 1
            return _FAILED
        if state is _MATCHED:
            return state
        length = state
        while length and code != pattern[length]:
            length = self.table[length - 1]
        if code == pattern[length]:
            length += 1
        return _MATCHED if length == len(pattern) else length

    @staticmethod
    def matched(state) -> bool:
        return state is _MATCHED


def _classification(matchers: tuple[_Matcher, ...], states: tuple) -> int:
    for position, (matcher, state) in enumerate(zip(matchers, states)):
        if matcher.matched(state):
            return position
    return len(matchers)


def category_probabilities(graph: TransitionGraph, rules: tuple[Rule, ...]) -> tuple[Fraction, ...]:
    """Exact probability that a complete record falls into each category.

    Returns one Fraction per rule plus the trailing fallback. Public edges append
    one code, silent edges append none, and only records that actually end are
    classified. Every finite ending path is counted, including silent loops and
    same-code branches.
    """
    matchers = tuple(_Matcher(rule) for rule in rules)
    events = _next_events(graph)
    start = (graph.initial, tuple(0 for _ in matchers))
    index = {start: 0}
    nodes = [start]
    queue = deque([start])
    continuations = []
    ends = []
    while queue:
        protocol_state, classifier_state = queue.popleft()
        event = events[protocol_state]
        ends.append(event.end)
        row = {}
        for code, targets in event.emissions.items():
            next_classifier = tuple(matcher.advance(state, code)
                                    for matcher, state in zip(matchers, classifier_state))
            for target, mass in targets.items():
                node = (target, next_classifier)
                position = index.get(node)
                if position is None:
                    position = len(nodes)
                    index[node] = position
                    nodes.append(node)
                    queue.append(node)
                row[position] = row.get(position, Fraction(0)) + mass
        continuations.append(row)

    size = len(nodes)
    coefficients = [[Fraction(i == j) for j in range(size)] for i in range(size)]
    for i, row in enumerate(continuations):
        for j, mass in row.items():
            coefficients[i][j] -= mass
    system = RationalSystem(coefficients)
    classifications = [_classification(matchers, classifier_state)
                       for _, classifier_state in nodes]
    probabilities = []
    for category in range(len(rules) + 1):
        rhs = [end if classifications[i] == category else Fraction(0) for i, end in enumerate(ends)]
        probabilities.append(system.solve(rhs)[0])
    return tuple(probabilities)
