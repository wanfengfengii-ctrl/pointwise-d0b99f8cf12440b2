from collections import defaultdict, deque
from dataclasses import dataclass, field
from fractions import Fraction

from intake.selection import AcceptedPair
from issue.body import decode_body
from protocol.transitions import State, TransitionGraph, TransitionKind
from review.length import AgreementStatistics
from review.linear_system import RationalSystem, solve_rational_system


@dataclass
class _NextEvents:
    end: Fraction = Fraction(0)
    emissions: dict[str, dict[State, Fraction]] = field(default_factory=dict)


def _identity_matrix(size: int) -> list[list[int]]:
    return [[int(i == j) for j in range(size)] for i in range(size)]


def _next_events(graph: TransitionGraph) -> dict[State, _NextEvents]:
    reachable = graph.reachable()
    states = [state for state in graph.states if state in reachable]
    index = {state: i for i, state in enumerate(states)}
    coefficients = _identity_matrix(len(states))
    terminal_columns = {None: [Fraction(0) for _ in states]}
    has_silent = False
    for state in states:
        i = index[state]
        for edge in graph.positive_outgoing(state):
            if edge.kind is TransitionKind.SILENT:
                coefficients[i][index[edge.target]] -= edge.weight
                has_silent = True
                continue
            event = None if edge.kind is TransitionKind.END else (edge.code, edge.target)
            if event not in terminal_columns:
                terminal_columns[event] = [Fraction(0) for _ in states]
            terminal_columns[event][i] += edge.weight

    system = RationalSystem(coefficients) if has_silent else None
    events = {state: _NextEvents() for state in states}
    for event, rhs in terminal_columns.items():
        masses = system.solve(rhs) if system is not None else rhs
        for state, mass in zip(states, masses):
            if event is None:
                events[state].end = mass
            elif mass:
                code, target = event
                events[state].emissions.setdefault(code, {})[target] = mass
    return events


def _matching_successors(left: _NextEvents, right: _NextEvents) -> dict:
    successors = defaultdict(Fraction)
    for code in left.emissions.keys() & right.emissions.keys():
        for left_state, left_mass in left.emissions[code].items():
            for right_state, right_mass in right.emissions[code].items():
                successors[left_state, right_state] += left_mass * right_mass
    return dict(successors)


def _combine(left_graph: TransitionGraph, right_graph: TransitionGraph) -> AgreementStatistics:
    left_events = _next_events(left_graph)
    right_events = _next_events(right_graph)
    initial = (left_graph.initial, right_graph.initial)
    indexes = {initial: 0}
    queue = deque([initial])
    continuations = []
    ends = []
    while queue:
        left_state, right_state = queue.popleft()
        left = left_events[left_state]
        right = right_events[right_state]
        ends.append(left.end * right.end)
        successors = _matching_successors(left, right)
        row = {}
        for successor, mass in sorted(successors.items()):
            if successor not in indexes:
                indexes[successor] = len(indexes)
                queue.append(successor)
            row[indexes[successor]] = mass
        continuations.append(row)

    coefficients = _identity_matrix(len(ends))
    for i, row in enumerate(continuations):
        for j, mass in row.items():
            coefficients[i][j] -= mass
    # Each visit contributes its common end mass. An emission adds one code
    # only when the continuation also belongs to the agreement event.
    system = RationalSystem(coefficients)
    probability = system.solve(ends)
    next_agreement_mass = [sum((mass * probability[j] for j, mass in row.items()), Fraction(0))
                           for row in continuations]
    length_mass = system.solve(next_agreement_mass)
    return AgreementStatistics.from_moments(probability[0], length_mass[0])


def compute_agreement(pair: AcceptedPair) -> AgreementStatistics:
    left_graph = decode_body(pair.left.envelope.body).graph
    right_graph = decode_body(pair.right.envelope.body).graph
    return _combine(left_graph, right_graph)
