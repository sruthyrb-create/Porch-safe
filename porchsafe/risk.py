"""Porch exposure assessment: is this delivery inside its labeled temperature range, and for how long?

Inputs are explainable on purpose: the outdoor air temperature at the porch (National Weather
Service observation), the forecast for the next hours, minutes since delivery, and the item's
labeled ranges. The package's own temperature lags the air and depends on packaging, so the
air temperature is used as a conservative proxy and every status says so.

Statuses
  OK         inside the labeled storage range
  WATCH      outside the storage range but inside the label's permitted excursions; a time budget runs
  ACT_NOW    outside what the label permits (or budget used up / about to be): bring it inside now
  EXCEEDED   it has been in ACT_NOW long enough that the person should ask the pharmacy before using
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .profiles import Profile

Status = Literal["OK", "WATCH", "ACT_NOW", "EXCEEDED"]
RANK = {"OK": 0, "WATCH": 1, "ACT_NOW": 2, "EXCEEDED": 3}


@dataclass
class Assessment:
    status: Status
    headline: str  # one short sentence for a phone notification / screen
    reasons: list[str] = field(default_factory=list)
    minutes_on_porch: int = 0
    porch_temp_f: float | None = None
    minutes_until_act_now: int | None = None  # countdown for WATCH, or forecast-based warning for OK
    forecast_note: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _fmt(t: float) -> str:
    return f"{round(t)}°F"


def classify_temp(p: Profile, t: float) -> tuple[Status, str]:
    """Instantaneous classification of one temperature against the label."""
    if p.hard_max is not None and t >= p.hard_max:
        return "ACT_NOW", f"{_fmt(t)} is at or above the label's {_fmt(p.hard_max)} limit"
    if p.freeze_sensitive and t <= 32:
        return "ACT_NOW", f"{_fmt(t)} is at or below freezing, and the label says do not freeze"
    lo = p.store_min if p.store_min is not None else float("-inf")
    hi = p.store_max if p.store_max is not None else float("inf")
    if lo <= t <= hi:
        return "OK", f"{_fmt(t)} is inside the labeled range"
    if p.excursion_min is not None and p.excursion_max is not None and p.excursion_min <= t <= p.excursion_max:
        return "WATCH", f"{_fmt(t)} is outside the storage range but within permitted excursions ({_fmt(p.excursion_min)}-{_fmt(p.excursion_max)})"
    if p.freeze_sensitive and t < lo:
        return "ACT_NOW", f"{_fmt(t)} is below the labeled minimum of {_fmt(lo)} and close to freezing"
    if p.key == "perishable_food":
        return "WATCH", f"{_fmt(t)} is above 40°F, the cold-food limit"
    return "ACT_NOW", f"{_fmt(t)} is outside the range the label permits"


def assess(
    p: Profile,
    minutes_on_porch: int,
    porch_temp_f: float | None,
    forecast_f: list[tuple[int, float]] | None = None,  # (minutes from now, temp)
    minutes_in_act_now: int = 0,
    direct_sun: bool = False,
) -> Assessment:
    if porch_temp_f is None:
        return Assessment(
            status="WATCH",
            headline=f"The {p.short} is on the porch, but the current temperature is unavailable. Bring it in to be safe.",
            reasons=["No weather observation available for the porch location."],
            minutes_on_porch=minutes_on_porch,
        )

    status, why = classify_temp(p, porch_temp_f)
    reasons = [why + " (outdoor air temperature, used as a stand-in for the package)."]
    budget = p.watch_budget_min // 2 if direct_sun else p.watch_budget_min
    if direct_sun:
        reasons.append("The porch gets direct sun, so a package can run hotter than the air: time budgets are halved.")

    until: int | None = None
    if p.key == "perishable_food" and porch_temp_f > 40:
        budget = 60 if porch_temp_f > 90 else 120
        if direct_sun:
            budget //= 2
        reasons.append(f"Food-safety rule: no more than {budget // 60 if budget >= 60 else budget} "
                       f"{'hour' if budget == 60 else 'hours' if budget >= 60 else 'minutes'} above 40°F"
                       f"{' when it is over 90°F' if porch_temp_f > 90 else ''}.")
    if status == "WATCH":
        until = max(budget - minutes_on_porch, 0)
        if until == 0:
            status = "ACT_NOW"
            reasons.append(f"It has been outside the storage range for {minutes_on_porch} min, past the {budget}-minute budget.")
    if status == "ACT_NOW" and p.exceed_after_min and minutes_in_act_now >= p.exceed_after_min:
        status = "EXCEEDED"
        reasons.append(f"It has been in conditions the label does not permit for {minutes_in_act_now} min.")
    if p.key == "perishable_food" and porch_temp_f > 40 and minutes_on_porch >= budget:
        status = "EXCEEDED"

    forecast_note = ""
    if forecast_f and status in ("OK", "WATCH"):
        for mins, temp in sorted(forecast_f):
            s, w = classify_temp(p, temp)
            if RANK[s] > RANK[status]:
                forecast_note = f"National Weather Service forecast in about {mins} min: {w}."
                until = min(until, mins) if until is not None else mins
                break

    headline = {
        "OK": f"The {p.short} is on the porch. {_fmt(porch_temp_f)} is inside its labeled range.",
        "WATCH": f"Bring the {p.short} inside within {until} min: {_fmt(porch_temp_f)} is outside its storage range.",
        "ACT_NOW": f"Bring the {p.short} inside now: {_fmt(porch_temp_f)} on the porch.",
        "EXCEEDED": f"The {p.short} was exposed beyond its label. Bring it in and ask your pharmacist before using it.",
    }[status]
    if p.key == "perishable_food" and status == "EXCEEDED":
        headline = "Groceries have been above 40°F past the food-safety time limit. Check them before eating."
    return Assessment(status=status, headline=headline, reasons=reasons, minutes_on_porch=minutes_on_porch,
                      porch_temp_f=porch_temp_f, minutes_until_act_now=until, forecast_note=forecast_note)
