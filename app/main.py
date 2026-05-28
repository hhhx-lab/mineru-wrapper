from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .settings import get_settings
from .storage import (
    create_task_record,
    full_zip_url,
    get_existing_task_for_idem,
    get_task,
    idempotency_key,
    now_ts,
    public_status,
    redis_client,
    result_zip_path,
    update_task,
    verify_download_token,
)
from .tasks import process_mineru_task


PARSER_VERSION = "official-compatible-wrapper-v1"
logger = logging.getLogger(__name__)

app = FastAPI(title="MinerU Official-Compatible Wrapper", version="1.0.0")


class ExtractTaskRequest(BaseModel):
    url: str = Field(..., min_length=1)
    model_version: str = "vlm"


def require_auth(authorization: str = Header(default="")) -> None:
    settings = get_settings()
    expected = f"Bearer {settings.wrapper_auth_token}"
    if authorization.strip() != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


def official_response(data: dict[str, Any], msg: str = "ok", code: int = 0) -> dict[str, Any]:
    return {"code": code, "msg": msg, "data": data}


def _bool_field(record: dict[str, str], key: str) -> bool:
    return (record.get(key) or "").strip().lower() in {"1", "true", "yes"}


def _int_field(record: dict[str, str], key: str) -> int:
    try:
        return int(record.get(key) or "0")
    except (TypeError, ValueError):
        return 0


def _validate_task_id(task_id: str) -> str:
    try:
        return str(UUID(task_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid task_id")


def _task_artifact_dir(root: str, task_id: str) -> Path:
    root_path = Path(root).resolve()
    task_path = (root_path / task_id).resolve()
    if task_path.parent != root_path:
        raise HTTPException(status_code=400, detail="invalid task artifact path")
    return task_path


def _remove_task_artifact_dir(root: str, task_id: str) -> bool:
    task_path = _task_artifact_dir(root, task_id)
    if not task_path.exists():
        return False
    if not task_path.is_dir():
        raise HTTPException(status_code=500, detail=f"artifact path is not a directory: {task_path.name}")
    shutil.rmtree(task_path)
    return True


def _artifacts_available(record: dict[str, str]) -> bool:
    task_id = record.get("task_id", "")
    return bool(task_id) and not _bool_field(record, "artifact_deleted") and os.path.exists(result_zip_path(task_id))


def _delete_task_artifacts(task_id: str, *, reason: str) -> dict[str, Any]:
    settings = get_settings()
    removed_results = _remove_task_artifact_dir(settings.result_dir, task_id)
    removed_tmp = _remove_task_artifact_dir(settings.tmp_dir, task_id)
    update_task(
        task_id,
        artifact_deleted="true",
        artifact_deleted_at=now_ts(),
        artifact_deleted_reason=reason,
        result_zip_path="",
        full_zip_url="",
    )
    return {
        "task_id": task_id,
        "status": "done",
        "artifact_deleted": True,
        "artifact_deleted_reason": reason,
        "removed_results": removed_results,
        "removed_tmp": removed_tmp,
    }


def _ttl_delete_reason(record: dict[str, str], now: int) -> str | None:
    settings = get_settings()
    task_id = record.get("task_id", "")
    if not task_id or public_status(record) != "done" or not _artifacts_available(record):
        return None

    done_at = _int_field(record, "artifact_done_at") or _int_field(record, "updated_at") or _int_field(record, "created_at")
    if done_at > 0 and now - done_at >= settings.artifact_done_ttl_hours * 3600:
        return "ttl_after_done"

    downloaded_at = _int_field(record, "artifact_last_downloaded_at") or _int_field(record, "artifact_downloaded_at")
    if downloaded_at > 0 and now - downloaded_at >= settings.artifact_download_ttl_hours * 3600:
        return "ttl_after_download"
    return None


def cleanup_expired_artifacts_once() -> dict[str, int]:
    client = redis_client()
    task_ids = sorted(client.smembers("tasks:success"))
    now = now_ts()
    scanned = deleted = failed = 0
    for task_id in task_ids:
        scanned += 1
        record = client.hgetall(f"task:{task_id}")
        reason = _ttl_delete_reason(record, now)
        if not reason:
            continue
        try:
            result = _delete_task_artifacts(task_id, reason=reason)
            deleted += 1
            logger.info(
                "[ArtifactsCleanup] deleted task_id=%s reason=%s removed_results=%s removed_tmp=%s",
                task_id,
                reason,
                result["removed_results"],
                result["removed_tmp"],
            )
        except Exception:
            failed += 1
            logger.exception("[ArtifactsCleanup] failed task_id=%s", task_id)
    return {"scanned": scanned, "deleted": deleted, "failed": failed}


async def _artifact_cleanup_loop() -> None:
    while True:
        settings = get_settings()
        try:
            cleanup_expired_artifacts_once()
        except Exception:
            logger.exception("[ArtifactsCleanup] cleanup loop failed")
        await asyncio.sleep(max(60, settings.artifact_cleanup_interval_seconds))


def _record_to_response(record: dict[str, str]) -> dict[str, Any]:
    task_id = record.get("task_id", "")
    status = public_status(record)
    data: dict[str, Any] = {
        "task_id": task_id,
        "status": status,
    }
    if status == "done":
        artifact_deleted = not _artifacts_available(record)
        data["artifact_deleted"] = artifact_deleted
        if not artifact_deleted:
            data["full_zip_url"] = full_zip_url(task_id)
    if status == "failed":
        data["error"] = record.get("error", "")
        data["internal_status"] = record.get("status", "")
    return data


@app.on_event("startup")
async def start_artifact_cleanup_loop() -> None:
    asyncio.create_task(_artifact_cleanup_loop())


@app.get("/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    client = redis_client()
    redis_status = "ok"
    try:
        client.ping()
    except Exception as exc:
        redis_status = f"error: {exc}"

    mineru_status: Any = "unknown"
    try:
        with httpx.Client(timeout=5, trust_env=settings.request_trust_env) as http:
            resp = http.get(f"{settings.native_api_base}/health")
            resp.raise_for_status()
            mineru_status = resp.json()
    except Exception as exc:
        mineru_status = f"error: {exc}"

    disk = shutil.disk_usage(settings.result_dir)
    queue_pending = queue_processing = success = failed = 0
    if redis_status == "ok":
        queue_pending = client.scard("tasks:pending")
        queue_processing = client.scard("tasks:processing") + client.scard("tasks:submitted") + client.scard("tasks:downloading") + client.scard("tasks:saving")
        success = client.scard("tasks:success")
        failed = client.scard("tasks:failed_final")

    return {
        "status": "healthy" if redis_status == "ok" and not isinstance(mineru_status, str) else "degraded",
        "redis": redis_status,
        "mineru_api": mineru_status,
        "queue_pending": queue_pending,
        "queue_processing": queue_processing,
        "success": success,
        "failed": failed,
        "disk_free_gb": round(disk.free / 1024 / 1024 / 1024, 2),
    }


@app.post("/api/v4/extract/task")
def create_extract_task(payload: ExtractTaskRequest, _: None = Depends(require_auth)) -> dict[str, Any]:
    settings = get_settings()
    normalized_url = payload.url.strip()
    normalized_model = (payload.model_version or "vlm").strip() or "vlm"
    idem_payload = {
        "url": normalized_url,
        "model_version": normalized_model,
        "backend": settings.mineru_backend,
        "parse_method": settings.mineru_parse_method,
        "lang_list": settings.lang_items,
        "return_images": settings.mineru_return_images,
        "parser_version": PARSER_VERSION,
    }
    idem_key = idempotency_key(idem_payload)
    existing = get_existing_task_for_idem(idem_key)
    if existing and public_status(existing) in {"pending", "processing"}:
        return official_response(_record_to_response(existing))
    if existing and public_status(existing) == "done" and _artifacts_available(existing):
        return official_response(_record_to_response(existing))

    task_id = create_task_record(
        url=normalized_url,
        model_version=normalized_model,
        idem_key=idem_key,
        parser_version=PARSER_VERSION,
    )
    process_mineru_task.delay(task_id)
    record = get_task(task_id)
    return official_response(_record_to_response(record))


@app.get("/api/v4/extract/task/{task_id}")
def get_extract_task(task_id: str, _: None = Depends(require_auth)) -> dict[str, Any]:
    record = get_task(task_id)
    if not record:
        raise HTTPException(status_code=404, detail="task not found")
    return official_response(_record_to_response(record), msg="failed" if public_status(record) == "failed" else "ok")


@app.delete("/api/v4/extract/task/{task_id}/artifacts")
def delete_extract_task_artifacts(task_id: str, _: None = Depends(require_auth)) -> dict[str, Any]:
    task_id = _validate_task_id(task_id)
    record = get_task(task_id)
    if not record:
        raise HTTPException(status_code=404, detail="task not found")
    if public_status(record) != "done":
        raise HTTPException(status_code=409, detail="artifacts can only be deleted after task is done")

    return official_response(_delete_task_artifacts(task_id, reason="client_delete"))


@app.get("/api/v4/files/{task_id}/full.zip")
def download_full_zip(task_id: str, token: str = Query(default="")) -> FileResponse:
    if not verify_download_token(task_id, token):
        raise HTTPException(status_code=403, detail="invalid or expired token")
    record = get_task(task_id)
    if not record or public_status(record) != "done":
        raise HTTPException(status_code=404, detail="zip not ready")
    if _bool_field(record, "artifact_deleted"):
        raise HTTPException(status_code=404, detail="zip deleted")
    zip_path = result_zip_path(task_id)
    if not os.path.exists(zip_path):
        raise HTTPException(status_code=404, detail="zip not found")
    download_count = _int_field(record, "artifact_download_count") + 1
    ts = now_ts()
    update_fields: dict[str, Any] = {
        "artifact_last_downloaded_at": ts,
        "artifact_download_count": download_count,
    }
    if _int_field(record, "artifact_done_at") <= 0:
        update_fields["artifact_done_at"] = _int_field(record, "updated_at") or ts
    if _int_field(record, "artifact_downloaded_at") <= 0:
        update_fields["artifact_downloaded_at"] = ts
    update_task(task_id, **update_fields)
    return FileResponse(zip_path, media_type="application/zip", filename="full.zip")


@app.post("/admin/tasks/{task_id}/replay")
def replay_task(task_id: str, _: None = Depends(require_auth)) -> dict[str, Any]:
    record = get_task(task_id)
    if not record:
        raise HTTPException(status_code=404, detail="task not found")
    update_task(
        task_id,
        "pending",
        error="",
        full_zip_url="",
        result_zip_path="",
        artifact_deleted="false",
        artifact_deleted_at="",
        artifact_deleted_reason="",
        artifact_downloaded_at="",
        artifact_last_downloaded_at="",
        artifact_download_count="0",
    )
    process_mineru_task.delay(task_id)
    return {"task_id": task_id, "status": "pending", "replayed": True}
