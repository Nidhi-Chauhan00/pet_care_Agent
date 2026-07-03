# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Serve the reasoning_engine ``{class_method, input}`` contract over HTTP.

Exists to guarantee support for the Vertex AI Console Playground and Gemini
Enterprise (via ADK registration), which both invoke the engine through this
contract. Agent Engine forwards calls to ``/api/reasoning_engine`` (sync) and
``/api/stream_reasoning_engine`` (streaming); dispatch is limited to the
:class:`AdkApp` ``register_operations()`` methods so the wire output matches a
packaged Agent Engine.
"""

import inspect
import json
import os
import uuid

from fastapi import FastAPI, HTTPException, Request, encoders, responses
from google.adk.runners import Runner
from google.genai import types
import vertexai
from vertexai.agent_engines.templates.adk import AdkApp

from app.app_utils import services


def attach_reasoning_engine_routes(app: FastAPI) -> None:
    """Register reasoning_engine routes that dispatch to an AdkApp."""
    runtime: AdkApp | None = None
    streaming_methods: set[str] = set()
    sync_methods: set[str] = set()

    def get_runtime() -> AdkApp:
        nonlocal runtime, streaming_methods, sync_methods
        if runtime is None:
            from app.agent import app as adk_app

            if os.getenv("INTEGRATION_TEST", "").lower() in ("true", "1"):
                vertexai.init(
                    project=os.getenv("GOOGLE_CLOUD_PROJECT", "local-test-project"),
                    location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-east1"),
                )

            # Reuse the process-wide services so sessions created here are
            # visible to the adk_api and A2A paths, and vice versa (see services.py).
            runtime = AdkApp(
                app=adk_app,
                session_service_builder=services.get_session_service,
                artifact_service_builder=services.get_artifact_service,
            )
            runtime.set_up()
            operations = runtime.register_operations()
            streaming_methods = set(operations.get("stream", [])) | set(
                operations.get("async_stream", [])
            )
            sync_methods = set(operations.get("", [])) | set(
                operations.get("async", [])
            )
        return runtime

    def is_local_integration_test() -> bool:
        return os.getenv("INTEGRATION_TEST", "").lower() in ("true", "1")

    def get_local_runner() -> Runner:
        if runner := getattr(app.state, "runner", None):
            return runner

        from app.agent import app as adk_app

        return Runner(
            app=adk_app,
            session_service=services.get_session_service(),
            artifact_service=services.get_artifact_service(),
            auto_create_session=True,
        )

    async def local_stream_query(user_id: str, message: str, **_: object):
        """Local reasoning_engine-compatible stream path for tests without ADC."""
        runner = get_local_runner()
        content = types.Content(
            role="user", parts=[types.Part.from_text(text=message)]
        )
        async for event in runner.run_async(
            user_id=user_id,
            session_id=f"local-{uuid.uuid4()}",
            new_message=content,
        ):
            yield encoders.jsonable_encoder(event)

    def resolve_method(class_method: str, *, streaming: bool):
        rt = get_runtime()
        allowed = streaming_methods if streaming else sync_methods
        if class_method not in allowed:
            raise HTTPException(
                status_code=404,
                detail=f"Unsupported reasoning_engine method: {class_method!r}",
            )
        return getattr(rt, class_method)

    @app.post("/api/stream_reasoning_engine")
    async def stream_query(request: Request) -> responses.StreamingResponse:
        body = await request.json()
        if is_local_integration_test():
            if body["class_method"] != "async_stream_query":
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "Unsupported local reasoning_engine stream method: "
                        f"{body['class_method']!r}"
                    ),
                )

            async def local_generator():
                async for event in local_stream_query(**(body.get("input") or {})):
                    yield json.dumps(event) + "\n"

            return responses.StreamingResponse(
                content=local_generator(), media_type="application/json"
            )

        method = resolve_method(body["class_method"], streaming=True)

        async def generator():
            async for event in method(**(body.get("input") or {})):
                yield json.dumps(event) + "\n"

        return responses.StreamingResponse(
            content=generator(), media_type="application/json"
        )

    @app.post("/api/reasoning_engine")
    async def query(request: Request) -> responses.JSONResponse:
        body = await request.json()
        method = resolve_method(body["class_method"], streaming=False)
        kwargs = body.get("input") or {}
        output = (
            await method(**kwargs)
            if inspect.iscoroutinefunction(method)
            else method(**kwargs)
        )
        return responses.JSONResponse(
            content=encoders.jsonable_encoder({"output": output})
        )
