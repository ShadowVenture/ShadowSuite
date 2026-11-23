"""Instrument registry: exchange-neutral instruments plus separately stored eligibility."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from shadow_trading_suite.core.instrument import (
    AssetClass,
    Instrument,
    InstrumentId,
    InstrumentKind,
    Venue,
)
from shadow_trading_suite.core.time import Clock, SystemClock
from shadow_trading_suite.exchanges.base import DiscoveryResult
from shadow_trading_suite.exchanges.errors import ExchangeError
from shadow_trading_suite.instruments.eligibility import (
    Eligibility,
    EligibilityResolver,
    EligibilityStatus,
    germany_policy,
)

DEFAULT_ASSET_CLASSES = frozenset({AssetClass.CRYPTO})


class UnknownInstrumentError(KeyError):
    pass


def unified_symbol(inst: Instrument) -> str:
    """Venue-independent symbol: BASE/QUOTE[:SETTLE[-YYMMDD]]."""
    symbol = f"{inst.base}/{inst.quote}"
    if inst.kind is InstrumentKind.SPOT:
        return symbol
    symbol = f"{symbol}:{inst.settle}"
    if inst.kind is InstrumentKind.DATED_FUTURE and inst.expiry is not None:
        symbol = f"{symbol}-{inst.expiry:%y%m%d}"
    return symbol


@dataclass(frozen=True)
class RefreshReport:
    counts: Mapping[Venue, int]
    rejected: Mapping[Venue, int]
    duplicates: tuple[InstrumentId, ...] = ()
    eligibility_conflicts: tuple[InstrumentId, ...] = ()


class InstrumentSource(Protocol):
    @property
    def venue(self) -> Venue: ...

    def list_instruments(self) -> DiscoveryResult: ...


def discover_all(
    sources: Iterable[InstrumentSource],
) -> tuple[list[DiscoveryResult], dict[Venue, ExchangeError]]:
    """Discover per venue; an unavailable venue does not block the others.

    Only ExchangeError is isolated. Safety violations always propagate.
    """
    results: list[DiscoveryResult] = []
    errors: dict[Venue, ExchangeError] = {}
    for source in sources:
        try:
            results.append(source.list_instruments())
        except ExchangeError as exc:
            errors[source.venue] = exc
    return results, errors


def _default_resolver() -> EligibilityResolver:
    return EligibilityResolver(policy=germany_policy())


class InstrumentRegistry:
    def __init__(
        self,
        eligibility: EligibilityResolver | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.eligibility_resolver = eligibility or _default_resolver()
        self.clock = clock or SystemClock()
        self._by_venue: dict[Venue, dict[InstrumentId, Instrument]] = {}
        self._eligibility: dict[InstrumentId, Eligibility] = {}
        self._refreshed_at: dict[Venue, datetime] = {}

    def refresh(self, results: Iterable[DiscoveryResult]) -> RefreshReport:
        now = self.clock.now()
        staged: dict[Venue, dict[InstrumentId, Instrument]] = {}
        staged_eligibility: dict[InstrumentId, Eligibility] = {}
        rejected: dict[Venue, int] = {}
        duplicates: list[InstrumentId] = []
        conflicts: list[InstrumentId] = []
        for res in results:
            snapshot: dict[InstrumentId, Instrument] = {}
            for inst in res.instruments:
                if inst.id.venue is not res.venue:
                    raise ValueError(f"instrument {inst.id} does not belong to venue {res.venue}")
                if inst.id in snapshot:
                    duplicates.append(inst.id)
                    continue
                if self.eligibility_resolver.conflicts(inst):
                    conflicts.append(inst.id)
                snapshot[inst.id] = inst
                staged_eligibility[inst.id] = self.eligibility_resolver.resolve(inst, now)
            staged[res.venue] = snapshot
            rejected[res.venue] = len(res.rejected)
        for venue, snapshot in staged.items():  # commit only after every result validated
            for old_id in self._by_venue.get(venue, {}):
                self._eligibility.pop(old_id, None)
            self._by_venue[venue] = snapshot
            self._refreshed_at[venue] = now
        self._eligibility.update(staged_eligibility)
        return RefreshReport(
            counts={v: len(s) for v, s in staged.items()},
            rejected=rejected,
            duplicates=tuple(duplicates),
            eligibility_conflicts=tuple(conflicts),
        )

    def __len__(self) -> int:
        return sum(len(s) for s in self._by_venue.values())

    def refreshed_at(self, venue: Venue) -> datetime | None:
        return self._refreshed_at.get(venue)

    def get(self, instrument_id: InstrumentId) -> Instrument:
        try:
            return self._by_venue[instrument_id.venue][instrument_id]
        except KeyError:
            raise UnknownInstrumentError(str(instrument_id)) from None

    def eligibility(self, instrument_id: InstrumentId) -> Eligibility:
        try:
            return self._eligibility[instrument_id]
        except KeyError:
            raise UnknownInstrumentError(str(instrument_id)) from None

    def find(
        self,
        *,
        venue: Venue | None = None,
        kind: InstrumentKind | None = None,
        asset_classes: frozenset[AssetClass] = DEFAULT_ASSET_CLASSES,
        base: str | None = None,
        quote: str | None = None,
        eligibility: EligibilityStatus | None = None,
    ) -> list[Instrument]:
        venues = [venue] if venue is not None else list(self._by_venue)
        found = [
            inst
            for v in venues
            for inst in self._by_venue.get(v, {}).values()
            if inst.asset_class in asset_classes
            and (kind is None or inst.kind is kind)
            and (base is None or inst.base == base)
            and (quote is None or inst.quote == quote)
            and (eligibility is None or self._eligibility[inst.id].status is eligibility)
        ]
        return sorted(found, key=lambda i: str(i.id))

    def by_unified(
        self, symbol: str, asset_classes: frozenset[AssetClass] = DEFAULT_ASSET_CLASSES
    ) -> list[Instrument]:
        return [i for i in self.find(asset_classes=asset_classes) if unified_symbol(i) == symbol]
