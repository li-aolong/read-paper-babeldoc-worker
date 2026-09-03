from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.getenv("BABELDOC_LAB_DATA", ROOT / "data")).resolve()
STATIC_ROOT = ROOT / "static"
MAX_UPLOAD_BYTES = int(os.getenv("BABELDOC_MAX_UPLOAD_MB", "100")) * 1024 * 1024
BABELDOC_REVISION = "38d3896dcde9b5a940c62cf5563cadea673a64d3"
TABLE_NOTICE = (
    "BabelDOC v0.6.4 能翻译文本型 PDF 表格；已退役的是额外的 RapidOCR 表格检测器，"
    "因此扫描件或图片表格不保证可翻译。"
)

DATA_ROOT.mkdir(parents=True, exist_ok=True)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="babeldoc-lab")
_jobs_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}

app = FastAPI(title="BabelDOC 本地效果实验", docs_url=None, redoc_url=None)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_name(name: str | None) -> str:
    cleaned = Path(name or "document.pdf").name.replace("\x00", "").strip()
    return cleaned if cleaned.lower().endswith(".pdf") else f"{cleaned or 'document'}.pdf"


def _job_dir(job_id: str) -> Path:
    return DATA_ROOT / job_id


def _status_path(job_id: str) -> Path:
    return _job_dir(job_id) / "status.json"


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if not key.startswith("_")}


def _write_status(job: dict[str, Any]) -> None:
    with _jobs_lock:
        _jobs[job["id"]] = job
        path = _status_path(job["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".writing")
        temporary.write_text(json.dumps(_public_job(job), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


def _patch_job(job_id: str, **patch: Any) -> None:
    with _jobs_lock:
        current = dict(_jobs[job_id])
    current.update(patch, updated_at=_now())
    _write_status(current)


def _load_existing_jobs() -> None:
    for path in DATA_ROOT.glob("*/status.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("status") in {"queued", "running"}:
            job.update(status="failed", error="本地服务重启，任务已中断，请重新提交。", updated_at=_now())
        inputs = sorted(path.parent.glob("*.pdf"))
        if inputs:
            job["_input_path"] = str(inputs[0])
        _jobs[str(job["id"])] = job


def _result_files(result: Any, job_id: str) -> dict[str, str]:
    candidates = {
        "mono": getattr(result, "no_watermark_mono_pdf_path", None) or getattr(result, "mono_pdf_path", None),
        "dual": getattr(result, "no_watermark_dual_pdf_path", None) or getattr(result, "dual_pdf_path", None),
    }
    files: dict[str, str] = {"original": f"/api/jobs/{job_id}/files/original"}
    for kind, path in candidates.items():
        if path and Path(path).is_file():
            files[kind] = f"/api/jobs/{job_id}/files/{kind}"
    return files


async def _translate(job_id: str) -> None:
    from babeldoc.format.pdf import high_level
    from babeldoc.format.pdf.translation_config import TranslationConfig, WatermarkOutputMode
    from babeldoc.translator.translator import OpenAITranslator, set_translate_rate_limiter
    from babeldoc.docvision.doclayout import DocLayoutModel

    api_key = os.getenv("BABELDOC_API_KEY", "").strip()
    base_url = os.getenv("BABELDOC_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").strip()
    model = os.getenv("BABELDOC_MODEL", "glm-4-flash-250414").strip()
    if not api_key:
        raise RuntimeError("缺少 BABELDOC_API_KEY；请通过 run.sh 继承 GLM_API_KEY 或显式设置。")

    with _jobs_lock:
        job = dict(_jobs[job_id])
    output_dir = _job_dir(job_id) / "output"
    working_dir = _job_dir(job_id) / "working"
    output_dir.mkdir(parents=True, exist_ok=True)
    working_dir.mkdir(parents=True, exist_ok=True)

    translator = OpenAITranslator(
        lang_in="en",
        lang_out="zh",
        model=model,
        base_url=base_url,
        api_key=api_key,
        ignore_cache=False,
        enable_json_mode_if_requested=False,
        send_temperature=True,
    )
    set_translate_rate_limiter(int(job["qps"]))
    layout_model = DocLayoutModel.load_onnx()
    config = TranslationConfig(
        input_file=job["_input_path"],
        output_dir=output_dir,
        working_dir=working_dir,
        pages=job.get("pages") or None,
        translator=translator,
        lang_in="en",
        lang_out="zh",
        doc_layout_model=layout_model,
        no_dual=False,
        no_mono=False,
        qps=int(job["qps"]),
        pool_max_workers=int(job["qps"]),
        skip_scanned_detection=bool(job["skip_scanned_detection"]),
        auto_extract_glossary=bool(job["auto_extract_glossary"]),
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        primary_font_family="serif",
        report_interval=0.25,
        metadata_extra_data="read-paper-babeldoc-lab",
    )
    high_level.init()
    async for event in high_level.async_translate(config):
        event_type = event.get("type")
        if event_type in {"progress_start", "progress_update", "progress_end"}:
            progress = float(event.get("overall_progress") or event.get("stage_progress") or 0)
            _patch_job(
                job_id,
                status="running",
                progress=max(0.0, min(100.0, progress)),
                stage=str(event.get("stage") or "处理中"),
            )
        elif event_type == "error":
            raise RuntimeError(str(event.get("message_for_user") or event.get("error") or "BabelDOC 处理失败"))
        elif event_type == "finish":
            result = event.get("translate_result")
            if result is None:
                raise RuntimeError("BabelDOC 完成事件缺少输出结果")
            _patch_job(
                job_id,
                status="completed",
                progress=100.0,
                stage="完成",
                files=_result_files(result, job_id),
                metrics={
                    "seconds": round(float(getattr(result, "total_seconds", 0) or 0), 2),
                    "peak_memory_mb": round(float(getattr(result, "peak_memory_usage", 0) or 0), 2),
                    "characters": int(getattr(result, "total_valid_character_count", 0) or 0),
                },
            )
            return
    raise RuntimeError("BabelDOC 未返回完成事件")


def _run_job(job_id: str) -> None:
    _patch_job(job_id, status="running", stage="初始化 BabelDOC", progress=0.0, error=None)
    try:
        asyncio.run(_translate(job_id))
    except Exception as exc:  # noqa: BLE001
        logging.exception("BabelDOC job %s failed", job_id)
        _patch_job(job_id, status="failed", stage="失败", error=str(exc)[:2000])


def _create_job(raw: bytes, filename: str, pages: str, qps: int, skip_scanned_detection: bool, auto_extract_glossary: bool) -> dict[str, Any]:
    if not raw.startswith(b"%PDF-"):
        raise HTTPException(400, "文件内容不是 PDF")
    digest = hashlib.sha256(raw).hexdigest()
    job_id = f"{digest[:10]}-{uuid.uuid4().hex[:6]}"
    directory = _job_dir(job_id)
    directory.mkdir(parents=True, exist_ok=False)
    input_path = directory / _safe_name(filename)
    input_path.write_bytes(raw)
    job = {
        "id": job_id,
        "filename": input_path.name,
        "sha256": digest,
        "status": "queued",
        "stage": "排队中",
        "progress": 0.0,
        "error": None,
        "files": {"original": f"/api/jobs/{job_id}/files/original"},
        "pages": pages.strip(),
        "qps": max(1, min(4, int(qps))),
        "skip_scanned_detection": bool(skip_scanned_detection),
        "auto_extract_glossary": bool(auto_extract_glossary),
        "babeldoc_revision": BABELDOC_REVISION,
        "model": os.getenv("BABELDOC_MODEL", "glm-4-flash-250414"),
        "table_notice": TABLE_NOTICE,
        "created_at": _now(),
        "updated_at": _now(),
        "_input_path": str(input_path),
    }
    _write_status(job)
    _executor.submit(_run_job, job_id)
    return _public_job(job)


async def _read_upload(file: UploadFile) -> bytes:
    data = bytearray()
    while chunk := await file.read(1024 * 1024):
        data.extend(chunk)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"PDF 不能超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
    return bytes(data)


_load_existing_jobs()


@app.get("/api/info")
def info() -> dict[str, Any]:
    return {
        "babeldoc_revision": BABELDOC_REVISION,
        "model": os.getenv("BABELDOC_MODEL", "glm-4-flash-250414"),
        "api_configured": bool(os.getenv("BABELDOC_API_KEY") or os.getenv("GLM_API_KEY")),
        "sample_available": Path(os.getenv("BABELDOC_SAMPLE_PDF", "")).is_file(),
        "table_notice": TABLE_NOTICE,
    }


@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    with _jobs_lock:
        values = [_public_job(job) for job in _jobs.values()]
    return sorted(values, key=lambda job: job["created_at"], reverse=True)[:20]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return _public_job(job)


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    pages: str = Form(""),
    qps: int = Form(2),
    skip_scanned_detection: bool = Form(True),
    auto_extract_glossary: bool = Form(False),
) -> dict[str, Any]:
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "只支持 PDF 文件")
    raw = await _read_upload(file)
    return _create_job(raw, file.filename or "document.pdf", pages, qps, skip_scanned_detection, auto_extract_glossary)


@app.post("/api/sample")
def create_sample_job(
    pages: str = Form("1,6"),
    qps: int = Form(2),
    auto_extract_glossary: bool = Form(False),
) -> dict[str, Any]:
    sample = Path(os.getenv("BABELDOC_SAMPLE_PDF", ""))
    if not sample.is_file():
        raise HTTPException(404, "内置样本不存在")
    return _create_job(sample.read_bytes(), sample.name, pages, qps, True, auto_extract_glossary)


@app.get("/api/jobs/{job_id}/files/{kind}")
def get_file(job_id: str, kind: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or kind not in {"original", "mono", "dual"}:
        raise HTTPException(404, "文件不存在")
    if kind == "original":
        input_path = job.get("_input_path")
        if not input_path:
            raise HTTPException(404, "原 PDF 路径缺失")
        path = Path(input_path)
    else:
        output = _job_dir(job_id) / "output"
        marker = "mono" if kind == "mono" else "dual"
        candidates = sorted(output.glob(f"*{marker}*.pdf"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not candidates:
            raise HTTPException(404, "产物尚未生成")
        path = candidates[0]
    if not path.is_file() or _job_dir(job_id) not in path.resolve().parents:
        raise HTTPException(404, "文件不存在")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


app.mount("/", StaticFiles(directory=STATIC_ROOT, html=True), name="static")
