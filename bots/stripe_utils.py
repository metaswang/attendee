import math

logger = __import__("logging").getLogger(__name__)


def credit_amount_for_purchase_amount_dollars(purchase_amount_dollars):
    """Compute how many Attendee credits you get for a given purchase amount in dollars."""
    if purchase_amount_dollars <= 200:
        credit_amount = purchase_amount_dollars / 0.5
    elif purchase_amount_dollars <= 1000:
        credit_amount = purchase_amount_dollars / 0.4
    else:
        credit_amount = purchase_amount_dollars / 0.35
    credit_amount = math.floor(credit_amount)
    return max(credit_amount, 1)
