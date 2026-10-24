"""Request and response bodies for the demo risk switch (/demo/risk)."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class ForceRiskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # warn or review only: forcing allow would just switch the protection off.
    band: Literal["warn", "review"]
    # Expires by itself, so a switch left on after the demo cannot keep
    # refusing real users. The upper bound is RISK_DEMO_FORCE_MAX_MINUTES.
    minutes: Annotated[StrictInt, Field(gt=0)] = 10


class ForcedRiskResponse(BaseModel):
    band: Literal["warn", "review"] | None  # None: real scoring
    expires_at: datetime | None
