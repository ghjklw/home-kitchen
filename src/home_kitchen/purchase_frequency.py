"""Coverage-aware, per-provider, exponentially recency-weighted purchase frequency logic."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from itertools import pairwise
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

DEFAULT_HALF_LIFE_DAYS = 90.0
PROVIDER_RETENTION_MONTHS: dict[str, int] = {
    "trumf": 12,
    "rema1000": 24,
}


class ProviderObservationWindow(BaseModel):
    """Actual historical observation span and completeness metadata for one provider."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    total_records: int = Field(default=0, ge=0)
    retained_months: int | None = None
    non_receipt_stub_count: int = Field(default=0, ge=0)
    incomplete_history: bool = True

    @property
    def observation_days(self) -> int:
        if self.start is None or self.end is None:
            return 0
        return max(0, (self.end.date() - self.start.date()).days)


class ProviderCoverageSummary(BaseModel):
    """Per-provider coverage summary attached to a product's purchase frequency."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    sample_size: int = Field(default=0, ge=0)
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    observation_days: int = Field(default=0, ge=0)
    retained_months: int | None = None
    incomplete_history: bool = True


class FrequencyAnalysis(BaseModel):
    """Structured result of recency-weighted, coverage-aware frequency analysis."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["known", "unknown"]
    average_interval_days: float | None = None
    recency_weighted_interval_days: float | None = None
    weighted_purchases_per_30_days: float | None = None
    recency_score: float = 0.0
    sample_size: int = 0
    first_purchased_at: AwareDatetime | None = None
    last_purchased_at: AwareDatetime | None = None
    date_range_days: int | None = None
    coverage_days: int | None = None
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS
    is_lower_bound: bool = True
    confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    provider_windows: list[ProviderCoverageSummary] = Field(default_factory=list)
    coverage_note: str | None = None
    frequency_display: str = "unknown"
    provider_verified_count: int = 0
    user_confirmed_count: int = 0
    uncertain_count: int = 0


def _to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def normalize_provider_key(value: str | None) -> str:
    cleaned = str(value or "").strip().casefold()
    if not cleaned:
        return "unknown"
    if cleaned in {"receipt", "retailer_order"}:
        return "oda"
    return cleaned


def recency_weight(
    occurred_at: datetime,
    *,
    reference_time: datetime,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """Compute exponential half-life decay weight for a timestamp relative to reference_time."""
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    t_utc = _to_utc(occurred_at)
    ref_utc = _to_utc(reference_time)
    assert t_utc is not None and ref_utc is not None
    age_days = max(0.0, (ref_utc - t_utc).total_seconds() / 86400.0)
    return float(2.0 ** (-age_days / half_life_days))


def merge_observation_intervals(
    intervals: Sequence[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Merge overlapping [start, end] observation windows without double-counting spans."""
    cleaned: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        s_utc = _to_utc(start)
        e_utc = _to_utc(end)
        if s_utc is None or e_utc is None:
            continue
        if e_utc < s_utc:
            s_utc, e_utc = e_utc, s_utc
        cleaned.append((s_utc, e_utc))
    if not cleaned:
        return []
    cleaned.sort(key=lambda pair: (pair[0], pair[1]))
    merged: list[tuple[datetime, datetime]] = [cleaned[0]]
    for start, end in cleaned[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _merged_span_days(intervals: Sequence[tuple[datetime, datetime]]) -> int:
    merged = merge_observation_intervals(intervals)
    if not merged:
        return 0
    total_days = 0
    # Merge on date granularity as well so adjacent intra-day windows don't drop/double count days
    date_ranges = sorted((s.date(), e.date()) for s, e in merged)
    cur_start, cur_end = date_ranges[0]
    for s_date, e_date in date_ranges[1:]:
        if s_date <= cur_end:
            cur_end = max(cur_end, e_date)
        else:
            total_days += max(0, (cur_end - cur_start).days)
            cur_start, cur_end = s_date, e_date
    total_days += max(0, (cur_end - cur_start).days)
    return total_days


def _extract_event_provider(event: Any) -> str:
    prov = getattr(event, "provider", None)
    if isinstance(prov, str) and prov.strip():
        return normalize_provider_key(prov)
    src = getattr(event, "evidence_source", None)
    if isinstance(src, str) and src.strip():
        return normalize_provider_key(src)
    return "unknown"


def _build_coverage_note(
    *,
    status: Literal["known", "unknown"],
    provider_summaries: Sequence[ProviderCoverageSummary],
    coverage_days: int | None,
) -> str:
    if status == "unknown":
        return "For få bekreftede kjøp til å beregne frekvens; observert historikk kan være ufullstendig."
    capped = [
        p for p in provider_summaries if p.retained_months is not None or p.incomplete_history
    ]
    span_txt = (
        f"{coverage_days} dagers observasjonsvindu" if coverage_days else "tilgjengelig vindu"
    )
    if capped:
        return (
            f"Nedre grense beregnet over {span_txt} med eksponentiell vekting mot nyere kjøp "
            "(historikk fra enkelte butikker er tidsbegrenset eller ufullstendig)."
        )
    return f"Nedre grense beregnet over {span_txt} med eksponentiell vekting mot nyere kjøp."


def compute_recency_weighted_frequency(
    events: Sequence[Any],
    *,
    provider_windows: Mapping[str, ProviderObservationWindow] | None = None,
    as_of: datetime | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> FrequencyAnalysis:
    """Compute a recency-weighted, per-provider-windowed, lower-bound purchase frequency.

    Accounts for:
    - Provenance separation: only ``provider_verified`` and ``user_confirmed`` events count.
    - Per-provider coverage windows: avoids dividing a 12-month retailer's purchases by a
      3-year global span from another retailer.
    - Changing household habits: applies exponential half-life decay to inter-purchase
      intervals and accounts for prolonged silence within active provider windows.
    - Incomplete history: marks estimates as lower bounds (``is_lower_bound=True``) and
      reports effective coverage and confidence.
    """
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")

    provider_verified_count = sum(
        1 for e in events if getattr(e, "provenance", None) == "provider_verified"
    )
    user_confirmed_count = sum(
        1 for e in events if getattr(e, "provenance", None) == "user_confirmed"
    )
    uncertain_count = sum(1 for e in events if getattr(e, "provenance", None) == "uncertain")

    confirmed_with_dt: list[tuple[datetime, str]] = []
    for event in events:
        if getattr(event, "provenance", None) not in ("provider_verified", "user_confirmed"):
            continue
        p_at = _to_utc(getattr(event, "purchased_at", None))
        if p_at is None:
            continue
        confirmed_with_dt.append((p_at, _extract_event_provider(event)))

    confirmed_with_dt.sort(key=lambda pair: pair[0])
    sample_size = len(confirmed_with_dt)

    normalized_windows: dict[str, ProviderObservationWindow] = {}
    if provider_windows:
        for key, win in provider_windows.items():
            norm_key = normalize_provider_key(key)
            normalized_windows[norm_key] = win.model_copy(
                update={
                    "provider": norm_key,
                    "start": _to_utc(win.start),
                    "end": _to_utc(win.end),
                }
            )

    by_provider_times: dict[str, list[datetime]] = defaultdict(list)
    for p_at, prov in confirmed_with_dt:
        by_provider_times[prov].append(p_at)

    provider_summaries: list[ProviderCoverageSummary] = []
    relevant_intervals: list[tuple[datetime, datetime]] = []
    for prov in sorted(by_provider_times):
        times = by_provider_times[prov]
        win = normalized_windows.get(prov)
        w_start = min(times)
        w_end = max(times)
        retained = PROVIDER_RETENTION_MONTHS.get(prov)
        incomplete = True
        if win is not None:
            if win.start is not None:
                w_start = min(w_start, win.start)
            if win.end is not None:
                w_end = max(w_end, win.end)
            retained = win.retained_months if win.retained_months is not None else retained
            incomplete = (
                win.incomplete_history or win.non_receipt_stub_count > 0 or retained is not None
            )
        relevant_intervals.append((w_start, w_end))
        provider_summaries.append(
            ProviderCoverageSummary(
                provider=prov,
                sample_size=len(times),
                window_start=w_start,
                window_end=w_end,
                observation_days=max(0, (w_end.date() - w_start.date()).days),
                retained_months=retained,
                incomplete_history=incomplete,
            )
        )

    # Also track active observation end across household providers when provider_windows was supplied
    active_window_ends = [win.end for win in normalized_windows.values() if win.end is not None]
    explicit_windows_supplied = bool(normalized_windows)

    if sample_size == 0:
        return FrequencyAnalysis(
            status="unknown",
            sample_size=0,
            coverage_days=_merged_span_days(relevant_intervals) if relevant_intervals else None,
            half_life_days=half_life_days,
            is_lower_bound=True,
            confidence="unknown",
            provider_windows=provider_summaries,
            coverage_note=_build_coverage_note(
                status="unknown",
                provider_summaries=provider_summaries,
                coverage_days=None,
            ),
            frequency_display="unknown",
            provider_verified_count=provider_verified_count,
            user_confirmed_count=user_confirmed_count,
            uncertain_count=uncertain_count,
        )

    first_at = confirmed_with_dt[0][0]
    last_at = confirmed_with_dt[-1][0]
    date_range_days = (last_at.date() - first_at.date()).days
    coverage_days = _merged_span_days(relevant_intervals) if relevant_intervals else date_range_days

    if as_of is not None:
        reference_time = _to_utc(as_of)
        assert reference_time is not None
        if reference_time < last_at:
            reference_time = last_at
    elif explicit_windows_supplied and active_window_ends:
        reference_time = max(last_at, *(s.window_end for s in provider_summaries if s.window_end))
    else:
        reference_time = last_at

    total_recency_score = round(
        sum(
            recency_weight(p_at, reference_time=reference_time, half_life_days=half_life_days)
            for p_at, _ in confirmed_with_dt
        ),
        3,
    )

    distinct_dates: list[date] = sorted({p_at.date() for p_at, _ in confirmed_with_dt})
    if sample_size < 2 or date_range_days <= 0 or len(distinct_dates) < 2:
        return FrequencyAnalysis(
            status="unknown",
            recency_score=total_recency_score,
            sample_size=sample_size,
            first_purchased_at=first_at,
            last_purchased_at=last_at,
            date_range_days=date_range_days,
            coverage_days=coverage_days,
            half_life_days=half_life_days,
            is_lower_bound=True,
            confidence="unknown",
            provider_windows=provider_summaries,
            coverage_note=_build_coverage_note(
                status="unknown",
                provider_summaries=provider_summaries,
                coverage_days=coverage_days,
            ),
            frequency_display="unknown",
            provider_verified_count=provider_verified_count,
            user_confirmed_count=user_confirmed_count,
            uncertain_count=uncertain_count,
        )

    weighted_rate_sum = 0.0
    weight_sum = 0.0
    for prev_date, next_date in pairwise(distinct_dates):
        delta_days = float(max(1, (next_date - prev_date).days))
        rate = 1.0 / delta_days
        mid_dt = datetime.combine(prev_date, dt_time.min, tzinfo=UTC) + timedelta(
            days=delta_days / 2.0
        )
        w = recency_weight(mid_dt, reference_time=reference_time, half_life_days=half_life_days)
        weighted_rate_sum += w * rate
        weight_sum += w

    raw_weighted_rate = weighted_rate_sum / weight_sum if weight_sum > 0 else 0.0
    raw_cadence_days = 1.0 / raw_weighted_rate if raw_weighted_rate > 0 else float(date_range_days)

    # Account for prolonged silence within the active observation window(s) of relevant providers
    relevant_end = last_at
    if explicit_windows_supplied or as_of is not None:
        candidate_ends = [s.window_end for s in provider_summaries if s.window_end is not None]
        if as_of is not None:
            candidate_ends.append(reference_time)
        if candidate_ends:
            relevant_end = max(candidate_ends)

    trailing_days = float(max(0, (relevant_end.date() - last_at.date()).days))
    grace_days = max(2.0 * raw_cadence_days, 14.0)
    w_silent = 0.0
    if trailing_days > grace_days:
        silent_days = trailing_days - grace_days
        missed_cycles = silent_days / max(raw_cadence_days, 1.0)
        silent_mid_dt = datetime.combine(last_at.date(), dt_time.min, tzinfo=UTC) + timedelta(
            days=grace_days + silent_days / 2.0
        )
        w_silent = missed_cycles * recency_weight(
            silent_mid_dt, reference_time=reference_time, half_life_days=half_life_days
        )

    effective_rate = (
        weighted_rate_sum / (weight_sum + w_silent) if (weight_sum + w_silent) > 0 else 0.0
    )
    weighted_interval = round(1.0 / effective_rate, 1) if effective_rate > 0 else None
    weighted_per_30 = round(effective_rate * 30.0, 2) if effective_rate > 0 else None

    if weighted_interval is None:
        display = "unknown"
        status: Literal["known", "unknown"] = "unknown"
        confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    else:
        status = "known"
        display = f"every {weighted_interval:g} days" if weighted_interval != 1 else "every 1 day"
        if (
            sample_size >= 6
            and coverage_days >= 30
            and trailing_days <= max(3.0 * weighted_interval, 45.0)
        ):
            confidence = "high"
        elif sample_size >= 3:
            confidence = "medium"
        else:
            confidence = "low"

    return FrequencyAnalysis(
        status=status,
        average_interval_days=weighted_interval,
        recency_weighted_interval_days=weighted_interval,
        weighted_purchases_per_30_days=weighted_per_30,
        recency_score=total_recency_score,
        sample_size=sample_size,
        first_purchased_at=first_at,
        last_purchased_at=last_at,
        date_range_days=date_range_days,
        coverage_days=coverage_days,
        half_life_days=half_life_days,
        is_lower_bound=True,
        confidence=confidence,
        provider_windows=provider_summaries,
        coverage_note=_build_coverage_note(
            status=status,
            provider_summaries=provider_summaries,
            coverage_days=coverage_days,
        ),
        frequency_display=display,
        provider_verified_count=provider_verified_count,
        user_confirmed_count=user_confirmed_count,
        uncertain_count=uncertain_count,
    )
