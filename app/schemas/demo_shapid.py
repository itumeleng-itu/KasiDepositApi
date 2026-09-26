"""Request and response bodies for the demo PayShap directory (/demo/shapids)."""

import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.banks import BANK_API_CODES, BANK_IDS, BANK_IDS_BY_API_CODE
from app.models import DemoShapId

_FILLER = re.compile(r"[\s\-()]")


def normalise_number(raw: str) -> str:
    """"082 555 1234", "0825551234", "+27 82 555 1234" -> "+27825551234"."""
    digits = _FILLER.sub("", raw)
    if digits.startswith("+27"):
        digits = "0" + digits[3:]
    elif digits.startswith("27") and len(digits) == 11:
        digits = "0" + digits[2:]
    if not re.fullmatch(r"0[678][0-9]{8}", digits):
        raise ValueError("a South African mobile number, e.g. 082 555 1234")
    return "+27" + digits[1:]


def mask_name(full_name: str) -> str:
    """"Thabo Sipho Mokoena" -> "T. Mokoena": what PayShap shows (POPIA)."""
    parts = unicodedata.normalize("NFC", full_name).split()
    if len(parts) < 2 or not parts[0][0].isalpha():
        raise ValueError("first name and surname, e.g. Thabo Mokoena")
    return f"{parts[0][0].upper()}. {parts[-1]}"


class AddDemoShapIdRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str
    full_name: str = Field(max_length=100)
    bank: str  # the API code ("FNB") or the slug ("fnb")

    @field_validator("number")
    @classmethod
    def _number(cls, value: str) -> str:
        return normalise_number(value)

    @field_validator("full_name")
    @classmethod
    def _name(cls, value: str) -> str:
        return mask_name(value)

    @field_validator("bank")
    @classmethod
    def _bank(cls, value: str) -> str:
        slug = BANK_IDS_BY_API_CODE.get(value.upper(), value.lower())
        if slug not in BANK_IDS:
            raise ValueError("one of: " + ", ".join(sorted(BANK_API_CODES.values())))
        return slug


class DemoShapIdResponse(BaseModel):
    number: str
    shap_name: str
    bank: str

    @classmethod
    def of(cls, entry: DemoShapId) -> "DemoShapIdResponse":
        return cls(number=entry.number, shap_name=entry.shap_name, bank=BANK_API_CODES[entry.bank_id])
