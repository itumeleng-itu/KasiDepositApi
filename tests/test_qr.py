"""The voucher QR: its payload format (what the app's scanner parses) and the SVG."""

import re

import pytest
import segno
from fastapi.testclient import TestClient

from app.qr import voucher_payload, voucher_qr_svg


def test_the_payload_is_a_kasideposit_redeem_link() -> None:
    assert voucher_payload("5008731775025283") == "kasideposit://redeem?pin=5008731775025283"


def test_leading_zeros_survive_in_the_payload() -> None:
    assert voucher_payload("0754762219816030").endswith("pin=0754762219816030")


@pytest.mark.parametrize("pin", ["", "123", "12345678901234567", "12345678901234ab", "1234 5678 9012 3456"])
def test_only_a_16_digit_pin_can_be_encoded(pin: str) -> None:
    with pytest.raises(ValueError):
        voucher_payload(pin)


def test_the_svg_is_the_qr_of_that_payload() -> None:
    pin = "5008731775025283"
    svg = voucher_qr_svg(pin)
    expected = segno.make(voucher_payload(pin), error="m", micro=False)
    assert svg == expected.svg_inline(scale=1, border=4, omitsize=True, dark="#000", light="#fff")
    assert expected.error == "M"


def test_the_svg_is_sized_by_viewbox_black_on_white() -> None:
    svg = voucher_qr_svg("5008731775025283")
    assert svg.startswith("<svg viewBox=")
    assert "width=" not in svg.split(">", 1)[0]  # the page chooses the size
    assert 'stroke="#000"' in svg and 'fill="#fff"' in svg
    assert "<script" not in svg


def test_each_pin_gets_its_own_qr() -> None:
    assert voucher_qr_svg("5008731775025283") != voucher_qr_svg("5008731775025284")


def test_vending_returns_the_qr_for_the_new_pin(client: TestClient) -> None:
    body = client.post("/demo/vouchers", json={"amount_cents": 50000}).json()
    assert body["qr_payload"] == f"kasideposit://redeem?pin={body['pin']}"
    assert body["qr_svg"] == voucher_qr_svg(body["pin"])


def test_the_till_slip_shows_the_qr(client: TestClient) -> None:
    page = client.get("/till").text
    assert 'id="slip-qr"' in page
    assert "showQr(voucher.qr_svg)" in page
    assert re.search(r"scan the QR code or type the PIN", page)
