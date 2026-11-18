"""Finding a customer id in free text."""

from __future__ import annotations

import re

# Ids shaped like CUST-1001. The separator is required: without it the pattern matched
# ordinary words ("customers", "accounting") as ids.
_CUSTOMER_ID = re.compile(r"\b(?:cust|customer|acct|account)[_-][A-Za-z0-9]{2,60}\b", re.IGNORECASE)


def extract_customer_id(text: str) -> str | None:
    match = _CUSTOMER_ID.search(text)
    # Ids are stored upper-case (CUST-1001), whatever case the person typed.
    return match.group(0).upper() if match else None
