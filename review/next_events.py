from dataclasses import dataclass, field
from fractions import Fraction

from protocol.transitions import State, TransitionGraph, TransitionKind
from review.linear_system import RationalSystem


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
