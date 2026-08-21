"""Parse law.go.kr-style Markdown into source-grounded semantic units."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .ids import stable_id
from .models import SemanticUnit, SourceRef


_ARTICLE_RE = re.compile(
    r"^#{1,6}\s+(?P<article>제\s*\d+(?:\s*의\s*\d+)?\s*조(?:\s*의\s*\d+)?)\s*(?P<title>.*)$"
)
_SUPPLEMENT_HEADING_RE = re.compile(r"^#{1,6}\s+부칙(?:\s|$|\()")
_PARAGRAPH_RE = re.compile(r"^\*\*(?P<marker>[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])\*\*\s*(?P<text>.*)$")
_ITEM_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>\d+|[가나다라마바사아자차카타파하])\\?\.\s+(?P<text>.*)$"
)
_CIRCLED_NUMBERS = {character: index for index, character in enumerate("①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳", start=1)}


@dataclass
class Article:
    article: str
    heading: str
    lines: list[str]


@dataclass
class Paragraph:
    number: int | None
    lines: list[str]


@dataclass
class ItemNode:
    marker: str
    indent: int
    lines: list[str] = field(default_factory=list)
    children: list["ItemNode"] = field(default_factory=list)

    @property
    def text(self) -> str:
        return _join_lines(self.lines)


def discover_markdown(input_path: Path) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() != ".md":
            raise ValueError(f"Input file must be Markdown: {input_path}")
        return [input_path]
    if not input_path.is_dir():
        raise FileNotFoundError(input_path)
    return sorted(path for path in input_path.rglob("*.md") if path.is_file())


def segment_markdown(path: Path, *, input_root: Path) -> list[SemanticUnit]:
    """Return reproducible extraction units for one source Markdown file.

    The splitter intentionally never mechanically breaks a long paragraph at
    punctuation. Numbered items are separate units; their parent text and
    ancestor items remain context instead of being silently discarded.
    """

    relative_path = _relative_source_path(path, input_root)
    return segment_text(path.read_text(encoding="utf-8"), source_path=relative_path, fallback_title=path.stem)


def segment_text(
    raw_text: str,
    *,
    source_path: str,
    fallback_title: str = "document",
    source_document_key: str | None = None,
) -> list[SemanticUnit]:
    """Segment Markdown text directly; useful for API callers and tests."""

    title, metadata, body = _front_matter_and_body(raw_text, fallback_title=fallback_title)
    identity = (
        f"source-key:{source_document_key}"
        if source_document_key is not None
        else _document_identity(metadata, title, source_path)
    )
    document_id = stable_id("document", identity)
    articles = _parse_articles(body)
    units: list[SemanticUnit] = []
    for article in articles:
        units.extend(
            _segment_article(
                article,
                document_id=document_id,
                document_title=title,
                source_path=source_path,
            )
        )
    return units


def segment_paths(input_path: Path) -> Iterator[SemanticUnit]:
    root = input_path if input_path.is_dir() else input_path.parent
    for path in discover_markdown(input_path):
        yield from segment_markdown(path, input_root=root)


def _front_matter_and_body(raw_text: str, *, fallback_title: str) -> tuple[str, dict[str, str], str]:
    lines = raw_text.splitlines()
    title = fallback_title
    metadata: dict[str, str] = {}
    if lines and lines[0].strip() == "---":
        try:
            closing_index = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
        except StopIteration:
            closing_index = -1
        if closing_index > 0:
            for line in lines[1:closing_index]:
                if ":" in line and not line.startswith((" ", "-")):
                    key, value = line.split(":", maxsplit=1)
                    metadata[key.strip()] = value.strip().strip("'\"")
            title = metadata.get("제목", title)
            return title, metadata, "\n".join(lines[closing_index + 1 :])

    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            break
    return title, metadata, raw_text


def _parse_articles(body: str) -> list[Article]:
    articles: list[Article] = []
    current: Article | None = None
    for line in body.splitlines():
        # The source files append historical supplementary provisions and
        # amendments after the consolidated current body. The v3 first pass
        # targets that current body only; otherwise old cross-law amendments
        # leak into the last main-body article and create enormous units.
        if _SUPPLEMENT_HEADING_RE.match(line):
            break
        match = _ARTICLE_RE.match(line)
        if match:
            if current is not None:
                articles.append(current)
            article = re.sub(r"\s+", "", match.group("article"))
            current = Article(article=article, heading=line.strip(), lines=[])
            continue
        if line.startswith("#"):
            # Chapter/section headings belong to navigation, not to the
            # preceding article's executable text.
            continue
        if current is not None:
            current.lines.append(line)
    if current is not None:
        articles.append(current)
    return articles


def _segment_article(
    article: Article,
    *,
    document_id: str,
    document_title: str,
    source_path: str,
) -> list[SemanticUnit]:
    units: list[SemanticUnit] = []
    for paragraph in _split_paragraphs(article.lines):
        roots, introductory_lines = _parse_items(paragraph.lines)
        base_context = [article.heading]
        if paragraph.number is not None:
            base_context.append(f"{article.article} 제{paragraph.number}항")

        introductory_text = _join_lines(introductory_lines)
        if not roots and introductory_text:
            units.append(
                _make_unit(
                    document_id=document_id,
                    document_title=document_title,
                    source_path=source_path,
                    article=article.article,
                    paragraph=paragraph.number,
                    item_path=(),
                    context_text="\n".join(base_context),
                    unit_text=introductory_text,
                    unit_kind="paragraph",
                )
            )

        for root in roots:
            units.extend(
                _segment_terminal_item(
                    root,
                    document_id=document_id,
                    document_title=document_title,
                    source_path=source_path,
                    article=article.article,
                    paragraph=paragraph.number,
                    base_context=base_context,
                    introductory_text=introductory_text,
                    ancestor_items=(),
                    item_path=(),
                )
            )
    return units


def _segment_terminal_item(
    item: ItemNode,
    *,
    document_id: str,
    document_title: str,
    source_path: str,
    article: str,
    paragraph: int | None,
    base_context: list[str],
    introductory_text: str,
    ancestor_items: tuple[str, ...],
    item_path: tuple[str, ...],
) -> list[SemanticUnit]:
    current_path = (*item_path, item.marker)
    context_parts = list(base_context)
    if introductory_text:
        context_parts.append(introductory_text)
    context_parts.extend(ancestor_items)
    if not item.children:
        if not item.text:
            return []
        return [
            _make_unit(
                document_id=document_id,
                document_title=document_title,
                source_path=source_path,
                article=article,
                paragraph=paragraph,
                item_path=current_path,
                context_text="\n".join(context_parts),
                unit_text=item.text,
                unit_kind="terminal_item",
            )
        ]

    units: list[SemanticUnit] = []
    next_ancestors = (*ancestor_items, item.text) if item.text else ancestor_items
    for child in item.children:
        units.extend(
            _segment_terminal_item(
                child,
                document_id=document_id,
                document_title=document_title,
                source_path=source_path,
                article=article,
                paragraph=paragraph,
                base_context=base_context,
                introductory_text=introductory_text,
                ancestor_items=next_ancestors,
                item_path=current_path,
            )
        )
    return units


def _make_unit(
    *,
    document_id: str,
    document_title: str,
    source_path: str,
    article: str,
    paragraph: int | None,
    item_path: tuple[str, ...],
    context_text: str,
    unit_text: str,
    unit_kind: str,
) -> SemanticUnit:
    unit_id = stable_id(
        "semantic_unit",
        document_id,
        article,
        paragraph if paragraph is not None else "article_body",
        "/".join(item_path),
        unit_kind,
        unit_text,
    )
    return SemanticUnit(
        semantic_unit_id=unit_id,
        document_id=document_id,
        document_title=document_title,
        source_path=source_path,
        source_ref=SourceRef(article=article, paragraph=paragraph, item_path=item_path),
        context_text=context_text,
        unit_text=unit_text,
        unit_kind=unit_kind,
    )


def _split_paragraphs(lines: list[str]) -> list[Paragraph]:
    paragraphs: list[Paragraph] = []
    current_number: int | None = None
    current_lines: list[str] = []
    seen_numbered_paragraph = False
    for line in lines:
        match = _PARAGRAPH_RE.match(line)
        if match:
            if current_lines or seen_numbered_paragraph:
                paragraphs.append(Paragraph(number=current_number, lines=current_lines))
            current_number = _CIRCLED_NUMBERS[match.group("marker")]
            current_lines = [match.group("text")]
            seen_numbered_paragraph = True
        else:
            current_lines.append(line)
    if current_lines or seen_numbered_paragraph:
        paragraphs.append(Paragraph(number=current_number, lines=current_lines))
    return [paragraph for paragraph in paragraphs if _join_lines(paragraph.lines)]


def _parse_items(lines: list[str]) -> tuple[list[ItemNode], list[str]]:
    roots: list[ItemNode] = []
    stack: list[ItemNode] = []
    introductory_lines: list[str] = []
    for line in lines:
        match = _ITEM_RE.match(line)
        if match:
            indent = len(match.group("indent").expandtabs(2))
            item = ItemNode(marker=match.group("marker"), indent=indent, lines=[match.group("text")])
            while stack and stack[-1].indent >= indent:
                stack.pop()
            if stack:
                stack[-1].children.append(item)
            else:
                roots.append(item)
            stack.append(item)
        elif stack:
            stack[-1].lines.append(line)
        else:
            introductory_lines.append(line)
    return roots, introductory_lines


def _join_lines(lines: list[str]) -> str:
    cleaned = [line.strip() for line in lines if line.strip()]
    return "\n".join(cleaned).strip()


def _relative_source_path(path: Path, input_root: Path) -> str:
    try:
        return path.relative_to(input_root).as_posix()
    except ValueError:
        return path.name


def _document_identity(metadata: dict[str, str], title: str, relative_path: str) -> str:
    """Prefer immutable official metadata over a CLI-dependent input path."""

    official_id = metadata.get("법령MST") or metadata.get("법령ID")
    if official_id:
        return f"official:{official_id}:{title}"
    source = metadata.get("출처")
    if source:
        return f"source:{source}"
    return f"path:{relative_path}:{title}"
