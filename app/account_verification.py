"""Bank account verification: a mock of the banks' account verification check.

A real check (reached through a verification provider) confirms that an
account exists and is open at that bank, and belongs to the person with this
ID number, so money only ever goes to the registered person's own account.
This seam is where that provider plugs in, replaceable by one class like the
other mocks.

The mock matches the mobile app's fake (src/api/fake.ts, and its TESTING.md),
keyed off the account number's last digit:

    9     account_not_found
    8     account_holder_mismatch (the account is someone else's)
    else  verified
"""

from typing import Protocol


class AccountRefused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AccountVerifier(Protocol):
    def verify(self, id_number: str, bank_id: str, account_number: str) -> str:
        """Returns the provider's reference, or raises AccountRefused."""
        ...


class MockAccountVerifier:
    def verify(self, id_number: str, bank_id: str, account_number: str) -> str:
        if account_number.endswith("9"):
            raise AccountRefused("account_not_found")
        if account_number.endswith("8"):
            raise AccountRefused("account_holder_mismatch")
        return "mock-account-verified"
