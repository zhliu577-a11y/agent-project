"""External stdio tool runtime.

The harness owns process startup, JSON-RPC framing, timeouts, and cleanup.
Plugin implementations may be written in any language as long as they expose
one JSON object per stdout line and keep diagnostic output on stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.errors import DeclaredPluginError, PluginError, ToolError
from core.tool import Tool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExternalToolDefinition:
    """Static tool metadata declared in plugin.json."""

    name: str
    description: str
    parameters: dict[str, Any]


class ExternalRpcError(Exception):
    """Remote JSON-RPC error returned by an external plugin."""

    def __init__(self, code: object, message: str) -> None:
        super().__init__(message)
        self.code = code


class StdioJsonRpcHost:
    """Lazily start and own one external plugin process."""

    def __init__(
        self,
        *,
        plugin_name: str,
        plugin_dir: Path,
        command: str,
        args: list[str],
        timeout: float,
    ) -> None:
        self._plugin_name = plugin_name
        self._plugin_dir = plugin_dir
        self._command = command
        self._args = args
        self._timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._lock = asyncio.Lock()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        async with self._lock:
            try:
                result = await self._request(
                    "tools/call",
                    {"name": name, "arguments": arguments},
                )
            except ExternalRpcError as exc:
                if isinstance(exc.code, str) and exc.code:
                    raise DeclaredPluginError(exc.code, str(exc)) from exc
                raise ToolError(str(exc)) from exc
        return _normalize_result(result, plugin_name=self._plugin_name, tool_name=name)

    async def close(self) -> None:
        async with self._lock:
            await self._terminate()

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        request_id = self._next_id
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )

        try:
            async with asyncio.timeout(self._timeout):
                process = await self._ensure_process()
                if process.stdin is None or process.stdout is None:
                    raise PluginError("external plugin pipes are unavailable")
                process.stdin.write(encoded)
                await process.stdin.drain()

                while True:
                    line = await process.stdout.readline()
                    if not line:
                        raise PluginError(
                            f"external plugin process exited unexpectedly "
                            f"(returncode={process.returncode})"
                        )
                    try:
                        response = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise PluginError(
                            "external plugin wrote a non-JSON line to stdout; "
                            "stdout is reserved for the protocol"
                        ) from exc
                    if not isinstance(response, dict):
                        raise PluginError("external plugin response must be a JSON object")

                    response_id = response.get("id")
                    if response_id is None and "id" not in response:
                        continue
                    if response_id != request_id:
                        raise PluginError(
                            f"external plugin returned mismatched response id "
                            f"{response_id!r}, expected {request_id!r}"
                        )
                    if "error" in response:
                        raise _rpc_error(response["error"])
                    return response.get("result")
        except TimeoutError as exc:
            await self._terminate()
            raise ToolError(
                f"external tool '{self._plugin_name}.{method}' timed out after {self._timeout:g}s"
            ) from exc
        except ExternalRpcError:
            raise
        except Exception as exc:
            await self._terminate()
            if isinstance(exc, PluginError):
                raise
            raise PluginError(
                f"external plugin '{self._plugin_name}' request failed: {exc}"
            ) from exc

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        process = self._process
        if process is not None and process.returncode is None:
            return process
        if process is not None:
            await self._terminate()

        try:
            process = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                cwd=str(self._plugin_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise PluginError(
                f"external plugin '{self._plugin_name}' could not start '{self._command}': {exc}"
            ) from exc

        self._process = process
        if process.stderr is not None:
            self._stderr_task = asyncio.create_task(
                _drain_stderr(self._plugin_name, process.stderr)
            )
        logger.info(
            "External plugin process started: %s (pid=%s, command=%s)",
            self._plugin_name,
            process.pid,
            self._command,
        )
        return process

    async def _terminate(self) -> None:
        process = self._process
        stderr_task = self._stderr_task
        self._process = None
        self._stderr_task = None

        if process is not None:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
                with contextlib.suppress(Exception):
                    await process.stdin.wait_closed()

            if process.returncode is None:
                try:
                    async with asyncio.timeout(2):
                        await process.wait()
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        async with asyncio.timeout(2):
                            await process.wait()
                    except TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                        await process.wait()
            logger.info("External plugin process stopped: %s", self._plugin_name)

        if stderr_task is not None:
            await stderr_task


class ExternalTool(Tool):
    """One tool exposed by a shared external plugin process."""

    def __init__(
        self,
        host: StdioJsonRpcHost,
        definition: ExternalToolDefinition,
    ) -> None:
        self._host = host
        self._definition = definition

    @property
    def name(self) -> str:
        return self._definition.name

    @property
    def description(self) -> str:
        return self._definition.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._definition.parameters

    async def execute(self, **kwargs: Any) -> Any:
        return await self._host.call_tool(self.name, kwargs)

    async def close(self) -> None:
        await self._host.close()


def _rpc_error(raw: object) -> ExternalRpcError:
    if not isinstance(raw, dict):
        return ExternalRpcError(None, f"invalid JSON-RPC error: {raw!r}")
    code = raw.get("code")
    data = raw.get("data")
    if isinstance(data, dict) and isinstance(data.get("code"), str):
        code = data["code"]
    message = raw.get("message")
    if not isinstance(message, str) or not message:
        message = f"external plugin returned error {code!r}"
    return ExternalRpcError(code, message)


def _normalize_result(result: Any, *, plugin_name: str, tool_name: str) -> Any:
    if isinstance(result, str):
        return result
    if result is None:
        return ""
    if isinstance(result, dict):
        if result.get("isError") is True:
            text = _content_text(result.get("content"))
            raise ToolError(text or f"external tool '{plugin_name}.{tool_name}' failed")
        if "content" in result:
            return _content_text(result["content"])
    return json.dumps(result, ensure_ascii=False)


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


async def _drain_stderr(
    plugin_name: str,
    stream: asyncio.StreamReader,
) -> None:
    while line := await stream.readline():
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            logger.info("External plugin %s stderr: %s", plugin_name, text)
