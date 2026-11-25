from review.store import ReportStore, SignedReport, SignedShiftReport


def read_report(store: ReportStore, report_id: int) -> SignedReport:
    return store.read(report_id)


def read_shift_report(store: ReportStore, report_id: int) -> SignedShiftReport:
    return store.read_shift_report(report_id)
