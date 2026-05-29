"""
Thin client for the Streak CRM API (https://streak.readme.io/).

Streak holds Examberry's student records: each student is a *box*, a *stage*
is a class, and there are several *pipelines* (roughly one per year group or
class type). Registers read their rosters from here. See README / project notes.

Auth is HTTP Basic with the API key as the username and an empty password.
The key is read from STREAK_API_KEY (or a .env file).

Read-only methods (list_pipelines, get_pipeline, list_boxes) are safe to run
freely. The create/move methods mutate the live CRM — call them deliberately.
"""

import os

import requests
from dotenv import load_dotenv

API_ROOT = "https://www.streak.com/api/v1"
TIMEOUT = 30


class StreakError(RuntimeError):
    pass


class StreakClient:
    def __init__(self, api_key=None):
        if api_key is None:
            load_dotenv()
            api_key = os.environ.get("STREAK_API_KEY")
        if not api_key:
            raise StreakError(
                "No Streak API key. Put STREAK_API_KEY=... in a .env file "
                "(in the project root) or export it in your shell."
            )
        self._session = requests.Session()
        self._session.auth = (api_key, "")  # key as username, blank password

    # --- low-level ----------------------------------------------------------

    def _request(self, method, path, **kwargs):
        url = f"{API_ROOT}{path}"
        resp = self._session.request(method, url, timeout=TIMEOUT, **kwargs)
        if not resp.ok:
            raise StreakError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # --- read-only ----------------------------------------------------------

    def list_pipelines(self):
        """All pipelines visible to the key. Each has key, name, and a stages map."""
        return self._request("GET", "/pipelines")

    def get_pipeline(self, pipeline_key):
        """One pipeline, including its `stages` map (stageKey -> {name, ...})."""
        return self._request("GET", f"/pipelines/{pipeline_key}")

    def list_boxes(self, pipeline_key, stage_key=None):
        """Boxes (students) in a pipeline, optionally filtered to one stage (class)."""
        boxes = self._request("GET", f"/pipelines/{pipeline_key}/boxes")
        if stage_key is not None:
            boxes = [b for b in boxes if b.get("stageKey") == stage_key]
        return boxes

    # --- mutating (live CRM) ------------------------------------------------

    def create_pipeline(self, name):
        """Create a pipeline. Used to stand up a test roster for trying the app."""
        return self._request("PUT", "/pipelines", params={"name": name})

    def create_stage(self, pipeline_key, name):
        """Add a stage (class) to a pipeline. Returns the stage incl. its key."""
        return self._request(
            "PUT", f"/pipelines/{pipeline_key}/stages", params={"name": name}
        )

    def delete_pipeline(self, pipeline_key):
        """Permanently delete a pipeline. For cleaning up test artifacts."""
        return self._request("DELETE", f"/pipelines/{pipeline_key}")

    def create_box(self, pipeline_key, name, stage_key=None):
        """Create a box, e.g. an ad-hoc student. Optionally place it in a stage."""
        box = self._request(
            "PUT", f"/pipelines/{pipeline_key}/boxes", params={"name": name}
        )
        if stage_key is not None:
            box = self.set_box_stage(box["key"], stage_key)
        return box

    def set_box_stage(self, box_key, stage_key):
        """Move a box to another stage. Used to 'remove' an ad-hoc student by
        moving it back to the holding stage rather than deleting it."""
        return self._request(
            "POST", f"/boxes/{box_key}", json={"stageKey": stage_key}
        )

    def delete_box(self, box_key):
        """Permanently delete a box. NOT used by the ad-hoc remove flow (that
        archives via set_box_stage); kept for cleaning up test artifacts."""
        return self._request("DELETE", f"/boxes/{box_key}")
