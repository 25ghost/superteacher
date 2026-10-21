"""Unit tests: the pure pypdf extraction parser (book P2, ADR-004).

No database, no HTTP, no storage — bytes in, :class:`ExtractionResult`
out. The contract under test:

- **outline mode** (a real ``/Outlines`` tree): chapters and topics come
  from the outline, printed chapter/topic heading lines stay BODY text,
  and only example/exercise lines carve leaf units;
- **heading mode** (no usable outline): conservative printed headings
  build the hierarchy, front matter is preserved under the first chapter
  with an ``unresolved_structure`` flag, and a heading-less document
  collapses to one flagged synthetic root;
- **no silent omission** in every mode: each source line ends up either
  in exactly one unit's content or as a recognised heading line's unit
  title — proven with exact multisets where titles cannot collide;
- **order stability and page traceability**: sibling ``display_order``
  is 1..n, every title is non-empty and every page range is inside the
  document;
- **honest failures**: encrypted/unreadable/textless documents raise
  :class:`ExtractionError` with one of exactly three stable codes and a
  message of our own — never a raw library string, never document text;
- **the disabled classifier**: ``unresolved`` everywhere by default,
  while a supplied classifier sees every unit exactly once.

Fixtures are built by ``tests.conftest``'s real (offset-accurate) PDF
builders, so these tests exercise the same pypdf code paths production
does.
"""
from __future__ import annotations

import io
import json
from collections import Counter

import pytest
from pypdf import PdfReader, PdfWriter

from app.models.enums import AssessmentRequirement, BookUnitKind
from app.services.book_extraction import (
    DISABLED_CLASSIFIER,
    ERROR_ENCRYPTED,
    ERROR_NO_TEXT,
    ERROR_UNREADABLE,
    WARNING_NO_HEADINGS,
    WARNING_NO_OUTLINE,
    WARNING_UNRESOLVED_STRUCTURE,
    ExtractionError,
    extract_book_pdf,
)
from tests.conftest import (
    build_minimal_pdf,
    build_multiline_pdf,
    build_outline_pdf,
)

# --- fixtures ------------------------------------------------------------------------


def _science_pages() -> list[list[str]]:
    return [
        ["Chapter 1 Introduction", "Science studies the world.", "It asks why things happen."],
        [
            "1.1 Water Cycle",
            "Water evaporates and falls as rain.",
            "Exercises",
            "1. Solve the puzzle.",
            "2. Count the drops.",
        ],
        [
            "1.2 Plants",
            "Plants make food from light.",
            "Example 1: Leaf mass.",
            "Weigh one leaf first.",
        ],
        ["Chapter 2 Forces", "Forces push and pull.", "Practice Exercises", "1. Pull the rope."],
    ]


def _science_outline() -> list[tuple[str, int, list[tuple[str, int]] | None]]:
    return [
        ("Chapter 1 Introduction", 0, [("1.1 Water Cycle", 1), ("1.2 Plants", 2)]),
        ("Chapter 2 Forces", 3, None),
    ]


def _units(result) -> list:
    return result.walk()


def _content_counter(units: list) -> Counter:
    lines: list[str] = []
    for unit in units:
        lines.extend(unit.content_lines)
    return Counter(lines)


def _titles(units: list) -> list[str]:
    return [unit.title for unit in units]


def _assert_orders_are_1_to_n(units: list) -> None:
    for unit in units:
        orders = sorted(child.display_order for child in unit.children)
        assert orders == list(range(1, len(orders) + 1)), (
            f"sibling orders under {unit.title!r}: {orders}"
        )
    root_orders = sorted(u.display_order for u in units)
    # Roots live on the walk's top level; the caller passes them in.
    assert root_orders == list(range(1, len(root_orders) + 1))


def _assert_pages_in_range(result, units: list) -> None:
    for unit in units:
        assert 1 <= unit.source_page_start <= unit.source_page_end <= result.page_count
        assert unit.title.strip(), "every unit title must be non-empty"
        _assert_pages_in_range(result, unit.children)


# --- outline mode --------------------------------------------------------------------


def test_outline_mode_builds_the_hierarchy_the_outline_declares() -> None:
    pages = _science_pages()
    result = extract_book_pdf(build_outline_pdf(pages, _science_outline()))

    assert result.page_count == 4
    assert result.warnings == []  # an outline AND printed headings exist

    roots = result.units
    assert [u.kind for u in roots] == ["chapter", "chapter"]
    assert _titles(roots) == ["Chapter 1 Introduction", "Chapter 2 Forces"]
    _assert_orders_are_1_to_n(roots)
    _assert_pages_in_range(result, _units(result))

    introduction = roots[0]
    forces = roots[1]
    # Chapter ranges are contiguous: 1..3 then 4..4.
    assert (introduction.source_page_start, introduction.source_page_end) == (1, 3)
    assert (forces.source_page_start, forces.source_page_end) == (4, 4)

    topic_titles = [u.title for u in introduction.children if u.kind == "topic"]
    assert topic_titles == ["1.1 Water Cycle", "1.2 Plants"]
    water = introduction.children[0]
    plants = introduction.children[1]
    assert (water.source_page_start, water.source_page_end) == (2, 2)
    assert (plants.source_page_start, plants.source_page_end) == (3, 3)
    # The printed leaf headings carve units under the deepest open topic.
    assert [(u.kind, u.title) for u in water.children] == [
        ("exercise_set", "Exercises")
    ]
    assert [(u.kind, u.title) for u in plants.children] == [
        ("example", "Example 1: Leaf mass.")
    ]
    # "Practice Exercises" appears after chapter 2 opened: it is chapter-level.
    assert [(u.kind, u.title) for u in forces.children] == [
        ("exercise_set", "Practice Exercises")
    ]


def test_outline_titles_are_outline_owned_and_heading_lines_stay_body_text() -> None:
    pages = _science_pages()
    result = extract_book_pdf(build_outline_pdf(pages, _science_outline()))
    units = _units(result)
    content = _content_counter(units)

    # Printed chapter/topic heading lines are body text in outline mode:
    # the outline owns the structure, the line itself stays source text.
    assert content["Chapter 1 Introduction"] == 1
    assert content["1.1 Water Cycle"] == 1
    assert content["1.2 Plants"] == 1
    # ...while recognised leaf heading lines became titles exactly once
    # and appear in NO unit's content.
    for leaf_title in ("Exercises", "Example 1: Leaf mass.", "Practice Exercises"):
        assert leaf_title in _titles(units)
        assert content[leaf_title] == 0


def test_outline_mode_never_loses_a_line_even_when_a_chapter_wins_no_pages() -> None:
    """Three bookmark chapters all pointing at page 1.

    Ranges are made contiguous, so the middle chapter's range collapses
    to a page the first chapter already owns — it never wins a visit.
    It must still exist as a root unit with its outline title, orders
    stay 1..n, and no source line disappears.
    """
    pages = _science_pages()
    outline: list[tuple[str, int, list[tuple[str, int]] | None]] = [
        ("Chapter A", 0, None),
        ("Chapter B", 0, None),
        ("Chapter C", 1, None),
    ]
    result = extract_book_pdf(build_outline_pdf(pages, outline))

    root_titles = _titles(result.units)
    assert root_titles == ["Chapter A", "Chapter B", "Chapter C"]
    _assert_orders_are_1_to_n(result.units)
    _assert_pages_in_range(result, _units(result))

    source = Counter(line for page in pages for line in page)
    # Every source line is in some unit's content, and content only
    # ever holds source lines (chapter/topic lines here: headings are
    # chapter-declared only, no leaf headings in range were consumed...
    # page 2+ content includes exercise lines as leaf titles, so compare
    # content against the source lines that are not leaf headings).
    content = _content_counter(_units(result))
    assert not (set(content) - set(source))
    covered = set(content) | set(_titles(_units(result)))
    assert set(source) <= covered


def test_front_matter_before_the_first_outline_chapter_is_preserved() -> None:
    pages = [
        ["Preface words live here.", "Exercises"],
        ["Chapter 1 Begins", "Body under the chapter."],
    ]
    outline = [("Chapter 1 Begins", 1, None)]
    result = extract_book_pdf(build_outline_pdf(pages, outline))

    chapter = result.units[0]
    assert chapter.title == "Chapter 1 Begins"
    # Both page-1 lines (even the leaf heading with no parent yet) were
    # flushed into the first chapter rather than dropped...
    assert chapter.content_lines[:2] == ["Preface words live here.", "Exercises"]
    assert WARNING_UNRESOLVED_STRUCTURE in chapter.extraction_warnings
    # ...and the reviewer is told the structure there is unresolved.
    assert chapter.assessment_requirement == AssessmentRequirement.UNRESOLVED.value


def test_outline_mode_with_no_printed_headings_still_builds_chapters() -> None:
    pages = [
        ["Plain prose on page one."],
        ["More prose on page two."],
    ]
    outline = [("Chapter Only", 0, None)]
    result = extract_book_pdf(build_outline_pdf(pages, outline))

    assert result.warnings == [WARNING_NO_HEADINGS]  # no leaf heading anywhere
    assert [u.kind for u in result.units] == ["chapter"]
    assert result.units[0].content_lines == [
        "Plain prose on page one.",
        "More prose on page two.",
    ]


def test_outline_mode_content_is_every_line_that_is_not_a_leaf_heading() -> None:
    from app.services.book_extraction import classify_heading

    pages = _science_pages()
    result = extract_book_pdf(build_outline_pdf(pages, _science_outline()))

    expected = Counter(
        line
        for page in pages
        for line in page
        if classify_heading(line) in (None, "chapter", "topic")
    )
    assert _content_counter(_units(result)) == expected


# --- heading mode --------------------------------------------------------------------


def test_heading_mode_carves_the_tree_from_printed_headings() -> None:
    pages = _science_pages()
    result = extract_book_pdf(build_multiline_pdf(pages))

    assert result.warnings == [WARNING_NO_OUTLINE]
    roots = result.units
    assert [u.kind for u in roots] == ["chapter", "chapter"]
    assert _titles(roots) == ["Chapter 1 Introduction", "Chapter 2 Forces"]
    _assert_orders_are_1_to_n(roots)
    _assert_pages_in_range(result, _units(result))

    introduction = roots[0]
    water, plants = introduction.children
    assert (water.kind, water.title) == ("topic", "1.1 Water Cycle")
    assert (plants.kind, plants.title) == ("topic", "1.2 Plants")
    assert [u.title for u in water.children] == ["Exercises"]
    assert [u.title for u in plants.children] == ["Example 1: Leaf mass."]
    forces = roots[1]
    assert [u.title for u in forces.children] == ["Practice Exercises"]


def test_heading_mode_content_is_exactly_the_non_heading_lines() -> None:
    """Exact multiset: heading lines became titles, nothing else moved."""
    from app.services.book_extraction import classify_heading

    pages = _science_pages()
    result = extract_book_pdf(build_multiline_pdf(pages))

    source = [line for page in pages for line in page]
    expected = Counter(line for line in source if classify_heading(line) is None)
    assert _content_counter(_units(result)) == expected

    heading_lines = [line for line in source if classify_heading(line) is not None]
    assert set(heading_lines) <= set(_titles(_units(result)))
    content = _content_counter(_units(result))
    for heading_line in heading_lines:
        assert content[heading_line] == 0, f"heading consumed as title: {heading_line!r}"


def test_front_matter_opens_the_first_chapter_flagged_for_review() -> None:
    pages = [
        ["Preface words live here."],
        ["Chapter 1 Solar System", "Our star and planets."],
    ]
    result = extract_book_pdf(build_multiline_pdf(pages))

    chapter = result.units[0]
    assert chapter.title == "Chapter 1 Solar System"
    assert chapter.content_lines == ["Preface words live here.", "Our star and planets."]
    assert WARNING_UNRESOLVED_STRUCTURE in chapter.extraction_warnings


def test_a_document_without_any_heading_collapses_to_one_flagged_root() -> None:
    pages = [["Just some text."], ["More text."], ["Even more."]]
    result = extract_book_pdf(build_multiline_pdf(pages))

    assert result.warnings == [WARNING_NO_OUTLINE, WARNING_NO_HEADINGS]
    assert len(result.units) == 1
    root = result.units[0]
    assert root.kind == BookUnitKind.CHAPTER.value
    assert root.title == "Just some text."  # first line, no metadata
    assert root.content_lines == ["Just some text.", "More text.", "Even more."]
    assert WARNING_UNRESOLVED_STRUCTURE in root.extraction_warnings
    _assert_pages_in_range(result, _units(result))


def test_never_a_line_lost_in_heading_mode_for_any_document_shape() -> None:
    from app.services.book_extraction import classify_heading

    shapes = [
        _science_pages(),
        [["Preface words live here."], ["Chapter 1 Solar System", "Our star and planets."]],
        [["Just some text."], ["More text."]],
        # topic/leaf before any chapter: the implicit chapter wrapper
        [["1.1 Starts Early", "Body under an ensured chapter."]],
    ]
    for pages in shapes:
        result = extract_book_pdf(build_multiline_pdf(pages))
        source = [line for page in pages for line in page]
        content = _content_counter(_units(result))
        # content holds precisely the non-heading source lines...
        assert content == Counter(
            line for line in source if classify_heading(line) is None
        ), pages
        # ...and every heading line is a title somewhere.
        headings = {line for line in source if classify_heading(line) is not None}
        assert headings <= set(_titles(_units(result))), pages


# --- classification -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("Chapter 1 Numbers", "chapter"),
        ("Chapter VII Energy", "chapter"),
        ("lesson 3 water", "chapter"),
        ("Lesson 3 Water", "chapter"),
        ("Unit 2 Plants", "chapter"),
        ("Part IV Energy", "chapter"),
        ("1.1 Water Cycle", "topic"),
        ("2.3.1 Detail View", "topic"),
        ("1.1 Capitalised Title with trailing text", "topic"),
        ("3.5 kg of sugar", None),  # measurement, lowercase after number
        ("1.2 lower case stays prose", None),
        ("Example 2: Add three.", "example"),
        ("worked example 1: Mass", "example"),
        ("Worked Example: do it", "example"),
        ("Example: solve this", "example"),
        ("Example 3.1 - Compare", "example"),
        ("Exercises", "exercise_set"),
        ("Exercises 3", "exercise_set"),
        ("Practice", "exercise_set"),
        ("Practice Problems", "exercise_set"),
        ("Problems", "exercise_set"),
        ("Activities", "exercise_set"),
        ("Chapter review", "exercise_set"),
        ("Review exercises", "exercise_set"),
        ("Exercise 4", None),  # singular bare "exercise" is not the set pattern
        ("This is ordinary prose that mentions Chapter 1 in the middle.", None),
        ("", None),
        ("   ", None),
    ],
)
def test_classify_heading_matches_only_the_fixed_vocabulary(line, kind) -> None:
    from app.services.book_extraction import classify_heading

    assert classify_heading(line) == kind


def test_a_long_line_is_never_a_heading() -> None:
    from app.services.book_extraction import classify_heading

    long_chapter = "Chapter 1 " + "x" * 120
    assert len(long_chapter) > 100
    assert classify_heading(long_chapter) is None
    # A heading-looking line just under the cap still classifies.
    assert classify_heading("Chapter 1 " + "x" * 80) == "chapter"


# --- failures -------------------------------------------------------------------------


def test_garbage_bytes_fail_as_unreadable_with_our_own_message() -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extract_book_pdf(b"not a pdf at all")
    error = excinfo.value
    assert error.code == ERROR_UNREADABLE
    assert str(error) == "the document could not be parsed as a PDF"
    assert error.stored() == (
        "unreadable_pdf: the document could not be parsed as a PDF"
    )
    # The stored reason never echoes the payload or a library string.
    assert b"not a pdf" not in error.stored().encode()


def test_an_image_only_document_fails_as_no_extractable_text() -> None:
    with pytest.raises(ExtractionError) as excinfo:
        extract_book_pdf(build_minimal_pdf(None))
    error = excinfo.value
    assert error.code == ERROR_NO_TEXT
    assert error.stored() == "no_extractable_text: the document contains no extractable text"


def test_an_encrypted_document_fails_as_encrypted() -> None:
    reader = PdfReader(io.BytesIO(build_minimal_pdf()))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("locked")
    buffer = io.BytesIO()
    writer.write(buffer)

    with pytest.raises(ExtractionError) as excinfo:
        extract_book_pdf(buffer.getvalue())
    error = excinfo.value
    assert error.code == ERROR_ENCRYPTED
    assert error.stored() == "encrypted_pdf: the document is encrypted and cannot be read"


def test_only_the_three_fixed_codes_exist() -> None:
    codes = set()
    for payload in (b"nope", build_minimal_pdf(None)):
        try:
            extract_book_pdf(payload)
        except ExtractionError as error:
            codes.add(error.code)
    assert codes == {ERROR_UNREADABLE, ERROR_NO_TEXT}


# --- document title, summary, classifier -------------------------------------------------


def test_the_document_title_prefers_pdf_metadata_over_the_first_line() -> None:
    reader = PdfReader(io.BytesIO(build_multiline_pdf([["First Line On Page"]])))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.add_metadata({"/Title": "  Science Book 2027  "})
    buffer = io.BytesIO()
    writer.write(buffer)

    result = extract_book_pdf(buffer.getvalue())
    assert result.title == "Science Book 2027"


def test_summary_json_is_sorted_counts_kinds_and_carries_no_content() -> None:
    result = extract_book_pdf(build_outline_pdf(_science_pages(), _science_outline()))
    summary = json.loads(result.summary_json())

    assert set(summary) == {"by_kind", "page_count", "title", "unit_count", "warnings"}
    assert summary["page_count"] == result.page_count
    assert summary["unit_count"] == len(result.walk())
    assert sum(summary["by_kind"].values()) == summary["unit_count"]
    assert summary["by_kind"]["chapter"] == 2
    assert summary["by_kind"]["topic"] == 2
    assert summary["by_kind"] == dict(sorted(summary["by_kind"].items()))
    assert summary["warnings"] == []
    # Serialisation is deterministic (sorted keys).
    assert result.summary_json() == result.summary_json()
    assert result.summary_json().index('"by_kind"') < result.summary_json().index('"title"')
    # The summary describes, it does not quote: no extracted prose.
    assert "Water evaporates" not in result.summary_json()


def test_the_default_classifier_marks_every_unit_unresolved_without_io() -> None:
    result = extract_book_pdf(build_outline_pdf(_science_pages(), _science_outline()))
    assert result.units  # something was classified
    assert {
        unit.assessment_requirement for unit in _units(result)
    } == {AssessmentRequirement.UNRESOLVED.value}
    assert DISABLED_CLASSIFIER.classify(kind="chapter", title="X", content="Y") == (
        AssessmentRequirement.UNRESOLVED.value
    )


def test_a_supplied_classifier_sees_every_unit_exactly_once() -> None:
    seen: list[dict] = []

    class _Spy:
        def classify(self, *, kind: str, title: str, content: str) -> str:
            seen.append({"kind": kind, "title": title, "content": content})
            return AssessmentRequirement.REQUIRED.value

    result = extract_book_pdf(
        build_outline_pdf(_science_pages(), _science_outline()), classifier=_Spy()
    )
    units = _units(result)
    assert len(seen) == len(units)
    # The classifier received exactly what the unit carries...
    assert [call["title"] for call in seen] == [unit.title for unit in units]
    assert [call["kind"] for call in seen] == [unit.kind for unit in units]
    # ...and its answer is what the unit stores.
    assert {unit.assessment_requirement for unit in units} == {
        AssessmentRequirement.REQUIRED.value
    }


# --- global invariants -------------------------------------------------------------------


def test_every_source_line_is_content_or_a_title_in_every_mode() -> None:
    fixtures = [
        (_science_pages(), build_outline_pdf(_science_pages(), _science_outline())),
        (_science_pages(), build_multiline_pdf(_science_pages())),
        ([["Only prose here."]], build_multiline_pdf([["Only prose here."]])),
    ]
    for pages, payload in fixtures:
        source = set(line for page in pages for line in page)
        result = extract_book_pdf(payload)
        units = _units(result)
        content_lines = [line for unit in units for line in unit.content_lines]
        assert set(content_lines) <= source, result.title
        assert source <= set(content_lines) | set(_titles(units)), result.title
        # Content is stored as newline-joined lines; no line ever gains
        # or loses a character on the way in.
        for unit in units:
            assert unit.content.split("\n") == unit.content_lines or (
                unit.content == "" and unit.content_lines == []
            )


def test_page_count_reflects_the_real_page_tree() -> None:
    assert extract_book_pdf(build_multiline_pdf([["a"], ["b"]])).page_count == 2
    assert extract_book_pdf(build_multiline_pdf([["a"], ["b"], ["c"]])).page_count == 3
