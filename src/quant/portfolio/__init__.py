"""Layer 4, portfolio: what to hold and what to trade. Imports core to models; no account or broker access.

    model         the traded model's parameters, in one place
    broker        which broker the paper path uses
    targets       the target book from the newest scores
    shorts        the short basket
    schedule      Tuesday slots and tranche targets
    plan          order rules, the plan hash, the tranche plan from a target book
    accounting    the monthly account return under the paper account's terms
    shadow        the forward shadow arms recorded beside the live book (registered code)
"""
