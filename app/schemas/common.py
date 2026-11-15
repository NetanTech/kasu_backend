from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

# Amounts in the API are in major units (naira), max 2 decimal places, always positive.
Money = Annotated[Decimal, Field(gt=0, max_digits=16, decimal_places=2)]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)
