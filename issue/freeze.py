from issue.body import FixedBody, encode_body
from issue.envelope import IssuedEnvelope
from issue.register import IssueRegister
from issue.shift_body import ShiftSummaryBody, encode_shift_body
from issue.shift_rules import classify_sequence, parse_rules
from protocol.revisions import FrozenRevision, RevisionStore
from protocol.transitions import Transition, TransitionGraph
from runs.history import RunHistory


def project_revision(revision: FrozenRevision) -> FixedBody:
    graph = revision.graph
    local = {state: index for index, state in enumerate(graph.states)}
    transitions = []
    for edge in graph.transitions:
        successor = None if edge.target is None else local[edge.target]
        transitions.append(Transition(local[edge.source], edge.kind, edge.weight, successor, edge.code))
    return FixedBody(TransitionGraph(tuple(range(len(local))), local[graph.initial], tuple(transitions)))


def issue_revision(register: IssueRegister, revision_id: int) -> IssuedEnvelope:
    revision = RevisionStore(register.connection).load(revision_id)
    return register.create(revision.revision_id, encode_body(project_revision(revision)))


def register_shift(register: IssueRegister, protocol_issue_id: int, rules, fallback: str) -> int:
    protocol = register.read(protocol_issue_id)
    if protocol.version != 1:
        raise ValueError('a shift must register against an issued V1 protocol piece')
    parsed, fallback = parse_rules(rules, fallback)
    return register.add_shift(protocol, parsed, fallback)


def issue_shift_summary(register: IssueRegister, shift_id: int, run_ids) -> IssuedEnvelope:
    shift = register.read_shift(shift_id)
    if shift.summary_issue_id is not None:
        raise ValueError(f'shift {shift_id} is already fixed')
    if not isinstance(run_ids, (list, tuple)) or not run_ids:
        raise ValueError('a shift summary requires a nonempty selection of runs')
    history = RunHistory(register.connection)
    seen = set()
    selected = []
    for run_id in run_ids:
        run = history.read(run_id)
        if run.run_id in seen:
            raise ValueError(f'duplicate run {run.run_id} in the shift selection')
        seen.add(run.run_id)
        if run.run_id <= shift.run_boundary:
            raise ValueError(f'run {run.run_id} was recorded before the shift was registered')
        if run.revision_id != shift.revision_id:
            raise ValueError(f'run {run.run_id} does not belong to the registered protocol revision')
        selected.append(run)
    categories = tuple(rule.category for rule in shift.rules) + (shift.fallback,)
    slots = {category: position for position, category in enumerate(categories)}
    counts = [0] * len(categories)
    for run in selected:
        counts[slots[classify_sequence(shift.rules, shift.fallback, run.codes)]] += 1
    body = encode_shift_body(ShiftSummaryBody(
        shift.shift_id, shift.protocol_issue_id, shift.protocol_body_sha256,
        shift.rules, shift.fallback, tuple(counts), sum(counts)))
    return register.fix_shift_summary(shift.shift_id, shift.revision_id, body)
