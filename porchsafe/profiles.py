"""Temperature-sensitive item profiles, each tied to the labeling or agency guidance it comes from.

All temperatures are °F. PorchSafe never says an item is "safe to use" or "ruined": it says
whether the porch is inside the labeled range, and when it isn't, it tells the person to bring
the item in and ask their pharmacist (or follow food-safety guidance) before using it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Profile:
    key: str
    label: str
    short: str  # how alerts refer to it: 'the insulin'
    store_min: float | None  # labeled storage range
    store_max: float | None
    excursion_min: float | None  # short excursions the label permits (None = no excursion allowance)
    excursion_max: float | None
    freeze_sensitive: bool  # label says do not freeze / do not use if frozen
    hard_max: float | None  # absolute limit stated on the label (e.g. aerosol burst risk)
    watch_budget_min: int  # minutes allowed outside the storage range (but inside excursions) before "act now"
    exceed_after_min: int  # minutes in "act now" before we write an exposure report
    guidance: str  # what the label / agency says, in plain words
    source: str
    source_url: str

    def public(self) -> dict:
        return asdict(self)


PROFILES: dict[str, Profile] = {
    p.key: p
    for p in [
        Profile(
            key="insulin",
            label="Insulin (unopened)",
            short="insulin",
            store_min=36, store_max=46,  # refrigerated
            excursion_min=36, excursion_max=86,  # opened/in-use insulin may be kept at room temp up to 86°F
            freeze_sensitive=True, hard_max=None,
            watch_budget_min=60, exceed_after_min=30,
            guidance="Store unopened insulin in the refrigerator at 36-46°F. Opened insulin can be kept at 59-86°F. "
                     "Do not freeze insulin, and do not use insulin that has been frozen.",
            source="MedlinePlus (U.S. National Library of Medicine): Insulin storage",
            source_url="https://medlineplus.gov/ency/patientinstructions/000962.htm",
        ),
        Profile(
            key="epinephrine",
            label="Epinephrine auto-injector (EpiPen)",
            short="EpiPen",
            store_min=68, store_max=77,
            excursion_min=59, excursion_max=86,
            freeze_sensitive=False, hard_max=None,
            watch_budget_min=60, exceed_after_min=30,
            guidance="Store at 68-77°F; excursions permitted to 59-86°F. Do not refrigerate.",
            source="EpiPen prescribing information, section 16.2 Storage and Handling",
            source_url="https://medlibrary.org/lib/rx/meds/epipen-5/page/3/",
        ),
        Profile(
            key="albuterol",
            label="Albuterol HFA inhaler",
            short="inhaler",
            store_min=68, store_max=77,
            excursion_min=59, excursion_max=86,
            freeze_sensitive=False, hard_max=120,
            watch_budget_min=60, exceed_after_min=30,
            guidance="Store at 68-77°F; excursions permitted to 59-86°F. Contents are under pressure: "
                     "exposure to temperatures above 120°F may cause bursting.",
            source="Ventolin HFA prescribing information, section 16",
            source_url="https://medlibrary.org/lib/rx/meds/ventolin-hfa-5/page/4/",
        ),
        Profile(
            key="room_temp_rx",
            label="Room-temperature prescription (USP controlled room temperature)",
            short="medicine",
            store_min=68, store_max=77,
            excursion_min=59, excursion_max=86,
            freeze_sensitive=False, hard_max=None,
            watch_budget_min=90, exceed_after_min=60,
            guidance="Most room-temperature medicines are labeled 68-77°F with excursions permitted to 59-86°F "
                     "(USP controlled room temperature). Check your own label.",
            source="USP controlled room temperature, as cited on U.S. drug labels",
            source_url="https://medlibrary.org/lib/rx/meds/ventolin-hfa-5/page/4/",
        ),
        Profile(
            key="refrigerated_rx",
            label="Refrigerated medicine (biologic, liquid)",
            short="refrigerated medicine",
            store_min=36, store_max=46,
            excursion_min=None, excursion_max=None,
            freeze_sensitive=True, hard_max=None,
            watch_budget_min=30, exceed_after_min=30,
            guidance="Refrigerated medicines are labeled 36-46°F. Shipping coolers buy time but not unlimited time; "
                     "move it to the refrigerator and check your label or pharmacist about any excursion.",
            source="Typical refrigerated drug labeling (2-8°C); confirm with your product label",
            source_url="https://medlineplus.gov/ency/patientinstructions/000962.htm",
        ),
        Profile(
            key="perishable_food",
            label="Perishable groceries / meal kit",
            short="groceries",
            store_min=None, store_max=40,  # cold food at or below 40°F
            excursion_min=None, excursion_max=None,
            freeze_sensitive=False, hard_max=None,
            watch_budget_min=120, exceed_after_min=0,  # 2-hour rule (1 hour above 90°F) handled in risk.py
            guidance="Perishable food should not sit above 40°F for more than 2 hours, or more than 1 hour "
                     "above 90°F. Bacteria grow quickly between 40°F and 140°F.",
            source="USDA food-safety 2-hour rule (via UF/IFAS Extension)",
            source_url="https://blogs.ifas.ufl.edu/browardco/2023/10/02/food-safety-the-2-hour-rule/",
        ),
    ]
}


def get(key: str) -> Profile:
    try:
        return PROFILES[key]
    except KeyError as exc:
        raise ValueError(f"unknown profile '{key}'. Known: {', '.join(PROFILES)}") from exc
