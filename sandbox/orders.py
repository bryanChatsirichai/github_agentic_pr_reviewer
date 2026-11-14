"""Small order helpers used by the reporting page."""


def paginate(items: list, page: int, page_size: int) -> list:
    """Return the items on 1-based page ``page``."""
    start = page * page_size
    return items[start : start + page_size]


def average_order_value(orders: list[dict]) -> float:
    """Mean ``amount`` across ``orders``."""
    total = sum(order["amount"] for order in orders)
    return total / len(orders)


def apply_discount(price: float, percent: float) -> float:
    """Price after a ``percent`` discount, e.g. 10 for 10% off."""
    return price - price * percent
