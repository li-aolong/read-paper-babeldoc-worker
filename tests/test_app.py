from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app
from compact_ir import CompactIRCaptureError, CompactIRCollector, observe_babeldoc


@pytest.fixture(autouse=True)
def disable_worker_auth(monkeypatch) -> None:
    monkeypatch.setattr(app, "WORKER_TOKEN", "")


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple[object, ...]]] = []
        self.lock = threading.Lock()

    def submit(self, function, *args):
        with self.lock:
            self.calls.append((function, args))


def pdf_bytes(label: str = "test", pages: int = 1) -> bytes:
    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page()
        page.insert_text((72, 72), f"{label} {index + 1}")
    data = document.tobytes()
    document.close()
    return data


def test_info_exposes_pinned_engine_without_secrets() -> None:
    response = TestClient(app.app).get("/api/info")
    assert response.status_code == 200
    body = response.json()
    assert body["babeldoc_version"] == app.BABELDOC_VERSION
    assert body["babeldoc_revision"] == app.BABELDOC_REVISION
    assert body["lang_in"] == "en"
    assert body["lang_out"] == "zh"
    assert body["model"] == app.ENGINE["model"]
    assert body["engine"]["version"] == app.BABELDOC_VERSION
    assert body["engine"]["revision"] == app.BABELDOC_REVISION
    assert body["engine_valid"] is True
    assert body["page_selection_supported"] is True
    assert "api_key" not in body
    assert "token" not in json.dumps(body).lower()
    assert "文本型 PDF 表格" in body["table_notice"]
    assert "图片表格不保证" in body["table_notice"]


def test_upload_rejects_non_pdf_content() -> None:
    response = TestClient(app.app).post(
        "/api/jobs",
        files={"file": ("fake.pdf", b"not a pdf", "application/pdf")},
    )
    assert response.status_code == 400
    assert "不是 PDF" in response.json()["detail"]


def test_home_page_is_available() -> None:
    response = TestClient(app.app).get("/")
    assert response.status_code == 200
    assert "BabelDOC 本地效果实验" in response.text
    assert "译文 PDF" in response.text
    assert "纯译文 PDF" not in response.text
    assert "适应宽度" in response.text


def test_google_translator_uses_bounded_reasoning():
    translator = app._make_translator(
        {"id": "reasoning-test", "lang_in": "en", "lang_out": "zh", "model": "gemini-3.5-flash", "provider_fingerprint": "test"},
        "https://generativelanguage.googleapis.com/v1beta/openai", "test-key",
    )
    try:
        assert translator.extra_body["reasoning_effort"] == "low"
    finally:
        translator.client.close()


def test_explicit_regeneration_bypasses_translation_cache():
    translator=app._make_translator({"id":"fresh","lang_in":"en","lang_out":"zh","model":"test","provider_fingerprint":"test","force_retranslate":True},"https://api.example/v1","test-key")
    try:
        assert translator.ignore_cache is True
    finally:
        translator.client.close()


def test_pdf_preview_is_inline_and_download_is_attachment(
    tmp_path: Path, monkeypatch
) -> None:
    job_id = "preview-test"
    job_dir = tmp_path / job_id
    output_dir = job_dir / "output"
    output_dir.mkdir(parents=True)
    original = job_dir / "paper.pdf"
    mono = output_dir / "paper.no_watermark.zh.mono.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "preview")
    pdf = document.tobytes()
    document.close()
    original.write_bytes(pdf)
    mono.write_bytes(pdf)
    monkeypatch.setattr(app, "DATA_ROOT", tmp_path)
    monkeypatch.setitem(app._jobs, job_id, {"id": job_id, "_input_path": str(original)})

    client = TestClient(app.app)
    preview = client.get(f"/api/jobs/{job_id}/files/mono")
    assert preview.status_code == 200
    assert preview.headers["content-disposition"].startswith("inline;")

    download = client.get(f"/api/jobs/{job_id}/files/mono?download=1")
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment;")

    meta = client.get(f"/api/jobs/{job_id}/preview/mono")
    assert meta.json() == {"pages": 1}
    preview_image = client.get(f"/api/jobs/{job_id}/preview/mono/1.png")
    assert preview_image.status_code == 200
    assert preview_image.headers["content-type"] == "image/png"
    assert preview_image.content.startswith(b"\x89PNG")


def test_worker_token_protects_jobs_and_artifacts(tmp_path: Path, monkeypatch) -> None:
    job_id = "protected-test"
    output_dir = tmp_path / job_id / "output"
    output_dir.mkdir(parents=True)
    ir_path = output_dir / "compact-ir.v1.json"
    ir_path.write_text('{"schema_version":1}', encoding="utf-8")
    monkeypatch.setattr(app, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(app, "WORKER_TOKEN", "worker-secret")
    monkeypatch.setitem(
        app._jobs, job_id, {"id": job_id, "created_at": "2026-01-01T00:00:00Z"}
    )
    client = TestClient(app.app)

    assert client.get("/api/info").status_code == 200
    assert client.get("/").status_code == 200
    unauthorized = client.get("/api/jobs")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert (
        client.get("/api/jobs", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )

    headers = {"Authorization": "Bearer worker-secret"}
    assert client.get("/api/jobs", headers=headers).status_code == 200
    ir_response = client.get(f"/api/jobs/{job_id}/files/ir?download=1", headers=headers)
    assert ir_response.status_code == 200
    assert ir_response.headers["content-type"].startswith("application/json")
    assert ir_response.headers["content-disposition"].startswith("attachment;")
    assert ir_response.json() == {"schema_version": 1}


def test_status_and_manifest_record_engine_without_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(app, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(app, "_jobs", {})
    monkeypatch.setattr(
        app, "_executor", SimpleNamespace(submit=lambda *_args, **_kwargs: None)
    )
    monkeypatch.setenv("BABELDOC_API_KEY", "api-secret")
    monkeypatch.setattr(app, "WORKER_TOKEN", "worker-secret")

    job = app._create_job(pdf_bytes(), "paper.pdf", "", 2, True, False)
    status_text = (tmp_path / job["id"] / "status.json").read_text(encoding="utf-8")
    status = json.loads(status_text)
    assert status["engine"] == app.ENGINE
    assert status["babeldoc_version"] == app.BABELDOC_VERSION
    assert status["babeldoc_revision"] == app.BABELDOC_REVISION
    assert status["lang_in"] == "en"
    assert status["lang_out"] == "zh"
    assert status["engine"]["version"] == app.BABELDOC_VERSION
    assert status["engine"]["model"] == app.ENGINE["model"]

    manifest_path = app._write_manifest(status, {"ir": "/example/ir"})
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["engine"] == app.ENGINE
    for field in (
        "babeldoc_version",
        "babeldoc_revision",
        "lang_in",
        "lang_out",
        "model",
    ):
        assert manifest[field] == status[field]
    for text in (status_text, manifest_text):
        assert "api-secret" not in text
        assert "worker-secret" not in text


def test_runtime_revision_mismatch_is_reported(monkeypatch) -> None:
    class FakeDistribution:
        version = app.BABELDOC_VERSION

        @staticmethod
        def read_text(_name: str) -> str:
            return json.dumps(
                {
                    "vcs_info": {
                        "commit_id": "wrong-revision",
                        "requested_revision": app.BABELDOC_REVISION,
                    }
                }
            )

    monkeypatch.setattr(
        app.importlib.metadata, "distribution", lambda _name: FakeDistribution()
    )
    engine, error = app._inspect_babeldoc_runtime()
    assert engine["revision"] == "wrong-revision"
    assert error is not None and "revision 不匹配" in error


def test_idempotent_concurrent_submit_creates_one_job(
    tmp_path: Path, monkeypatch
) -> None:
    executor = RecordingExecutor()
    monkeypatch.setattr(app, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(app, "_jobs", {})
    monkeypatch.setattr(app, "_idempotency_jobs", {})
    monkeypatch.setattr(app, "_executor", executor)
    key = "a" * 32
    raw = pdf_bytes("concurrent", pages=6)

    def submit(qps: int) -> dict:
        return app._create_job(
            raw,
            "paper.pdf",
            "1, 6",
            qps,
            True,
            False,
            key,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(submit, range(1, 9)))

    assert len({job["id"] for job in jobs}) == 1
    assert len(executor.calls) == 1
    assert len(list(tmp_path.glob("*/status.json"))) == 1
    assert jobs[0]["idempotency_key"] == key
    assert jobs[0]["pages"] == "1,6"


def test_idempotency_index_recovers_after_restart(tmp_path: Path, monkeypatch) -> None:
    executor = RecordingExecutor()
    monkeypatch.setattr(app, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(app, "_jobs", {})
    monkeypatch.setattr(app, "_idempotency_jobs", {})
    monkeypatch.setattr(app, "_executor", executor)
    key = "b" * 64
    raw = pdf_bytes("restart", pages=2)
    first = app._create_job(raw, "paper.pdf", "2", 2, True, True, key)
    app._patch_job(first["id"], status="completed", stage="完成")

    monkeypatch.setattr(app, "_jobs", {})
    monkeypatch.setattr(app, "_idempotency_jobs", {})
    app._load_existing_jobs()
    restored = app._create_job(raw, "paper.pdf", "2", 4, True, True, key)

    assert restored["id"] == first["id"]
    assert restored["status"] == "completed"
    assert len(executor.calls) == 1
    assert app._idempotency_jobs[key] == first["id"]


def test_idempotency_conflict_and_failed_retry(tmp_path: Path, monkeypatch) -> None:
    executor = RecordingExecutor()
    monkeypatch.setattr(app, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(app, "_jobs", {})
    monkeypatch.setattr(app, "_idempotency_jobs", {})
    monkeypatch.setattr(app, "_executor", executor)
    key = "c" * 32
    first_pdf = pdf_bytes("first", pages=2)
    second_pdf = pdf_bytes("second")
    first = app._create_job(first_pdf, "paper.pdf", "1", 2, True, False, key)

    with pytest.raises(HTTPException) as source_conflict:
        app._create_job(second_pdf, "paper.pdf", "1", 2, True, False, key)
    assert source_conflict.value.status_code == 409
    with pytest.raises(HTTPException) as config_conflict:
        app._create_job(first_pdf, "paper.pdf", "2", 2, True, False, key)
    assert config_conflict.value.status_code == 409

    app._patch_job(first["id"], status="failed", stage="失败")
    retry = app._create_job(first_pdf, "paper.pdf", "1", 4, True, False, key)
    assert retry["id"] != first["id"]
    assert app._idempotency_jobs[key] == retry["id"]
    assert len(executor.calls) == 2


@pytest.mark.parametrize(
    "key",
    [
        "A" * 32,
        "g" * 32,
        "a" * 31,
        "a" * 33,
        "a" * 63,
        "a" * 65,
        " " + "a" * 32,
        "a" * 32 + " ",
        " ",
    ],
)
def test_idempotency_key_validation(key: str) -> None:
    with pytest.raises(HTTPException) as invalid:
        app._create_job(b"%PDF-1.7\n", "paper.pdf", "", 2, True, False, key)
    assert invalid.value.status_code == 422


def _sample_il():
    from babeldoc.format.pdf.document_il import (
        Box,
        Cropbox,
        Document,
        GraphicState,
        Mediabox,
        Page,
        PageLayout,
        PdfCharacter,
        PdfFont,
        PdfParagraph,
        PdfParagraphComposition,
        PdfSameStyleCharacters,
        PdfSameStyleUnicodeCharacters,
        PdfStyle,
        VisualBbox,
    )

    box = Box(x=10, y=20, x2=110, y2=40)
    style = PdfStyle(
        graphic_state=GraphicState(passthrough_per_char_instruction=""),
        font_id="F1",
        font_size=10,
    )
    char = PdfCharacter(
        pdf_style=style,
        box=box,
        visual_bbox=VisualBbox(box=box),
        char_unicode="Hello",
    )
    paragraph = PdfParagraph(
        box=box,
        pdf_style=style,
        pdf_paragraph_composition=[
            PdfParagraphComposition(
                pdf_same_style_characters=PdfSameStyleCharacters(
                    box=box, pdf_style=style, pdf_character=[char]
                )
            )
        ],
        unicode="Hello",
        debug_id="source",
        layout_id=4,
        layout_label="text",
        render_order=12,
    )
    page_box = Box(x=0, y=0, x2=612, y2=792)
    page = Page(
        mediabox=Mediabox(box=page_box),
        cropbox=Cropbox(box=page_box),
        pdf_font=[
            PdfFont(
                name="Times-Bold",
                font_id="F1",
                xref_id=1,
                encoding_length=1,
                bold=True,
                italic=False,
                monospace=False,
                serif=True,
            )
        ],
        page_layout=[
            PageLayout(
                box=Box(x=8, y=18, x2=112, y2=42),
                id=4,
                conf=0.87549,
                class_name="table_cell",
            )
        ],
        pdf_paragraph=[paragraph],
        page_number=0,
        unit="pt",
    )
    return (
        Document(page=[page], total_pages=1),
        paragraph,
        style,
        PdfParagraphComposition,
        PdfSameStyleUnicodeCharacters,
    )


def test_compact_ir_observer_captures_and_restores_methods(
    tmp_path: Path, monkeypatch
) -> None:
    from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
    from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import (
        ILTranslatorLLMOnly,
    )
    from babeldoc.format.pdf.document_il.midend.styles_and_formulas import (
        StylesAndFormulas,
    )

    document, paragraph, style, composition_type, unicode_type = _sample_il()
    collector = CompactIRCollector(
        source_sha256="abc123",
        source_filename="paper.pdf",
        source_language="en",
        target_language="zh",
        engine={
            "name": "BabelDOC",
            "version": app.BABELDOC_VERSION,
            "revision": app.BABELDOC_REVISION,
            "model": "test-model",
        },
    )

    def source_process(_instance, _document):
        return None

    def target_translate(_instance, _document):
        paragraph.unicode = "你好"
        paragraph.pdf_paragraph_composition = [
            composition_type(
                pdf_same_style_unicode_characters=unicode_type(
                    pdf_style=style, unicode="你好"
                )
            )
        ]

    monkeypatch.setattr(StylesAndFormulas, "process", source_process)
    monkeypatch.setattr(ILTranslator, "translate", target_translate)
    monkeypatch.setattr(ILTranslatorLLMOnly, "translate", target_translate)

    with observe_babeldoc(SimpleNamespace(split_strategy=None), collector):
        StylesAndFormulas.process(None, document)
        ILTranslatorLLMOnly.translate(None, document)

    assert StylesAndFormulas.process is source_process
    assert ILTranslator.translate is target_translate
    assert ILTranslatorLLMOnly.translate is target_translate

    output = collector.write(tmp_path / "compact-ir.v1.json")
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["producer"]["model"] == "test-model"
    assert payload["source"] == {
        "sha256": "abc123",
        "filename": "paper.pdf",
        "language": "en",
    }
    page_payload = payload["pages"][0]
    assert page_payload["layouts"] == [
        {
            "id": 4,
            "class_name": "table_cell",
            "conf": 0.875,
            "bbox": [8.0, 18.0, 112.0, 42.0],
        }
    ]
    assert len(page_payload["paragraphs"]) == len(document.page[0].pdf_paragraph)
    paragraph_payload = page_payload["paragraphs"][0]
    assert paragraph_payload["id"] == "p0001-b0001"
    assert paragraph_payload["page_number"] == 1
    assert paragraph_payload["bbox"] == [10.0, 20.0, 110.0, 40.0]
    assert paragraph_payload["layout"] == {"id": 4, "label": "text"}
    assert paragraph_payload["source_text"] == "Hello"
    assert paragraph_payload["target_text"] == "你好"
    assert paragraph_payload["style"]["bold"] is True


def test_compact_ir_collector_merges_split_parts_and_writes_one_page(
    tmp_path: Path,
) -> None:
    collector = CompactIRCollector(
        source_sha256="hash",
        source_filename="paper.pdf",
        source_language="en",
        target_language="zh",
        engine={},
    )
    first, *_ = _sample_il()
    second, *_ = _sample_il()
    collector.capture_source(first, page_offset=0)
    collector.capture_target(first, page_offset=0)
    collector.capture_source(second, page_offset=1)
    # Save PDF 事件与下一页解析交错时，已经完成的第一页仍可立即发布。
    ready = collector.to_dict(page_numbers={1})
    assert [page["page_number"] for page in ready["pages"]] == [1]
    with pytest.raises(CompactIRCaptureError, match="尚未完整捕获"):
        collector.to_dict(page_numbers={2})
    with pytest.raises(CompactIRCaptureError, match="未完整捕获"):
        collector.to_dict()
    ready["pages"][0]["paragraphs"].clear()
    collector.capture_target(second, page_offset=1)

    full = collector.to_dict()
    assert [page["page_number"] for page in full["pages"]] == [1, 2]
    assert full["pages"][0]["paragraphs"][0]["id"] == "p0001-b0001"
    assert full["pages"][1]["paragraphs"][0]["id"] == "p0002-b0001"
    partial_path = collector.write(tmp_path / "page-2.json", page_numbers={2})
    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    assert [page["page_number"] for page in partial["pages"]] == [2]


def test_publish_page_preserves_vector_text_and_updates_status(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(app, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(
        app,
        "_jobs",
        {
            "job-1": {
                "id": "job-1",
                "status": "running",
                "available_pages": [],
                "total_pages": 2,
            }
        },
    )
    source = tmp_path / "source.pdf"
    source.write_bytes(pdf_bytes("incremental", pages=2))
    document, *_ = _sample_il()
    collector = CompactIRCollector(
        source_sha256="hash",
        source_filename="paper.pdf",
        source_language="en",
        target_language="zh",
        engine={
            "name": "BabelDOC",
            "version": "test",
            "revision": "test",
            "model": "test",
        },
    )
    collector.capture_source(document)
    collector.capture_target(document)

    app._publish_page("job-1", 1, source, collector, source_page_index=1)
    output = tmp_path / "job-1" / "pages" / "0001" / "mono.pdf"
    with pymupdf.open(output) as preview:
        assert preview.page_count == 1
        assert "incremental 2" in preview[0].get_text()
        assert preview[0].get_fonts()
        assert preview[0].get_images() == []
    assert output.stat().st_size < 1_000_000
    assert app._jobs["job-1"]["available_pages"] == [1]
    assert app._jobs["job-1"]["partial_revision"] == 1
    response = TestClient(app.app).get("/api/jobs/job-1/pages/1/mono.pdf")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")


def test_compact_ir_refuses_incomplete_observation() -> None:
    document, *_ = _sample_il()
    collector = CompactIRCollector(
        source_sha256="hash",
        source_filename="paper.pdf",
        source_language="en",
        target_language="zh",
        engine={},
    )
    collector.capture_source(document)
    with pytest.raises(CompactIRCaptureError, match="未完整捕获"):
        collector.to_dict()
