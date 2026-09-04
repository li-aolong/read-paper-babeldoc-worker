from __future__ import annotations

import copy
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class CompactIRCaptureError(RuntimeError):
    pass


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 3)


def _box(box: Any) -> list[float] | None:
    if box is None:
        return None
    values = [_number(getattr(box, name, None)) for name in ("x", "y", "x2", "y2")]
    if any(value is None for value in values):
        return None
    return values


def _text_from_characters(characters: Any) -> str:
    return "".join(
        str(value)
        for char in characters or []
        if (value := getattr(char, "char_unicode", None)) is not None
    )


def _font_map(page: Any, xobj_id: Any) -> dict[str, Any]:
    fonts = {
        str(font.font_id): font
        for font in getattr(page, "pdf_font", []) or []
        if getattr(font, "font_id", None) is not None
    }
    for xobj in getattr(page, "pdf_xobject", []) or []:
        if getattr(xobj, "xobj_id", None) != xobj_id:
            continue
        fonts.update(
            {
                str(font.font_id): font
                for font in getattr(xobj, "pdf_font", []) or []
                if getattr(font, "font_id", None) is not None
            }
        )
        break
    return fonts


def _style(style: Any, fonts: dict[str, Any]) -> dict[str, Any] | None:
    if style is None:
        return None
    font_id = getattr(style, "font_id", None)
    font = fonts.get(str(font_id)) if font_id is not None else None
    result: dict[str, Any] = {
        "font_id": str(font_id) if font_id is not None else None,
        "font_size_pt": _number(getattr(style, "font_size", None)),
    }
    if font is not None:

        def optional_bool(name: str) -> bool | None:
            value = getattr(font, name, None)
            return bool(value) if value is not None else None

        result.update(
            {
                "font_name": getattr(font, "name", None),
                "bold": optional_bool("bold"),
                "italic": optional_bool("italic"),
                "monospace": optional_bool("monospace"),
                "serif": optional_bool("serif"),
            }
        )
    return result


def _run_from_composition(
    composition: Any, fonts: dict[str, Any]
) -> dict[str, Any] | None:
    same_unicode = getattr(composition, "pdf_same_style_unicode_characters", None)
    if same_unicode is not None:
        return {
            "kind": "text",
            "text": str(getattr(same_unicode, "unicode", None) or ""),
            "bbox": None,
            "style": _style(getattr(same_unicode, "pdf_style", None), fonts),
        }

    same_characters = getattr(composition, "pdf_same_style_characters", None)
    if same_characters is not None:
        return {
            "kind": "text",
            "text": _text_from_characters(
                getattr(same_characters, "pdf_character", [])
            ),
            "bbox": _box(getattr(same_characters, "box", None)),
            "style": _style(getattr(same_characters, "pdf_style", None), fonts),
        }

    formula = getattr(composition, "pdf_formula", None)
    if formula is not None:
        characters = getattr(formula, "pdf_character", []) or []
        first_style = getattr(characters[0], "pdf_style", None) if characters else None
        return {
            "kind": "formula",
            "text": _text_from_characters(characters),
            "bbox": _box(getattr(formula, "box", None)),
            "style": _style(first_style, fonts),
        }

    line = getattr(composition, "pdf_line", None)
    if line is not None:
        characters = getattr(line, "pdf_character", []) or []
        first_style = getattr(characters[0], "pdf_style", None) if characters else None
        return {
            "kind": "text",
            "text": _text_from_characters(characters),
            "bbox": _box(getattr(line, "box", None)),
            "style": _style(first_style, fonts),
        }

    character = getattr(composition, "pdf_character", None)
    if character is not None:
        return {
            "kind": "text",
            "text": str(getattr(character, "char_unicode", None) or ""),
            "bbox": _box(getattr(character, "box", None)),
            "style": _style(getattr(character, "pdf_style", None), fonts),
        }
    return None


def _runs(paragraph: Any, fonts: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for composition in getattr(paragraph, "pdf_paragraph_composition", []) or []:
        run = _run_from_composition(composition, fonts)
        if run is not None and (run["text"] or run["kind"] == "formula"):
            result.append(run)
    return result


def _visible_text(paragraph: Any) -> str:
    from babeldoc.format.pdf.document_il.utils.layout_helper import (
        get_paragraph_unicode,
    )

    return str(get_paragraph_unicode(paragraph) or "")


def _fallback_paragraph_box(paragraph: Any) -> list[float]:
    direct = _box(getattr(paragraph, "box", None))
    if direct is not None:
        return direct
    boxes = [run["bbox"] for run in _runs(paragraph, {}) if run.get("bbox") is not None]
    if not boxes:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        min(value[0] for value in boxes),
        min(value[1] for value in boxes),
        max(value[2] for value in boxes),
        max(value[3] for value in boxes),
    ]


class CompactIRCollector:
    def __init__(
        self,
        *,
        source_sha256: str,
        source_filename: str,
        source_language: str,
        target_language: str,
        engine: dict[str, Any],
    ) -> None:
        self.source_sha256 = source_sha256
        self.source_filename = source_filename
        self.source_language = source_language
        self.target_language = target_language
        self.engine = copy.deepcopy(engine)
        self._source_offsets: set[int] = set()
        self._target_offsets: set[int] = set()
        self._pages_by_index: dict[int, dict[str, Any]] = {}
        self._paragraphs_by_object_id: dict[int, dict[str, Any]] = {}
        self._object_ids_by_offset: dict[int, set[int]] = {}

    def capture_source(self, document: Any, *, page_offset: int = 0) -> None:
        if page_offset in self._source_offsets:
            raise CompactIRCaptureError(
                f"compact IR source part {page_offset} 重复捕获"
            )
        self._source_offsets.add(page_offset)
        part_object_ids: set[int] = set()

        for page in getattr(document, "page", []) or []:
            local_page_index = int(getattr(page, "page_number", 0) or 0)
            page_index = page_offset + local_page_index
            page_number = page_index + 1
            if page_index in self._pages_by_index:
                raise CompactIRCaptureError(f"compact IR 第 {page_number} 页重复捕获")
            page_box_obj = getattr(getattr(page, "mediabox", None), "box", None)
            page_box = _box(page_box_obj) or [0.0, 0.0, 0.0, 0.0]
            page_record: dict[str, Any] = {
                "page_index": page_index,
                "page_number": page_number,
                "unit": getattr(page, "unit", None) or "pt",
                "coordinate_space": "pdf_points_bottom_left",
                "bbox": page_box,
                "layouts": [
                    {
                        "id": getattr(layout, "id", None),
                        "class_name": getattr(layout, "class_name", None),
                        "conf": _number(getattr(layout, "conf", None)),
                        "bbox": _box(getattr(layout, "box", None))
                        or [0.0, 0.0, 0.0, 0.0],
                    }
                    for layout in getattr(page, "page_layout", []) or []
                ],
                "paragraphs": [],
            }
            for sequence, paragraph in enumerate(
                getattr(page, "pdf_paragraph", []) or []
            ):
                fonts = _font_map(page, getattr(paragraph, "xobj_id", None))
                source_text = str(getattr(paragraph, "unicode", None) or "")
                if not source_text:
                    source_text = _visible_text(paragraph)
                paragraph_record: dict[str, Any] = {
                    "id": f"p{page_number:04d}-b{sequence + 1:04d}",
                    "page_index": page_index,
                    "page_number": page_number,
                    "sequence": sequence,
                    "order_kind": "babel_paragraph_sequence",
                    "render_order": getattr(paragraph, "render_order", None),
                    "bbox": _fallback_paragraph_box(paragraph),
                    "layout": {
                        "id": getattr(paragraph, "layout_id", None),
                        "label": getattr(paragraph, "layout_label", None),
                    },
                    "style": _style(getattr(paragraph, "pdf_style", None), fonts),
                    "source_text": source_text,
                    "target_text": "",
                    "source_runs": _runs(paragraph, fonts),
                    "target_runs": [],
                }
                page_record["paragraphs"].append(paragraph_record)
                self._paragraphs_by_object_id[id(paragraph)] = paragraph_record
                part_object_ids.add(id(paragraph))
            self._pages_by_index[page_index] = page_record
        self._object_ids_by_offset[page_offset] = part_object_ids

    def capture_target(self, document: Any, *, page_offset: int = 0) -> None:
        if page_offset in self._target_offsets:
            raise CompactIRCaptureError(
                f"compact IR target part {page_offset} 重复捕获"
            )
        if page_offset not in self._source_offsets:
            raise CompactIRCaptureError(
                f"BabelDOC target hook 先于 source hook 触发：part {page_offset}"
            )
        self._target_offsets.add(page_offset)

        captured: set[int] = set()
        for page in getattr(document, "page", []) or []:
            for paragraph in getattr(page, "pdf_paragraph", []) or []:
                record = self._paragraphs_by_object_id.get(id(paragraph))
                if record is None:
                    continue
                fonts = _font_map(page, getattr(paragraph, "xobj_id", None))
                record["target_text"] = _visible_text(paragraph)
                record["target_runs"] = _runs(paragraph, fonts)
                captured.add(id(paragraph))

        missing = self._object_ids_by_offset.get(page_offset, set()) - captured
        if missing:
            raise CompactIRCaptureError(
                f"BabelDOC 翻译后缺少 {len(missing)} 个源段落，拒绝生成不完整 compact IR"
            )

    def to_dict(self, *, page_numbers: set[int] | None = None) -> dict[str, Any]:
        if not self._source_offsets or self._source_offsets != self._target_offsets:
            raise CompactIRCaptureError(
                "BabelDOC observer 未完整捕获 source/target，拒绝静默生成 IR"
            )
        pages = sorted(
            self._pages_by_index.values(), key=lambda page: page["page_index"]
        )
        if page_numbers is not None:
            pages = [page for page in pages if int(page["page_number"]) in page_numbers]
            if {int(page["page_number"]) for page in pages} != page_numbers:
                raise CompactIRCaptureError("请求发布的页面尚未完整捕获")
        return {
            "schema": "read-paper.babeldoc.compact-ir",
            "schema_version": 1,
            "producer": copy.deepcopy(self.engine),
            "source": {
                "sha256": self.source_sha256,
                "filename": self.source_filename,
                "language": self.source_language,
            },
            "target_language": self.target_language,
            "pages": pages,
        }

    def write(self, path: Path, *, page_numbers: set[int] | None = None) -> Path:
        payload = self.to_dict(page_numbers=page_numbers)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".writing")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path


_OBSERVER_LOCK = threading.Lock()


@contextmanager
def observe_babeldoc(config: Any, collector: CompactIRCollector) -> Iterator[None]:
    from babeldoc.format.pdf import high_level
    from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
    from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import (
        ILTranslatorLLMOnly,
    )
    from babeldoc.format.pdf.document_il.midend.styles_and_formulas import (
        StylesAndFormulas,
    )

    with _OBSERVER_LOCK:
        current_part = threading.local()
        original_do_translate_single = high_level._do_translate_single
        original_styles_process = StylesAndFormulas.process
        original_il_translate = ILTranslator.translate
        original_llm_translate = ILTranslatorLLMOnly.translate

        def do_translate_single(progress_monitor: Any, part_config: Any):
            parent = getattr(progress_monitor, "parent_monitor", None)
            page_offset = (
                int(getattr(progress_monitor, "part_index", 0) or 0) if parent else 0
            )
            current_part.page_offset = page_offset
            try:
                return original_do_translate_single(progress_monitor, part_config)
            finally:
                current_part.page_offset = 0

        def styles_process(instance: Any, document: Any):
            result = original_styles_process(instance, document)
            collector.capture_source(
                document, page_offset=int(getattr(current_part, "page_offset", 0))
            )
            return result

        def il_translate(instance: Any, document: Any):
            result = original_il_translate(instance, document)
            collector.capture_target(
                document, page_offset=int(getattr(current_part, "page_offset", 0))
            )
            return result

        def llm_translate(instance: Any, document: Any):
            result = original_llm_translate(instance, document)
            collector.capture_target(
                document, page_offset=int(getattr(current_part, "page_offset", 0))
            )
            return result

        high_level._do_translate_single = do_translate_single
        StylesAndFormulas.process = styles_process
        ILTranslator.translate = il_translate
        ILTranslatorLLMOnly.translate = llm_translate
        try:
            yield
        finally:
            high_level._do_translate_single = original_do_translate_single
            StylesAndFormulas.process = original_styles_process
            ILTranslator.translate = original_il_translate
            ILTranslatorLLMOnly.translate = original_llm_translate
