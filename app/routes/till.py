"""GET /till: the demo till page, for a cashier (DEMO SCAFFOLDING).

A plain page that sells vouchers and lets a manager see and top up the float.
It lives outside /demo because a browser opening a URL cannot send the
X-Demo-Key header. The page holds no secret: everything it does is a /demo
request carrying the till code the cashier typed in (the server's
DEMO_API_KEY), so without the code it can do nothing.

Mounted only when the demo routes are, and never listed in the API schema.
"""

from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse

TILL_PAGE = Path(__file__).resolve().parent.parent / "static" / "till.html"

router = APIRouter(include_in_schema=False)

_HEADERS = {
    "Cache-Control": "no-store",
    # Never framed by another site (no clickjacking of the Sell button).
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


@router.get("/till", response_class=HTMLResponse)
def till_page() -> HTMLResponse:
    return HTMLResponse(TILL_PAGE.read_text(encoding="utf-8"), headers=_HEADERS)


def mount(app: FastAPI) -> None:
    app.include_router(router)
