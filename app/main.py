from __future__ import annotations

import json
import secrets
import asyncio
import logging
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile, WebSocket
from starlette.websockets import WebSocketDisconnect
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .config import settings
from .db import Database
from .activity import ActivityActionError, ActivityActions, ActivityFeed
from .artifacts import ArtifactStaticFiles, ArtifactStore
from .asr import ASRError, recognize_file
from .service import JarvisService, PermissionDenied, PermissionUnavailable
from .projects import InvalidProjectTransition, StaleProjectRevision
from .domain import EventSource, EventType
from .llm import LLMError
from .usage import UsageLimitExceeded, usage_snapshot
from .metrics import RequestMetricsMiddleware, request_stats
from .jobs import JobStore
from .workspace_agent import WorkspaceAgent
from .eventbus import EventBus


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
event_bus = EventBus()
database = Database(settings.database_path)
service = JarvisService(database)
service.event_publisher = event_bus.publish
service.orchestrator.event_publisher = event_bus.publish
job_store = JobStore(database, Path(settings.database_path).parent / "jobs", event_bus.publish)
job_agent: WorkspaceAgent | None = None
job_tasks: dict[str, asyncio.Task[Any]] = {}
connected_devices: dict[str, WebSocket] = {}
task_runner: asyncio.Task[None] | None = None
event_worker: asyncio.Task[None] | None = None
kill_switch = False


async def push_to_device(device_id: str, payload: dict[str, Any]) -> bool:
    socket = connected_devices.get(device_id)
    if socket is None:
        logger.warning(
            "device push skipped device_id=%s type=%s reason=not_connected",
            device_id,
            payload.get("type"),
        )
        return False
    try:
        await socket.send_json(payload)
        logger.info(
            "device push sent device_id=%s type=%s run_id=%s",
            device_id,
            payload.get("type"),
            payload.get("run_id"),
        )
        return True
    except Exception as exc:
        logger.warning(
            "device push failed device_id=%s type=%s error=%s",
            device_id,
            payload.get("type"),
            type(exc).__name__,
        )
        connected_devices.pop(device_id, None)
        return False


async def _push_job_progress(_job_id: str, payload: dict[str, Any]) -> bool:
    for device_id in list(connected_devices):
        await push_to_device(device_id, payload)
    return True


def _start_job(goal: str, max_steps: int = 60) -> dict[str, Any]:
    """Create a job. Nothing runs until it is approved — that is the consent point."""
    job = job_store.create(goal, max_steps)
    return {"id": job["id"], "status": job["status"]}


def _start_job_by_id(job_id: str) -> dict[str, Any]:
    job = job_store.get(job_id)
    if job is None:
        return {"id": job_id, "status": "missing"}
    if job["status"] in {"running", "done"}:
        return {"id": job_id, "status": job["status"]}
    job_store.set_status(job_id, "running")
    if job_agent:
        job_tasks[job_id] = asyncio.create_task(job_agent.run_job(job_id))
    return {"id": job_id, "status": "running"}


service.job_store = job_store
service.start_job = _start_job


def resolve_device_id(device_id: str | None, header_device_id: str | None) -> str | None:
    """Prefer an explicit device id, then identify a single connected device."""
    explicit = (device_id or header_device_id or "").strip()
    if explicit:
        return explicit
    if len(connected_devices) == 1:
        inferred = next(iter(connected_devices))
        logger.info("device id inferred from sole websocket device_id=%s", inferred)
        return inferred
    logger.warning(
        "device id missing connected_devices=%d; response cannot be pushed",
        len(connected_devices),
    )
    return None


async def run_task_scheduler() -> None:
    while True:
        try:
            if not kill_switch:
                await service.dispatch_due_tasks()
        except Exception:
            # The scheduler must not kill the API process because one task failed.
            pass
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global task_runner, event_worker, job_agent
    database.init()
    service.set_push_callback(push_to_device)
    job_agent = WorkspaceAgent(
    job_store,
    artifacts=service.artifacts,
        push_callback=_push_job_progress,
        event_publisher=event_bus.publish,
    # No live job-scoped grant means no shell: approving a job is the consent point.
    grant_checker=lambda job_id: service.has_live_grant("job.run_shell", "job", job_id),
)
    task_runner = asyncio.create_task(run_task_scheduler())
    event_worker = asyncio.create_task(service.run_event_worker())
    for job in job_store.recover():
        job_tasks[job["id"]] = asyncio.create_task(job_agent.run_job(job["id"]))
    yield
    for runner in (task_runner, event_worker):
        if runner:
            runner.cancel()
    await asyncio.gather(
        *(runner for runner in (task_runner, event_worker) if runner),
        return_exceptions=True,
    )
    for task in job_tasks.values():
        task.cancel()
    await asyncio.gather(*job_tasks.values(), return_exceptions=True)


app = FastAPI(title="Jarvis API", version="0.1.0", lifespan=lifespan)
app.add_middleware(RequestMetricsMiddleware, database=database)
CONSOLE_DIR = Path(__file__).resolve().parent.parent / "console"


def verify_token(
    authorization: str | None = Header(default=None),
    x_device_token: str | None = Header(default=None),
) -> None:
    configured = settings.device_token
    if not configured:
        if settings.app_env == "production":
            raise HTTPException(503, "DEVICE_TOKEN is not configured")
        return
    presented = x_device_token or (
        authorization.removeprefix("Bearer ").strip()
        if authorization
        else ""
    )
    if not secrets.compare_digest(presented, configured):
        raise HTTPException(401, "Invalid device token")


class MessageRequest(BaseModel):
    text: str = Field(default="", max_length=12000)
    device_id: str | None = None
    image_data: str | None = None
    project_id: str | None = None
    conversation_id: str | None = None


class TaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    run_at: str | None = None
    source: str = "api"
    payload: dict[str, Any] = Field(default_factory=dict)
    timeout_at: str | None = None
    max_retries: int = Field(default=3, ge=0, le=20)
    depends_on: list[str] = Field(default_factory=list)


class JobRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=12000)
    max_steps: int = Field(default=60, ge=1, le=200)


class IgnoreRuleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    pattern: str = Field(min_length=1, max_length=500)
    match_type: str = "contains"
    source: str | None = None
    event_type: str | None = None
    device_id: str | None = None
    project_id: str | None = None


class DeviceRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=100)
    name: str = Field(default="Jarvis device", max_length=100)
    platform: str = Field(default="android", max_length=50)


class DeviceStatusRequest(BaseModel):
    battery_level: int | None = Field(default=None, ge=0, le=100)
    network_type: str | None = Field(default=None, max_length=30)
    app_version: str | None = Field(default=None, max_length=50)
    storage_free_mb: int | None = Field(default=None, ge=0)
    charging: bool | None = None
    tts_enabled: bool | None = None
    tts_ready: bool | None = None
    tts_error: str | None = Field(default=None, max_length=200)
    sequence: int | None = Field(default=None, ge=0)
    current_ma: float | None = None
    cache_size: int | None = None
    queue_size: int | None = None
    sensing_state: str | None = None
    model: str | None = None
    battery_mah: int | None = None
    cache_limit: int | None = None
    input_caps: str | None = None
    sensing_policy: str | None = None


class KillSwitchRequest(BaseModel):
    on: bool


class DeviceCommandRequest(BaseModel):
    action: str = Field(min_length=1, max_length=80)
    payload: dict[str, Any] = Field(default_factory=dict)


class DeviceCommandAckRequest(BaseModel):
    command_id: str = Field(min_length=1, max_length=100)
    success: bool = True
    error: str | None = Field(default=None, max_length=500)
    result: dict[str, Any] = Field(default_factory=dict)


class ExternalEventRequest(BaseModel):
    source: EventSource
    event_type: EventType
    content: Any
    context: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0, le=1)


class PermissionMatrixRequest(BaseModel):
    level: str


class PermissionGrantRequest(BaseModel):
    capability: str
    level: str
    scope_kind: str = "none"
    scope_value: str | None = None
    subject: str = "user"
    expires_at: str | None = None
    single_use: bool = True
    parent_grant_id: str | None = None


class AgentBoundaryRequest(BaseModel):
    capabilities: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    expires_at: str


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "jarvis", "env": settings.app_env}


@app.get("/console", include_in_schema=False)
async def console_redirect() -> RedirectResponse:
    return RedirectResponse("/console/")


@app.get("/api/dashboard")
async def dashboard(
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    service.expire_pending_actions()
    tasks = service.list_tasks()
    return {
        "health": await health(),
        "counts": {
            "tasks": len(tasks),
            "pending_tasks": sum(task["status"] == "pending" for task in tasks),
            "active_memories": len(service.list_memories()),
            "projects": len(service.list_projects()),
            "devices": len(database.fetch_all("SELECT id FROM devices")),
            "events": database.fetch_one("SELECT COUNT(*) AS count FROM events")["count"],
        },
        "recent_tasks": tasks[:6],
        "pending_actions": database.fetch_all(
            "SELECT * FROM pending_actions WHERE expires_at > ? ORDER BY created_at DESC LIMIT 8",
            (now_iso(),),
        ),
        "recent_events": database.fetch_all(
            "SELECT * FROM events ORDER BY created_at DESC LIMIT 8"
        ),
        "recent_audit": database.fetch_all(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT 8"
        ),
        "usage": usage_snapshot(database),
    }


@app.get("/api/usage")
async def usage(_: None = Depends(verify_token)) -> dict[str, Any]:
    return {"usage": usage_snapshot(database)}


@app.get("/api/stream")
async def stream(token: str = "") -> StreamingResponse:
    configured = settings.device_token
    if configured and not secrets.compare_digest(token, configured):
        raise HTTPException(401, "Invalid device token")
    queue = event_bus.subscribe()

    async def generate():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    name = str(event.get("event", "message"))
                    payload = json.dumps(
                        event, ensure_ascii=False, separators=(",", ":")
                    ).replace("\n", "\\n")
                    yield f"event: {name}\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            event_bus.unsubscribe(queue)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/jobs")
async def create_job(request: JobRequest, _: None = Depends(verify_token)) -> dict[str, Any]:
    return _start_job(request.goal, request.max_steps)


@app.post("/api/jobs/{job_id}/approve")
async def approve_job(job_id: str, _: None = Depends(verify_token)) -> dict[str, Any]:
    """Approve a job, which is what authorises it to run shell commands."""
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    service.create_permission_grant({
        "capability": "job.run_shell",
        "level": "per_action",
        "scope_kind": "job",
        "scope_value": job_id,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        "single_use": False,
    })
    service.audit(None, None, "job_approved", {"job_id": job_id})
    started = _start_job_by_id(job_id)
    return {"approved": True, "job": started}


@app.get("/api/jobs")
async def list_jobs(_: None = Depends(verify_token)) -> dict[str, Any]:
    return {"jobs": job_store.list()}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, _: None = Depends(verify_token)) -> dict[str, Any]:
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    # What the job produced is part of its answer: a workspace the user cannot
    # open is not a deliverable.
    return {"job": job, "artifacts": service.artifacts.for_job(job_id)}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, _: None = Depends(verify_token)) -> dict[str, Any]:
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] in {"queued", "running"}:
        job_store.set_status(job_id, "cancelled")
        job_store.audit(job_id, "job_cancelled", {"job_id": job_id})
    return {"id": job_id, "status": "cancelled"}


@app.get("/api/metrics/requests")
async def request_metrics(window: str = "24h", _: None = Depends(verify_token)) -> dict[str, Any]:
    if not window.endswith("h") or not window[:-1].isdigit() or int(window[:-1]) < 1:
        raise HTTPException(400, "window must be a positive number of hours")
    return request_stats(database, int(window[:-1]))


ARTIFACT_ROOT = service.artifacts.root


UPLOAD_ROOT = service.uploads.root
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/files/uploads", ArtifactStaticFiles(directory=str(UPLOAD_ROOT), check_dir=False),
          name="upload-files")
app.mount("/files", ArtifactStaticFiles(directory=str(ARTIFACT_ROOT), check_dir=False),
          name="artifact-files")



@app.post("/api/uploads")
async def upload_file(
    file: UploadFile = File(...),
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    """Inbound material. Stored and text-extracted; never executed."""
    data = await file.read()
    try:
        return service.uploads.save(file.filename or "upload", data,
                                    content_type=file.content_type or "")
    except UploadRefused as exc:
        raise HTTPException(400, exc.detail) from exc


@app.get("/api/uploads")
async def list_uploads(limit: int = 10, _: None = Depends(verify_token)) -> dict[str, Any]:
    return {"uploads": service.uploads.list(limit)}


@app.get("/api/uploads/{upload_id}/text")
async def read_upload(upload_id: str, _: None = Depends(verify_token)) -> dict[str, Any]:
    try:
        return service.uploads.read_text(upload_id)
    except UploadRefused as exc:
        raise HTTPException(404, exc.detail) from exc


@app.get("/api/artifacts")
async def list_artifacts(limit: int = 10, code: str | None = None, _: None = Depends(verify_token)) -> dict[str, Any]:
    """Things the agent produced, with links the user can actually open."""
    store = ArtifactStore(database, ARTIFACT_ROOT)
    if code is not None:
        item = store.get_by_code(code.strip())
        if item is None:
            raise HTTPException(404, "Artifact not found")
        return {"artifact": item}
    return {"artifacts": store.list(limit)}


class ModelSettingsRequest(BaseModel):
    primary: str
    fallback: str = ""
    timeout_seconds: float | None = None
    provider_base_url: str | None = None

    @field_validator("primary")
    @classmethod
    def valid_primary(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 120:
            raise ValueError("primary model must be non-empty and at most 120 characters")
        return value

    @field_validator("fallback")
    @classmethod
    def valid_fallback(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 120:
            raise ValueError("fallback model must be at most 120 characters")
        return value


@app.get("/api/model-settings")
async def get_model_settings(_: None = Depends(verify_token)) -> dict[str, Any]:
    return service.model_settings()


@app.put("/api/model-settings")
async def put_model_settings(request: ModelSettingsRequest, _: None = Depends(verify_token)) -> dict[str, Any]:
    values = {"primary.model": request.primary, "fallback.model": request.fallback}
    if request.timeout_seconds is not None:
        if request.timeout_seconds <= 0:
            raise HTTPException(422, "timeout_seconds must be positive")
        values["timeout_seconds"] = request.timeout_seconds
    if request.provider_base_url is not None:
        values["provider_base_url"] = request.provider_base_url
    return service.update_model_settings(values)


@app.get("/api/routes")
async def list_routes(_: None = Depends(verify_token)) -> dict[str, Any]:
    """Current model routes, their health, and what degradation would cost."""
    return service.router.snapshot()


@app.get("/api/permissions/matrix")
async def get_permission_matrix(_: None = Depends(verify_token)) -> dict[str, Any]:
    return service.permission_matrix()


@app.put("/api/permissions/matrix/{capability}")
async def put_permission_matrix(
    capability: str,
    request: PermissionMatrixRequest,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    try:
        return {"capability": service.set_permission_level(capability, request.level)}
    except PermissionDenied as exc:
        raise HTTPException(403, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "Capability not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/permissions/grants")
async def get_permission_grants(
    capability: str | None = None,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    if capability:
        return {"grants": database.fetch_all(
            "SELECT * FROM permission_grants WHERE capability = ? ORDER BY granted_at DESC",
            (capability,),
        )}
    return {"grants": database.fetch_all(
        "SELECT * FROM permission_grants ORDER BY granted_at DESC"
    )}


@app.post("/api/permissions/grants")
async def post_permission_grant(
    request: PermissionGrantRequest,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    try:
        return {"grant": service.create_permission_grant(request.model_dump())}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/permissions/grants/{grant_id}/revoke")
async def revoke_permission(
    grant_id: str,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    grant = service.revoke_permission_grant(grant_id)
    if grant is None:
        raise HTTPException(404, "Grant not found")
    return {"grant": grant}


@app.get("/api/permissions/agents")
async def get_permission_agents(_: None = Depends(verify_token)) -> dict[str, Any]:
    return {"agents": database.fetch_all(
        "SELECT * FROM permission_grants WHERE subject LIKE 'agent:%' ORDER BY granted_at DESC"
    )}


@app.put("/api/permissions/agents/{name}")
async def put_permission_agent(
    name: str,
    request: AgentBoundaryRequest,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    if name in {"", "user"}:
        raise HTTPException(400, "Invalid agent name")
    grants = []
    try:
        for capability in request.capabilities:
            grants.append(service.create_permission_grant({
                "capability": capability, "level": "once", "scope_kind": "project",
                "scope_value": ",".join(request.projects), "subject": f"agent:{name}",
                "expires_at": request.expires_at, "single_use": False,
            }))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    service.audit(None, None, "agent_boundary_edit", {"agent": name, "capabilities": request.capabilities})
    return {"grants": grants}


@app.post("/api/message")
async def message(
    request: MessageRequest,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    if not request.text and not request.image_data:
        raise HTTPException(400, "text or image_data is required")
    try:
        return await service.handle_message(
            request.text,
            request.device_id,
            request.image_data,
            request.project_id,
            request.conversation_id,
        )
    except UsageLimitExceeded as exc:
        raise HTTPException(429, "今日 AI 配额已用尽") from exc
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/message/audio")
async def audio_message(
    audio: UploadFile = File(...),
    device_id: str | None = None,
    project_id: str | None = None,
    x_device_id: str | None = Header(default=None, alias="X-Device-ID"),
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    request_id = uuid.uuid4().hex[:12]
    request_started = time.monotonic()
    device_id = resolve_device_id(device_id, x_device_id)
    suffix = ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp:
        audio_data = await audio.read()
        temp.write(audio_data)
        temp_path = temp.name
    audio_seconds = max(0, len(audio_data) - 44) / (16000 * 1 * 2)
    logger.info(
        "audio request received request_id=%s device_id=%s bytes=%d "
        "duration_seconds=%.2f",
        request_id,
        device_id or "-",
        len(audio_data),
        audio_seconds,
    )
    reservation = None
    try:
        try:
            reservation = service.reserve_asr_seconds(audio_seconds)
        except UsageLimitExceeded as exc:
            raise HTTPException(429, "今日 ASR 配额已用尽") from exc
        if service.push_callback and device_id:
            await service.push_callback(device_id, {
                "type": "perception_state",
                "state": "asr",
                "text": "正在进行 ASR 识别",
            })
        asr_started = time.monotonic()
        try:
            text = await recognize_file(temp_path)
        except ASRError as exc:
            logger.error(
                "audio request failed request_id=%s stage=asr elapsed_seconds=%.2f "
                "error_type=%s error=%s",
                request_id,
                time.monotonic() - asr_started,
                type(exc).__name__,
                _redact_sensitive_detail(str(exc)),
            )
            if service.push_callback and device_id:
                await service.push_callback(device_id, {
                    "type": "perception_state",
                    "state": "error",
                    "text": "ASR 识别失败",
                })
            raise HTTPException(502, str(exc)) from exc
        service.settle_asr_seconds(reservation, audio_seconds)
        reservation = None
        text = text.strip()
        if not text:
            logger.info(
                "audio request ignored request_id=%s reason=empty_transcript "
                "asr_elapsed_seconds=%.2f",
                request_id,
                time.monotonic() - asr_started,
            )
            if service.push_callback and device_id:
                await service.push_callback(device_id, {
                    "type": "perception_state",
                    "state": "listening",
                    "text": "没有识别到需要处理的语音",
                })
            return {
                "action": "ignore",
                "reason": "empty_transcript",
                "transcript": "",
            }
        logger.info(
            "audio ASR succeeded request_id=%s elapsed_seconds=%.2f "
            "transcript_chars=%d device_id=%s routing=agent",
            request_id,
            time.monotonic() - asr_started,
            len(text),
            device_id or "-",
        )
        if service.push_callback and device_id:
            await service.push_callback(device_id, {
                "type": "speech_final",
                "state": "processing",
                "text": text,
            })
        agent_started = time.monotonic()
        try:
            result = await service.handle_message(
                text,
                device_id=device_id,
                project_id=project_id,
                event_source=EventSource.MICROPHONE,
            )
        except UsageLimitExceeded as exc:
            if service.push_callback and device_id:
                await service.push_callback(device_id, {
                    "type": "perception_state",
                    "state": "error",
                    "text": "今日 AI 配额已用尽",
                })
            raise HTTPException(429, "今日 AI 配额已用尽") from exc
        except LLMError as exc:
            logger.error(
                "audio request failed request_id=%s stage=agent elapsed_seconds=%.2f "
                "error_type=%s error=%s",
                request_id,
                time.monotonic() - agent_started,
                type(exc).__name__,
                _redact_sensitive_detail(str(exc)),
            )
            if service.push_callback and device_id:
                await service.push_callback(device_id, {
                    "type": "perception_state",
                    "state": "error",
                    "text": "内容处理失败",
                })
            raise HTTPException(502, str(exc)) from exc
        result["transcript"] = text
        logger.info(
            "audio request succeeded request_id=%s device_id=%s "
            "action=%s total_elapsed_seconds=%.2f",
            request_id,
            device_id or "-",
            result.get("action"),
            time.monotonic() - request_started,
        )
        return result
    finally:
        if reservation is not None:
            service.release_usage(reservation)
        try:
            import os

            os.unlink(temp_path)
        except OSError:
            pass


def _redact_sensitive_detail(message: str) -> str:
    for secret in (
        settings.asr_api_key,
        settings.asr_access_key,
        settings.deepseek_api_key,
        settings.vision_api_key,
        settings.device_token,
    ):
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:500]


@app.post("/api/events")
async def submit_event(
    request: ExternalEventRequest,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    try:
        return await service.submit_external_event(
            source=request.source,
            event_type=request.event_type,
            content=request.content,
            context=request.context,
            confidence=request.confidence,
        )
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/tasks")
async def list_tasks(
    status: str | None = None,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    return {"tasks": service.list_tasks(status)}


@app.post("/api/tasks")
async def create_task(
    request: TaskRequest,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    return {"task": service.create_task(**request.model_dump())}


@app.post("/api/tasks/{task_id}/complete")
async def complete_task(
    task_id: str,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    task = service.complete_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return {"task": task}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    task = service.cancel_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return {"task": task}


@app.post("/api/tasks/{task_id}/pause")
async def pause_task(
    task_id: str,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    task = service.transition_task(task_id, "paused")
    if task is None:
        raise HTTPException(409, "Task cannot be paused from its current state")
    return {"task": task}


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(
    task_id: str,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    task = service.transition_task(task_id, "pending")
    if task is None:
        raise HTTPException(409, "Task cannot be resumed from its current state")
    return {"task": task}


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(
    task_id: str,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    task = database.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task or task["status"] not in {"failed", "cancelled"}:
        raise HTTPException(409, "Task cannot be retried from its current state")
    database.execute(
        """
        UPDATE tasks
        SET status = 'pending', retry_count = 0, last_error = NULL,
            dead_letter_at = NULL, next_attempt_at = run_at,
            locked_at = NULL, lock_token = NULL, updated_at = ?
        WHERE id = ?
        """,
        (now_iso(), task_id),
    )
    return {"task": database.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))}


@app.get("/api/pending-actions")
async def list_pending_actions(
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    service.expire_pending_actions()
    return {
        "actions": database.fetch_all(
            "SELECT * FROM pending_actions WHERE expires_at > ? ORDER BY created_at DESC",
            (now_iso(),),
        )
    }


@app.post("/api/pending-actions/{action_id}/confirm")
async def confirm_pending_action(
    action_id: str,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    service.expire_pending_actions()
    result = await service.async_confirm_pending_action(action_id)
    if result is None:
        raise HTTPException(404, "Pending action not found or expired")
    return {"confirmed": True, "result": result}


@app.post("/api/pending-actions/{action_id}/reject")
async def reject_pending_action(
    action_id: str,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    service.expire_pending_actions()
    if not service.reject_pending_action(action_id):
        raise HTTPException(404, "Pending action not found")
    return {"rejected": True, "action_id": action_id}


@app.get("/api/memories")
async def list_memories(
    layer: str | None = None,
    status: str | None = None,
    project: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    return {"memories": service.list_memories(layer=layer, status=status, project_id=project, tag=tag, q=q)}


@app.get("/api/agents")
async def list_agents(
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    return {"agents": service.agents.describe()}


@app.get("/api/tools")
async def list_tools(
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    return {"tools": service.tools.list_schemas()}


@app.get("/api/audit")
async def list_audit(
    limit: int = 100,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 500))
    return {
        "records": database.fetch_all(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
            (safe_limit,),
        )
    }


@app.get("/api/events")
async def list_events(
    limit: int = 100,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 500))
    return {
        "events": database.fetch_all(
            "SELECT * FROM events ORDER BY created_at DESC LIMIT ?",
            (safe_limit,),
        )
    }


@app.get("/api/activities")
async def list_activities(
    limit: int = 50,
    cursor: str | None = None,
    source: str | None = None,
    status: str | None = None,
    device_id: str | None = None,
    project_id: str | None = None,
    query: str | None = None,
    tag: str | None = None,
    include_ignored: bool = False,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    try:
        return ActivityFeed(database).list(
            limit=limit, cursor=cursor, source=source, status=status,
            device_id=device_id, project_id=project_id, query=query, tag=tag,
            include_ignored=include_ignored,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/activities/{event_id:path}")
async def get_activity(
    event_id: str,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    item = ActivityFeed(database).get(event_id)
    if item is None:
        raise HTTPException(404, "Activity not found")
    return {"item": item}


@app.post("/api/activities/{event_id:path}/actions")
async def apply_activity_action(
    event_id: str,
    payload: dict[str, Any],
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    try:
        if str(payload.get("action") or "").lower() == "accept":
            current = ActivityFeed(database).get(event_id.removeprefix("event:"))
            pending_id = current.get("pending_action_id") if current else None
            pending = (
                database.fetch_one(
                    "SELECT target_agent, capability FROM pending_actions WHERE id = ?",
                    (pending_id,),
                )
                if pending_id
                else None
            )
            if pending and (pending.get("target_agent") or pending.get("capability")):
                confirmed = await service.async_confirm_pending_action(pending_id)
                if confirmed is None:
                    raise ActivityActionError(409, "Pending action failed to execute")
                payload = {**payload, "_confirmed_result": confirmed}
        return ActivityActions(database, service).apply(event_id, payload)
    except ActivityActionError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


@app.get("/api/ignore-rules")
async def list_ignore_rules(
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    return {"rules": service.orchestrator.ignore_rules.list_rules()}


@app.post("/api/ignore-rules")
async def create_ignore_rule(
    request: IgnoreRuleRequest,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    if request.match_type not in {"contains", "regex"}:
        raise HTTPException(400, "match_type must be contains or regex")
    return {
        "rule": service.orchestrator.ignore_rules.add_rule(**request.model_dump())
    }


@app.post("/api/ignore-rules/{rule_id}/enable")
async def enable_ignore_rule(
    rule_id: str,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    rule = service.orchestrator.ignore_rules.set_enabled(rule_id, True)
    if rule is None:
        raise HTTPException(404, "Ignore rule not found")
    return {"rule": rule}


@app.post("/api/ignore-rules/{rule_id}/disable")
async def disable_ignore_rule(
    rule_id: str,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    rule = service.orchestrator.ignore_rules.set_enabled(rule_id, False)
    if rule is None:
        raise HTTPException(404, "Ignore rule not found")
    return {"rule": rule}


@app.delete("/api/ignore-rules/{rule_id}")
async def delete_ignore_rule(
    rule_id: str,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    if not service.orchestrator.ignore_rules.remove_rule(rule_id):
        raise HTTPException(404, "Ignore rule not found")
    return {"deleted": True, "rule_id": rule_id}


@app.get("/api/devices")
async def list_devices(
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    return {
        "devices": database.fetch_all(
            "SELECT * FROM devices ORDER BY last_seen DESC"
        ),
        "connected": list(connected_devices),
    }


class MemoryRequest(BaseModel):
    content: str = Field(min_length=1, max_length=5000)
    memory_type: str = "fact"
    project_id: str | None = None
    importance: float = Field(default=0.5, ge=0, le=1)


class MemoryRevisionRequest(BaseModel):
    content: str = Field(min_length=1, max_length=5000)
    layer: str | None = None
    status: str | None = None
    project_id: str | None = None
    importance: float | None = Field(default=None, ge=0, le=1)
    tags: list[str] | None = None


class DestroyRequest(BaseModel):
    confirm_phrase: str
    exported: bool
    export_token: str = ""


EXPORT_TOKEN_TTL = timedelta(minutes=10)


@app.get("/api/conversations")
async def list_conversations(
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    return {"conversations": service.list_conversations()}


class ProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    goal: str = Field(default="", max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    origin: str = Field(default="user", pattern="^(user|auto|scheduled)$")
    context_snapshot: dict[str, Any] = Field(default_factory=dict)


class ProjectStatusRequest(BaseModel):
    status: str = Field(
        pattern="^(active|paused|blocked|done|failed|cancelled)$"
    )
    reason: str | None = Field(default=None, max_length=500)
    one_liner: str | None = Field(default=None, max_length=60)
    progress: int | None = Field(default=None, ge=0, le=100)
    expect_revision: int | None = Field(default=None, ge=0)


@app.get("/api/projects")
async def list_projects(
    status: str | None = None,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    return {"projects": service.list_projects(status=status)}


@app.post("/api/projects")
async def create_project(
    request: ProjectRequest,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    return {
        "project": service.create_project(
            request.name,
            request.goal,
            request.metadata,
            origin=request.origin,
            context_snapshot=request.context_snapshot,
        )
    }


@app.patch("/api/projects/{project_id}")
async def update_project_status(
    project_id: str,
    request: ProjectStatusRequest,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    try:
        project = service.update_project_status(
            project_id,
            request.status,
            reason=request.reason,
            one_liner=request.one_liner,
            progress=request.progress,
            expect_revision=request.expect_revision,
        )
    except KeyError as exc:
        raise HTTPException(404, "Project not found") from exc
    except StaleProjectRevision as exc:
        raise HTTPException(409, str(exc)) from exc
    except InvalidProjectTransition as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"project": project}


@app.get("/api/projects/index")
async def project_index(
    limit: int = 20,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    """主 Agent 每轮注入用的紧凑项目索引。"""

    return {
        "text": service.project_index_text(limit=limit),
        "items": service.projects.index_view(limit=limit),
    }


@app.post("/api/memories")
async def create_memory(
    request: MemoryRequest,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    return {
        "memory": service.create_memory(
            content=request.content,
            memory_type=request.memory_type,
            project_id=request.project_id,
            importance=request.importance,
        )
    }


@app.put("/api/memories/{memory_id}")
async def revise_memory(memory_id: str, request: MemoryRevisionRequest, _: None = Depends(verify_token)) -> dict[str, Any]:
    memory = service.revise_memory(memory_id, **request.model_dump())
    if memory is None: raise HTTPException(404, "Memory not found")
    return {"memory": memory}


@app.post("/api/memories/{memory_id}/forget")
async def forget_memory(memory_id: str, _: None = Depends(verify_token)) -> dict[str, Any]:
    row = database.fetch_one("SELECT id FROM memories WHERE id=?", (memory_id,))
    if not row: raise HTTPException(404, "Memory not found")
    await service.memory.forget(memory_id)
    return {"forgotten": True, "id": memory_id}


@app.post("/api/memories/{memory_id}/restore")
async def restore_memory(memory_id: str, _: None = Depends(verify_token)) -> dict[str, Any]:
    database.execute("UPDATE memories SET status='confirmed' WHERE id=?", (memory_id,))
    row = database.fetch_one("SELECT * FROM memories WHERE id=?", (memory_id,))
    if not row: raise HTTPException(404, "Memory not found")
    return {"memory": row}


@app.delete("/api/memories/{memory_id}/raw")
async def delete_memory_raw(memory_id: str, _: None = Depends(verify_token)) -> dict[str, Any]:
    row = database.fetch_one("SELECT raw_ref FROM memories WHERE id=?", (memory_id,))
    if not row: raise HTTPException(404, "Memory not found")
    database.execute("UPDATE memories SET raw_ref=NULL WHERE id=?", (memory_id,))
    return {"deleted": True, "id": memory_id}


@app.get("/api/memories/{memory_id}/evidence")
async def memory_evidence(memory_id: str, _: None = Depends(verify_token)) -> dict[str, Any]:
    row = database.fetch_one("SELECT id, source_event_id, raw_ref, created_at FROM memories WHERE id=?", (memory_id,))
    if not row: raise HTTPException(404, "Memory not found")
    return {"evidence": row}


@app.get("/api/memories/candidates")
async def memory_candidates(_: None = Depends(verify_token)) -> dict[str, Any]:
    return {"memories": service.list_memories(status="candidate")}


@app.post("/api/memories/candidates/{memory_id}/accept")
async def accept_memory_candidate(memory_id: str, _: None = Depends(verify_token)) -> dict[str, Any]:
    if not service.memory.accept_candidate(memory_id): raise HTTPException(404, "Candidate not found")
    return {"accepted": True, "id": memory_id}


@app.post("/api/memories/candidates/{memory_id}/ignore")
async def ignore_memory_candidate(memory_id: str, _: None = Depends(verify_token)) -> dict[str, Any]:
    if not service.memory.ignore_candidate(memory_id): raise HTTPException(404, "Candidate not found")
    return {"ignored": True, "id": memory_id}


@app.post("/api/memories/{memory_id}/apply-today")
async def apply_memory_today(memory_id: str, _: None = Depends(verify_token)) -> dict[str, Any]:
    result = service.apply_memory_today(memory_id)
    if result is None: raise HTTPException(404, "Memory not found")
    return result


@app.post("/api/memories/export")
async def export_memories(_: None = Depends(verify_token)) -> dict[str, Any]:
    """Export is a precondition for destruction, so it leaves a verifiable token."""
    rows = service.list_memories()
    token = uuid.uuid4().hex
    timestamp = now_iso()
    database.execute(
        "INSERT INTO audit_log(action, detail, created_at) VALUES (?, ?, ?)",
        ("data_export", json.dumps({"token": token, "count": len(rows)}, ensure_ascii=False), timestamp),
    )
    return {
        "exported": True,
        "token": token,
        "expires_at": (datetime.now(timezone.utc) + EXPORT_TOKEN_TTL).isoformat(),
        "memories": rows,
    }


def _consume_export_token(token: str) -> bool:
    """True only when the most recent export produced this token, recently.

    Destruction wipes the audit log, so a token can never be replayed after a
    successful destroy; this only guards the accidental and the forged case.
    """
    if not token:
        return False
    row = database.fetch_one(
        "SELECT detail, created_at FROM audit_log WHERE action = 'data_export' "
        "ORDER BY id DESC LIMIT 1"
    )
    if not row:
        return False
    try:
        detail = json.loads(row["detail"] or "{}")
        created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if detail.get("token") != token:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created >= datetime.now(timezone.utc) - EXPORT_TOKEN_TTL


@app.post("/api/data/destroy")
async def destroy_data(request: DestroyRequest, _: None = Depends(verify_token)) -> dict[str, Any]:
    global kill_switch
    if request.confirm_phrase.strip() != "销毁全部" or request.exported is not True:
        raise HTTPException(400, "Exact confirmation phrase and successful export required")
    if not _consume_export_token(request.export_token):
        raise HTTPException(400, "A recent successful export is required before destruction")
    kill_switch = True
    revoked = database.fetch_all("SELECT id FROM permission_grants WHERE revoked_at IS NULL AND consumed_at IS NULL")
    timestamp = now_iso()
    database.execute("UPDATE permission_grants SET revoked_at=?, revoked_reason='data_destroyed' WHERE revoked_at IS NULL AND consumed_at IS NULL", (timestamp,))
    counts = {}
    for table in ("memories", "events", "tasks", "projects", "conversations", "pending_actions", "device_commands"):
        counts[table] = database.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
        database.execute(f"DELETE FROM {table}")
    with database.connect() as connection:
        connection.execute("DELETE FROM permission_grants")
        connection.execute("DELETE FROM permission_matrix_overrides")
        connection.execute("DELETE FROM audit_log")
        connection.execute("INSERT INTO audit_log(action, detail, created_at) VALUES (?, ?, ?)",
                           ("data_destroyed", __import__("json").dumps({"counts": counts, "grant_ids": [g["id"] for g in revoked], "backup_skipped": True}), timestamp))
        connection.commit()
    return {"destroyed": True, "counts": counts, "grant_ids": [g["id"] for g in revoked]}


@app.post("/api/device/register")
async def register_device(
    request: DeviceRequest,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    return {
        "device": service.register_device(
            request.device_id,
            request.name,
            request.platform,
        )
    }


@app.post("/api/device/{device_id}/heartbeat")
async def heartbeat(
    device_id: str,
    request: DeviceStatusRequest | None = None,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    device = service.update_device_status(
        device_id,
        **(request.model_dump() if request else {}),
    )
    if device is None:
        raise HTTPException(404, "Device not found")
    service.record_device_roundtrip(device_id)
    return {"ok": True, "device": device}


@app.get("/api/devices/{device_id}/metrics")
async def device_metrics(device_id: str, window: str = "24h", _: None = Depends(verify_token)) -> dict[str, Any]:
    if window != "24h":
        raise HTTPException(400, "Only window=24h is supported")
    request_data = request_stats(database)
    now_dt = datetime.now(timezone.utc)
    start = now_dt - timedelta(hours=24)
    rows = database.fetch_all("SELECT * FROM device_sessions WHERE device_id = ?", (device_id,))
    if not rows:
        return {"online_seconds": None, "offline_seconds": None, "online_ratio": None,
                "sessions": None, "degraded_minutes": None, "source": "insufficient_data",
                **{key: request_data[key] for key in ("p50_ms", "p95_ms", "error_rate", "samples")}}
    online_intervals: list[tuple[datetime, datetime]] = []
    degraded = 0.0
    for row in rows:
        if not row["last_roundtrip_at"]:
            continue
        session_start = datetime.fromisoformat(row["started_at"])
        session_end = datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else now_dt
        if session_start.tzinfo is None: session_start = session_start.replace(tzinfo=timezone.utc)
        if session_end.tzinfo is None: session_end = session_end.replace(tzinfo=timezone.utc)
        left, right = max(start, session_start), min(now_dt, session_end)
        if right > left:
            online_intervals.append((left, right))
            roundtrip = datetime.fromisoformat(row["last_roundtrip_at"])
            if roundtrip.tzinfo is None: roundtrip = roundtrip.replace(tzinfo=timezone.utc)
            degraded += max(0.0, (right-max(left, roundtrip)).total_seconds())
    # Reconnects create overlapping sessions; merge them so a flapping link
    # cannot report more online time than the window actually contains.
    online_intervals.sort()
    merged: list[tuple[datetime, datetime]] = []
    for left, right in online_intervals:
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    online = min(86400.0, sum((right-left).total_seconds() for left, right in merged))
    return {"online_seconds": online, "offline_seconds": 86400.0-online,
            "online_ratio": online/86400.0, "sessions": len(rows),
            "degraded_minutes": degraded/60.0, "source": "device_sessions",
            **{key: request_data[key] for key in ("p50_ms", "p95_ms", "error_rate", "samples")}}


@app.get("/api/devices/{device_id}/sessions")
async def device_sessions(device_id: str, range: str = "15m", _: None = Depends(verify_token)) -> list[dict[str, str]]:
    if range != "15m":
        raise HTTPException(400, "Only range=15m is supported")
    now_dt = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    rows = database.fetch_all("SELECT * FROM device_sessions WHERE device_id = ?", (device_id,))
    result = []
    for offset in (15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1):
        minute_start = now_dt-timedelta(minutes=offset)
        minute_end = minute_start+timedelta(minutes=1)
        state = "offline"
        for row in rows:
            started = datetime.fromisoformat(row["started_at"])
            ended = datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else now_dt
            if started.tzinfo is None: started = started.replace(tzinfo=timezone.utc)
            if ended.tzinfo is None: ended = ended.replace(tzinfo=timezone.utc)
            if started < minute_end and ended > minute_start:
                if state == "offline":
                    state = "degraded"
                if row["last_roundtrip_at"]:
                    ping = datetime.fromisoformat(row["last_roundtrip_at"])
                    if ping.tzinfo is None: ping = ping.replace(tzinfo=timezone.utc)
                    if ping >= minute_end: state = "online"
        result.append({"minute": minute_start.strftime("%H:%M"), "state": state})
    return result


@app.post("/api/runtime/kill-switch")
async def set_kill_switch(request: KillSwitchRequest, _: None = Depends(verify_token)) -> dict[str, bool]:
    global kill_switch
    kill_switch = request.on
    action = "kill_switch_on" if request.on else "kill_switch_off"
    if request.on:
        timestamp = datetime.now(timezone.utc).isoformat()
        database.execute("UPDATE device_commands SET status='expired' WHERE status='pending'")
        database.execute("""UPDATE permission_grants SET revoked_at=?, revoked_reason='kill_switch'
                            WHERE revoked_at IS NULL AND consumed_at IS NULL""", (timestamp,))
    service.audit(None, None, action, {})
    return {"on": kill_switch}


@app.get("/api/runtime/kill-switch")
async def get_kill_switch(_: None = Depends(verify_token)) -> dict[str, bool]:
    return {"on": kill_switch}


@app.post("/api/devices/{device_id}/upgrade")
async def device_upgrade(device_id: str, _: None = Depends(verify_token)) -> None:
    raise HTTPException(501, "设备升级未接入")


@app.post("/api/devices/{device_id}/config")
async def device_config(device_id: str, _: None = Depends(verify_token)) -> None:
    raise HTTPException(501, "设备配置未接入")


@app.post("/api/rce/request")
async def rce_request(_: None = Depends(verify_token)) -> None:
    raise HTTPException(501, "远程代码执行未接入")


@app.post("/api/device/{device_id}/command")
async def command_device(
    device_id: str,
    request: DeviceCommandRequest,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    if not settings.personal_mode:
        raise HTTPException(403, "Personal mode is disabled")
    allowed = {"vibrate", "show_notification", "open_url"}
    if request.action not in allowed:
        raise HTTPException(400, "Unsupported device action")
    device = database.fetch_one("SELECT id FROM devices WHERE id = ?", (device_id,))
    if not device:
        raise HTTPException(404, "Device not found")
    command = await service.queue_device_command(
        device_id,
        request.action,
        request.payload,
    )
    service.audit(
        None,
        None,
        "device_command",
        {
            "device_id": device_id,
            "action": request.action,
            "command_id": command["command_id"],
            "status": command["status"],
        },
    )
    return command


@app.post("/api/device/{device_id}/command-ack")
async def acknowledge_command(
    device_id: str,
    request: DeviceCommandAckRequest,
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    if not service.acknowledge_device_command(
        device_id,
        request.command_id,
        request.success,
        request.error,
        request.result,
    ):
        raise HTTPException(404, "Command not found")
    return {"ok": True}


@app.websocket("/ws/device/{device_id}")
async def device_socket(websocket: WebSocket, device_id: str, token: str = "") -> None:
    if settings.device_token and not secrets.compare_digest(token, settings.device_token):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    session_id = str(uuid.uuid4())
    session_started = datetime.now(timezone.utc).isoformat()
    database.execute(
        "INSERT INTO device_sessions(id, device_id, started_at) VALUES (?, ?, ?)",
        (session_id, device_id, session_started),
    )
    connected_devices[device_id] = websocket
    service.touch_device(device_id)
    await websocket.send_json({"type": "connected", "device_id": device_id})
    await service.deliver_pending_commands(device_id)
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "heartbeat":
                device = service.update_device_status(
                    device_id,
                    battery_level=message.get("battery_level"),
                    network_type=message.get("network_type"),
                    app_version=message.get("app_version"),
                    storage_free_mb=message.get("storage_free_mb"),
                    charging=message.get("charging"),
                    tts_enabled=message.get("tts_enabled"),
                    tts_ready=message.get("tts_ready"),
                    tts_error=message.get("tts_error"),
                    sequence=message.get("sequence"),
                    current_ma=message.get("current_ma"),
                    cache_size=message.get("cache_size"),
                    queue_size=message.get("queue_size"),
                    sensing_state=message.get("sensing_state"),
                    model=message.get("model"),
                    battery_mah=message.get("battery_mah"),
                    cache_limit=message.get("cache_limit"),
                    input_caps=message.get("input_caps"),
                    sensing_policy=message.get("sensing_policy"),
                )
                service.record_device_roundtrip(device_id)
                await websocket.send_json({
                    "type": "ack",
                    "request_id": message.get("request_id"),
                    "sequence": message.get("sequence"),
                    "device": device,
                })
            elif message.get("type") == "command_ack":
                accepted = service.acknowledge_device_command(
                    device_id,
                    str(message.get("command_id") or ""),
                    bool(message.get("success", False)),
                    message.get("error"),
                    message.get("result") or {},
                )
                await websocket.send_json({
                    "type": "ack",
                    "request_id": message.get("request_id"),
                    "command_id": message.get("command_id"),
                    "accepted": accepted,
                })
            elif message.get("type") in {
                "audio_start",
                "audio_chunk",
                "audio_end",
            }:
                response = await service.ingest_audio_message(device_id, message)
                if message.get("type") != "audio_chunk":
                    await websocket.send_json(response)
            else:
                service.touch_device(device_id)
                await websocket.send_json({
                    "type": "ack",
                    "request_id": message.get("request_id"),
                    "sequence": message.get("sequence"),
                })
    except WebSocketDisconnect:
        # The client may close with code 1000 while reconnecting. That is a
        # normal lifecycle event and must not trigger a second close frame.
        pass
    finally:
        database.execute(
            "UPDATE device_sessions SET ended_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), session_id),
        )
        if connected_devices.get(device_id) is websocket:
            connected_devices.pop(device_id, None)


app.mount(
    "/console",
    StaticFiles(directory=CONSOLE_DIR, html=True),
    name="console",
)
