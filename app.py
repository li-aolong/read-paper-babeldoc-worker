from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import logging
import os
import re
import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from compact_ir import CompactIRCollector, observe_babeldoc
from outbound_url import UnsafeOutboundUrl, validate_outbound_url
from translation_guard import TranslationGuard

ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.getenv("BABELDOC_LAB_DATA", ROOT / "data")).resolve()
STATIC_ROOT = ROOT / "static"
MAX_UPLOAD_BYTES = int(os.getenv("BABELDOC_MAX_UPLOAD_MB", "100")) * 1024 * 1024
BABELDOC_REVISION = "38d3896dcde9b5a940c62cf5563cadea673a64d3"
BABELDOC_VERSION = "0.6.4"
WORKER_TOKEN = os.getenv("BABELDOC_WORKER_TOKEN", "").strip()
TABLE_NOTICE = (
    "BabelDOC v0.6.4 能翻译文本型 PDF 表格；已退役的是额外的 RapidOCR 表格检测器，"
    "因此扫描件或图片表格不保证可翻译。"
)

DATA_ROOT.mkdir(parents=True, exist_ok=True)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="babeldoc-lab")
_jobs_lock = threading.RLock()
_preview_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_idempotency_jobs: dict[str, str] = {}
_job_credentials: dict[str, dict[str, str]] = {}
logger = logging.getLogger(__name__)

app = FastAPI(title="BabelDOC 本地效果实验", docs_url=None, redoc_url=None)


def _inspect_babeldoc_runtime() -> tuple[dict[str, str | None], str | None]:
    model = os.getenv("BABELDOC_MODEL", "glm-4-flash-250414").strip()
    try:
        distribution = importlib.metadata.distribution("BabelDOC")
        version = distribution.version
        direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
        vcs_info = direct_url.get("vcs_info") or {}
        revision = vcs_info.get("commit_id")
        requested_revision = vcs_info.get("requested_revision")
    except (
        AttributeError,
        importlib.metadata.PackageNotFoundError,
        json.JSONDecodeError,
        OSError,
        TypeError,
    ) as exc:
        return (
            {
                "name": "BabelDOC",
                "version": None,
                "revision": None,
                "model": model,
            },
            f"无法读取 BabelDOC 安装来源：{exc}",
        )

    engine = {
        "name": "BabelDOC",
        "version": version,
        "revision": revision,
        "model": model,
    }
    if version != BABELDOC_VERSION:
        return engine, f"BabelDOC 版本不匹配：期望 {BABELDOC_VERSION}，实际 {version}"
    if revision != BABELDOC_REVISION or requested_revision != BABELDOC_REVISION:
        return (
            engine,
            (
                "BabelDOC revision 不匹配："
                f"期望 {BABELDOC_REVISION}，实际 commit={revision}, requested={requested_revision}"
            ),
        )
    return engine, None


ENGINE, ENGINE_VALIDATION_ERROR = _inspect_babeldoc_runtime()


def _allowed_models() -> list[str]:
    configured = os.getenv("BABELDOC_MODELS", str(ENGINE["model"] or ""))
    return list(
        dict.fromkeys(value.strip() for value in configured.split(",") if value.strip())
    )


def _default_model() -> str:
    allowed = _allowed_models()
    configured = str(ENGINE["model"] or "")
    return configured if configured in allowed else (allowed[0] if allowed else "")


def _provider_fingerprint(source: str, provider: str, base_url: str) -> str:
    value = json.dumps(
        [source, provider, base_url.strip().rstrip("/")], separators=(",", ":")
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _worker_base_url() -> str:
    return (
        os.getenv("BABELDOC_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
        .strip()
        .rstrip("/")
    )


def _require_worker_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if not WORKER_TOKEN:
        return
    scheme, separator, supplied = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not secrets.compare_digest(supplied, WORKER_TOKEN)
    ):
        raise HTTPException(
            status_code=401,
            detail="需要有效的 worker Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


AUTH_REQUIRED = [Depends(_require_worker_auth)]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_name(name: str | None) -> str:
    cleaned = Path(name or "document.pdf").name.replace("\x00", "").strip()
    return (
        cleaned if cleaned.lower().endswith(".pdf") else f"{cleaned or 'document'}.pdf"
    )


def _job_dir(job_id: str) -> Path:
    return DATA_ROOT / job_id


def _status_path(job_id: str) -> Path:
    return _job_dir(job_id) / "status.json"


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if not key.startswith("_")}


def _normalize_idempotency_key(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{64})", value) is None:
        raise HTTPException(422, "idempotency_key 必须是 32 或 64 位小写十六进制字符串")
    return value


def _normalize_pages(value: str, total_pages: int | None = None) -> str:
    if not value.strip():
        return ""
    selected: set[int] = set()
    for part in value.split(","):
        match = re.fullmatch(r"\s*([0-9]+)\s*(?:-\s*([0-9]+)\s*)?", part)
        if match is None:
            raise HTTPException(422, "页码格式应为 1-3,5")
        start = int(match[1])
        end = int(match[2] or match[1])
        if start < 1 or end < start or (total_pages is not None and end > total_pages):
            raise HTTPException(422, "页码范围无效或超出 PDF 总页数")
        selected.update(range(start, end + 1))
    ranges: list[str] = []
    ordered = sorted(selected)
    start = end = ordered[0]
    for page in ordered[1:]:
        if page == end + 1:
            end = page
        else:
            ranges.append(str(start) if start == end else f"{start}-{end}")
            start = end = page
    ranges.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(ranges)


def _idempotency_signature(job: dict[str, Any]) -> tuple[Any, ...]:
    return (
        job.get("sha256"),
        _normalize_pages(str(job.get("pages") or ""), job.get("total_pages")),
        bool(job.get("skip_scanned_detection")),
        bool(job.get("auto_extract_glossary")),
        job.get("model"),
        job.get("provider_fingerprint"),
    )


def _write_status(job: dict[str, Any]) -> None:
    with _jobs_lock:
        _jobs[job["id"]] = job
        path = _status_path(job["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".writing")
        temporary.write_text(
            json.dumps(_public_job(job), ensure_ascii=False, indent=2), encoding="utf-8"
        )
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
            job.update(
                status="failed",
                error="本地服务重启，任务已中断，请重新提交。",
                updated_at=_now(),
            )
            if job.get("model_source") == "user":
                job["error"] = "服务重启后用户 API 凭据已清除，请显式重新提交任务。"
            path.write_text(
                json.dumps(_public_job(job), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        job.setdefault("engine", dict(ENGINE))
        job.setdefault("babeldoc_version", ENGINE["version"])
        job.setdefault("babeldoc_revision", ENGINE["revision"])
        job.setdefault("lang_in", "en")
        job.setdefault("lang_out", "zh")
        job.setdefault("model", ENGINE["model"])
        inputs = sorted(path.parent.glob("*.pdf"))
        if inputs:
            job["_input_path"] = str(inputs[0])
        job_id = str(job["id"])
        _jobs[job_id] = job
        idempotency_key = job.get("idempotency_key")
        if not isinstance(idempotency_key, str):
            continue
        previous_id = _idempotency_jobs.get(idempotency_key)
        previous = _jobs.get(previous_id) if previous_id else None
        if previous is None or str(job.get("created_at") or "") >= str(
            previous.get("created_at") or ""
        ):
            _idempotency_jobs[idempotency_key] = job_id


def _result_files(result: Any, job_id: str, ir_path: Path) -> dict[str, str]:
    candidates = {
        "mono": getattr(result, "no_watermark_mono_pdf_path", None)
        or getattr(result, "mono_pdf_path", None),
        "dual": getattr(result, "no_watermark_dual_pdf_path", None)
        or getattr(result, "dual_pdf_path", None),
    }
    files: dict[str, str] = {
        "original": f"/api/jobs/{job_id}/files/original",
        "ir": f"/api/jobs/{job_id}/files/ir",
        "manifest": f"/api/jobs/{job_id}/files/manifest",
    }
    if not ir_path.is_file():
        raise RuntimeError("compact IR 文件未生成")
    for kind, path in candidates.items():
        if not path or not Path(path).is_file():
            raise RuntimeError(f"BabelDOC 完成事件缺少 {kind} PDF")
        files[kind] = f"/api/jobs/{job_id}/files/{kind}"
    return files


def _write_manifest(job: dict[str, Any], files: dict[str, str]) -> Path:
    path = _job_dir(job["id"]) / "manifest.json"
    payload = {
        "schema": "read-paper.babeldoc.worker-manifest",
        "schema_version": 1,
        "job_id": job["id"],
        "idempotency_key": job.get("idempotency_key"),
        "source": {
            "filename": job["filename"],
            "sha256": job["sha256"],
            "language": job["lang_in"],
        },
        "target_language": job["lang_out"],
        "babeldoc_version": job["babeldoc_version"],
        "babeldoc_revision": job["babeldoc_revision"],
        "lang_in": job["lang_in"],
        "lang_out": job["lang_out"],
        "model": job["model"],
        "provider_fingerprint": job.get("provider_fingerprint"),
        "pages": job.get("pages") or "",
        "requested_pages": job.get("requested_pages"),
        "engine": dict(job["engine"]),
        "files": dict(files),
    }
    temporary = path.with_suffix(".writing")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)
    return path


def _page_dir(job_id: str, page_number: int) -> Path:
    return _job_dir(job_id) / "pages" / f"{page_number:04d}"


def _publish_page(
    job_id: str,
    page_number: int,
    source_pdf: Path,
    collector: CompactIRCollector,
    source_page_index: int = 0,
    translated: bool = True,
) -> None:
    import pymupdf

    page_dir = _page_dir(job_id, page_number)
    page_dir.mkdir(parents=True, exist_ok=True)
    destination = page_dir / "mono.pdf"
    if destination.exists():
        raise FileExistsError(f"已发布的第 {page_number} 页 PDF 不可覆盖")
    temporary = destination.with_name(f".mono.{secrets.token_hex(8)}.writing.pdf")
    with pymupdf.open(source_pdf) as document:
        if source_page_index < 0 or source_page_index >= document.page_count:
            raise RuntimeError(f"第 {page_number} 页增量产物页码无效")
        with pymupdf.open() as preview:
            # Copy the resource graph, then retain only glyphs used on this page.
            # BabelDOC skips font subsetting for split parts; copying full CJK
            # fonts otherwise makes each incremental page tens of megabytes.
            preview.insert_pdf(
                document, from_page=source_page_index, to_page=source_page_index
            )
            preview.subset_fonts()
            preview.save(temporary, garbage=4, deflate=True, deflate_fonts=True)
    temporary.replace(destination)
    collector.write(page_dir / "ir.json", page_numbers={page_number})
    with _jobs_lock:
        current = dict(_jobs[job_id])
        available_pages = sorted(
            {int(page) for page in current.get("available_pages") or []} | {page_number}
        )
        if available_pages != list(range(1, len(available_pages) + 1)):
            raise RuntimeError("增量页面必须按原始页序发布")
        translated_pages = sorted(
            set(current.get("translated_pages") or [])
            | ({page_number} if translated else set())
        )
    _patch_job(
        job_id,
        available_pages=available_pages,
        partial_revision=len(available_pages),
        translated_pages=translated_pages,
        stage=f"第 {page_number} 页可阅读",
    )


def _publish_missing_pages(
    job_id: str,
    mono_pdf: Path,
    collector: CompactIRCollector,
) -> None:
    import pymupdf

    with _jobs_lock:
        available = {int(page) for page in _jobs[job_id].get("available_pages") or []}
        total_pages = int(_jobs[job_id]["total_pages"])
        requested = set(_jobs[job_id].get("requested_pages", range(1, total_pages + 1)))
    with pymupdf.open(mono_pdf) as document:
        if document.page_count != total_pages:
            raise RuntimeError("BabelDOC 最终译文页数与原 PDF 不一致")
        for page_number in range(1, total_pages + 1):
            if page_number in available:
                continue
            _publish_page(
                job_id,
                page_number,
                mono_pdf,
                collector,
                source_page_index=page_number - 1,
                translated=page_number in requested,
            )


def _untranslated_part(config: Any, collector: CompactIRCollector, page_number: int):
    import pymupdf
    from babeldoc.format.pdf.translation_config import TranslateResult

    output = Path(config.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    mono = output / "original.mono.pdf"
    dual = output / "original.dual.pdf"
    with pymupdf.open(config.input_file) as original:
        if original.page_count != 1:
            raise RuntimeError("跳过翻译仅支持逐页分片")
        collector.capture_original_page(original[0], page_number=page_number)
        original.save(mono)
        with pymupdf.open() as paired:
            paired.insert_pdf(original)
            paired.insert_pdf(original)
            paired.save(dual)
    return TranslateResult(mono_pdf_path=mono, dual_pdf_path=dual)


def _make_translator(job: dict[str, Any], base_url: str, api_key: str):
    from babeldoc.translator.translator import OpenAITranslator

    translator = OpenAITranslator(
        lang_in=job["lang_in"],
        lang_out=job["lang_out"],
        model=job["model"],
        base_url=base_url,
        api_key=api_key,
        ignore_cache=False,
        enable_json_mode_if_requested=False,
        send_temperature=True,
    )
    # BabelDOC's built-in cache only keys the model, not its API provider.
    # Keep every job isolated, with no credential material in cache parameters.
    translator.add_cache_impact_parameters(
        "provider_fingerprint", job["provider_fingerprint"]
    )
    translator.add_cache_impact_parameters("job_id", job["id"])
    return translator


async def _translate(job_id: str) -> None:
    from babeldoc.docvision.doclayout import DocLayoutModel
    from babeldoc.format.pdf import high_level
    from babeldoc.format.pdf.split_manager import PageCountStrategy
    from babeldoc.format.pdf.translation_config import (
        TranslationConfig,
        WatermarkOutputMode,
    )
    from babeldoc.translator.translator import (
        set_translate_rate_limiter,
    )

    if ENGINE_VALIDATION_ERROR:
        raise RuntimeError(ENGINE_VALIDATION_ERROR)

    with _jobs_lock:
        job = dict(_jobs[job_id])
        credentials = dict(_job_credentials.get(job_id) or {})
    if job.get("model_source") == "user":
        if not credentials:
            raise RuntimeError("用户 API 凭据已清除，请显式重新提交任务。")
        api_key = credentials["api_key"]
        base_url = await validate_outbound_url(
            credentials["api_base_url"],
            allow_private=os.getenv("BABELDOC_ALLOW_PRIVATE_AI_URLS", "").lower()
            == "true",
        )
    else:
        api_key = os.getenv("BABELDOC_API_KEY", "").strip()
        base_url = _worker_base_url()
    if not api_key:
        raise RuntimeError(
            "缺少 BABELDOC_API_KEY；请通过 run.sh 继承 GLM_API_KEY 或显式设置。"
        )

    output_dir = _job_dir(job_id) / "output"
    working_dir = _job_dir(job_id) / "working"
    output_dir.mkdir(parents=True, exist_ok=True)
    working_dir.mkdir(parents=True, exist_ok=True)

    translator = _make_translator(job, base_url, api_key)
    guard = TranslationGuard(lambda reason: _patch_job(job_id, waiting_reason=reason))
    guard.attach(translator)
    set_translate_rate_limiter(int(job["qps"]))
    layout_model = DocLayoutModel.load_onnx()
    config = TranslationConfig(
        input_file=job["_input_path"],
        output_dir=output_dir,
        working_dir=working_dir,
        pages=job.get("pages") or None,
        translator=translator,
        lang_in=job["lang_in"],
        lang_out=job["lang_out"],
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
        split_strategy=PageCountStrategy(max_pages_per_part=1),
    )

    def cancel_failed_translation() -> None:
        if guard.failure is not None:
            _patch_job(
                job_id,
                error_code=guard.failure.code,
                error=str(guard.failure),
                waiting_reason=None,
            )
        config.cancel_translation()

    guard.cancel = cancel_failed_translation
    collector = CompactIRCollector(
        source_sha256=job["sha256"],
        source_filename=job["filename"],
        source_language=job["lang_in"],
        target_language=job["lang_out"],
        engine=job["engine"],
    )
    high_level.init()
    def publish_part(part_config: Any, page_number: int, result: Any) -> None:
        mono = Path(result.no_watermark_mono_pdf_path or result.mono_pdf_path)
        _publish_page(
            job_id, page_number, mono, collector,
            translated=page_number in job["requested_pages"],
        )

    with (
        translator.client,
        observe_babeldoc(
            config, collector, check_translation=guard.check,
            untranslated_part=_untranslated_part, part_completed=publish_part,
        ),
    ):
        async for event in high_level.async_translate(config):
            guard.check()
            event_type = event.get("type")
            if event_type in {"progress_start", "progress_update", "progress_end"}:
                progress = float(
                    event.get("overall_progress") or event.get("stage_progress") or 0
                )
                _patch_job(
                    job_id,
                    status="running",
                    progress=max(0.0, min(100.0, progress)),
                    stage=str(event.get("stage") or "处理中"),
                )
            elif event_type == "error":
                raise RuntimeError(
                    str(
                        event.get("message_for_user")
                        or event.get("error")
                        or "BabelDOC 处理失败"
                    )
                )
            elif event_type == "finish":
                result = event.get("translate_result")
                if result is None:
                    raise RuntimeError("BabelDOC 完成事件缺少输出结果")
                mono_pdf = Path(
                    getattr(result, "no_watermark_mono_pdf_path", None)
                    or getattr(result, "mono_pdf_path", "")
                )
                _publish_missing_pages(job_id, mono_pdf, collector)
                with _jobs_lock:
                    if _jobs[job_id]["translated_pages"] != job["requested_pages"]:
                        raise RuntimeError("已完成翻译页与请求页码不一致")
                ir_path = collector.write(output_dir / "compact-ir.v1.json")
                files = _result_files(result, job_id, ir_path)
                _write_manifest(job, files)
                _patch_job(
                    job_id,
                    status="completed",
                    progress=100.0,
                    stage="完成",
                    files=files,
                    available_pages=list(range(1, int(job["total_pages"]) + 1)),
                    translated_pages=job["requested_pages"],
                    partial_revision=int(job["total_pages"]),
                    metrics={
                        "seconds": round(
                            float(getattr(result, "total_seconds", 0) or 0), 2
                        ),
                        "peak_memory_mb": round(
                            float(getattr(result, "peak_memory_usage", 0) or 0), 2
                        ),
                        "characters": int(
                            getattr(result, "total_valid_character_count", 0) or 0
                        ),
                    },
                )
                return
    raise RuntimeError("BabelDOC 未返回完成事件")


def _run_job(job_id: str) -> None:
    _patch_job(
        job_id,
        status="running",
        stage="初始化 BabelDOC",
        progress=0.0,
        error=None,
        error_code=None,
        waiting_reason=None,
    )
    try:
        asyncio.run(_translate(job_id))
    except Exception as exc:  # noqa: BLE001 — task boundary; redact SDK details
        # SDK exceptions can contain request details: never persist/log their text.
        logger.error("BabelDOC job %s failed (%s)", job_id, type(exc).__name__)
        with _jobs_lock:
            failure = dict(_jobs[job_id])
        _patch_job(
            job_id,
            status="failed",
            stage="失败",
            error=failure.get("error")
            if failure.get("error_code")
            else "翻译未完成，请检查模型配置或更换模型后重试。",
            error_code=failure.get("error_code") or "translation_failed",
            waiting_reason=None,
        )
    finally:
        with _jobs_lock:
            _job_credentials.pop(job_id, None)


def _create_job(
    raw: bytes,
    filename: str,
    pages: str,
    qps: int,
    skip_scanned_detection: bool,
    auto_extract_glossary: bool,
    idempotency_key: str | None = None,
    model: str | None = None,
    credentials: dict[str, str] | None = None,
) -> dict[str, Any]:
    selected_model = model.strip() if model is not None else _default_model()
    source = "user" if credentials else "worker"
    if credentials and not WORKER_TOKEN:
        raise HTTPException(503, "用户 API 配置需要部署者设置 worker Bearer token")
    if (
        not selected_model
        or selected_model.startswith("free:")
        or len(selected_model) > 256
    ):
        raise HTTPException(422, "无效的 BabelDOC 模型")
    if not credentials and selected_model not in _allowed_models():
        raise HTTPException(422, "模型不在部署者允许的 BABELDOC_MODELS 列表中")
    fingerprint = _provider_fingerprint(
        source,
        credentials.get("provider", "custom") if credentials else "worker",
        credentials["api_base_url"] if credentials else _worker_base_url(),
    )
    if not raw.startswith(b"%PDF-"):
        raise HTTPException(400, "文件内容不是 PDF")
    normalized_key = _normalize_idempotency_key(idempotency_key)
    try:
        import pymupdf

        with pymupdf.open(stream=raw, filetype="pdf") as document:
            total_pages = int(document.page_count)
    except Exception as exc:
        raise HTTPException(400, "文件内容不是有效的 PDF") from exc
    if total_pages < 1:
        raise HTTPException(400, "PDF 没有可处理页面")
    digest = hashlib.sha256(raw).hexdigest()
    normalized_pages = _normalize_pages(pages, total_pages)
    requested_pages = (
        [page for part in normalized_pages.split(",")
         for page in range(int(part.split("-")[0]), int(part.split("-")[-1]) + 1)]
        if normalized_pages else list(range(1, total_pages + 1))
    )
    requested_signature = (
        digest,
        normalized_pages,
        bool(skip_scanned_detection),
        bool(auto_extract_glossary),
        selected_model,
        fingerprint,
    )

    with _jobs_lock:
        if normalized_key:
            existing_id = _idempotency_jobs.get(normalized_key)
            existing = _jobs.get(existing_id) if existing_id else None
            if existing is not None:
                if _idempotency_signature(existing) != requested_signature:
                    raise HTTPException(
                        409,
                        "idempotency_key 已用于不同 source 或输出配置",
                    )
                if existing.get("status") in {"queued", "running", "completed"}:
                    return _public_job(existing)

        job_id = f"{digest[:10]}-{uuid.uuid4().hex[:6]}"
        directory = _job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=False)
        input_path = directory / _safe_name(filename)
        input_path.write_bytes(raw)
        now = _now()
        job = {
            "id": job_id,
            "filename": input_path.name,
            "sha256": digest,
            "idempotency_key": normalized_key,
            "status": "queued",
            "stage": "排队中",
            "progress": 0.0,
            "total_pages": total_pages,
            "available_pages": [],
            "requested_pages": requested_pages,
            "translated_pages": [],
            "partial_revision": 0,
            "error": None,
            "files": {"original": f"/api/jobs/{job_id}/files/original"},
            "pages": normalized_pages,
            "qps": max(1, min(4, int(qps))),
            "skip_scanned_detection": bool(skip_scanned_detection),
            "auto_extract_glossary": bool(auto_extract_glossary),
            "babeldoc_version": ENGINE["version"],
            "babeldoc_revision": ENGINE["revision"],
            "lang_in": "en",
            "lang_out": "zh",
            "engine": {**ENGINE, "model": selected_model},
            "model": selected_model,
            "model_source": source,
            "provider_fingerprint": fingerprint,
            "table_notice": TABLE_NOTICE,
            "created_at": now,
            "updated_at": now,
            "_input_path": str(input_path),
        }
        _write_status(job)
        if credentials:
            _job_credentials[job_id] = dict(credentials)
        if normalized_key:
            _idempotency_jobs[normalized_key] = job_id
    try:
        _executor.submit(_run_job, job_id)
    except RuntimeError:
        with _jobs_lock:
            _job_credentials.pop(job_id, None)
        _patch_job(job_id, status="failed", error="任务调度失败，请显式重新提交。")
        raise HTTPException(503, "任务调度失败，请显式重新提交。") from None
    return _public_job(job)


async def _read_upload(file: UploadFile) -> bytes:
    data = bytearray()
    while chunk := await file.read(1024 * 1024):
        data.extend(chunk)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413, f"PDF 不能超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB"
            )
    return bytes(data)


_load_existing_jobs()


@app.get("/api/info")
def info() -> dict[str, Any]:
    return {
        "babeldoc_version": ENGINE["version"],
        "babeldoc_revision": ENGINE["revision"],
        "lang_in": "en",
        "lang_out": "zh",
        "engine": dict(ENGINE),
        "engine_valid": ENGINE_VALIDATION_ERROR is None,
        "model": _default_model(),
        "models": _allowed_models(),
        "provider_fingerprint": _provider_fingerprint(
            "worker", "worker", _worker_base_url()
        ),
        "user_provider_overrides": bool(WORKER_TOKEN),
        "api_configured": bool(
            os.getenv("BABELDOC_API_KEY") or os.getenv("GLM_API_KEY")
        ),
        "auth_required": bool(WORKER_TOKEN),
        "split_parts_supported": True,
        "incremental_pages": True,
        "page_selection_supported": True,
        "worker_api_version": 2,
        "sample_available": Path(os.getenv("BABELDOC_SAMPLE_PDF", "")).is_file(),
        "table_notice": TABLE_NOTICE,
    }


@app.get("/api/jobs", dependencies=AUTH_REQUIRED)
def list_jobs() -> list[dict[str, Any]]:
    with _jobs_lock:
        values = [_public_job(job) for job in _jobs.values()]
    return sorted(values, key=lambda job: job["created_at"], reverse=True)[:20]


@app.get("/api/jobs/{job_id}", dependencies=AUTH_REQUIRED)
def get_job(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return _public_job(job)


@app.post("/api/jobs", dependencies=AUTH_REQUIRED)
async def create_job(
    file: Annotated[UploadFile, File()],
    pages: Annotated[str, Form()] = "",
    qps: Annotated[int, Form()] = 2,
    skip_scanned_detection: Annotated[bool, Form()] = True,
    auto_extract_glossary: Annotated[bool, Form()] = False,
    idempotency_key: Annotated[str | None, Form()] = None,
    model: Annotated[str | None, Form()] = None,
    api_base_url: Annotated[str | None, Form()] = None,
    api_key: Annotated[str | None, Form()] = None,
    provider: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    credentials = None
    if api_base_url is not None or api_key is not None or provider is not None:
        if not WORKER_TOKEN:
            raise HTTPException(503, "用户 API 配置需要部署者设置 worker Bearer token")
        if not api_base_url or not api_key or not model:
            raise HTTPException(422, "用户模式必须提供 model、api_base_url 和 api_key")
        try:
            safe_url = await validate_outbound_url(
                api_base_url,
                allow_private=os.getenv("BABELDOC_ALLOW_PRIVATE_AI_URLS", "").lower()
                == "true",
            )
        except UnsafeOutboundUrl as exc:
            raise HTTPException(400, f"Base URL 不安全：{exc}") from None
        credentials = {
            "api_base_url": safe_url,
            "api_key": api_key,
            "provider": provider or "custom",
        }
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "只支持 PDF 文件")
    raw = await _read_upload(file)
    return _create_job(
        raw,
        file.filename or "document.pdf",
        pages,
        qps,
        skip_scanned_detection,
        auto_extract_glossary,
        idempotency_key,
        model,
        credentials,
    )


@app.post("/api/sample", dependencies=AUTH_REQUIRED)
def create_sample_job(
    pages: Annotated[str, Form()] = "1,6",
    qps: Annotated[int, Form()] = 2,
    auto_extract_glossary: Annotated[bool, Form()] = False,
) -> dict[str, Any]:
    sample = Path(os.getenv("BABELDOC_SAMPLE_PDF", ""))
    if not sample.is_file():
        raise HTTPException(404, "内置样本不存在")
    return _create_job(
        sample.read_bytes(), sample.name, pages, qps, True, auto_extract_glossary
    )


@app.get("/api/jobs/{job_id}/files/{kind}", dependencies=AUTH_REQUIRED)
def get_file(job_id: str, kind: str, download: bool = False):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or kind not in {"original", "mono", "dual", "ir", "manifest"}:
        raise HTTPException(404, "文件不存在")
    path = _artifact_for_kind(job_id, job, kind)
    is_pdf = path.suffix.lower() == ".pdf"
    return FileResponse(
        path,
        media_type="application/pdf" if is_pdf else "application/json",
        filename=path.name,
        content_disposition_type="attachment" if download else "inline",
    )


@app.get("/api/jobs/{job_id}/pages/{page_number}/{kind}", dependencies=AUTH_REQUIRED)
def get_page_file(job_id: str, page_number: int, kind: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if (
        not job
        or kind not in {"mono.pdf", "ir.json"}
        or page_number not in {int(page) for page in job.get("available_pages") or []}
    ):
        raise HTTPException(404, "增量页面不存在")
    path = _page_dir(job_id, page_number) / kind
    if not path.is_file():
        raise HTTPException(404, "增量页面不存在")
    return FileResponse(
        path,
        media_type="application/pdf" if kind.endswith(".pdf") else "application/json",
        filename=path.name,
        content_disposition_type="inline",
    )


def _artifact_for_kind(job_id: str, job: dict[str, Any], kind: str) -> Path:
    if kind == "original":
        input_path = job.get("_input_path")
        if not input_path:
            raise HTTPException(404, "原 PDF 路径缺失")
        path = Path(input_path)
    elif kind in {"mono", "dual"}:
        output = _job_dir(job_id) / "output"
        marker = "mono" if kind == "mono" else "dual"
        candidates = sorted(
            output.glob(f"*{marker}*.pdf"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise HTTPException(404, "产物尚未生成")
        path = candidates[0]
    elif kind == "ir":
        path = _job_dir(job_id) / "output" / "compact-ir.v1.json"
    elif kind == "manifest":
        path = _job_dir(job_id) / "manifest.json"
    else:
        raise HTTPException(404, "文件不存在")
    if not path.is_file() or _job_dir(job_id) not in path.resolve().parents:
        raise HTTPException(404, "文件不存在")
    return path


def _pdf_for_kind(job_id: str, job: dict[str, Any], kind: str) -> Path:
    path = _artifact_for_kind(job_id, job, kind)
    if path.suffix.lower() != ".pdf":
        raise HTTPException(404, "PDF 文件不存在")
    return path


@app.get("/api/jobs/{job_id}/preview/{kind}", dependencies=AUTH_REQUIRED)
def preview_info(job_id: str, kind: str) -> dict[str, int]:
    import pymupdf

    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or kind not in {"original", "mono", "dual"}:
        raise HTTPException(404, "文件不存在")
    with pymupdf.open(_pdf_for_kind(job_id, job, kind)) as document:
        return {"pages": document.page_count}


@app.get("/api/jobs/{job_id}/preview/{kind}/{page}.png", dependencies=AUTH_REQUIRED)
def preview_page(job_id: str, kind: str, page: int):
    import pymupdf

    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or kind not in {"original", "mono", "dual"}:
        raise HTTPException(404, "文件不存在")
    pdf = _pdf_for_kind(job_id, job, kind)
    cache = _job_dir(job_id) / "preview" / f"{kind}-{page}.png"
    with _preview_lock:
        if not cache.is_file():
            with pymupdf.open(pdf) as document:
                if page < 1 or page > document.page_count:
                    raise HTTPException(404, "页码不存在")
                cache.parent.mkdir(parents=True, exist_ok=True)
                pixmap = document[page - 1].get_pixmap(
                    matrix=pymupdf.Matrix(1.6, 1.6), alpha=False
                )
                pixmap.save(cache)
    return FileResponse(cache, media_type="image/png")


app.mount("/", StaticFiles(directory=STATIC_ROOT, html=True), name="static")
