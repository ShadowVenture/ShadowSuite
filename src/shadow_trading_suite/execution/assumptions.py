"""Versioned, content-hashed simulation assumptions and deterministic latency profiles (§3.1, §7).

Every parameter is labelled:
- VERIFIED: backed by recorded evidence (fixture path, dated probe or document reference);
- ASSUMPTION: a simulation parameter chosen by us, never claimed as venue behaviour;
- DERIVED: computed from VERIFIED inputs by a documented formula (inputs are named).

The set's SHA-256 over its canonical JSON is stored with every execution record, so each fill,
funding charge and liquidation names the exact assumptions it depends on. Changing a value
without bumping `version` is caught by a hash-pinning test.
"""

import hashlib
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from shadow_trading_suite.core.model import DomainModel
from shadow_trading_suite.risk.config import PortfolioConfig
from shadow_trading_suite.storage.ledger import canonical_json


class Source(StrEnum):
    VERIFIED = "verified"
    ASSUMPTION = "assumption"
    DERIVED = "derived"


class Parameter(DomainModel):
    value: str
    source: Source
    evidence: str | None = None
    inputs: tuple[str, ...] = ()
    note: str = ""

    @model_validator(mode="after")
    def _traceable(self) -> Self:
        if self.source is Source.VERIFIED and not self.evidence:
            raise ValueError("a VERIFIED parameter needs evidence")
        if self.source is Source.DERIVED and not self.inputs:
            raise ValueError("a DERIVED parameter must name its inputs")
        return self


class LatencyProfile(DomainModel):
    """All values are ASSUMPTIONS (milliseconds of venue market time)."""

    profile_id: str = Field(pattern=r"^[a-z_]{1,32}$")
    submit_ms: int = Field(ge=0)
    cancel_ms: int = Field(ge=0)
    liquidation_ms: int = Field(ge=0)
    jitter_ms: int = Field(ge=0)


PROFILES: dict[str, LatencyProfile] = {
    p.profile_id: p
    for p in (
        LatencyProfile(profile_id="ideal", submit_ms=0, cancel_ms=0, liquidation_ms=0, jitter_ms=0),
        LatencyProfile(
            profile_id="colocated", submit_ms=20, cancel_ms=20, liquidation_ms=20, jitter_ms=5
        ),
        LatencyProfile(
            profile_id="retail_eu", submit_ms=250, cancel_ms=250, liquidation_ms=250, jitter_ms=50
        ),
        LatencyProfile(
            profile_id="degraded",
            submit_ms=1000,
            cancel_ms=1500,
            liquidation_ms=1000,
            jitter_ms=250,
        ),
        LatencyProfile(
            profile_id="stressed",
            submit_ms=3000,
            cancel_ms=5000,
            liquidation_ms=3000,
            jitter_ms=1000,
        ),
    )
}
DEFAULT_PROFILE = "retail_eu"


class LatencyAction(StrEnum):
    SUBMIT = "submit"
    CANCEL = "cancel"
    LIQUIDATION = "liquidation"


def jitter_ms(profile: LatencyProfile, seed: str, subject: str, action: LatencyAction) -> Decimal:
    """(SHA-256(profile|seed|subject|action) mod 10^6 / 10^6 * 2 - 1) * jitter_ms: no RNG
    state, identical on replay."""
    digest = hashlib.sha256(f"{profile.profile_id}|{seed}|{subject}|{action}".encode()).digest()
    unit = Decimal(int.from_bytes(digest, "big") % 1_000_000) / Decimal(1_000_000)
    return (unit * 2 - 1) * profile.jitter_ms


def latency(profile: LatencyProfile, seed: str, subject: str, action: LatencyAction) -> timedelta:
    base = {
        LatencyAction.SUBMIT: profile.submit_ms,
        LatencyAction.CANCEL: profile.cancel_ms,
        LatencyAction.LIQUIDATION: profile.liquidation_ms,
    }[action]
    total = max(Decimal(0), base + jitter_ms(profile, seed, subject, action))
    return timedelta(microseconds=int(total * 1000))


class AssumptionSet(DomainModel):
    set_id: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    parameters: dict[str, Parameter]
    profiles: tuple[LatencyProfile, ...]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json")).encode()).hexdigest()

    def decimal(self, name: str) -> Decimal:
        return Decimal(self.parameters[name].value)

    def text(self, name: str) -> str:
        return self.parameters[name].value

    def profile(self, profile_id: str) -> LatencyProfile:
        for p in self.profiles:
            if p.profile_id == profile_id:
                return p
        raise KeyError(f"unknown latency profile {profile_id!r}")

    def by_source(self, source: Source) -> list[str]:
        return sorted(k for k, p in self.parameters.items() if p.source is source)


_PROBE = "docs/venue-notes.md (public probes 2026-10-19)"
_CHANNELS = "docs/venue-notes.md (public channels, live probe 2026-10-19)"


def _a(value: object, note: str) -> Parameter:
    return Parameter(value=str(value), source=Source.ASSUMPTION, note=note)


def _v(value: object, evidence: str, note: str = "") -> Parameter:
    return Parameter(value=str(value), source=Source.VERIFIED, evidence=evidence, note=note)


def default_assumption_set(cfg: PortfolioConfig) -> AssumptionSet:
    """M6-B defaults. Fees and margins are the unchanged M5 ASSUMPTION values (R8)."""
    fees, margin, lim = cfg.fees, cfg.margin, cfg.limits
    params = {
        # VERIFIED venue facts (recorded evidence)
        "book.depth.coinbase": _v(50, _PROBE, "level2 is full depth; top 50 published"),
        "book.depth.cryptocom": _v(50, _PROBE, "book.{sym}.50 LIVE 50x50, 0 gaps"),
        "trade.aggressor.cryptocom": _v("taker_side", _CHANNELS, "`s` is the taker side"),
        "trade.aggressor.coinbase": _v("unknown", _CHANNELS, "side semantics undocumented"),
        "mark.cryptocom": _v("mark.{sym} stream", _CHANNELS),
        "funding.cryptocom.interval_s": _v(
            3600, "docs/venue-notes.md: funding_hist spacing, hourly"
        ),
        # fees (M5 values, unchanged; R8)
        "fee.spot_taker": _a(fees.spot_taker.value, "M5 fee assumption"),
        "fee.spot_maker": _a(fees.spot_maker.value, "M5 fee assumption"),
        "fee.derivatives_taker": _a(fees.derivatives_taker.value, "M5 fee assumption"),
        "fee.derivatives_maker": _a(fees.derivatives_maker.value, "M5 fee assumption"),
        "fee.maker_rebates": _a("none", "no maker rebates"),
        # matching
        "price_band.min": _a("0.0025", "25 bp minimum protection band"),
        "price_band.slippage_multiple": _a(2, "band = max(2 x M5 slippage estimate, 25 bp)"),
        "liquidity.replenish_ms": _a(1000, "consumed displayed size returns after 1 s"),
        "queue.model": _a("conservative_e2", "queue ahead = displayed size when resting"),
        "queue.coinbase_unknown_aggressor": _a(
            "at_price_reduces_queue_only", "Coinbase: only trades through the price fill"
        ),
        "order.gtc_lifetime_s": _a(86400, "GTC maximum lifetime 24 h"),
        "latency.seed": _a("m6b-v1", "deterministic jitter seed"),
        # data integrity
        "gap.cancel_after_s": _a(5, "venue DOWN > 5 s cancels resting orders (E4)"),
        "mark.missing_emergency_s": _a(5, "mark unavailable > 5 s triggers emergency"),
        "data.max_age_spot_s": _a(lim.max_data_age_spot_s, "M5 limit"),
        "data.max_age_derivatives_s": _a(lim.max_data_age_derivatives_s, "M5 limit"),
        # liquidation and funding (R7)
        "liquidation.model": _a("sim-isolated-v1", "simulated isolated margin"),
        "liquidation.trigger_policy": _a("trigger_on_breach", "no grace, no partial, no ADL"),
        "liquidation.fee_rate": _a("0.005", "of executed notional, replaces the taker fee"),
        "liquidation.price_band": _a("none", "B6: no band; bounded by visible fresh depth"),
        "mmr.cryptocom_major": _a(margin.cryptocom_major_mmr.value, "M5 assumption"),
        "mmr.cryptocom_other": _a(margin.cryptocom_other_mmr.value, "M5 assumption"),
        "mmr.cryptocom_major_bases": _a(
            ",".join(sorted(margin.cryptocom_major_bases)), "M5 assumption"
        ),
        "insurance.absorbs_beyond_margin": _a("true", "simulated recovery, never income"),
        "funding.settlement": _a(
            "interval_boundary_last_rate",
            "charged at each interval boundary with the last published rate; cash, then "
            "margin, then an outstanding payable (B5)",
        ),
        "liquidation.trigger_price": Parameter(
            value="equity(mark) < mmr x notional + taker fee x notional",
            source=Source.DERIVED,
            inputs=("mmr.cryptocom_major", "mmr.cryptocom_other", "fee.derivatives_taker"),
            note="equivalent to the M5 isolated-margin liquidation price formula",
        ),
        "eur.rate": Parameter(
            value="mid(BTC-EUR) / mid(BTC-USD)",
            source=Source.DERIVED,
            inputs=("coinbase BTC-EUR ticker", "coinbase BTC-USD ticker"),
            note="pure function only; the live subscription is M6-C",
        ),
    }
    return AssumptionSet(
        set_id="suite-default",
        version="1.0.0",
        parameters=params,
        profiles=tuple(PROFILES.values()),
    )
