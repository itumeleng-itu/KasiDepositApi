"""Keep personal data out of the access log.

The app's contract puts the ShapID (a cellphone number) in the URL of
GET /v1/shapid/{shap_id}, so uvicorn's access log would record it. This filter
masks it. PINs and voucher tokens travel in request bodies, which the access
log never records, and SQL parameters are kept out of error messages by the
engine (hide_parameters, app/db.py).
"""

import logging
import re

_SHAP_ID_PATH = re.compile(r"^(/v1/shapid/)[^?\s]+")


class RedactShapIds(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access records: (client_addr, method, full_path, http_version, status_code)
        if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
            args = list(record.args)
            args[2] = _SHAP_ID_PATH.sub(r"\1***", args[2])
            record.args = tuple(args)
        return True


def install() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactShapIds) for f in access.filters):
        access.addFilter(RedactShapIds())
