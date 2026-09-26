"""Identity verification: a mock of a Home Affairs (DHA) check.

A real provider confirms that an ID number exists, belongs to a living
person, and carries these names. Today the only real check is the ID
algorithm (app/sa_id.py); this seam is where a DHA-linked KYC provider plugs
in, replaceable by one class like the ShapID and payout mocks.

The mock matches the mobile app's fake (src/api/fake.ts, and its TESTING.md)
scenario for scenario, keyed off the ID's sequence digits (positions 7-10):

    0000  id_verification_failed
    0001  id_number_already_registered
    else  verified
"""

from typing import Protocol

_SCENARIOS = {
    "0000": "id_verification_failed",
    "0001": "id_number_already_registered",
}


class IdentityRefused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class IdentityVerifier(Protocol):
    def verify(self, id_number: str, full_names: str) -> str:
        """Returns the provider's reference, or raises IdentityRefused."""
        ...


class MockIdentityVerifier:
    def verify(self, id_number: str, full_names: str) -> str:
        reason = _SCENARIOS.get(id_number[6:10])
        if reason is not None:
            raise IdentityRefused(reason)
        return "mock-verified"
