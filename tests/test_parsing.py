import datetime as dt

import pytest

from studia.domain import Course, Recording
from studia.ieulearn import extract_folder_id, parse_dashboard, split_course_title
from studia.panopto import parse_dotnet_date, parse_sessions
from studia.sections import available_sections, classify, guess_my_section, parse_section_tag


@pytest.mark.parametrize("title,expected", [
    ("2026-1 EEE 215 Blgsyr. Müh. için Elktrk. Devre &amp; Elektroniğe Giriş 0 - 1",
     ("EEE 215", "Blgsyr. Müh. için Elktrk. Devre & Elektroniğe Giriş", "0 - 1")),
    ("2026-1 EEE 242 Sayısal Tasarım 1", ("EEE 242", "Sayısal Tasarım", "1")),
    ("2026-1 MATH 240 Mühendisler için Olasılık 1", ("MATH 240", "Mühendisler için Olasılık", "1")),
    ("2026-1 MATH 250 Mühendisler İçin Doğrusal Cebir 3", ("MATH 250", "Mühendisler İçin Doğrusal Cebir", "3")),
    ("2026-1 FENG 495 Çift Ana Dal Bitirme Projesi I (Mühendislik Fakültesi) 3",
     ("FENG 495", "Çift Ana Dal Bitirme Projesi I (Mühendislik Fakültesi)", "3")),
])
def test_split_course_title(title, expected):
    assert split_course_title(title) == expected


DASHBOARD = """
<strong class="ov-user-name">Test Student</strong><span class="ov-user-role">20230000000</span>
<article class="dash-course" data-course-id="111" data-course-status="ongoing"
  data-course-name="x" data-course-title="2026-1 EEE 242 Sayısal Tasarım 1" data-course-href="h">
  <span class="dash-course-school" title="Jane Doe">Jane Doe</span>
  <span class="dash-course-class" title="T">2026-2027 Fall Undergrad</span></article>
<article class="dash-course" data-course-id="222" data-course-status="past"
  data-course-title="2025-2 MATH 153 Calculus &amp; Analysis 2" data-course-href="h">
  <span class="dash-course-school" title="John Roe">John Roe</span></article>
"""


def test_parse_dashboard():
    profile, courses = parse_dashboard(DASHBOARD)
    assert profile.name == "Test Student" and profile.student_number == "20230000000"
    assert [c.id for c in courses] == ["111", "222"]
    assert courses[0].code == "EEE 242" and courses[0].instructor == "Jane Doe"
    assert courses[0].term == "2026-2027 Fall Undergrad" and courses[0].section == "1"
    assert courses[1].status == "past" and courses[1].name == "Calculus & Analysis"


def test_extract_folder_id_plain_and_encoded():
    fid = "d893262b-1027-4262-a147-b4cb007bffcc"
    assert extract_folder_id([f"https://x/EmbeddedList.aspx?embedded=1&folderID={fid}&isLTIEmbed=true"]) == fid
    assert extract_folder_id([f"https://x/List.aspx?embedded=1#folderID=%22{fid}%22"]) == fid
    assert extract_folder_id(["https://x/other"]) is None


def test_dotnet_date_is_local_wall_clock():
    # Panopto shows "Tuesday, October 20, 2026 at 11:15 AM" for this value
    assert parse_dotnet_date("/Date(1792494951083)/") == dt.datetime(2026, 10, 20, 11, 15, 51, 83000)


def test_parse_sessions():
    d = {"Results": [{"DeliveryID": "ABC-1", "SessionID": "S1", "SessionName": "EEE 215 Section 1 Week 4 Lab",
                      "StartTime": "/Date(1792494951083)/", "Duration": 2400.5, "FolderID": "F",
                      "ViewerUrl": "https://v"}], "TotalNumber": 1}
    (r,) = parse_sessions(d, "19226", "Labs")
    assert r.delivery_id == "abc-1" and r.kind == "lab" and r.section_tag == "1" and r.duration_s == 2400.5


@pytest.mark.parametrize("name,tag", [
    ("Tuesday, October 13, 2026 at 12:10 PM EEE 215 Section 1", "1"),
    ("Wednesday, October 14, 2026 at 11:17:26 AM EEE 215 Sections 2-3", "2-3"),
    ("Wednesday, September 23, 2026 at 11:25:40 AM EEE Section 2*3", "2-3"),
    ("EEE215-2 Lab Week-4 15 Ekim 2026 Perşembe at 08:28:00", "2"),
    ("EEE 215 SECTION 3 WEEK 4 LAB 16 Ekim 2026 Cuma at 08:29:45", "3"),
    ("Tuesday, October 6, 2026 at 1:04:34 PM", ""),
    ("Lecture4.1-Friday, October 16, 2026 at 2:05:56 PM", ""),
])
def test_parse_section_tag(name, tag):
    assert parse_section_tag(name) == tag


def _rec(did, day, name, kind="lecture"):
    return Recording(delivery_id=did, course_id="c", folder_id="f", folder_path="", name=name,
                     start=dt.datetime(2026, 10, day, 11, 0), duration_s=2400, viewer_url="", kind=kind,
                     section_tag=parse_section_tag(name))


def test_classify_with_weekday_inference():
    course = Course(id="c", title="", code="EEE 215", name="", section="0 - 1")
    recs = [_rec("a", 13, "x Section 1"),          # Tuesday, mine
            _rec("b", 13, "Tuesday 1:04 PM"),       # Tuesday, untagged -> mine by weekday
            _rec("c", 14, "x Sections 2-3"),        # Wednesday, other
            _rec("d", 14, "Wednesday 1:05 PM"),     # Wednesday, untagged -> other
            _rec("e", 16, "Friday something")]      # Friday, no evidence -> unknown
    assert guess_my_section(course) is None                   # a merged course page: the app asks
    assert classify(course, recs) == {r.delivery_id: None for r in recs}
    res = classify(course, recs, my_section="1")
    assert res == {"a": True, "b": True, "c": False, "d": False, "e": None}
    assert available_sections(recs) == ["1", "2", "3"]
    assert classify(course, recs, my_section="2")["c"] is True


@pytest.mark.parametrize("suffix,guess", [("3", "3"), ("1", "1"), ("0 - 1", None), ("2-3", None), ("", None), ("0", None)])
def test_guess_my_section_only_from_a_single_section(suffix, guess):
    assert guess_my_section(Course(id="c", title="", code="X 100", name="", section=suffix)) == guess
