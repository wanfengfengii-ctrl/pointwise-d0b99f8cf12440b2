from intake.selection import select_pair
from intake.store import AcceptedPiece, IntakeStore
from issue.body import decode_body
from issue.shift_body import decode_shift_body
from review.agreement import compute_agreement
from review.shift_expectation import category_probabilities
from review.store import ReportStore, SignedReport, SignedShiftReport


def review_pair(store: ReportStore, left_id: int, right_id: int) -> SignedReport:
    pair = select_pair(IntakeStore(store.connection), left_id, right_id)
    if pair.left.envelope.version != 1 or pair.right.envelope.version != 1:
        raise ValueError('pair review requires two V1 protocol pieces')
    return store.commit_report(compute_agreement(pair), pair)


def _select_piece(intake: IntakeStore, intake_id: int, position: str) -> AcceptedPiece:
    try:
        return intake.read(intake_id)
    except (KeyError, ValueError) as exc:
        raise ValueError(f'{position} request does not name a valid accepted piece') from exc


def review_shift(store: ReportStore, protocol_intake_id: int, summary_intake_id: int) -> SignedShiftReport:
    intake = IntakeStore(store.connection)
    protocol_piece = _select_piece(intake, protocol_intake_id, 'protocol')
    summary_piece = _select_piece(intake, summary_intake_id, 'summary')
    if protocol_piece.envelope.version != 1:
        raise ValueError('protocol position must hold a V1 protocol piece')
    if summary_piece.envelope.version != 2:
        raise ValueError('summary position must hold a V2 shift summary piece')
    body = decode_shift_body(summary_piece.envelope.body)
    parent = protocol_piece.envelope
    envelope = summary_piece.envelope
    if envelope.site != parent.site:
        raise ValueError('shift summary site does not match the protocol piece')
    if body.protocol_issue_id != parent.issue_id:
        raise ValueError('shift summary protocol issue does not match the protocol piece')
    if envelope.revision_id != parent.revision_id:
        raise ValueError('shift summary revision does not match the protocol piece')
    if body.protocol_body_sha256 != parent.body_sha256:
        raise ValueError('shift summary protocol digest does not match the protocol piece')
    graph = decode_body(parent.body).graph
    probabilities = category_probabilities(graph, body.rules)
    categories = tuple(rule.category for rule in body.rules) + (body.fallback,)
    rows = tuple(
        (category, measured, expected, measured - expected)
        for category, measured, probability in zip(categories, body.counts, probabilities)
        for expected in (body.total * probability,)
    )
    return store.commit_shift_report(body.shift_id, rows, protocol_piece, summary_piece)
