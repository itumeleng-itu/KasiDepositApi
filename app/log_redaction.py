"""Keep personal data out of the access log.

The app's contract puts the ShapID (a cellphone number) in the URL of
GET /v1/shapid/{shap_id}, so uvicorn's access log would record it, and the
demo directory's DELETE /demo/shapids/{number} does the same. This filter
masks both. PINs and voucher tokens travel in request bodies, which the access
log never records, and SQL parameters are kept out of error messages by the
engine (hide_parameters, app/db.py).

Risk decisions (app/risk.py) and deposit transitions (app/deposits.py, the
payout worker in app/worker.py) are written with ids and states only. As a
second line of defence those loggers also mask any run of six or more
digits (a PIN, an ID number, an account number, a cellphone number), so a
mistake in a log call cannot put one in the log. Digits inside a UUID are left
alone: the user's internal id is how a decision is traced.
"""

import logging
import re

_SHAP_ID_PATH = re.compile(r"^(/v1/shapid/|/demo/shapids/)[^?\s]+")
# Six or more digits, optionally after a +, not part of a UUID's hex groups.
_PERSONAL_NUMBER = re.compile(r"(?<![0-9A-Za-z-])\+?[0-9][0-9 ]{4,}[0-9](?![0-9A-Za-z-])")
RISK_LOGGER = "app.risk"
# Loggers that carry the personal-number filter.
FILTERED_LOGGERS = (RISK_LOGGER, "app.deposits", "app.worker")


class RedactShapIds(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access records: (client_addr, method, full_path, http_version, status_code)
        if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
            args = list(record.args)
            args[2] = _SHAP_ID_PATH.sub(r"\1***", args[2])
            record.args = tuple(args)
        return True


class RedactPersonalNumbers(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _PERSONAL_NUMBER.sub("***", message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def install() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactShapIds) for f in access.filters):
        access.addFilter(RedactShapIds())
    for name in FILTERED_LOGGERS:
        filtered = logging.getLogger(name)
        if not any(isinstance(f, RedactPersonalNumbers) for f in filtered.filters):
            filtered.addFilter(RedactPersonalNumbers())
