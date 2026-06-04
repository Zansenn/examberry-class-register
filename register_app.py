"""
Tutor-facing class register (Streamlit).

Roster comes live from Streak; submitted attendance goes to the Google Sheet
store. Ad-hoc drop-ins are created as Streak boxes and "removed" by moving them
back to the pipeline's holding stage (never deleted).

Books distribution: books are handed out twice termly but the exact week
varies, so an admin defines and *activates* a round. Only while a round is
active do tutors see a per-student "received books" checkbox; students already
recorded for the round show ticked + locked.

Run locally:  streamlit run register_app.py

Access is gated by a single shared app password (set APP_PASSWORD in
.streamlit/secrets.toml on Streamlit Cloud, or in .env locally). The tutor's
typed email is for attribution only — it is not verified.
"""

import datetime as dt
import hmac
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

import roster
from store import AttendanceStore
from streak_client import StreakClient

load_dotenv()  # so APP_PASSWORD from .env is available before get_clients() runs

STATUS_OPTIONS = ["Present", "Absent", "Late"]

# ---- Examberry brand -------------------------------------------------------
ASSETS = Path(__file__).parent / "assets"
LOGO = str(ASSETS / "examberry-logo.svg")
ICON = str(ASSETS / "examberry-favicon.png")
BRAND_BLUE = "#2A3798"   # logo wordmark
BRAND_RED = "#EF2A1E"    # berry
BRAND_GREEN = "#35A70D"  # leaf


def apply_branding():
    """Page config, logo and brand CSS. Must run before any other st call."""
    st.set_page_config(page_title="Examberry Register", page_icon=ICON, layout="centered")
    st.logo(LOGO, link="https://examberry.com", icon_image=ICON, size="large")
    st.markdown(
        f"""
        <style>
          [data-testid="stSidebarHeader"] img {{ height: 2.6rem; }}
          h1, h2, h3 {{ color: {BRAND_BLUE}; font-weight: 700; }}
          [data-testid="stMetricValue"] {{ color: {BRAND_BLUE}; }}
          .stButton button[kind="primary"] {{ background-color: {BRAND_BLUE}; border-color: {BRAND_BLUE}; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.cache_resource
def get_clients():
    return StreakClient(), AttendanceStore()


@st.cache_data(ttl=300)
def get_pipelines():
    client, _ = get_clients()
    return client.list_pipelines()


# RAG dot + a verdict word for the cramped roster row. Cloud strips most colour,
# so the emoji circle carries the signal and the word backs it up for clarity.
RAG_DOT = {"red": "🔴", "amber": "🟡", "green": "🟢", "grey": "⚪"}
RAG_WORD = {"red": "needs support", "amber": "on watch", "green": "on track",
            "grey": "no data"}

# A student's record on the Examberry Learning Platform — the front-end profile
# page keyed by the ELP student code (e.g. '32DR271013'), viewable by any logged-in
# ELP user. The domain is public, so it's safe in this public repo.
ELP_PROFILE_URL = "https://examberrylearning.com/student-directory/{code}/student-profile/"


def elp_profile_url(perf):
    """The ELP record URL for a student, or None if we have no student code
    (e.g. an email-only crosswalk match carries no code)."""
    code = str((perf or {}).get("elp_code", "")).strip()
    return ELP_PROFILE_URL.format(code=code) if code else None


@st.cache_data(ttl=300)
def get_exam_summary():
    """box_key -> per-student exam summary dict, from the dashboard-written tab.

    Empty dict (not an error) if the tab is missing or the ETL hasn't run yet, so
    the register works exactly as before when no exam data is available."""
    _, store = get_clients()
    try:
        rows = store.read_exam_summary()
    except Exception:
        return {}
    return {r["box_key"]: r for r in rows if r.get("box_key")}


# --- cached reads + resilient calls ------------------------------------------
# Streamlit re-runs this whole script on every widget interaction (every radio
# click, every checkbox), and on Community Cloud a *single* process serves all
# ~50 tutors. So an uncached read here is one API call per click per tutor, which
# quickly exhausts Google Sheets' ~60 reads/min-per-account and Streak's limits
# — the cause of the "it just breaks" crashes under load. Caching collapses that
# to about one call per key per TTL across everyone; writes call the matching
# .clear() so a tutor still sees their own change immediately.
READ_TTL = 60  # seconds — live enough for a register, easy on the APIs


@st.cache_data(ttl=READ_TTL)
def cached_roster(pipeline_key, stage_key):
    client, _ = get_clients()
    return roster.class_roster(client, pipeline_key, stage_key)


@st.cache_data(ttl=READ_TTL)
def cached_active_rounds():
    _, store = get_clients()
    return store.active_rounds()


@st.cache_data(ttl=READ_TTL)
def cached_rounds():
    _, store = get_clients()
    return store.read_rounds()


@st.cache_data(ttl=READ_TTL)
def cached_books_received(round_key):
    """box_keys currently recorded as received for `round_key` (latest state per
    student, so a corrected un-tick is reflected). Computed once per TTL."""
    _, store = get_clients()
    return store.received_box_keys(round_key)


@st.cache_data(ttl=READ_TTL)
def cached_favourites(tutor):
    _, store = get_clients()
    return store.read_favourites(tutor)


def _clear_roster_cache():
    cached_roster.clear()


def _clear_round_cache():
    cached_active_rounds.clear()
    cached_rounds.clear()
    cached_books_received.clear()


def _clear_favourites_cache():
    cached_favourites.clear()


def safe(action, fn, *args, **kwargs):
    """Run an external (Streak/Sheets) call. On failure, show a friendly message
    plus a Retry button and halt *this* run cleanly via st.stop(), instead of
    letting an unhandled exception crash the tutor's whole session.

    `action` is a short verb phrase shown to the tutor, e.g. 'load this class'."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        st.error(f"⚠️ Couldn't {action} just now — the server may be busy. "
                 "Wait a few seconds and tap Retry. Anything you've marked is kept.")
        st.button("Retry", key=f"retry_{action}".replace(" ", "_"))
        st.stop()


def _fmt_pct(v):
    try:
        return f"{float(v):.0f}%"
    except (TypeError, ValueError):
        return None


def format_perf(p):
    """One compact line of exam performance for under a student's name, mirroring
    the ELP profile: the student's latest term's Online Tests overall.
    Values arrive from the sheet as strings, so coerce defensively."""
    parts = [RAG_DOT.get(p.get("rag", "grey"), "⚪")]
    term = (p.get("term_label") or "").strip()
    if (overall := _fmt_pct(p.get("overall_pct"))):
        parts.append(f"{term} {overall}".strip() if term else f"overall {overall}")
    elif term:
        parts.append(term)
    if (last := _fmt_pct(p.get("last_pct"))):
        label = (p.get("last_label") or "").strip()
        parts.append(f"last {last}" + (f" ({label})" if label else ""))
    try:
        missed = int(float(p.get("missed", 0)))
    except (TypeError, ValueError):
        missed = 0
    if missed > 0:
        parts.append(f"⚠ {missed} missed")
    return " · ".join(parts)


# Per-subject averages for the latest term, shown on a second line.
# '–' = no test in that subject this term.
SUBJECT_FIELDS = [("M", "maths_pct"), ("E", "eng_pct"),
                  ("NVR", "nvr_pct"), ("VR", "vr_pct")]


def format_subjects(p):
    """'M 85% · E 71% · NVR 66% · VR –' — per-subject breakdown for the latest
    term, or None if the student has no subject data at all."""
    cells = [f"{lbl} {v}" if (v := _fmt_pct(p.get(field))) else f"{lbl} –"
             for lbl, field in SUBJECT_FIELDS]
    # If every subject is blank there's nothing to add beyond the overall line.
    if all(c.endswith("–") for c in cells):
        return None
    return " · ".join(cells)


def adhoc_keys():
    """box_keys this session created as ad-hoc, so we can flag + offer removal."""
    return st.session_state.setdefault("adhoc_keys", set())


def _configured_secret(name):
    """A secret value from Streamlit secrets (cloud) or env (.env locally)."""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass  # no secrets file locally — fall back to env
    return os.environ.get(name)


def _configured_password():
    """Shared app password — the gate for the whole app."""
    return _configured_secret("APP_PASSWORD")


def require_password():
    """Block the app behind a shared password. Returns True once authenticated."""
    if st.session_state.get("auth_ok"):
        return True

    expected = _configured_password()
    if not expected:
        st.error("APP_PASSWORD is not configured. Set it in secrets (cloud) or .env.")
        return False

    st.title("Class Register")
    entered = st.text_input("App password", type="password")
    if st.button("Enter"):
        # constant-time compare so a wrong guess can't be timed. Strip both sides:
        # an invisible trailing newline/space in the secret (a common Streamlit
        # secrets paste artifact) would otherwise reject the correct password.
        if hmac.compare_digest(entered.strip(), str(expected).strip()):
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False


def admin_rounds_panel(store):
    """Sidebar admin controls to create and activate book-distribution rounds.

    Locked behind a separate ADMIN_PASSWORD so ordinary tutors (who only know the
    shared APP_PASSWORD) can't create or activate rounds."""
    with st.sidebar.expander("📚 Admin: book rounds"):
        admin_pw = _configured_secret("ADMIN_PASSWORD")
        if not admin_pw:
            st.caption("Admin controls are disabled until an ADMIN_PASSWORD is set "
                       "in the app's Secrets (cloud) or .env (local).")
            return
        if not st.session_state.get("admin_ok"):
            entered = st.text_input("Admin password", type="password", key="admin_pw")
            if st.button("Unlock admin"):
                # constant-time compare; strip both sides so invisible whitespace
                # in the secret can't reject the correct password (see require_password).
                if hmac.compare_digest(entered.strip(), str(admin_pw).strip()):
                    st.session_state["admin_ok"] = True
                    st.rerun()
                else:
                    st.error("Incorrect admin password.")
            return
        if st.button("Lock admin"):
            st.session_state["admin_ok"] = False
            st.rerun()

        rounds = safe("load book rounds", cached_rounds)
        active_labels = [r["label"] for r in rounds if r.get("is_active") == "true"]
        st.caption("**Active now:** " + (", ".join(active_labels) if active_labels else "none"))

        # Several runs can be active at once (e.g. New Term + Revision); each has
        # its own on/off toggle rather than one mutually-exclusive choice.
        if rounds:
            st.markdown("**Runs** — tick to make active")
            for r in rounds:
                was = r.get("is_active") == "true"
                now_on = st.checkbox(r["label"], value=was, key=f"active_{r['round_key']}")
                if now_on != was:
                    safe("update the run", store.set_round_active, r["round_key"], now_on)
                    _clear_round_cache()
                    st.rerun()

        new_label = st.text_input("New run label", placeholder="New Term")
        if st.button("Create run"):
            if new_label.strip():
                safe("create the run", store.add_round, new_label.strip())
                _clear_round_cache()
                st.success(f"Created run '{new_label.strip()}'")
                st.rerun()
            else:
                st.warning("Enter a label first.")

        # Delete a run created by mistake (also clears any handouts logged to it).
        if rounds:
            st.divider()
            st.markdown("**Delete a run**")
            opts = {f"{r['label']} · {r.get('created_at', '')[:10]}": r["round_key"]
                    for r in rounds}
            choice = st.selectbox("Run to delete", ["— none —"] + list(opts), key="del_run")
            if choice != "— none —":
                st.warning(f"Permanently removes **{choice}** and any book handouts "
                           "recorded for it. This can't be undone.")
                if st.button("Delete run", type="secondary"):
                    safe("delete the run", store.delete_round, opts[choice])
                    _clear_round_cache()
                    st.success("Run deleted.")
                    st.rerun()


def main():
    apply_branding()

    if not require_password():
        return

    st.title("Class Register")

    client, store = get_clients()

    # Callout: which book run(s) are active right now (shared across all classes).
    active_rounds = safe("check book runs", cached_active_rounds)
    if active_rounds:
        names = " · ".join(f"**{r['label']}**" for r in active_rounds)
        st.info(f"📚 Active book run(s): {names}  \n"
                "Record handouts in the **Book handouts** section under each class.")

    pipelines = safe("load the class lists", get_pipelines)

    # --- who + what (sidebar) ----------------------------------------------
    tutor = st.sidebar.text_input("Your email (tutor)")
    admin_rounds_panel(store)
    years = roster.available_years(pipelines)
    if not years:
        st.error("No roster pipelines found in Streak.")
        return

    # A tutor's saved classes (keyed by their email). Lets them jump straight
    # to a class instead of stepping through year -> group -> class each time.
    saved = safe("load your saved classes", cached_favourites, tutor.strip()) if tutor.strip() else []

    use_saved = False
    if saved:
        use_saved = st.radio(
            "Find class", ["⭐ My saved classes", "Browse all"], horizontal=True,
        ) == "⭐ My saved classes"

    if use_saved:
        by_label = {f["class_name"]: f for f in saved}
        fav = by_label[st.selectbox("Saved class", list(by_label))]
        pipe = next((p for p in pipelines if p["key"] == fav["pipeline_key"]), None)
        if pipe is None:
            st.warning("That saved class's pipeline no longer exists in Streak. "
                       "Remove it below and pick it again from Browse all.")
            if st.button("Remove this saved class"):
                safe("remove the saved class", store.remove_favourite,
                     tutor.strip(), fav["stage_key"])
                _clear_favourites_cache()
                st.rerun()
            return
        year, stage_key, class_label = fav["year"], fav["stage_key"], fav["class_name"]
    else:
        year = st.sidebar.selectbox("Academic year", years, index=len(years) - 1)
        rps = roster.roster_pipelines(pipelines, year)
        by_name = {p["name"]: p for p in rps}
        pipe = by_name[st.selectbox("Year group / course", list(by_name))]

        cls = roster.classes(pipe)
        if not cls:
            st.info("This pipeline has no classes to register.")
            return
        label_to_stage = {c["name"]: c["stage_key"] for c in cls}
        class_label = st.selectbox("Class", list(label_to_stage))
        stage_key = label_to_stage[class_label]

    # Save / remove this class for quick access (needs an email to key on).
    if tutor.strip():
        is_saved = any(f["stage_key"] == stage_key for f in saved)
        if is_saved:
            if st.button("★ Saved — remove from my classes"):
                safe("remove the saved class", store.remove_favourite,
                     tutor.strip(), stage_key)
                _clear_favourites_cache()
                st.rerun()
        elif st.button("☆ Save this class for quick access"):
            safe("save this class", store.add_favourite, {
                "tutor": tutor.strip(), "year": year,
                "pipeline_key": pipe["key"], "pipeline_name": pipe["name"],
                "stage_key": stage_key, "class_name": class_label,
                "saved_at": dt.datetime.now().isoformat(timespec="seconds"),
            })
            _clear_favourites_cache()
            st.rerun()
    else:
        st.caption("Enter your email in the sidebar to save classes for quick access.")

    class_date = st.date_input("Date of class", value=dt.date.today())

    # --- roster -------------------------------------------------------------
    students = safe("load this class", cached_roster, pipe["key"], stage_key)
    holding = roster.holding_stage_key(pipe)
    adhoc = adhoc_keys()
    exam_summary = get_exam_summary()  # box_key -> performance; {} if no ELP data

    # Current received state of each active book run, used to render one
    # collapsible section per run below (open while a run still has outstanding
    # pupils, collapsed once everyone in this class has received it).
    received_by_round = {
        r["round_key"]: safe("load book handouts", cached_books_received, r["round_key"])
        for r in active_rounds
    }

    st.subheader(f"{class_label}")
    st.caption(f"{len(students)} students")
    if any(s["box_key"] in exam_summary for s in students):
        st.caption(
            "📊 Exam performance (updated nightly, mirrors the ELP): "
            "🟢 on track · 🟡 on watch · 🔴 needs support · "
            "**Term N** = latest term's online-test overall · **last** = most recent test · "
            "second line = per-subject this term (**M**aths · **E**nglish · **NVR** · **VR**) · "
            "**⚠ missed** = tests their year group sat that they haven't. "
            "Click a student's name to open their ELP record."
        )

    marks = {}
    for s in students:
        is_adhoc = s["box_key"] in adhoc
        cols = st.columns([4, 6, 1])
        prefix = "🆕 " if is_adhoc else ""
        perf = exam_summary.get(s["box_key"])
        url = elp_profile_url(perf)
        # Link the name to the student's ELP record when we can resolve it.
        name_md = f"[**{s['name']}**]({url})" if url else f"**{s['name']}**"
        cols[0].markdown(prefix + name_md)
        if perf:
            cols[0].caption(format_perf(perf))
            if (subj_line := format_subjects(perf)):
                cols[0].caption(subj_line)
        marks[s["box_key"]] = cols[1].radio(
            s["name"], STATUS_OPTIONS, index=0, horizontal=True,
            label_visibility="collapsed", key=f"mark_{s['box_key']}",
        )
        # Removal is only offered for ad-hoc adds, and only where a holding
        # stage exists to move them back to.
        rm_col = cols[-1]
        if is_adhoc and holding and rm_col.button("✕", key=f"rm_{s['box_key']}",
                                                   help="Remove ad-hoc student"):
            safe("remove the ad-hoc student", client.set_box_stage, s["box_key"], holding)
            adhoc.discard(s["box_key"])
            _clear_roster_cache()
            st.rerun()

    # --- add ad-hoc student -------------------------------------------------
    with st.expander("➕ Add ad-hoc student"):
        new_name = st.text_input("Student name", key="adhoc_name")
        if st.button("Add to this class"):
            if new_name.strip():
                box = safe("add the ad-hoc student", client.create_box,
                           pipe["key"], new_name.strip(), stage_key=stage_key)
                adhoc.add(box["key"])
                _clear_roster_cache()
                st.success(f"Added {new_name.strip()} to {class_label}")
                st.rerun()
            else:
                st.warning("Enter a name first.")

    # --- book handouts (one collapsible section per active run) -------------
    # Each active run gets its own panel: open while any pupil here is still
    # outstanding, auto-collapsed to a ✅ summary once everyone's received it.
    # A collapsed panel can be reopened to correct a mistake. Ticks are gathered
    # here and saved together with the register on Submit.
    books_ticked = {}  # round_key -> {box_key: bool}
    if active_rounds:
        st.markdown("### 📚 Book handouts")
        for r in active_rounds:
            rk = r["round_key"]
            received = received_by_round[rk]
            outstanding = [s for s in students if s["box_key"] not in received]
            complete = not outstanding
            header = (f"✅ {r['label']} — all received"
                      if complete else
                      f"📕 {r['label']} — {len(outstanding)} of {len(students)} outstanding")
            with st.expander(header, expanded=not complete):
                st.caption("Tick each student as they receive this run's books · "
                           "untick to correct a mistake.")
                ticks = {}
                for s in students:
                    ticks[s["box_key"]] = st.checkbox(
                        s["name"], value=s["box_key"] in received,
                        key=f"book_{rk}_{s['box_key']}",
                    )
                books_ticked[rk] = ticks

    # --- submit -------------------------------------------------------------
    st.divider()
    if st.button("Submit register", type="primary"):
        if not tutor.strip():
            st.error("Enter your email in the sidebar before submitting.")
            return
        now = dt.datetime.now().isoformat(timespec="seconds")
        rows = [{
            "year": year,
            "pipeline_key": pipe["key"],
            "stage_key": stage_key,
            "class_name": class_label,
            "class_date": class_date.isoformat(),
            "box_key": s["box_key"],
            "student_name": s["name"],
            "status": marks[s["box_key"]],
            "is_adhoc": str(s["box_key"] in adhoc).lower(),
            "submitted_by": tutor.strip(),
            "submitted_at": now,
        } for s in students]

        # Record book-handout *changes* only, across every active run: a row
        # whenever a student's tick now differs from what's on record (newly
        # given, or unticked to correct a mistake). Readers take the latest event
        # per student, so this re-submit overwrites the earlier state without
        # rewriting the whole tab.
        book_rows = []
        for r in active_rounds:
            rk = r["round_key"]
            received = received_by_round[rk]
            ticks = books_ticked.get(rk, {})
            for s in students:
                desired = bool(ticks.get(s["box_key"]))
                current = s["box_key"] in received
                if desired != current:
                    book_rows.append({
                        "round_key": rk,
                        "year": year,
                        "pipeline_key": pipe["key"],
                        "stage_key": stage_key,
                        "class_name": class_label,
                        "box_key": s["box_key"],
                        "student_name": s["name"],
                        "recorded_by": tutor.strip(),
                        "recorded_at": now,
                        "received": str(desired).lower(),
                    })

        # Don't crash the session on a transient API error: keep the marks on
        # screen (widget state survives the rerun) so the tutor can just retry.
        try:
            store.ensure_header()
            store.append_rows(rows)
            if book_rows:
                store.append_books(book_rows)
                _clear_round_cache()  # so the ticks reflect the new state next render
        except Exception:
            st.error("⚠️ Couldn't save the register just now — the server may be "
                     "busy. Your marks are still here; tap **Submit register** "
                     "again in a few seconds.")
            return

        msg = f"Submitted {len(rows)} marks for {class_label} on {class_date}."
        if book_rows:
            msg += f" Recorded {len(book_rows)} book update(s)."
        st.success(msg)


if __name__ == "__main__":
    main()
