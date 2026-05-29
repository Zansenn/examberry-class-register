"""
Attendance store: a Google Sheet "master register".

Streamlit Community Cloud has an ephemeral filesystem, so attendance (the only
copy) lives in a Google Sheet instead of a local file. Reuses the same service
account as ingest.py, but needs WRITE scope and the sheet shared with the
service-account email as Editor.

Three tabs in the one spreadsheet:
  - attendance   one row per student x session (the register submissions)
  - book_rounds  admin-defined book-distribution rounds (timing varies, so an
                 admin activates a round; only then do tutors see the checkbox)
  - books        one row per book handout (student received books in a round)

Config:
  - credentials/service-account.json  (same key file as ingest.py)
  - ATTENDANCE_SHEET_ID  (in .env)    the target spreadsheet's ID
"""

import datetime as dt
import os
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

CREDENTIALS_FILE = Path(__file__).parent / "credentials" / "service-account.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]  # write

ATTENDANCE_TAB = "attendance"
ATTENDANCE_COLUMNS = [
    "year", "pipeline_key", "stage_key", "class_name", "class_date",
    "box_key", "student_name", "status", "is_adhoc",
    "submitted_by", "submitted_at",
]

ROUNDS_TAB = "book_rounds"
ROUNDS_COLUMNS = ["round_key", "label", "is_active", "created_at"]

BOOKS_TAB = "books"
BOOKS_COLUMNS = [
    "round_key", "year", "pipeline_key", "stage_key", "class_name",
    "box_key", "student_name", "recorded_by", "recorded_at",
]

# A tutor's saved classes for quick access. Keyed by the email they type in
# (unverified — this is a convenience, not access control). A class is one row.
FAVOURITES_TAB = "favourites"
FAVOURITES_COLUMNS = [
    "tutor", "year", "pipeline_key", "pipeline_name",
    "stage_key", "class_name", "saved_at",
]


def _load_credentials():
    """Service-account creds: a local key file if present (dev), otherwise the
    JSON from Streamlit secrets under [gcp_service_account] (Streamlit Cloud,
    where the repo has no key file)."""
    if CREDENTIALS_FILE.exists():
        return Credentials.from_service_account_file(str(CREDENTIALS_FILE), scopes=SCOPES)
    try:
        import streamlit as st
        info = dict(st.secrets["gcp_service_account"])
    except Exception as e:
        raise RuntimeError(
            "No service-account credentials. Add credentials/service-account.json "
            "locally, or a [gcp_service_account] section to the Streamlit app's secrets."
        ) from e
    return Credentials.from_service_account_info(info, scopes=SCOPES)


class AttendanceStore:
    def __init__(self, sheet_id=None):
        load_dotenv()
        self.sheet_id = sheet_id or os.environ.get("ATTENDANCE_SHEET_ID")
        if not self.sheet_id:
            raise RuntimeError(
                "No ATTENDANCE_SHEET_ID. Create a Google Sheet, share it with the "
                "service-account email as Editor, and put its ID in .env."
            )
        self._svc = build("sheets", "v4", credentials=_load_credentials())

    # --- generic worksheet helpers -----------------------------------------
    def _ensure_worksheet(self, tab, columns):
        """Create `tab` if missing and write the header row if empty. Idempotent."""
        meta = self._svc.spreadsheets().get(spreadsheetId=self.sheet_id).execute()
        titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
        if tab not in titles:
            self._svc.spreadsheets().batchUpdate(
                spreadsheetId=self.sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
            ).execute()
        first = self._svc.spreadsheets().values().get(
            spreadsheetId=self.sheet_id, range=f"{tab}!A1:A1",
        ).execute().get("values", [])
        if not first:
            self._svc.spreadsheets().values().update(
                spreadsheetId=self.sheet_id, range=f"{tab}!A1",
                valueInputOption="RAW", body={"values": [columns]},
            ).execute()

    def _append(self, tab, columns, rows):
        if not rows:
            return 0
        values = [[str(r.get(c, "")) for c in columns] for r in rows]
        self._svc.spreadsheets().values().append(
            spreadsheetId=self.sheet_id, range=f"{tab}!A1",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()
        return len(values)

    def _read(self, tab):
        # One call, not two: a missing tab surfaces as a 400 range-parse error,
        # which we treat as "no rows yet". Avoids an extra metadata round-trip on
        # every read (every Streamlit rerun), halving the network surface.
        try:
            vals = self._svc.spreadsheets().values().get(
                spreadsheetId=self.sheet_id, range=f"{tab}!A1:ZZ",
            ).execute().get("values", [])
        except HttpError as e:
            if e.resp.status == 400:
                return []
            raise
        if not vals:
            return []
        header, *data = vals
        return [dict(zip(header, row + [""] * (len(header) - len(row)))) for row in data]

    # --- attendance ---------------------------------------------------------
    def ensure_header(self):
        self._ensure_worksheet(ATTENDANCE_TAB, ATTENDANCE_COLUMNS)

    def append_rows(self, rows):
        return self._append(ATTENDANCE_TAB, ATTENDANCE_COLUMNS, rows)

    def read_all(self):
        return self._read(ATTENDANCE_TAB)

    # --- book rounds --------------------------------------------------------
    def read_rounds(self):
        return self._read(ROUNDS_TAB)

    def add_round(self, label):
        """Create a round (inactive by default). round_key is a timestamp slug."""
        self._ensure_worksheet(ROUNDS_TAB, ROUNDS_COLUMNS)
        now = dt.datetime.now().isoformat(timespec="seconds")
        round_key = now.replace(":", "").replace("-", "")
        self._append(ROUNDS_TAB, ROUNDS_COLUMNS, [{
            "round_key": round_key, "label": label,
            "is_active": "false", "created_at": now,
        }])
        return round_key

    def set_active_round(self, round_key):
        """Activate one round and deactivate all others (rewrites the tab)."""
        self._ensure_worksheet(ROUNDS_TAB, ROUNDS_COLUMNS)
        rounds = self.read_rounds()
        for r in rounds:
            r["is_active"] = "true" if r.get("round_key") == round_key else "false"
        self._overwrite(ROUNDS_TAB, ROUNDS_COLUMNS, rounds)

    def active_round(self):
        """The single active round dict, or None."""
        for r in self.read_rounds():
            if r.get("is_active") == "true":
                return r
        return None

    def _overwrite(self, tab, columns, rows):
        """Replace all data rows (keeps header)."""
        self._svc.spreadsheets().values().clear(
            spreadsheetId=self.sheet_id, range=f"{tab}!A2:ZZ",
        ).execute()
        if rows:
            values = [[str(r.get(c, "")) for c in columns] for r in rows]
            self._svc.spreadsheets().values().update(
                spreadsheetId=self.sheet_id, range=f"{tab}!A2",
                valueInputOption="RAW", body={"values": values},
            ).execute()

    # --- books --------------------------------------------------------------
    def append_books(self, rows):
        self._ensure_worksheet(BOOKS_TAB, BOOKS_COLUMNS)
        return self._append(BOOKS_TAB, BOOKS_COLUMNS, rows)

    def read_books(self):
        return self._read(BOOKS_TAB)

    # --- saved classes (favourites) ----------------------------------------
    def read_favourites(self, tutor=None):
        """All saved classes, or just one tutor's (case-insensitive email)."""
        rows = self._read(FAVOURITES_TAB)
        if tutor is None:
            return rows
        t = tutor.strip().lower()
        return [r for r in rows if r.get("tutor", "").strip().lower() == t]

    def add_favourite(self, row):
        """Save a class for a tutor. No-op if already saved (same tutor+stage)."""
        self._ensure_worksheet(FAVOURITES_TAB, FAVOURITES_COLUMNS)
        existing = self.read_favourites(row["tutor"])
        if any(r.get("stage_key") == row["stage_key"] for r in existing):
            return False
        self._append(FAVOURITES_TAB, FAVOURITES_COLUMNS, [row])
        return True

    def remove_favourite(self, tutor, stage_key):
        """Drop one tutor's saved class (matched on tutor email + stage)."""
        self._ensure_worksheet(FAVOURITES_TAB, FAVOURITES_COLUMNS)
        t = tutor.strip().lower()
        kept = [r for r in self._read(FAVOURITES_TAB)
                if not (r.get("tutor", "").strip().lower() == t
                        and r.get("stage_key") == stage_key)]
        self._overwrite(FAVOURITES_TAB, FAVOURITES_COLUMNS, kept)
