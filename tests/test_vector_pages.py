from pathlib import Path
from unittest.mock import Mock

import pymupdf
import pytest

import app


@pytest.mark.parametrize("subset", [False, True])
@pytest.mark.parametrize("rotation", [0, 90])
def test_published_page_is_independent_with_embedded_fonts_and_exact_zoom(
    tmp_path: Path, monkeypatch, subset: bool, rotation: int
):
    monkeypatch.setattr(app, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(app, "_jobs", {"job": {"id": "job", "available_pages": []}})
    source = tmp_path / "source.pdf"
    with pymupdf.open() as document:
        document.new_page(width=320, height=240)
        page = document.new_page(width=320, height=240)
        page.insert_font(fontname="EmbeddedCJK", fontbuffer=pymupdf.Font("cjk").buffer)
        page.insert_text((35, 65), "清晰中文 Vector 123", fontname="EmbeddedCJK", fontsize=11)
        page.draw_line((35, 85), (265, 85), width=0.25, color=(0.2, 0.4, 0.8))
        page.draw_circle((90, 120), 15, width=0.4)
        page.set_cropbox(pymupdf.Rect(12, 8, 308, 232))
        page.set_rotation(rotation)
        if subset:
            document.subset_fonts()
        document.save(source)
    with pymupdf.open(source) as document:
        original = document[1]
        text = original.get_text()
        fonts = [document.extract_font(font[0])[3] for font in original.get_fonts()]
        assert fonts and all(fonts)
        expected = original.get_pixmap(matrix=pymupdf.Matrix(6, 6), alpha=False)
        geometry = (original.mediabox, original.cropbox, original.rotation)
    app._publish_page("job", 1, source, Mock(), source_page_index=1)
    # Simulate upstream's part cleanup: only the independent output is opened.
    source.rename(tmp_path / "source-no-longer-at-original-path.pdf")
    with pymupdf.open(tmp_path / "job/pages/0001/mono.pdf") as document:
        assert document.page_count == 1
        page = document[0]
        assert page.get_text() == text
        assert "清晰中文" in text
        published_fonts = [document.extract_font(font[0])[3] for font in page.get_fonts()]
        assert len(published_fonts) == len(fonts)
        assert all(published_fonts)
        if not subset:
            assert sum(map(len, published_fonts)) < sum(map(len, fonts)) / 5
        assert (tmp_path / "job/pages/0001/mono.pdf").stat().st_size < 100_000
        assert page.get_images() == []
        assert len(page.get_drawings()) == 2
        assert (page.mediabox, page.cropbox, page.rotation) == geometry
        actual = page.get_pixmap(matrix=pymupdf.Matrix(6, 6), alpha=False)
        assert (actual.width, actual.height, actual.samples) == (expected.width, expected.height, expected.samples)


def test_published_immutable_pdf_cannot_be_replaced(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(app, "DATA_ROOT", tmp_path)
    destination = tmp_path / "job/pages/0001/mono.pdf"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"already-published-raster-pdf")
    collector = Mock()
    with pytest.raises(FileExistsError, match="不可覆盖"):
        app._publish_page("job", 1, tmp_path / "source.pdf", collector)
    assert destination.read_bytes() == b"already-published-raster-pdf"
    collector.write.assert_not_called()
