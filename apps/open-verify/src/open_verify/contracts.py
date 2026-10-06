"""Closed data contracts shared by plans, steps, and engine adapters."""

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
