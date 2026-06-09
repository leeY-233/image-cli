import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException

from .config import APP_ROOT, settings
from .constants import (
    DEBUG_LOG_DOCKER_TARGET_RE,
    DEBUG_LOG_FILE_MAX_PATH_LENGTH,
    DEBUG_LOG_FILE_POLL_SECONDS,
    DEBUG_LOG_FILE_TAIL_LINES,
    DEBUG_LOG_HEARTBEAT_SECONDS,
    DEBUG_LOG_SERVICE_SLOTS,
    DEBUG_LOG_SYSTEMD_TARGET_RE,
    DEBUG_LOG_TYPE_DOCKER,
    DEBUG_LOG_TYPE_FILE,
    DEBUG_LOG_TYPE_SYSTEMD,
)


def _debug_log_slot_id(slot: int) -> str:
    return f"log-{slot}"


def _safe_debug_log_type(value: Any) -> Literal["systemd", "docker", "file"]:
    text = str(value or "").strip().lower()
    if text in {"docker", "container", "docker_container"}:
        return DEBUG_LOG_TYPE_DOCKER
    if text in {"file", "log_file", "local_file", "path"}:
        return DEBUG_LOG_TYPE_FILE
    return DEBUG_LOG_TYPE_SYSTEMD


def _debug_log_target_key(log_type: str) -> str:
    if log_type == DEBUG_LOG_TYPE_DOCKER:
        return "container"
    if log_type == DEBUG_LOG_TYPE_FILE:
        return "path"
    return "service"


def _debug_log_type_label(log_type: str) -> str:
    if log_type == DEBUG_LOG_TYPE_DOCKER:
        return "Docker Container"
    if log_type == DEBUG_LOG_TYPE_FILE:
        return "Log File"
    return "systemd"


def _bool_from_any(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _debug_log_target_valid(log_type: str, target: str) -> bool:
    if not target or target.startswith("-"):
        return False
    if log_type == DEBUG_LOG_TYPE_DOCKER:
        return bool(DEBUG_LOG_DOCKER_TARGET_RE.fullmatch(target))
    if log_type == DEBUG_LOG_TYPE_FILE:
        return "\x00" not in target and len(target) <= DEBUG_LOG_FILE_MAX_PATH_LENGTH
    return bool(DEBUG_LOG_SYSTEMD_TARGET_RE.fullmatch(target))


def _default_debug_log_services() -> list[dict[str, Any]]:
    target = settings.systemd_unit.strip()
    services: list[dict[str, Any]] = []
    for slot in range(1, DEBUG_LOG_SERVICE_SLOTS + 1):
        is_primary = slot == 1 and bool(target)
        services.append(
            {
                "id": _debug_log_slot_id(slot),
                "slot": slot,
                "name": target if is_primary else f"日志 {slot}",
                "type": DEBUG_LOG_TYPE_SYSTEMD,
                "target": target if is_primary else "",
                "enabled": is_primary,
            }
        )
    return services


def _parse_debug_log_service_items(raw: Any) -> list[Any] | None:
    if raw is None:
        raw = settings.debug_log_services
    if isinstance(raw, list):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    return data


def _normalize_debug_log_services(raw: Any = None) -> list[dict[str, Any]]:
    parsed = _parse_debug_log_service_items(raw)
    items = parsed if parsed is not None else _default_debug_log_services()
    services: list[dict[str, Any]] = []
    for index in range(DEBUG_LOG_SERVICE_SLOTS):
        raw_item = items[index] if index < len(items) and isinstance(items[index], dict) else {}
        slot = index + 1
        log_type = _safe_debug_log_type(raw_item.get("type"))
        target = str(
            raw_item.get("target")
            or raw_item.get(_debug_log_target_key(log_type))
            or raw_item.get("file")
            or raw_item.get("file_path")
            or ""
        ).strip()
        name = str(raw_item.get("name") or raw_item.get("label") or "").strip()
        if len(name) > 80:
            name = name[:80]
        target_valid = _debug_log_target_valid(log_type, target)
        enabled = _bool_from_any(raw_item.get("enabled"), bool(target)) and target_valid
        if not name:
            name = target or f"日志 {slot}"
        services.append(
            {
                "id": _debug_log_slot_id(slot),
                "slot": slot,
                "name": name,
                "type": log_type,
                "target": target,
                "enabled": enabled,
                "target_valid": target_valid,
                "type_label": _debug_log_type_label(log_type),
            }
        )
    return services


def _debug_log_services_env_value(services: list[dict[str, Any]]) -> str:
    payload = [
        {
            "name": str(service.get("name") or "").strip(),
            "type": _safe_debug_log_type(service.get("type")),
            "target": str(service.get("target") or "").strip(),
            "enabled": bool(service.get("enabled")),
        }
        for service in _normalize_debug_log_services(services)
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _get_debug_log_service(slot_id: str) -> dict[str, Any]:
    match = re.fullmatch(r"log-([1-4])", str(slot_id or "").strip())
    if not match:
        raise HTTPException(status_code=404, detail="Unknown debug log slot")
    slot = int(match.group(1))
    return _normalize_debug_log_services()[slot - 1]


def _list_docker_containers() -> dict[str, Any]:
    docker = shutil.which("docker")
    if not docker:
        return {
            "available": False,
            "containers": [],
            "error": "当前系统没有 docker，无法读取容器列表。",
        }
    try:
        result = subprocess.run(
            [docker, "ps", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except subprocess.TimeoutExpired:
        return {
            "available": False,
            "containers": [],
            "error": "docker ps 执行超时，请检查 Docker daemon 状态。",
        }
    if result.returncode != 0:
        return {
            "available": False,
            "containers": [],
            "error": result.stderr.strip() or "docker ps 执行失败。",
        }
    containers: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        values = (line.split("\t", 3) + ["", "", "", ""])[:4]
        container_id, name, image, status = values
        if not name:
            continue
        containers.append(
            {
                "id": container_id,
                "name": name,
                "image": image,
                "status": status,
            }
        )
    return {"available": True, "containers": containers, "error": ""}


def _sse_payload(line: str) -> str:
    return f"data: {json.dumps(line, ensure_ascii=False)}\n\n"


def _sse_event(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_heartbeat() -> str:
    return ": heartbeat\n\n"


def _debug_timestamp(ts: int | float | None = None) -> str:
    timestamp = time.localtime(ts if ts is not None else time.time())
    return time.strftime("%Y-%m-%d %H:%M:%S", timestamp)


def _starts_with_timestamp(line: str) -> bool:
    if len(line) >= 19 and line[4:5] == "-" and line[7:8] == "-" and line[10:11] in {" ", "T"}:
        return True
    return False


def _format_debug_line(line: str, source: str) -> str:
    if _starts_with_timestamp(line):
        return line

    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        raw_ts = payload.get("ts")
        if isinstance(raw_ts, (int, float)):
            return f"{_debug_timestamp(raw_ts)} {line}"

    return f"{_debug_timestamp()} {line}"


def _resolve_debug_log_file_path(target: str) -> Path:
    try:
        path = Path(target).expanduser()
    except RuntimeError:
        path = Path(target)
    if not path.is_absolute():
        path = APP_ROOT / path
    return path


def _tail_debug_log_file(path: Path) -> tuple[list[str], int]:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        file_size = handle.tell()
        position = file_size
        chunks: list[bytes] = []
        newline_count = 0
        while position > 0 and newline_count <= DEBUG_LOG_FILE_TAIL_LINES:
            read_size = min(8192, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
    raw_lines = b"".join(reversed(chunks)).splitlines()[-DEBUG_LOG_FILE_TAIL_LINES:]
    return [line.decode("utf-8", errors="replace") for line in raw_lines], file_size


async def _stream_file_lines(target: str, source: str):
    path = _resolve_debug_log_file_path(target)
    try:
        if not path.is_file():
            yield _sse_payload(_format_debug_line(f"日志文件不存在：{path}", source))
            yield _sse_event("close", {"reason": "file_not_found"})
            return
        stat = path.stat()
        signature = (stat.st_dev, stat.st_ino)
        lines, position = _tail_debug_log_file(path)
    except PermissionError:
        yield _sse_payload(_format_debug_line(f"没有权限读取日志文件：{path}", source))
        yield _sse_event("close", {"reason": "file_permission_denied"})
        return
    except OSError as exc:
        yield _sse_payload(
            _format_debug_line(f"日志文件读取失败：{exc.__class__.__name__}: {exc}", source)
        )
        yield _sse_event("close", {"reason": "file_open_failed"})
        return

    for line in lines:
        yield _sse_payload(_format_debug_line(line, source))

    idle_seconds = 0.0
    while True:
        await asyncio.sleep(DEBUG_LOG_FILE_POLL_SECONDS)
        try:
            stat = path.stat()
        except FileNotFoundError:
            yield _sse_payload(_format_debug_line(f"日志文件已不存在：{path}", source))
            yield _sse_event("close", {"reason": "file_removed"})
            return
        except PermissionError:
            yield _sse_payload(_format_debug_line(f"没有权限读取日志文件：{path}", source))
            yield _sse_event("close", {"reason": "file_permission_denied"})
            return
        except OSError as exc:
            yield _sse_payload(
                _format_debug_line(f"日志文件状态读取失败：{exc.__class__.__name__}: {exc}", source)
            )
            yield _sse_event("close", {"reason": "file_stat_failed"})
            return

        next_signature = (stat.st_dev, stat.st_ino)
        if next_signature != signature or stat.st_size < position:
            try:
                lines, position = _tail_debug_log_file(path)
                signature = next_signature
            except OSError as exc:
                yield _sse_payload(
                    _format_debug_line(f"日志文件重新打开失败：{exc.__class__.__name__}: {exc}", source)
                )
                yield _sse_event("close", {"reason": "file_reopen_failed"})
                return
            for line in lines:
                yield _sse_payload(_format_debug_line(line, source))
            idle_seconds = 0.0
            continue

        if stat.st_size == position:
            idle_seconds += DEBUG_LOG_FILE_POLL_SECONDS
            if idle_seconds >= DEBUG_LOG_HEARTBEAT_SECONDS:
                yield _sse_heartbeat()
                idle_seconds = 0.0
            continue

        try:
            with path.open("rb") as handle:
                handle.seek(position)
                data = handle.read()
                position = handle.tell()
        except OSError as exc:
            yield _sse_payload(
                _format_debug_line(f"日志文件追加内容读取失败：{exc.__class__.__name__}: {exc}", source)
            )
            yield _sse_event("close", {"reason": "file_read_failed"})
            return

        for line in data.decode("utf-8", errors="replace").splitlines():
            yield _sse_payload(_format_debug_line(line, source))
        idle_seconds = 0.0


async def _stream_command_lines(
    command: list[str], unavailable_message: str, source: str
):
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        yield _sse_payload(_format_debug_line(unavailable_message, source))
        yield _sse_event("close", {"reason": "command_not_found"})
        return
    except Exception as exc:
        yield _sse_payload(
            _format_debug_line(f"日志进程启动失败：{exc.__class__.__name__}: {exc}", source)
        )
        yield _sse_event("close", {"reason": "process_start_failed"})
        return

    read_task: asyncio.Task[bytes] | None = None
    try:
        if process.stdout is None:
            yield _sse_payload(_format_debug_line("日志进程没有可读取的输出", source))
            yield _sse_event("close", {"reason": "no_stdout"})
            return
        read_task = asyncio.create_task(process.stdout.readline())
        while True:
            done, _ = await asyncio.wait(
                {read_task}, timeout=DEBUG_LOG_HEARTBEAT_SECONDS
            )
            if read_task not in done:
                yield _sse_heartbeat()
                continue
            line = read_task.result()
            if not line:
                return_code = await process.wait()
                if return_code:
                    yield _sse_payload(
                        _format_debug_line(f"日志进程已退出，exit={return_code}", source)
                    )
                yield _sse_event("close", {"reason": "process_exited", "exit": return_code})
                return
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            yield _sse_payload(_format_debug_line(text, source))
            read_task = asyncio.create_task(process.stdout.readline())
    finally:
        if read_task is not None and not read_task.done():
            read_task.cancel()
            try:
                await read_task
            except BaseException:
                pass
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


def _validate_debug_log_service(service: dict[str, Any]) -> tuple[str, str]:
    log_type = service["type"]
    target = str(service.get("target") or "").strip()
    if not service.get("enabled"):
        raise HTTPException(status_code=404, detail="Debug log slot is disabled")
    if not _debug_log_target_valid(log_type, target):
        raise HTTPException(status_code=400, detail="Debug log target is invalid")
    return log_type, target


def _debug_log_command(service: dict[str, Any]) -> tuple[list[str], str]:
    log_type, target = _validate_debug_log_service(service)
    if log_type == DEBUG_LOG_TYPE_SYSTEMD:
        journalctl = shutil.which("journalctl")
        if not journalctl:
            return (
                ["journalctl"],
                "当前系统没有 journalctl，systemd 日志只能在 Linux systemd 服务器上查看。",
            )
        return (
            [
                journalctl,
                "-u",
                target,
                "-n",
                "200",
                "-f",
                "--no-pager",
                "-o",
                "short-iso",
            ],
            "",
        )

    if log_type == DEBUG_LOG_TYPE_DOCKER:
        docker = shutil.which("docker")
        if not docker:
            return (
                ["docker"],
                "当前系统没有 docker，无法查看 Docker 容器日志。",
            )
        return (
            [docker, "logs", "--tail", "200", "-f", target],
            "",
        )

    raise HTTPException(status_code=400, detail="Debug log type is invalid")


debug_log_command = _debug_log_command
debug_log_services_env_value = _debug_log_services_env_value
get_debug_log_service = _get_debug_log_service
list_docker_containers = _list_docker_containers
normalize_debug_log_services = _normalize_debug_log_services
stream_command_lines = _stream_command_lines
stream_file_lines = _stream_file_lines
validate_debug_log_service = _validate_debug_log_service


