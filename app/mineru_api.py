from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from .settings import get_settings


IMAGE_EXTENSIONS = {".apng", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp"}
LEGACY_WORD_EXTENSIONS = {".doc"}


def safe_source_name(url: str) -> str:
    path = unquote(urlparse(url).path or "")
    name = posixpath.basename(path).strip() or "document.pdf"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    if "." not in name:
        name = f"{name}.pdf"
    return name[:160]


def download_source(url: str, dest_dir: str) -> tuple[str, dict[str, str]]:
    settings = get_settings()
    os.makedirs(dest_dir, exist_ok=True)
    filename = safe_source_name(url)
    path = os.path.join(dest_dir, filename)
    headers: dict[str, str] = {}
    digest = ""
    total = 0
    sha = hashlib.sha256()
    with httpx.Client(timeout=settings.mineru_download_timeout_seconds, follow_redirects=True, trust_env=settings.request_trust_env) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            with open(path, "wb") as out:
                for chunk in resp.iter_bytes(1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > settings.max_download_bytes:
                        raise RuntimeError(f"source file exceeds max_download_bytes={settings.max_download_bytes}")
                    sha.update(chunk)
                    out.write(chunk)
    digest = sha.hexdigest()
    meta = {
        "source_file_name": filename,
        "source_file_size": str(total),
        "source_sha256": digest,
        "source_etag": headers.get("etag", ""),
        "source_last_modified": headers.get("last-modified", ""),
        "source_content_type": headers.get("content-type", ""),
    }
    return path, meta


def _conversion_binary() -> str:
    for name in ("soffice", "libreoffice"):
        binary = shutil.which(name)
        if binary:
            return binary
    raise RuntimeError("legacy .doc requires LibreOffice/soffice, but it is not installed in wrapper image")


def _office_user_installation_uri(profile_dir: Path) -> str:
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir.resolve().as_uri()


def _convert_legacy_doc_to_docx(file_path: str) -> tuple[str, dict[str, str]]:
    settings = get_settings()
    source = Path(file_path)
    converted_dir = source.parent / "converted"
    converted_dir.mkdir(parents=True, exist_ok=True)
    profile_uri = _office_user_installation_uri(source.parent / "lo-profile")
    output_path = converted_dir / f"{source.stem}.docx"
    if output_path.exists():
        output_path.unlink()

    cmd = [
        _conversion_binary(),
        f"-env:UserInstallation={profile_uri}",
        "--headless",
        "--nologo",
        "--nofirststartwizard",
        "--convert-to",
        "docx",
        "--outdir",
        str(converted_dir),
        str(source),
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=settings.legacy_doc_conversion_timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "legacy .doc conversion failed "
            f"exit_code={result.returncode} stdout={result.stdout[-1000:]} stderr={result.stderr[-1000:]}"
        )

    if not output_path.exists():
        candidates = sorted(converted_dir.glob("*.docx"), key=lambda item: item.stat().st_mtime, reverse=True)
        if candidates:
            output_path = candidates[0]

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError(
            "legacy .doc conversion produced no docx "
            f"stdout={result.stdout[-1000:]} stderr={result.stderr[-1000:]}"
        )

    return str(output_path), {
        "source_conversion": "doc_to_docx",
        "native_source_file_name": output_path.name,
        "native_source_file_size": str(output_path.stat().st_size),
    }


def prepare_source_for_native(file_path: str) -> tuple[str, dict[str, str]]:
    source = Path(file_path)
    suffix = source.suffix.lower()
    if suffix in LEGACY_WORD_EXTENSIONS:
        return _convert_legacy_doc_to_docx(file_path)
    return file_path, {
        "source_conversion": "none",
        "native_source_file_name": source.name,
        "native_source_file_size": str(source.stat().st_size if source.exists() else 0),
    }


def submit_native_task(file_path: str) -> str:
    settings = get_settings()
    url = f"{settings.native_api_base}/tasks"
    file_name = os.path.basename(file_path)
    content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    data = {
        "backend": settings.mineru_backend,
        "parse_method": settings.mineru_parse_method,
        "lang_list": settings.lang_items[0],
        "formula_enable": str(settings.mineru_formula_enable).lower(),
        "table_enable": str(settings.mineru_table_enable).lower(),
        "image_analysis": str(settings.mineru_image_analysis).lower(),
        "return_md": "true",
        "return_middle_json": "false",
        "return_model_output": "false",
        "return_content_list": "false",
        "return_images": str(settings.mineru_return_images).lower(),
        "response_format_zip": "false",
    }
    with open(file_path, "rb") as file_obj:
        files = [("files", (file_name, file_obj, content_type))]
        with httpx.Client(timeout=120, follow_redirects=True, trust_env=settings.request_trust_env) as client:
            resp = client.post(url, data=data, files=files)
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = resp.text[:1000]
        raise RuntimeError(
            "native MinerU submit failed "
            f"status={resp.status_code} file={file_name} content_type={content_type} body={body}"
        ) from exc
    payload = resp.json()
    task_id = payload.get("task_id") or payload.get("id")
    if not task_id:
        raise RuntimeError(f"native MinerU did not return task_id: {payload}")
    return str(task_id)


def wait_native_result(native_task_id: str) -> Any:
    settings = get_settings()
    deadline = time.monotonic() + max(1, settings.mineru_task_timeout_seconds)
    status_url = f"{settings.native_api_base}/tasks/{native_task_id}"
    result_url = f"{settings.native_api_base}/tasks/{native_task_id}/result"
    with httpx.Client(timeout=120, follow_redirects=True, trust_env=settings.request_trust_env) as client:
        while True:
            status_resp = client.get(status_url)
            status_resp.raise_for_status()
            status_payload = status_resp.json()
            status = str(status_payload.get("status") or "").strip().lower()
            if status == "completed":
                result_resp = client.get(result_url)
                if result_resp.status_code == 202:
                    time.sleep(settings.mineru_poll_interval_seconds)
                    continue
                result_resp.raise_for_status()
                content_type = result_resp.headers.get("content-type", "")
                if "json" in content_type:
                    return result_resp.json()
                return result_resp.content
            if status == "failed":
                raise RuntimeError(status_payload.get("error") or f"native MinerU task failed: {status_payload}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"native MinerU task timeout: {native_task_id}")
            time.sleep(settings.mineru_poll_interval_seconds)


def _find_markdown(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("md_content", "markdown", "md", "full_md", "content"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item
        for item in value.values():
            found = _find_markdown(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_markdown(item)
            if found:
                return found
    return ""


def _looks_base64_image(text: str) -> bool:
    if not isinstance(text, str) or len(text) < 32:
        return False
    if text.startswith("data:image/"):
        return True
    sample = text[:128]
    return bool(re.fullmatch(r"[A-Za-z0-9+/=\s_-]+", sample))


def _decode_image(value: str) -> bytes | None:
    raw = value.strip()
    if raw.startswith("data:image/"):
        raw = raw.split(",", 1)[-1]
    raw = "".join(raw.split())
    try:
        data = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=False)
    except Exception:
        return None
    if len(data) < 8:
        return None
    return data


def _image_ext_from_bytes(data: bytes, fallback: str = ".png") -> str:
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"GIF"):
        return ".gif"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return ".webp"
    return fallback


def _collect_images(value: Any, prefix: str = "image") -> list[tuple[str, bytes]]:
    images: list[tuple[str, bytes]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_name = str(key or prefix).strip().replace("\\", "/").split("/")[-1] or prefix
            if isinstance(item, str) and _looks_base64_image(item):
                data = _decode_image(item)
                if data:
                    ext = posixpath.splitext(key_name)[1].lower()
                    if ext not in IMAGE_EXTENSIONS:
                        key_name = f"{key_name}{_image_ext_from_bytes(data)}"
                    images.append((key_name, data))
                    continue
            images.extend(_collect_images(item, key_name))
    elif isinstance(value, list):
        for index, item in enumerate(value, start=1):
            images.extend(_collect_images(item, f"{prefix}_{index}"))
    return images


def _normalize_zip_bytes(zip_bytes: bytes, output_zip_path: str) -> None:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as source_zip:
        md_candidates = [name for name in source_zip.namelist() if name.lower().endswith(".md")]
        if not md_candidates:
            raise RuntimeError("native zip result does not contain markdown")
        full_md_name = min(md_candidates, key=len)
        markdown = source_zip.read(full_md_name)
        image_members = [
            name for name in source_zip.namelist()
            if posixpath.splitext(name.lower())[1] in IMAGE_EXTENSIONS
            and not name.endswith("/")
        ]
        os.makedirs(os.path.dirname(output_zip_path), exist_ok=True)
        tmp_path = f"{output_zip_path}.tmp"
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as out_zip:
            out_zip.writestr("full.md", markdown)
            for index, name in enumerate(image_members, start=1):
                ext = posixpath.splitext(name)[1].lower() or ".png"
                out_zip.writestr(f"images/{index}_{posixpath.basename(name) or ('image' + ext)}", source_zip.read(name))
        os.replace(tmp_path, output_zip_path)


def write_official_zip(native_result: Any, output_zip_path: str) -> dict[str, str]:
    if isinstance(native_result, (bytes, bytearray)) and bytes(native_result).startswith(b"PK"):
        _normalize_zip_bytes(bytes(native_result), output_zip_path)
        return {"markdown_source": "zip"}

    markdown = _find_markdown(native_result)
    if not markdown.strip():
        raise RuntimeError("native MinerU result did not contain md_content")
    images = _collect_images(native_result)
    os.makedirs(os.path.dirname(output_zip_path), exist_ok=True)
    tmp_path = f"{output_zip_path}.tmp"
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as out_zip:
        out_zip.writestr("full.md", markdown.encode("utf-8"))
        used_names: set[str] = set()
        for index, (name, data) in enumerate(images, start=1):
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name) or f"image_{index}.png"
            if safe_name in used_names:
                root, ext = posixpath.splitext(safe_name)
                safe_name = f"{root}_{index}{ext or '.png'}"
            used_names.add(safe_name)
            out_zip.writestr(f"images/{safe_name}", data)
    os.replace(tmp_path, output_zip_path)
    return {"markdown_source": "json", "image_count": str(len(images))}
