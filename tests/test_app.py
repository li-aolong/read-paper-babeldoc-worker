from __future__ import annotations

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
