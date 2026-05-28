from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any
from uuid import uuid4

import redis

from .settings import get_settings


TERMINAL_INTERNAL_STATUSES = {"success", "failed_final"}
RUNNING_INTERNAL_STATUSES = {"pending", "downloading", "submitted", "processing", "saving", "failed_retryable"}
STATUS_SETS = [
    "tasks:pending",
    "tasks:downloading",
    "tasks:submitted",
    "tasks:processing",
    "tasks:saving",
    "tasks:success",
    "tasks:failed_retryable",
    "tasks:failed_final",
]


def redis_client() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def now_ts() -> int:
    return int(time.time())


def task_key(task_id: str) -> str:
    return f"task:{task_id}"


def idempotency_key(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clean_mapping(value: dict[str, Any]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, item in value.items():
        if item is None:
            continue
        if isinstance(item, (dict, list)):
            cleaned[key] = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        else:
            cleaned[key] = str(item)
    return cleaned


def create_task_record(*, url: str, model_version: str, idem_key: str, parser_version: str) -> str:
    client = redis_client()
    task_id = str(uuid4())
    ts = now_ts()
    record = {
        "task_id": task_id,
        "url": url,
        "model_version": model_version,
        "idempotency_key": idem_key,
        "parser_version": parser_version,
        "status": "pending",
        "attempt_count": "0",
        "created_at": str(ts),
        "updated_at": str(ts),
    }
    pipe = client.pipeline()
    pipe.hset(task_key(task_id), mapping=record)
    pipe.set(f"idem:{idem_key}", task_id)
    pipe.sadd("tasks:pending", task_id)
    pipe.execute()
    return task_id


def get_task(task_id: str) -> dict[str, str]:
    return redis_client().hgetall(task_key(task_id))


def get_existing_task_for_idem(idem_key: str) -> dict[str, str] | None:
    client = redis_client()
    task_id = client.get(f"idem:{idem_key}")
    if not task_id:
        return None
    record = client.hgetall(task_key(task_id))
    return record or None


def update_task(task_id: str, status: str | None = None, **fields: Any) -> None:
    client = redis_client()
    values = dict(fields)
    if status:
        values["status"] = status
    values["updated_at"] = str(now_ts())
    pipe = client.pipeline()
    if values:
        pipe.hset(task_key(task_id), mapping=_clean_mapping(values))
    if status:
        for set_name in STATUS_SETS:
            pipe.srem(set_name, task_id)
        pipe.sadd(f"tasks:{status}", task_id)
    pipe.execute()


def increment_attempt(task_id: str) -> int:
    client = redis_client()
    attempt = int(client.hincrby(task_key(task_id), "attempt_count", 1))
    client.hset(task_key(task_id), "updated_at", str(now_ts()))
    return attempt


def result_zip_path(task_id: str) -> str:
    return os.path.join(get_settings().result_dir, task_id, "full.zip")


def sign_download_token(task_id: str, expires_at: int | None = None) -> str:
    settings = get_settings()
    expires = int(expires_at or (now_ts() + settings.signed_url_ttl_seconds))
    payload = f"{task_id}:{expires}"
    sig = hmac.new(settings.download_signing_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    token = f"{payload}:{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(token).decode("ascii").rstrip("=")


def verify_download_token(task_id: str, token: str) -> bool:
    settings = get_settings()
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        token_task_id, raw_expires, supplied_sig = raw.split(":", 2)
        expires = int(raw_expires)
    except Exception:
        return False
    if token_task_id != task_id or expires < now_ts():
        return False
    payload = f"{token_task_id}:{expires}"
    expected_sig = hmac.new(settings.download_signing_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_sig, supplied_sig)


def full_zip_url(task_id: str) -> str:
    token = sign_download_token(task_id)
    return f"{get_settings().public_api_base}/api/v4/files/{task_id}/full.zip?token={token}"


def public_status(record: dict[str, str]) -> str:
    status = (record.get("status") or "pending").strip().lower()
    if status == "success":
        return "done"
    if status == "failed_final":
        return "failed"
    if status in {"pending"}:
        return "pending"
    return "processing"
