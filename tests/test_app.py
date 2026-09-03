from __future__ import annotations

from pathlib import Path

import pymupdf
from fastapi.testclient import TestClient

import app


def test_info_exposes_pinned_engine_without_secrets() -> None:
    response = TestClient(app.app).get("/api/info")
    assert response.status_code == 200
    body = response.json()
    assert body["babeldoc_revision"] == app.BABELDOC_REVISION
    assert "api_key" not in body
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


def test_pdf_preview_is_inline_and_download_is_attachment(tmp_path: Path, monkeypatch) -> None:
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
