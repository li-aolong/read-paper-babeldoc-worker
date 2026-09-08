from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pymupdf
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app
from compact_ir import CompactIRCollector, observe_babeldoc
from test_app import RecordingExecutor, _sample_il, pdf_bytes


@pytest.mark.parametrize("pages", ["0", "5", "2-5", "3-2", "1,", "-2", "1-", "x", "1,,2"])
def test_invalid_selected_pages_rejected_before_enqueue(tmp_path, monkeypatch, pages):
    executor = RecordingExecutor()
    monkeypatch.setattr(app, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(app, "_executor", executor)
    with pytest.raises(HTTPException) as error:
        app._create_job(pdf_bytes(pages=4), "paper.pdf", pages, 1, True, False)
    assert error.value.status_code == 422
    assert executor.calls == []
    assert list(tmp_path.iterdir()) == []


def test_canonical_pages_idempotency(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(app, "_executor", RecordingExecutor())
    monkeypatch.setattr(app, "_jobs", {})
    monkeypatch.setattr(app, "_idempotency_jobs", {})
    raw = pdf_bytes(pages=4)
    first = app._create_job(raw, "paper.pdf", "4, 2-3,2", 1, True, False, "a" * 32)
    second = app._create_job(raw, "paper.pdf", "2-4", 1, True, False, "a" * 32)
    assert first["id"] == second["id"]
    assert first["pages"] == "2-4"
    assert first["requested_pages"] == [2, 3, 4]
    assert first["translated_pages"] == []


@pytest.mark.parametrize("count,pages,requested", [(4, "2,4", [2, 4]), (4, "2", [2]), (4, "", [1, 2, 3, 4]), (1, "1", [1])])
def test_upstream_split_preserves_all_pages_and_skips_api(tmp_path, monkeypatch, count, pages, requested):
    from babeldoc.format.pdf import high_level
    from babeldoc.format.pdf.split_manager import PageCountStrategy
    from babeldoc.format.pdf.translation_config import TranslationConfig, TranslateResult, WatermarkOutputMode

    source = tmp_path / "paper.pdf"
    source.write_bytes(pdf_bytes("source", pages=count))
    monkeypatch.setattr(app, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(app, "WORKER_TOKEN", "")
    monkeypatch.setattr(app, "_jobs", {"job": {"id": "job", "total_pages": count, "available_pages": [], "translated_pages": [], "status": "running"}})
    config = TranslationConfig(
        translator=Mock(), input_file=source, lang_in="en", lang_out="zh",
        doc_layout_model=None, pages=pages or None, output_dir=tmp_path / "output",
        working_dir=tmp_path / "working", split_strategy=PageCountStrategy(1),
        watermark_output_mode=WatermarkOutputMode.NoWatermark, skip_clean=True,
        auto_extract_glossary=False, save_auto_extracted_glossary=False,
    )
    collector = CompactIRCollector(source_sha256="hash", source_filename="paper.pdf", source_language="en", target_language="zh", engine={})
    api_calls = []

    def translated_part(monitor, part_config):
        number = monitor.part_index + 1 if monitor.parent_monitor else 1
        api_calls.append(number)
        document, *_ = _sample_il()
        collector.capture_source(document, page_offset=number - 1)
        collector.capture_target(document, page_offset=number - 1)
        output = Path(part_config.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        mono = output / "translated.mono.pdf"
        dual = output / "translated.dual.pdf"
        mono.write_bytes(pdf_bytes(f"translated page {number}"))
        with pymupdf.open(part_config.input_file) as original, pymupdf.open(mono) as target, pymupdf.open() as paired:
            paired.insert_pdf(original)
            paired.insert_pdf(target)
            paired.save(dual)
        return TranslateResult(mono, dual)

    monkeypatch.setattr(high_level, "_do_translate_single", translated_part)
    for name in ("check_metadata", "fix_cmap", "add_metadata", "migrate_toc"):
        monkeypatch.setattr(high_level, name, lambda *args: None)
    monitor = Mock(parent_monitor=None)
    monitor.create_part_monitor.side_effect = lambda index, total: SimpleNamespace(parent_monitor=monitor, part_index=index)
    snapshots = []

    def completed(part_config, number, result):
        app._publish_page("job", number, result.mono_pdf_path, collector, translated=number in requested)
        snapshots.append((app._jobs["job"]["available_pages"], app._jobs["job"]["translated_pages"]))
        client = TestClient(app.app)
        assert client.get(f"/api/jobs/job/pages/{number}/mono.pdf").status_code == 200
        assert client.get(f"/api/jobs/job/pages/{number}/ir.json").json()["pages"][0]["page_number"] == number
        assert client.get(f"/api/jobs/job/pages/{number + 1}/mono.pdf").status_code == 404

    with observe_babeldoc(config, collector, untranslated_part=app._untranslated_part, part_completed=completed):
        result = high_level.do_translate(monitor, config)
    assert api_calls == requested
    assert snapshots == [(list(range(1, number + 1)), [p for p in requested if p <= number]) for number in range(1, count + 1)]
    with pymupdf.open(result.mono_pdf_path) as mono, pymupdf.open(result.dual_pdf_path) as dual:
        assert mono.page_count == count
        assert dual.page_count == 2 * count
        for index in range(count):
            if index + 1 not in requested:
                assert f"source {index + 1}" in mono[index].get_text()
            assert f"source {index + 1}" in dual[2 * index].get_text()
    payload = collector.to_dict()
    assert [page["page_number"] for page in payload["pages"]] == list(range(1, count + 1))
    for page in payload["pages"]:
        if page["page_number"] not in requested:
            assert page["paragraphs"] == []
            assert page["translation_status"] == "skipped"
