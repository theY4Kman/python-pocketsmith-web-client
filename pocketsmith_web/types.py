import datetime
from typing import Any, Dict, List, Optional, TypedDict, Union

__all__ = [
    'Category',
    'Transaction',
    'MfaFormInfo',
]


class Category(TypedDict):
    id: int
    title: str
    parent_id: Optional[int]
    summary_override: str
    colour: Optional[str]
    is_transfer: Union[int, bool]


class Transaction(TypedDict):
    id: int
    date: datetime.date

    amount: float
    payee: str
    original_payee: str
    memo: Optional[str]
    closing_balance: float

    account_id: int
    account_name: str
    currency_code: str

    is_pending: bool
    is_transfer: bool

    filter_id: Optional[int]
    category_rule_id: Optional[int]

    category_id: int
    category_title: str
    category_colour: Optional[str]
    category_is_transfers: bool
    category_hierarchy: List[Dict[str, Any]]

    images_count: int
    documents_count: int
    attachments_count: int
    has_xero_receipts: bool


class MfaFormInfo(TypedDict):
    action: str
    uys_id: str
    item_id: str
    label: str
    seconds_remaining: Optional[int]
