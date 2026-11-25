from intake.store import AcceptedPiece, IntakeStore
from issue.body import decode_body
from issue.envelope import decode_envelope
from issue.shift_body import decode_shift_body


class PieceConflict(ValueError):
    pass


def receive_piece(store: IntakeStore, raw: bytes) -> AcceptedPiece:
    envelope = decode_envelope(raw)
    if envelope.version == 1:
        decode_body(envelope.body)
    else:
        decode_shift_body(envelope.body)
    accepted = store.accept_or_register_conflict(envelope)
    if accepted is None:
        raise PieceConflict(f'conflicting issue {envelope.site!r}/{envelope.issue_id}')
    return accepted
