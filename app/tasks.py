from __future__ import annotations

import os
import shutil

from celery import Celery

from .mineru_api import download_source, prepare_source_for_native, submit_native_task, wait_native_result, write_official_zip
from .settings import get_settings
from .storage import full_zip_url, get_task, increment_attempt, now_ts, result_zip_path, update_task


settings = get_settings()
celery_app = Celery("mineru_wrapper", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
)


@celery_app.task(bind=True, name="process_mineru_task")
def process_mineru_task(self, task_id: str) -> None:
    settings = get_settings()
    record = get_task(task_id)
    if not record:
        return
    if record.get("status") == "success":
        return

    attempt = increment_attempt(task_id)
    work_dir = os.path.join(settings.tmp_dir, task_id, f"attempt_{attempt}")
    try:
        update_task(task_id, "downloading", error="")
        source_path, source_meta = download_source(record["url"], work_dir)
        native_source_path, native_source_meta = prepare_source_for_native(source_path)
        update_task(task_id, "submitted", **source_meta, **native_source_meta)

        native_task_id = submit_native_task(native_source_path)
        update_task(task_id, "processing", mineru_task_id=native_task_id)

        native_result = wait_native_result(native_task_id)
        update_task(task_id, "saving")

        zip_path = result_zip_path(task_id)
        output_meta = write_official_zip(native_result, zip_path)
        update_task(
            task_id,
            "success",
            full_zip_url=full_zip_url(task_id),
            result_zip_path=zip_path,
            artifact_deleted="false",
            artifact_deleted_at="",
            artifact_deleted_reason="",
            artifact_done_at=now_ts(),
            artifact_downloaded_at="",
            artifact_last_downloaded_at="",
            artifact_download_count="0",
            **output_meta,
        )
    except Exception as exc:
        message = str(exc)
        if attempt < settings.mineru_max_attempts:
            update_task(task_id, "failed_retryable", error=message)
            raise self.retry(exc=exc, countdown=min(300, 10 * attempt * attempt))
        update_task(task_id, "failed_final", error=message)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
