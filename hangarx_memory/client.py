"""Small stdlib HTTP client for the HangarX Cortex API.

The Hermes plugin keeps this dependency-free so user-installed providers work in
standard Hermes environments without extra pip installs.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


class CortexError(RuntimeError):
    """Raised when Cortex returns an HTTP or JSON-RPC error."""


def probe_health(base_url: str, timeout: float = 0.5) -> Optional[Dict[str, Any]]:
    """Hit the unauthenticated ``/health`` endpoint with a tight timeout.

    Returns the decoded ``data`` payload on success, ``None`` on any
    failure (connection refused, timeout, non-2xx, malformed JSON).
    Designed for cheap auto-detection of a local Cortex instance — keep
    the timeout small so agent startup never visibly stalls.

    Default 0.5s covers realistic local-stack latency (Cortex's /health
    probes FalkorDB + Postgres internally and can take 200–300ms even on
    localhost). Adjust via the provider's ``local_probe_timeout`` knob
    if your stack is faster or slower.

    The Cortex API serves this endpoint at both ``/health`` and
    ``/v1/health``; we use the root path because it's unauthenticated
    and shorter.
    """
    if not base_url:
        return None
    url = base_url.rstrip("/") + "/health"
    req = urllib_request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    try:
        with urllib_request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                return None
            raw = response.read()
    except Exception:
        return None
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    # Cortex wraps everything in ``{success: bool, data: {...}}``.
    if payload.get("success") is False:
        return None
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return None
    return data


class CortexClient:
    """HTTP/JSON-RPC client for Cortex REST and MCP endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        workspace_id: str = "",
        organization_id: str = "",
        auth_mode: str = "bearer",
        timeout: float = 15.0,
    ) -> None:
        self.base_url = (base_url or "https://cortex.hangarx.ai").rstrip("/")
        self.api_key = api_key
        self.workspace_id = workspace_id or ""
        self.organization_id = organization_id or ""
        self.auth_mode = (auth_mode or "bearer").lower()
        self.timeout = float(timeout or 15.0)

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.auth_mode == "x-api-key":
            headers["x-api-key"] = self.api_key
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.workspace_id:
            headers["x-workspace-id"] = self.workspace_id
        if self.organization_id:
            headers["x-organization-id"] = self.organization_id
        return headers

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        """Send a JSON request and return decoded JSON or text.

        ``path`` may be absolute-ish (``/v1/...``) or relative (``v1/...``).
        """
        normalized = "/" + path.lstrip("/")
        url = f"{self.base_url}{normalized}"
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib_request.Request(
            url,
            data=payload,
            headers=self._headers(),
            method=method.upper(),
        )
        try:
            with urllib_request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
        except urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise CortexError(f"Cortex API {exc.code} {method.upper()} {normalized}: {detail}") from exc
        except urllib_error.URLError as exc:
            raise CortexError(f"Cortex API request failed for {method.upper()} {normalized}: {exc.reason}") from exc

        if not raw:
            return {}
        text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

    # ---- Agent/memU memory -------------------------------------------------

    def remember(self, content: str, **kwargs: Any) -> Any:
        body = {
            "content": content,
            "workspaceId": kwargs.get("workspace_id") or self.workspace_id or None,
            "agentId": kwargs.get("agent_id") or None,
            "category": kwargs.get("category") or "conversation_insight",
            "priority": kwargs.get("priority") or "normal",
            "metadata": kwargs.get("metadata") or {},
        }
        return self.request("POST", "/v1/memory/remember", _compact(body))

    def remember_raw(self, content: str, **kwargs: Any) -> Any:
        body = {
            "content": content,
            "workspaceId": kwargs.get("workspace_id") or self.workspace_id or None,
            "agentId": kwargs.get("agent_id") or None,
            "category": kwargs.get("category") or "conversation_turn",
            "priority": kwargs.get("priority") or "normal",
            "metadata": kwargs.get("metadata") or {},
        }
        return self.request("POST", "/v1/memory/remember-raw", _compact(body))

    def recall(self, query: str, **kwargs: Any) -> Any:
        body = {
            "query": query,
            "workspaceId": kwargs.get("workspace_id") or self.workspace_id or None,
            "agentId": kwargs.get("agent_id") or None,
            "method": kwargs.get("method") or "hybrid",
            "limit": kwargs.get("limit") or 5,
            "filters": kwargs.get("filters") or None,
        }
        return self.request("POST", "/v1/memory/recall", _compact(body))

    def memory_context(self, query: str, **kwargs: Any) -> Any:
        body = {
            "query": query,
            "workspaceId": kwargs.get("workspace_id") or self.workspace_id or None,
            "agentId": kwargs.get("agent_id") or None,
        }
        return self.request("POST", "/v1/memory/context", _compact(body))

    def reflect(self, **kwargs: Any) -> Any:
        body = {
            "workspaceId": kwargs.get("workspace_id") or self.workspace_id or None,
            "agentId": kwargs.get("agent_id") or None,
        }
        return self.request("POST", "/v1/memory/reflect", _compact(body))

    def auto_promote(self, **kwargs: Any) -> Any:
        """Trigger Cortex memU's auto-promotion pass.

        Cortex distills raw conversation turns stored via ``remember_raw``
        into structured insights/facts. Match mem0's server-side
        fact-extraction pattern.
        """
        body = {
            "workspaceId": kwargs.get("workspace_id") or self.workspace_id or None,
            "agentId": kwargs.get("agent_id") or None,
            "limit": kwargs.get("limit") or 25,
        }
        return self.request("POST", "/v1/memory/auto-promote", _compact(body))

    # -- Introspection -------------------------------------------------------

    def memory_stats(self, **kwargs: Any) -> Any:
        """Return high-level counts: total items, categories, by priority.

        Wraps ``GET /v1/memory/stats``. Returns a dict shaped like::

            {
                "totalItems": int,
                "totalCategories": int,
                "itemsByPriority": {"high": int, "normal": int, ...},
                "categories": [...]
            }
        """
        params = _compact({
            "workspaceId": kwargs.get("workspace_id") or self.workspace_id or None,
            "agentId": kwargs.get("agent_id") or None,
        })
        return self.request(
            "GET", "/v1/memory/stats" + _querystring(params)
        )

    def memory_categories(self, **kwargs: Any) -> Any:
        """List memory categories with counts.

        Wraps ``GET /v1/memory/categories``. Cortex groups memories into
        named categories (user_fact, agent_instruction, ...) — this
        endpoint returns the list with item counts per category so the
        agent can answer "what kinds of things do you remember?".
        """
        params = _compact({
            "workspaceId": kwargs.get("workspace_id") or self.workspace_id or None,
            "agentId": kwargs.get("agent_id") or None,
        })
        return self.request(
            "GET", "/v1/memory/categories" + _querystring(params)
        )

    def memory_items(self, **kwargs: Any) -> Any:
        """List individual memory items.

        Wraps ``GET /v1/memory/items``. Returns the raw stored facts so
        the user can audit exactly what the agent has persisted about
        them. The agent_id filter narrows to a single agent's memory
        bank when the workspace is shared across agents.
        """
        params = _compact({
            "workspaceId": kwargs.get("workspace_id") or self.workspace_id or None,
            "agentId": kwargs.get("agent_id") or None,
        })
        return self.request(
            "GET", "/v1/memory/items" + _querystring(params)
        )

    def memory_item(self, item_id: str, **kwargs: Any) -> Any:
        """Get a single memory item by ID.

        Wraps ``GET /v1/memory/items/:itemId``. Pass ``track_access=True``
        to bump the item's access counter (used by Cortex's freshness +
        trust scoring).
        """
        if not item_id:
            raise CortexError("memory_item requires a non-empty item_id")
        params = _compact({
            "workspaceId": kwargs.get("workspace_id") or self.workspace_id or None,
            "agentId": kwargs.get("agent_id") or None,
            "trackAccess": "true" if kwargs.get("track_access") else None,
        })
        return self.request(
            "GET", f"/v1/memory/items/{item_id}" + _querystring(params)
        )

    def memory_category(self, category_id: str, **kwargs: Any) -> Any:
        """Get a single memory category with optional file content.

        Wraps ``GET /v1/memory/categories/:categoryId``. Pass
        ``include_content=True`` to also fetch the raw markdown file
        backing the category (Cortex stores categories as files in a
        per-agent directory).
        """
        if not category_id:
            raise CortexError("memory_category requires a non-empty category_id")
        params = _compact({
            "workspaceId": kwargs.get("workspace_id") or self.workspace_id or None,
            "agentId": kwargs.get("agent_id") or None,
            "includeContent": "true" if kwargs.get("include_content") else None,
        })
        return self.request(
            "GET", f"/v1/memory/categories/{category_id}" + _querystring(params)
        )

    def feedback(self, memory_id: str, helpful: bool, **kwargs: Any) -> Any:
        """Rate a memory/result as helpful or not.

        Wraps the Cortex MCP ``cortex_feedback`` tool which trains trust
        and reranking signals on the recalled memory. ``helpful=True``
        means the item was useful for the current task; ``False`` flags
        it for downranking. Optional ``comment`` adds free-form context.
        """
        args: Dict[str, Any] = {
            "memoryId": memory_id,
            "helpful": bool(helpful),
            "workspaceId": kwargs.get("workspace_id") or self.workspace_id or None,
        }
        comment = kwargs.get("comment")
        if comment:
            args["comment"] = comment
        return self.mcp_call_tool("cortex_feedback", _compact(args))

    # ---- GraphRAG / vectors ------------------------------------------------

    def ask_context(self, message: str, **kwargs: Any) -> Any:
        body = {
            "message": message,
            "workspaceId": kwargs.get("workspace_id") or self.workspace_id or None,
            "organizationId": kwargs.get("organization_id") or self.organization_id or None,
            "enhanced": kwargs.get("enhanced", True),
            "forceFullPipeline": kwargs.get("force_full_pipeline", False),
        }
        return self.request("POST", "/v1/ask/chat", _compact(body))

    def ask_context_stream(self, message: str, **kwargs: Any) -> Any:
        """Stream /v1/ask/chat/stream SSE events and return the assembled context.

        Cortex emits progressive events while it plans, retrieves, and
        builds the enhanced context. We collect them and return a single
        merged dict that looks like ``ask_context`` did so callers don't
        have to branch. ``early_callback`` (if provided) is invoked with
        each text chunk as it arrives — useful for low-latency UIs.

        Falls back to ``ask_context`` if streaming fails for any reason.
        """
        early_callback = kwargs.pop("early_callback", None)
        body = {
            "message": message,
            "workspaceId": kwargs.get("workspace_id") or self.workspace_id or None,
            "organizationId": kwargs.get("organization_id") or self.organization_id or None,
            "enhanced": kwargs.get("enhanced", True),
            "forceFullPipeline": kwargs.get("force_full_pipeline", False),
        }
        payload = _compact(body)
        url = f"{self.base_url}/v1/ask/chat/stream"
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        req = urllib_request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        context_parts: list = []
        citations: list = []
        confidence: Any = None
        try:
            with urllib_request.urlopen(req, timeout=self.timeout) as response:
                buffer = ""
                while True:
                    chunk = response.readline()
                    if not chunk:
                        break
                    line = chunk.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                    if not line:
                        if buffer:
                            self._consume_sse_event(
                                buffer, context_parts, citations, early_callback
                            )
                            buffer = ""
                        continue
                    if line.startswith(":"):
                        continue  # SSE comment
                    if line.startswith("data:"):
                        buffer += line[5:].lstrip()
                        continue
                    if line.startswith("event:"):
                        # We don't currently key off event names — the
                        # JSON payload carries enough type info.
                        continue
                if buffer:
                    self._consume_sse_event(buffer, context_parts, citations, early_callback)
        except (urllib_error.HTTPError, urllib_error.URLError, TimeoutError):
            # Fall back to the non-streaming endpoint — preserves behavior
            # if the server doesn't have /chat/stream wired up.
            return self.ask_context(message, **kwargs)

        if not context_parts and not citations:
            # Empty stream → try non-streaming as last resort
            return self.ask_context(message, **kwargs)

        result = {
            "context": "".join(context_parts).strip(),
            "citations": citations,
        }
        if confidence is not None:
            result["confidence"] = confidence
        return {"result": result}

    @staticmethod
    def _consume_sse_event(raw: str, context_parts: list, citations: list, early_callback) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        # The stream sends a mix of token events, context snapshots, and
        # citation events. Be permissive about the shape since Cortex
        # versions differ slightly.
        chunk = payload.get("delta") or payload.get("text") or payload.get("content")
        if isinstance(chunk, str) and chunk:
            context_parts.append(chunk)
            if callable(early_callback):
                try:
                    early_callback(chunk)
                except Exception:
                    pass
        ctx = payload.get("context") or payload.get("contextSummary")
        if isinstance(ctx, str) and ctx and ctx not in context_parts:
            context_parts.append(ctx)
        cits = payload.get("citations") or payload.get("sources")
        if isinstance(cits, list):
            for cit in cits:
                if cit and cit not in citations:
                    citations.append(cit)

    def ask_answer(self, message: str, **kwargs: Any) -> Any:
        body = {
            "message": message,
            "workspaceId": kwargs.get("workspace_id") or self.workspace_id or None,
            "organizationId": kwargs.get("organization_id") or self.organization_id or None,
            "expanded": kwargs.get("expanded", False),
            "format": kwargs.get("format", "json"),
            "history": kwargs.get("history") or None,
        }
        return self.request("POST", "/v1/ask/chat/answer", _compact(body))

    def search_documents(self, query: str, **kwargs: Any) -> Any:
        body = {
            "query": query,
            "workspaceId": kwargs.get("workspace_id") or self.workspace_id or None,
            "limit": kwargs.get("limit") or 5,
            "filters": kwargs.get("filters") or None,
        }
        return self.request("POST", "/v1/search/vectors/search", _compact(body))

    # ---- MCP ---------------------------------------------------------------

    def mcp_list_tools(self) -> Any:
        return self._mcp_request("tools/list", {})

    def mcp_call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        return self._mcp_request("tools/call", {"name": name, "arguments": arguments or {}})

    def _mcp_request(self, method: str, params: Dict[str, Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        result = self.request("POST", "/mcp", payload)
        if isinstance(result, dict) and result.get("error"):
            message = result.get("error", {}).get("message") or result["error"]
            raise CortexError(f"Cortex MCP {method} failed: {message}")
        return result.get("result", result) if isinstance(result, dict) else result


def _compact(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop keys whose values are None, preserving false/zero values."""
    return {key: value for key, value in data.items() if value is not None}


def _querystring(params: Dict[str, Any]) -> str:
    """Encode params as ``?key=value&...`` or return empty string.

    Uses urllib.parse.urlencode under the hood so values are properly
    percent-encoded. Keys with empty-string values are dropped (Cortex's
    z.string().optional() validators treat them the same as missing).
    """
    if not params:
        return ""
    filtered = {k: str(v) for k, v in params.items() if v not in (None, "")}
    if not filtered:
        return ""
    return "?" + urllib_parse.urlencode(filtered)
