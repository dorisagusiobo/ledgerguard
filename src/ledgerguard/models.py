"""Shared data contracts; audit rules, not schemas, check invoice arithmetic."""
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, WithJsonSchema, model_validator


def reject_float(value: object) -> object:
    if isinstance(value, float):
        raise ValueError('Use a decimal string or Decimal, never a float')
    return value


DECIMAL_STRING = WithJsonSchema({'type': 'string', 'pattern': r'^\d+(\.\d+)?$'})
Money = Annotated[
    Decimal, BeforeValidator(reject_float),
    Field(ge=0, allow_inf_nan=False, decimal_places=2), DECIMAL_STRING,
]
UnitPrice = Annotated[
    Decimal, BeforeValidator(reject_float),
    Field(ge=0, allow_inf_nan=False, decimal_places=4), DECIMAL_STRING,
]
Quantity = Annotated[
    Decimal, BeforeValidator(reject_float), Field(gt=0, allow_inf_nan=False), DECIMAL_STRING,
]
Text = Annotated[str, Field(min_length=1)]
FindingType = Literal[
    'duplicate', 'contract_overcharge', 'policy_violation', 'split_purchase',
    'unusual_amount', 'arithmetic_error', 'extraction_issue',
]
FindingStatus = Literal['pending', 'approved', 'rejected']


class Schema(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class SourceRef(Schema):
    page: int = Field(ge=1, strict=True)
    text: Text = Field(
        description='The full printed row, every cell, exactly: description | quantity | unit price | amount'
    )


class LineItem(Schema):
    description: Text
    quantity: Quantity
    unit_price: UnitPrice
    amount: Money
    source: SourceRef


class Invoice(Schema):
    invoice_number: Text
    vendor_name: Text
    po_number: Text | None = None
    invoice_date: date
    due_date: date
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    subtotal: Money
    tax: Money
    total: Money
    line_items: list[LineItem] = Field(min_length=1)


class Citation(Schema):
    document: Text
    version: Text
    clause_id: Text
    quote: Text


class Finding(Schema):
    type: FindingType
    invoice_numbers: list[Text] = Field(min_length=1)
    amount_at_issue: Money
    calculation: Text
    evidence: list[SourceRef] = Field(min_length=1)
    citation: Citation | None = None
    no_clause_reason: Literal['no applicable clause identified'] | None = None
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    status: FindingStatus = 'pending'

    @model_validator(mode='after')
    def require_citation_or_reason(self) -> Self:
        if (self.citation is None) == (self.no_clause_reason is None):
            raise ValueError('Provide exactly one of citation or no_clause_reason')
        return self
