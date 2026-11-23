"""Crypto.com jurisdiction rules. Conservative configuration, not account-level evidence."""

from shadow_trading_suite.core.instrument import Instrument, InstrumentKind, Venue


def derivatives_rule(inst: Instrument) -> str | None:
    if inst.id.venue is Venue.CRYPTOCOM and inst.kind is not InstrumentKind.SPOT:
        return (
            "DE jurisdiction policy (conservative, revisable): "
            "Crypto.com derivatives are treated as unavailable for German accounts. "
            "Policy configuration, not account-verified evidence. Paper simulation is unaffected."
        )
    return None


DE_RULES = (derivatives_rule,)
