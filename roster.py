"""
Roster layer: turn Streak pipelines/stages into the registers the app shows.

Read-only. Operates on a pipelines list fetched once via StreakClient so we
don't hammer the API. Exposes:
  - available_years(pipelines)        -> ['25/26', '26/27', ...]
  - roster_pipelines(pipelines, year) -> in-scope pupil/course pipelines
  - classes(pipeline)                 -> selectable class stages (drops holding)
  - class_roster(client, pkey, skey)  -> students in one class

Scope rule (decided 2026-05-29): in scope = non-'Z' pipeline whose name has a
YY/YY prefix and looks like a pupil/course roster. Leavers / Sales Process /
Class Confirmations / Complaints / etc. are out of scope.
"""

import re

YEAR_RE = re.compile(r"^\s*(\d{2}/\d{2})\b")

# A roster pipeline's name contains one of these (case-insensitive)...
ROSTER_INCLUDE = ("pupils", "course", "skills")
# ...and none of these (exclusions win over includes).
ROSTER_EXCLUDE = ("leavers", "sales", "confirmation", "complaint",
                  "accident", "offers", "testimonial", "not confirmed", "log")

# The per-pipeline holding stage for unassigned students; also where an ad-hoc
# student is moved when "removed". Matched by name (its key is usually 5001).
HOLDING_STAGE_NAME = "allocate to class"


def _year_of(name):
    m = YEAR_RE.match(name)
    return m.group(1) if m else None


def _is_roster(name):
    if name.startswith("Z"):  # 'Z' prefix = archived
        return False
    low = name.lower()
    if not any(k in low for k in ROSTER_INCLUDE):
        return False
    return not any(k in low for k in ROSTER_EXCLUDE)


def available_years(pipelines):
    years = {_year_of(p["name"]) for p in pipelines if _is_roster(p["name"])}
    years.discard(None)
    return sorted(years)


def roster_pipelines(pipelines, year):
    return [p for p in pipelines
            if _is_roster(p["name"]) and _year_of(p["name"]) == year]


def holding_stage_key(pipeline):
    """stageKey of the 'Allocate to Class' holding stage, or None."""
    for sk, st in pipeline.get("stages", {}).items():
        if st.get("name", "").strip().lower() == HOLDING_STAGE_NAME:
            return sk
    return None


def classes(pipeline):
    """Selectable class stages in pipeline order, excluding the holding stage."""
    stages = pipeline.get("stages", {})
    order = pipeline.get("stageOrder") or list(stages.keys())
    out = []
    for sk in order:
        name = stages.get(sk, {}).get("name", "")
        if name.strip().lower() == HOLDING_STAGE_NAME:
            continue
        out.append({"stage_key": sk, "name": name})
    return out


def class_roster(client, pipeline_key, stage_key):
    """Students (boxes) currently in one class. Names have any leading '*' stripped."""
    boxes = client.list_boxes(pipeline_key, stage_key=stage_key)
    return [{
        "box_key": b["key"],
        "name": b.get("name", "").lstrip("*").strip(),
        "emails": b.get("emailAddresses") or [],
    } for b in boxes]
