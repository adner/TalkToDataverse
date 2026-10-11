"""FastAPI composition root: L0 capture, the L1 decision loop and L2 flights over AG-UI."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from agent_framework_ag_ui import add_agent_framework_fastapi_endpoint
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from sot_intent.decision import DecisionModel
from sot_streamer.contracts import StreamerProjection

from .config import AppConfig, load_config
from .diagnostics import DiagnosticLog
from .flight_workflow import FlightWorkflow
from .flights import FlightRuntime, build_flights
from .input_runtime import InputRuntime
from .presentation import NativePresentationHub
from .thread_context import ThreadContextBroker

WORKSPACE_ROOT = Path(__file__).parents[2].resolve()


class UtteranceRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2_000)
    session_id: str = Field(default="default", min_length=1, max_length=128)


class PressRequest(BaseModel):
    session_id: str = Field(default="default", min_length=1, max_length=128)


class AnswerRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    thread: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=2_000)


class ReleaseRequest(BaseModel):
    utterance_id: str = Field(min_length=1, max_length=128)


class AudioInitializeRequest(BaseModel):
    device_id: int | None = Field(default=None, ge=0)


class AudioSettingsRequest(BaseModel):
    device_id: int | None = Field(default=None, ge=0)
    preroll_ms: int = Field(default=600, ge=0, le=5_000)
    tail_flush_ms: int = Field(default=150, ge=0, le=5_000)
    minimum_clip_ms: int = Field(default=150, ge=0, le=5_000)


class DiagnosticsSettingsRequest(BaseModel):
    enabled: bool | None = None
    level: str | None = None
    console: bool | None = None
    file: bool | None = None
    file_path: str | None = Field(default=None, min_length=1, max_length=500)
    max_file_bytes: int | None = None
    backup_count: int | None = None
    memory_events: int | None = None
    include_transcripts: bool | None = None
    include_intent_payloads: bool | None = None
    include_tool_payloads: bool | None = None
    include_responses: bool | None = None


def create_app(
    settings: AppConfig,
    *,
    flight_runtime: FlightRuntime | Any | None = None,
    decision_model: DecisionModel | None = None,
    workspace_root: Path = WORKSPACE_ROOT,
) -> FastAPI:
    """Wire the layers together. Tests pass fakes for the flights and the decision model."""

    diagnostics = DiagnosticLog(settings.diagnostics)
    native_hub = NativePresentationHub(diagnostics)
    input_runtime = InputRuntime(settings, StreamerProjection(), native_hub, diagnostics)
    if decision_model is not None:
        input_runtime._decision_model = decision_model
    thread_context = ThreadContextBroker(
        timeout_seconds=settings.l2.thread_context_timeout_seconds,
        diagnostics=diagnostics,
    )

    async def publish_l2(event: dict[str, object]) -> None:
        await native_hub.publish("l2", event)

    flights = flight_runtime or build_flights(
        settings.l2,
        threads=input_runtime.threads,
        thread_context=thread_context,
        publish=publish_l2,
    )
    input_runtime.questions = flights.questions
    workflow = FlightWorkflow(flights, diagnostics)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        warming = asyncio.create_task(flights.warm())
        try:
            yield
        finally:
            await input_runtime.close()
            if not warming.done():
                warming.cancel()
            await flights.close()
            diagnostics.close()

    app = FastAPI(title="SpeedOfThought", version="0.3.0", lifespan=lifespan)
    app.state.input_runtime = input_runtime
    app.state.flights = flights
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.l3.origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type", "authorization"],
    )

    @app.middleware("http")
    async def diagnostic_http_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            diagnostics.record(
                "ERROR",
                "SYSTEM",
                "http.request.failed",
                f"{request.method} {request.url.path} raised an unhandled exception",
                duration_ms=(time.perf_counter() - started) * 1_000,
                data={"errorType": type(exc).__name__, "error": str(exc)},
            )
            raise
        diagnostics.record(
            "WARNING" if response.status_code >= 400 else "DEBUG",
            "SYSTEM",
            "http.request.completed",
            f"{request.method} {request.url.path} returned {response.status_code}",
            duration_ms=(time.perf_counter() - started) * 1_000,
            data={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
            },
        )
        return response

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "layers": {
                "l0": {"mode": "live", "model": "nvidia/nemotron-3.5-asr-streaming-0.6b"},
                "l1": {"mode": "decision", "model": "Microsoft-Decision-1"},
                "l2": {"mode": "flights", "model": settings.l2.model},
                "l3": {"mode": "ag-ui", "framework": "CopilotKit"},
            },
            "executionSurfaces": ["record-tools", "thread-context"],
            "environment": {
                "name": settings.l2.environment_name,
                "url": settings.l2.dataverse_url,
            },
            "diagnostics": {
                "enabled": diagnostics.settings()["enabled"],
                "level": diagnostics.settings()["level"],
            },
        }

    @app.get("/api/presentation/snapshot")
    async def presentation_snapshot() -> dict[str, object]:
        return {"schemaVersion": 2, "l0": input_runtime.streamer_projection.current_snapshot()}

    @app.get("/api/presentation/events")
    async def presentation_events() -> StreamingResponse:
        async def stream() -> AsyncGenerator[str, None]:
            async for envelope in native_hub.subscribe():
                yield f"data: {json.dumps(envelope, separators=(',', ':'))}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/files/open")
    async def open_workspace_file(
        path: str = Query(min_length=3, max_length=4_096),
    ) -> FileResponse:
        """Open a local artifact without allowing reads outside this workspace."""

        requested = Path(path)
        resolved = (
            requested.resolve()
            if requested.is_absolute()
            else (workspace_root / requested).resolve()
        )
        try:
            resolved.relative_to(workspace_root)
        except ValueError as exc:
            raise HTTPException(
                status_code=403, detail="The file is outside this workspace"
            ) from exc
        if not resolved.exists():
            raise HTTPException(status_code=404, detail="The file does not exist")
        if not resolved.is_file():
            raise HTTPException(status_code=400, detail="The path does not point to a file")
        return FileResponse(resolved, filename=resolved.name, content_disposition_type="inline")

    @app.post("/api/utterance")
    async def utterance(request: UtteranceRequest) -> dict[str, object]:
        """Typed text: one hold, cut into one segment that L3 launches as a flight."""

        try:
            return await input_runtime.typed(request.text.strip(), request.session_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/audio/devices")
    async def audio_devices() -> dict[str, object]:
        try:
            return await input_runtime.audio_devices()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/audio/settings")
    async def audio_settings() -> dict[str, object]:
        try:
            return await input_runtime.audio_settings()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/audio/settings")
    async def audio_settings_update(request: AudioSettingsRequest) -> dict[str, object]:
        try:
            return await input_runtime.configure_microphone(
                device_id=request.device_id,
                preroll_ms=request.preroll_ms,
                tail_flush_ms=request.tail_flush_ms,
                min_press_ms=request.minimum_clip_ms,
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/audio/initialize")
    async def audio_initialize(
        request: AudioInitializeRequest | None = None,
    ) -> dict[str, object]:
        try:
            return await input_runtime.initialize_microphone(request.device_id if request else None)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/audio/press")
    async def audio_press(request: PressRequest | None = None) -> dict[str, object]:
        try:
            session_id = request.session_id if request is not None else "default"
            return {"utteranceId": await input_runtime.press(session_id)}
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/audio/release")
    async def audio_release(request: ReleaseRequest) -> dict[str, object]:
        try:
            return await input_runtime.release(request.utterance_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/audio/cancel")
    async def audio_cancel() -> dict[str, object]:
        input_runtime.cancel()
        return {"status": "cancelled"}

    @app.post("/api/flights/answer")
    async def flight_answer(request: AnswerRequest) -> dict[str, object]:
        """Answer a flight's question from its card; spoken answers are routed by L1 instead."""

        if not flights.questions.answer(request.session_id, request.thread, request.text):
            raise HTTPException(status_code=404, detail="That thread is not waiting on a question")
        return {"status": "answered"}

    @app.post("/api/admin/reset")
    async def admin_reset(dry_run: bool = False) -> dict[str, object]:
        """Put Contoso Bikes back to the committed baseline (``sot-reset restore``)."""

        from sot_dataverse.reset import restore

        baseline = Path(settings.l2.schema_path).with_name("baseline.json")
        try:
            plan = await restore(flights.records.api, flights.schema, baseline, dry_run=dry_run)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"dryRun": dry_run, **plan.summary()}

    @app.get("/api/diagnostics/settings")
    async def diagnostics_settings() -> dict[str, object]:
        return diagnostics.settings()

    @app.post("/api/diagnostics/settings")
    async def diagnostics_settings_update(
        request: DiagnosticsSettingsRequest,
    ) -> dict[str, object]:
        try:
            return diagnostics.configure(request.model_dump(exclude_none=True))
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/diagnostics/events")
    async def diagnostics_events(
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=5_000),
        level: str | None = None,
        layer: str | None = None,
        trace: str | None = None,
        search: str | None = None,
    ) -> dict[str, object]:
        return diagnostics.query(
            after=after, limit=limit, level=level, layer=layer, trace=trace, search=search
        )

    @app.get("/api/diagnostics/export")
    async def diagnostics_export(trace: str | None = None) -> JSONResponse:
        suffix = f"-{trace[:24]}" if trace else ""
        return JSONResponse(
            diagnostics.export(trace),
            headers={"content-disposition": f'attachment; filename="sot-trace{suffix}.json"'},
        )

    @app.post("/api/diagnostics/clear")
    async def diagnostics_clear() -> dict[str, object]:
        return {"cleared": diagnostics.clear()}

    add_agent_framework_fastapi_endpoint(
        app=app, agent=workflow, path="/ag-ui", keepalive_seconds=15
    )
    return app


def create_default_app() -> FastAPI:
    """The app for ``config/sot.toml``; uvicorn calls this as a factory."""

    return create_app(load_config())
