"""Explicit YES/NO outcomes with authoritative event and series definitions."""

from ..contracts import Projection, require, strict_json
from . import coverage_row, native_row

_PRICE_FIELDS = {
    "yes_bid",
    "yes_ask",
    "no_bid",
    "no_ask",
    "last_price",
    "previous_price",
    "previous_yes_bid",
    "previous_yes_ask",
    "volume",
    "volume_24h",
    "open_interest",
    "liquidity",
    "dollar_volume",
    "dollar_open_interest",
}


def _definition(row):
    payload = strict_json(row["payload_json"])
    require(isinstance(payload, dict), "native payload must be an object")
    return {
        k: v
        for k, v in payload.items()
        if k not in _PRICE_FIELDS
        and not any(k.startswith(prefix + "_") for prefix in _PRICE_FIELDS)
        and k != "markets"
    }


def adapt(bundle):
    markets = {r["market_ticker"]: r for r in bundle.relations["markets"]}
    events = {r["event_ticker"]: r for r in bundle.relations["events"]}
    series = {r["series_ticker"]: r for r in bundle.relations["series"]}
    native, coverage = [], []
    for source in bundle.relations["coverage"]:
        market = markets.get(source["market_ticker"])
        usable = market["usable"] if market else None
        row = coverage_row(bundle, source, 2 if market else 0, usable)
        if market and usable and source["status"] == "found":
            event, parent = events[market["event_ticker"]], series[market["series_ticker"]]
            for side in ("YES", "NO"):
                key = {
                    "venue": "kalshi",
                    "market_id": market["market_ticker"],
                    "side": side,
                    "event_id": market["event_ticker"],
                    "series_id": market["series_ticker"],
                }
                definition = {
                    "market": _definition(market),
                    "outcome": {"side": side, "label": side.title()},
                    "parents": {"event": _definition(event), "series": _definition(parent)},
                }
                native.append(
                    native_row(
                        bundle,
                        market,
                        definition,
                        key,
                        side=side,
                        outcome_label=side.title(),
                        event_id=market["event_ticker"],
                        series_id=market["series_ticker"],
                    )
                )
            row["usable_outcome_count"] = row["exported_outcome_count"] = 2
        coverage.append(row)
    return Projection(native, coverage)
