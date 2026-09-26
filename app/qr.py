"""The QR code on a voucher slip.

It encodes a small link, `kasideposit://redeem?pin=<16 digits>`, rather than
the bare PIN, so the mobile app's scanner can tell a KasiDeposit voucher from
any other QR code (and, later, the phone's own camera could open the app from
it). A scan feeds the PIN into the same flow as typing it: lookup, then the
confirm screen, then Send. A scan never deposits on its own.

The QR is exactly as valuable as the printed PIN: it is a bearer voucher,
redeemable once by whoever holds it. It is made on the server, so the till
page loads no script and the PIN never appears in a URL we serve.
"""

import io
import re

import segno

SCHEME = "kasideposit"
_PIN = re.compile(r"^[0-9]{16}$")


def voucher_payload(pin: str) -> str:
    """The text inside the QR. The mobile app parses exactly this format."""
    if not _PIN.fullmatch(pin):
        raise ValueError("a voucher PIN is 16 digits")
    return f"{SCHEME}://redeem?pin={pin}"


def voucher_qr_svg(pin: str) -> str:
    """The QR as inline SVG, sized by its viewBox so the page chooses the size.

    Error correction M survives a creased or smudged slip; black on white,
    with the standard four-module quiet zone, scans reliably when printed.

    It carries the SVG namespace (segno's svg_inline leaves it out): the till
    page parses it with DOMParser as image/svg+xml, and without xmlns the
    elements are not SVG and the browser draws nothing.
    """
    qr = segno.make(voucher_payload(pin), error="m", micro=False)
    buff = io.BytesIO()
    qr.save(buff, kind="svg", xmldecl=False, svgns=True, nl=False,
            scale=1, border=4, omitsize=True, dark="#000", light="#fff")
    return buff.getvalue().decode("utf-8")
