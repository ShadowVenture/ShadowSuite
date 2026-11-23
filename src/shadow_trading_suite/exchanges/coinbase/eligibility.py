"""Coinbase jurisdiction rules. Conservative configuration, not account-level evidence."""

from shadow_trading_suite.core.instrument import Instrument, Venue


def us_fcm_rule(inst: Instrument) -> str | None:
    if inst.id.venue is Venue.COINBASE and inst.sub_venue == "FCM":
        return (
            "DE jurisdiction policy (conservative, revisable): Coinbase FCM products are offered "
            "by a US futures commission merchant and are treated as unavailable. This is not an "
            "account-specific determination."
        )
    return None


# Coinbase INTX perpetuals deliberately have no rule: eligibility stays unknown until verified.
DE_RULES = (us_fcm_rule,)
