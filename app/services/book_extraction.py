"""Pure PDF → ``BookUnit`` tree extraction (book P2, ADR-004).

One source document in, one deterministic structure out. This module
knows nothing about sessions, storage or HTTP — the orchestration that
claims a revision, reads its bytes and persists the result lives in
:mod:`app.services.book_extraction_service`.

How a document becomes a hierarchy (in order):

1. **Open and prove**: pypdf parses the bytes; an encrypted or corrupt
   document raises :class:`ExtractionError` with a stable code
   (``encrypted_pdf`` / ``unreadable_pdf``). A document whose pages
   yield no text raises ``no_extractable_text`` — P2 refuses to invent
   content, it only structures what is there (no OCR, ever).
2. **Outline pass** (when the PDF carries a usable bookmark tree):
   top-level destinations become chapters, nested destinations become
   topics, each with the 1-based page range the bookmark spans. Text on
   pages before the first chapter is front matter, preserved under the
   chapter that first opens with an ``unresolved_structure`` warning.
   A missing or unusable outline records the revision-level
   ``no_outline`` warning.
3. **Heading pass** (always): conservative, source-line-anchored
   regular expressions recognize printed ``chapter``/``topic``/
   ``example``/``exercise_set`` headings. In outline mode they only
   carve leaf units (the outline already owns chapters/topics, so their
   printed heading lines are body text); without an outline they build
   the whole tree. Every recognized heading line becomes a unit title
   VERBATIM (trimmed) — a unit's title is never generated or
   translated. A document with no recognizable heading records
   ``no_headings_recognized``.
4. **Assignment**: each remaining line is appended to the deepest open
   container (leaf → topic → chapter → the synthetic root created when
   the document opens with no chapter at all, titled from the PDF
   metadata or the first non-empty line — both source-derived).

Invariants the tests pin:

- **no silent omission** — every non-empty extracted line is exactly
  one unit's title or part of exactly one unit's content;
- **order stability** — sibling ``display_order`` is 1..n in creation
  order; content concatenation in tree order reproduces the source
  text (heading lines excluded, they became titles);
- **page traceability** — every unit's ``[source_page_start,
  source_page_end]`` covers the pages its title/content came from and
  spans its subtree, so a parent's range always contains its children's.

**No AI runs here.** :class:`AssessmentClassifier` is a provider-neutral
extension point whose only shipped implementation returns
``unresolved`` without performing any I/O — it is structurally incapable
of transmitting anything, which is what keeps P2 offline (no AI SDK, no
network, no OCR). Result content is source-derived text only: never
storage keys, filesystem paths or raw bytes.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from pypdf import PdfReader

from app.models.enums import AssessmentRequirement, BookUnitKind

# --- fixed vocabularies (the only strings this module invents) ---------------------

#: Revision-level warnings (``book_revisions.extraction_summary``).
WARNING_NO_OUTLINE = "no_outline"
WARNING_NO_HEADINGS = "no_headings_recognized"
#: Unit-level warning (``book_units.extraction_warnings``).
WARNING_UNRESOLVED_STRUCTURE = "unresolved_structure"

#: Stable refusal codes (``book_revisions.extraction_error``).
ERROR_ENCRYPTED = "encrypted_pdf"
ERROR_UNREADABLE = "unreadable_pdf"
ERROR_NO_TEXT = "no_extractable_text"


class ExtractionError(Exception):
    """A stable, source-safe reason the document could not be structured.

    The message is always one of ours (never a raw library message), so
    a failure can be stored and shown without leaking byte counts,
    paths or library internals.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code

    def stored(self) -> str:
        """The ``extraction_error`` column value: ``code: message``."""
        return f"{self.code}: {self}"


# --- heading recognition ------------------------------------------------------------

#: Longest line treated as a heading (printed headings are short; a
#: long "match" is a sentence and must stay body text).
_MAX_HEADING_CHARS = 100

_HEADING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        BookUnitKind.CHAPTER.value,
        re.compile(
            r"^(?:chapter|lesson|unit|part)\s+(?:\d+|[ivxlcdm]+)\b",
            re.IGNORECASE,
        ),
    ),
    (
        # "1.1 Water Cycle" / "2.3.1 Detail": a section number followed by
        # a capitalized title. The capital keeps measurements like
        # "3.5 kg of sugar" out of the structure.
        BookUnitKind.TOPIC.value,
        re.compile(r"^\d+(?:\.\d+)+\s+[A-Z].*"),
    ),
    (
        BookUnitKind.EXAMPLE.value,
        re.compile(
            r"^(?:worked\s+example|example)"
            r"(?:\s*\d[\d.]*\s*[:.\-–]?\s*\S.*|\s*[:.\-–]+\s*\S.*)$",
            re.IGNORECASE,
        ),
    ),
    (
        BookUnitKind.EXERCISE_SET.value,
        re.compile(
            r"^(?:chapter\s+review|review\s+exercises|practice\s+exercises"
            r"|practice\s+problems|exercises|problems|activities|practice)"
            r"(?:\s*\d[\d.]*|\s*[:.\-–]+|\s*$)",
            re.IGNORECASE,
        ),
    ),
)


def classify_heading(line: str) -> str | None:
    """The unit kind this line is a heading for, or None (body text)."""
    text = line.strip()
    if not text or len(text) > _MAX_HEADING_CHARS:
        return None
    for kind, pattern in _HEADING_PATTERNS:
        if pattern.match(text):
            return kind
    return None


# --- the disabled classification extension point ------------------------------------


class AssessmentClassifier(Protocol):
    """Provider-neutral decision point for ``assessment_requirement``.

    Deliberately tiny and synchronous: the ONLY implementation shipped
    performs no I/O of any kind, so calling it can never transmit the
    title or content it receives. A future phase may plug in an
    on-premise model behind this interface without changing the parser
    or the schema — and until then every unit stays ``unresolved``.
    """

    def classify(self, *, kind: str, title: str, content: str) -> str: ...


class _DisabledClassifier:
    """The shipped classifier: a constant answer, zero I/O, zero network."""

    def classify(self, *, kind: str, title: str, content: str) -> str:
        return AssessmentRequirement.UNRESOLVED.value


#: The classifier used by :func:`extract_book_pdf` unless overridden.
DISABLED_CLASSIFIER: AssessmentClassifier = _DisabledClassifier()


# --- results --------------------------------------------------------------------------


@dataclass
class ExtractedUnit:
    """One parsed unit before persistence (pages are 1-based)."""

    kind: str
    title: str
    display_order: int
    source_page_start: int
    source_page_end: int
    extraction_warnings: list[str] = field(default_factory=list)
    assessment_requirement: str = AssessmentRequirement.UNRESOLVED.value
    content_lines: list[str] = field(default_factory=list)
    children: list["ExtractedUnit"] = field(default_factory=list)
    #: Page tracking while the walk runs (title page first, then content).
    first_page: int = 0
    last_page: int = 0

    @property
    def content(self) -> str:
        """Extracted text; '' for a container that owns no text."""
        return "\n".join(self.content_lines)

    @property
    def warnings_text(self) -> str | None:
        """Newline-separated fixed codes, or NULL when clean."""
        return "\n".join(self.extraction_warnings) or None


@dataclass
class ExtractionResult:
    """The whole parse: roots, document-level facts and warnings."""

    #: Source-derived document title (PDF metadata or first non-empty
    #: line) — used for the synthetic root and the summary.
    title: str
    units: list[ExtractedUnit]
    page_count: int
    #: Revision-level warnings (``no_outline``, ``no_headings_recognized``).
    warnings: list[str] = field(default_factory=list)

    @property
    def unit_count(self) -> int:
        total = 0
        stack = list(self.units)
        while stack:
            unit = stack.pop()
            total += 1
            stack.extend(unit.children)
        return total

    def walk(self) -> list[ExtractedUnit]:
        """Every unit in depth-first document order (roots included)."""
        ordered: list[ExtractedUnit] = []

        def _visit(units: list[ExtractedUnit]) -> None:
            for unit in units:
                ordered.append(unit)
                _visit(unit.children)

        _visit(self.units)
        return ordered

    def summary_json(self) -> str:
        """The ``book_revisions.extraction_summary`` value (JSON)."""
        by_kind: dict[str, int] = {}
        for unit in self.walk():
            by_kind[unit.kind] = by_kind.get(unit.kind, 0) + 1
        return json.dumps(
            {
                "title": self.title,
                "page_count": self.page_count,
                "unit_count": self.unit_count,
                "by_kind": {kind: by_kind[kind] for kind in sorted(by_kind)},
                "warnings": self.warnings,
            },
            sort_keys=True,
        )


# --- outline model --------------------------------------------------------------------


@dataclass
class _OutlineTopic:
    title: str
    start: int
    end: int


@dataclass
class _OutlineChapter:
    title: str
    start: int
    end: int
    topics: list[_OutlineTopic] = field(default_factory=list)


# --- the pass --------------------------------------------------------------------------


def extract_book_pdf(
    pdf_bytes: bytes,
    *,
    classifier: AssessmentClassifier | None = None,
) -> ExtractionResult:
    """Parse one source PDF into a source-faithful unit tree.

    Raises :class:`ExtractionError` (stable ``code``) when the document
    cannot be structured; the caller turns that into
    ``extraction_failed``. Never raises a raw pypdf/OS error to its
    caller.
    """
    decide = classifier or DISABLED_CLASSIFIER

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:  # noqa: BLE001 — any parse refusal is one stable code
        raise ExtractionError(
            "the document could not be parsed as a PDF", code=ERROR_UNREADABLE
        ) from exc

    if reader.is_encrypted:
        raise ExtractionError(
            "the document is encrypted and cannot be read", code=ERROR_ENCRYPTED
        )

    try:
        page_texts = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 — a corrupt stream mid-parse
        raise ExtractionError(
            "the document could not be parsed as a PDF", code=ERROR_UNREADABLE
        ) from exc

    page_lines: list[list[str]] = [
        [stripped for stripped in (line.strip() for line in text.splitlines()) if stripped]
        for text in page_texts
    ]
    page_count = len(page_lines)
    if page_count == 0 or not any(page_lines):
        raise ExtractionError(
            "the document contains no extractable text", code=ERROR_NO_TEXT
        )

    document_title = _document_title(reader, page_lines)
    outline = _outline_chapters(reader, page_count)
    warnings: list[str] = []
    if not outline:
        warnings.append(WARNING_NO_OUTLINE)

    had_heading = any(
        classify_heading(line) is not None for lines in page_lines for line in lines
    )
    if not had_heading:
        warnings.append(WARNING_NO_HEADINGS)

    if outline:
        units = _walk_with_outline(page_lines, outline, document_title)
    else:
        units = _walk_with_headings(page_lines, document_title)

    result = ExtractionResult(
        title=document_title,
        units=units,
        page_count=page_count,
        warnings=warnings,
    )
    for unit in result.walk():
        unit.assessment_requirement = decide.classify(
            kind=unit.kind, title=unit.title, content=unit.content
        )
    return result


# --- helpers ----------------------------------------------------------------------------


def _document_title(reader: PdfReader, page_lines: list[list[str]]) -> str:
    """The source-derived document title: metadata, else first text line."""
    try:
        metadata = reader.metadata
        candidate = str(getattr(metadata, "title", "") or "").strip() if metadata else ""
    except Exception:  # noqa: BLE001 — broken /Info must not fail extraction
        candidate = ""
    if candidate:
        return candidate[:200]
    for lines in page_lines:
        for line in lines:
            return line[:200]
    return "document"  # unreachable: callers prove there is text first


def _outline_chapters(reader: PdfReader, page_count: int) -> list[_OutlineChapter]:
    """The bookmark tree as chapters/topics with 1-based page ranges.

    Anything unresolvable (a destination without a page, an entry
    pointing nowhere) is dropped; when nothing survives, the caller
    falls back to ``no_outline`` heading mode. Ranges are clamped into
    the document with end >= start, so a quirky bookmark tree can never
    produce an impossible page range. Chapters are made contiguous in
    document order (each ends where the next begins); topics stay
    inside their chapter's range.
    """
    try:
        raw_outline = reader.outline
    except Exception:  # noqa: BLE001 — a malformed outline is "no outline"
        return []

    collected: list[tuple[int, str, int]] = []  # (depth, title, page 1-based)

    def _walk(items, depth: int) -> None:
        for item in items:
            if isinstance(item, list):
                _walk(item, depth + 1)
                continue
            title = str(getattr(item, "title", "") or "").strip()
            if not title:
                continue
            try:
                page_index = reader.get_destination_page_number(item)
            except Exception:  # noqa: BLE001 — destination without a page
                continue
            if page_index is None or page_index < 0:
                continue
            collected.append((depth, title[:200], min(page_index + 1, page_count)))

    if raw_outline:
        _walk(raw_outline, 0)
    if not collected:
        return []

    # A tree whose entries are all nested still yields chapters: promote
    # every entry rather than losing the whole outline.
    if all(depth > 0 for depth, _, _ in collected):
        collected = [(0, title, page) for _, title, page in collected]

    # Document order (a stable sort makes a shuffled tree behave like
    # an ordered one; sibling ties keep their bookmark order).
    collected.sort(key=lambda entry: entry[2])

    chapters: list[_OutlineChapter] = []
    current_chapter: _OutlineChapter | None = None
    for depth, title, page in collected:
        if depth == 0:
            current_chapter = _OutlineChapter(title=title, start=page, end=page)
            chapters.append(current_chapter)
        elif current_chapter is not None:
            current_chapter.topics.append(_OutlineTopic(title=title, start=page, end=page))

    # Contiguous chapter ranges: each runs until the next starts (the
    # last runs to the end of the document).
    for index, chapter in enumerate(chapters):
        if index + 1 < len(chapters):
            chapter.end = max(chapter.start, chapters[index + 1].start - 1)
        else:
            chapter.end = page_count
        chapter.end = min(max(chapter.end, chapter.start), page_count)
        # Topics are page-granular and must sit inside their chapter.
        for topic in chapter.topics:
            topic.start = min(max(topic.start, chapter.start), chapter.end)
            topic.end = topic.start
        for position, topic in enumerate(chapter.topics):
            if position + 1 < len(chapter.topics):
                topic.end = max(topic.start, chapter.topics[position + 1].start - 1)
            else:
                topic.end = chapter.end
            topic.end = min(topic.end, chapter.end)
            topic.start = min(topic.start, topic.end)
    return chapters


def _new_unit(
    kind: str,
    title: str,
    *,
    page: int,
    display_order: int,
    warnings: list[str] | None = None,
) -> ExtractedUnit:
    return ExtractedUnit(
        kind=kind,
        title=title,
        display_order=display_order,
        source_page_start=page,
        source_page_end=page,
        extraction_warnings=list(warnings or []),
        first_page=page,
        last_page=page,
    )


def _append_line(unit: ExtractedUnit, line: str, page: int) -> None:
    unit.content_lines.append(line)
    unit.first_page = min(unit.first_page or page, page)
    unit.last_page = max(unit.last_page, page)


class _Orders:
    """Sibling display_order allocation: 1..n per parent, never reused.

    ``parent`` is either a ``ExtractedUnit`` (children live in
    ``parent.children``) or the root list itself; allocation continues
    after whatever is already attached, so a pre-created sibling and a
    later one can never share an order.
    """

    def __init__(self) -> None:
        self._next: dict[int, int] = {}

    def take(self, parent) -> int:
        key = id(parent)
        if key not in self._next:
            attached = getattr(parent, "children", parent)
            self._next[key] = len(attached) + 1
        order = self._next[key]
        self._next[key] = order + 1
        return order


def _finalize(units: list[ExtractedUnit]) -> list[ExtractedUnit]:
    """Materialise page ranges: own title/content pages, spanning children.

    A unit's range always covers the pages its own title and content came
    from, and — bottom-up — widens to contain its children's ranges, so
    a chapter reads as ``1..3`` when its topics live on pages 2-3. A
    parent's range therefore always contains every descendant's range,
    which is what makes the tree page-traceable at a glance.
    """
    for unit in units:
        if unit.first_page:
            unit.source_page_start = unit.first_page
            unit.source_page_end = max(unit.first_page, unit.last_page)
        _finalize(unit.children)
        if unit.children:
            unit.source_page_start = min(
                [unit.source_page_start]
                + [child.source_page_start for child in unit.children]
            )
            unit.source_page_end = max(
                [unit.source_page_end]
                + [child.source_page_end for child in unit.children]
            )
    return units


def _walk_with_outline(
    page_lines: list[list[str]],
    outline: list[_OutlineChapter],
    document_title: str,
) -> list[ExtractedUnit]:
    """Outline owns chapters/topics; printed headings carve leaf units."""
    orders = _Orders()

    # Chapters exist from the start (even one whose range never wins a
    # page), so no outline title can be lost to a range quirk.
    roots: list[ExtractedUnit] = []
    chapter_units: dict[int, ExtractedUnit] = {}  # id(_OutlineChapter) -> unit
    topic_units: dict[int, ExtractedUnit] = {}  # id(_OutlineTopic) -> unit
    for chapter in outline:
        unit = _new_unit(
            BookUnitKind.CHAPTER.value,
            chapter.title,
            page=chapter.start,
            display_order=orders.take(roots),
        )
        roots.append(unit)
        chapter_units[id(chapter)] = unit

    def _chapter_for(page: int) -> _OutlineChapter | None:
        for chapter in outline:
            if chapter.start <= page <= chapter.end:
                return chapter
        return None

    def _topic_for(page: int) -> _OutlineTopic | None:
        for chapter in outline:
            for topic in chapter.topics:
                if topic.start <= page <= topic.end:
                    return topic
        return None

    front_matter: list[tuple[str, int]] = []
    current_chapter: _OutlineChapter | None = None
    current_chapter_unit: ExtractedUnit | None = None
    current_topic: _OutlineTopic | None = None
    current_topic_unit: ExtractedUnit | None = None
    leaf: ExtractedUnit | None = None
    first_chapter_opened = False

    for page, lines in enumerate(page_lines, start=1):
        chapter_model = _chapter_for(page)
        topic_model = _topic_for(page)

        if chapter_model is not current_chapter:
            current_chapter = chapter_model
            current_chapter_unit = (
                chapter_units[id(chapter_model)]
                if chapter_model is not None
                else None
            )
            current_topic = None
            current_topic_unit = None
            leaf = None
            if chapter_model is not None and not first_chapter_opened:
                first_chapter_opened = True
                if front_matter and current_chapter_unit is not None:
                    for line, where in front_matter:
                        _append_line(current_chapter_unit, line, where)
                    current_chapter_unit.extraction_warnings.append(
                        WARNING_UNRESOLVED_STRUCTURE
                    )
                    front_matter.clear()

        if topic_model is not current_topic:
            current_topic = topic_model
            if topic_model is None:
                current_topic_unit = None
            else:
                chapter_unit = current_chapter_unit
                if chapter_unit is None:
                    owning = next(
                        (
                            chapter_units[id(model)]
                            for model in outline
                            if topic_model in model.topics and id(model) in chapter_units
                        ),
                        None,
                    )
                    chapter_unit = owning
                if chapter_unit is not None:
                    existing = topic_units.get(id(topic_model))
                    if existing is None:
                        existing = _new_unit(
                            BookUnitKind.TOPIC.value,
                            topic_model.title,
                            page=topic_model.start,
                            display_order=orders.take(chapter_unit),
                        )
                        topic_units[id(topic_model)] = existing
                        chapter_unit.children.append(existing)
                    current_topic_unit = existing
            leaf = None

        for line in lines:
            kind = classify_heading(line)
            if kind in (BookUnitKind.EXAMPLE.value, BookUnitKind.EXERCISE_SET.value):
                parent = current_topic_unit or current_chapter_unit
                if parent is None:
                    front_matter.append((line, page))
                    continue
                leaf = _new_unit(
                    kind,
                    line,
                    page=page,
                    display_order=orders.take(parent),
                )
                parent.children.append(leaf)
                continue
            # Body text — including printed chapter/topic heading lines:
            # the outline already owns that structure, the line itself
            # stays source text under the deepest open container.
            if leaf is not None:
                target: ExtractedUnit | None = leaf
            else:
                target = current_topic_unit or current_chapter_unit
            if target is None:
                front_matter.append((line, page))
            else:
                _append_line(target, line, page)

    if front_matter:
        # Pages before the first chapter were never opened (or the whole
        # outline lost the race): preserve the text, never drop it.
        target = roots[0] if roots else None
        if target is None:
            target = _new_unit(
                BookUnitKind.CHAPTER.value,
                document_title,
                page=front_matter[0][1],
                display_order=orders.take(roots),
                warnings=[WARNING_UNRESOLVED_STRUCTURE],
            )
            roots.append(target)
        for line, page in front_matter:
            _append_line(target, line, page)
        if WARNING_UNRESOLVED_STRUCTURE not in target.extraction_warnings:
            target.extraction_warnings.append(WARNING_UNRESOLVED_STRUCTURE)
    return _finalize(roots)


def _walk_with_headings(
    page_lines: list[list[str]],
    document_title: str,
) -> list[ExtractedUnit]:
    """No usable outline: printed headings build the whole hierarchy."""
    orders = _Orders()
    roots: list[ExtractedUnit] = []
    preamble: list[tuple[str, int]] = []
    chapter: ExtractedUnit | None = None
    topic: ExtractedUnit | None = None
    leaf: ExtractedUnit | None = None

    def _ensure_chapter(page: int) -> ExtractedUnit:
        nonlocal chapter, topic, leaf
        if chapter is None:
            chapter = _new_unit(
                BookUnitKind.CHAPTER.value,
                document_title,
                page=page,
                display_order=orders.take(roots),
                warnings=[WARNING_UNRESOLVED_STRUCTURE],
            )
            roots.append(chapter)
            for line, where in preamble:
                _append_line(chapter, line, where)
            preamble.clear()
        return chapter

    for page, lines in enumerate(page_lines, start=1):
        for line in lines:
            kind = classify_heading(line)
            if kind == BookUnitKind.CHAPTER.value:
                if chapter is None and preamble:
                    # The document opens with front text: it becomes this
                    # first chapter's content, flagged for review.
                    chapter = _new_unit(
                        BookUnitKind.CHAPTER.value,
                        line,
                        page=page,
                        display_order=orders.take(roots),
                        warnings=[WARNING_UNRESOLVED_STRUCTURE],
                    )
                    roots.append(chapter)
                    for old, where in preamble:
                        _append_line(chapter, old, where)
                    preamble.clear()
                else:
                    chapter = _new_unit(
                        BookUnitKind.CHAPTER.value,
                        line,
                        page=page,
                        display_order=orders.take(roots),
                    )
                    roots.append(chapter)
                topic = None
                leaf = None
                continue
            if kind == BookUnitKind.TOPIC.value:
                parent = _ensure_chapter(page)
                topic = _new_unit(
                    BookUnitKind.TOPIC.value,
                    line,
                    page=page,
                    display_order=orders.take(parent),
                )
                parent.children.append(topic)
                leaf = None
                continue
            if kind in (BookUnitKind.EXAMPLE.value, BookUnitKind.EXERCISE_SET.value):
                parent = topic if topic is not None else _ensure_chapter(page)
                leaf = _new_unit(
                    kind,
                    line,
                    page=page,
                    display_order=orders.take(parent),
                )
                parent.children.append(leaf)
                continue
            target = leaf if leaf is not None else (topic or chapter)
            if target is None:
                preamble.append((line, page))
            else:
                _append_line(target, line, page)

    if preamble:
        # No heading anywhere (or front text never met a chapter): one
        # synthetic root holds the whole text, flagged for review.
        root = _new_unit(
            BookUnitKind.CHAPTER.value,
            document_title,
            page=preamble[0][1],
            display_order=orders.take(roots),
            warnings=[WARNING_UNRESOLVED_STRUCTURE],
        )
        roots.append(root)
        for line, where in preamble:
            _append_line(root, line, where)
    return _finalize(roots)
