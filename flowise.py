"""
title: Flowise Integration for OpenWebUI
version: 1.1.3
description: Flowise integration with dynamic model loading, streaming, and UI feedback
Requirements:
  - Flowise API URL (set via FLOWISE_API_URL)
  - Flowise API Key (set via FLOWISE_API_KEY)
"""

from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List, Union, Generator, Iterator, Callable, Awaitable
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import os
import time
import asyncio


class Pipe:
    class Valves(BaseModel):
        flowise_url: str = Field(
            default=os.getenv("FLOWISE_API_URL", ""),
            description="Flowise API base URL",
        )
        flowise_api_key: str = Field(
            default=os.getenv("FLOWISE_API_KEY", ""),
            description="Flowise API key for authentication",
        )
        flow_type_filter: str = Field(
            default=os.getenv("FLOWISE_FLOW_TYPE", "AGENTFLOW"),
            description="Flowise flow type filter (e.g., AGENTFLOW, ALL)",
        )
        include_history: bool = Field(
            default=bool(int(os.getenv("FLOWISE_INCLUDE_HISTORY", "1") or 1)),
            description="Include prior history in requests",
        )
        enable_status_indicator: bool = Field(
            default=True,
            description="Enable status indicators in the UI"
        )
        emit_interval: float = Field(
            default=1.0,
            description="Interval between status emissions (seconds)"
        )
        timeout: int = Field(
            # Configurable via FLOWISE_TIMEOUT; defaults to 600 seconds
            default=int(os.getenv("FLOWISE_TIMEOUT", "600")) if os.getenv("FLOWISE_TIMEOUT", "").isdigit() else 600,
            description="Request timeout in seconds"
        )
        connect_timeout: int = Field(
            default=int(os.getenv("FLOWISE_CONNECT_TIMEOUT", "15")) if os.getenv("FLOWISE_CONNECT_TIMEOUT", "").isdigit() else 15,
            description="Connection timeout in seconds"
        )
        read_timeout: int = Field(
            default=int(os.getenv("FLOWISE_READ_TIMEOUT", "600")) if os.getenv("FLOWISE_READ_TIMEOUT", "").isdigit() else 600,
            description="Read timeout in seconds (non-streaming)"
        )
        read_timeout_stream: int = Field(
            default=int(os.getenv("FLOWISE_READ_TIMEOUT_STREAM", "1800")) if os.getenv("FLOWISE_READ_TIMEOUT_STREAM", "").isdigit() else 1800,
            description="Read timeout in seconds for streaming responses"
        )
        debug_mode: bool = Field(
            default=bool(int(os.getenv("FLOWISE_DEBUG_MODE", "0") or 0)),
            description="Enable debug logging"
        )
        allow_remote_file_urls: bool = Field(
            default=bool(int(os.getenv("FLOWISE_ALLOW_REMOTE_FILE_URLS", "0") or 0)),
            description="Allow passing remote http(s) URLs as uploads (experimental)"
        )
        default_upload_type: str = Field(
            default=os.getenv("FLOWISE_DEFAULT_UPLOAD_TYPE", "file:full"),
            description="Default Flowise upload type when not provided"
        )
        force_override_history: bool = Field(
            default=bool(int(os.getenv("FLOWISE_FORCE_OVERRIDE_HISTORY", "1") or 1)),
            description="Force Flowise to replace stored conversation with provided history"
        )
        history_limit: int = Field(
            default=int(os.getenv("FLOWISE_HISTORY_LIMIT", "10")),
            description="Max number of prior user/assistant messages to include in history"
        )
        retry_total: int = Field(
            default=int(os.getenv("FLOWISE_RETRY_TOTAL", "3")),
            description="Total number of retries for HTTP requests",
        )
        retry_backoff: float = Field(
            default=float(os.getenv("FLOWISE_RETRY_BACKOFF", "0.3")),
            description="Backoff factor for retries",
        )
        user_agent: str = Field(
            default=os.getenv("FLOWISE_USER_AGENT", "OpenWebUI-Flowise-Connector/1.1"),
            description="User-Agent header value",
        )

    def __init__(self):
        self.type = "manifold"
        self.id = "flowise_chat"
        self.name = "Flowise Integration"
        self.valves = self.Valves()
        self.last_emit_time = 0
        url = (self.valves.flowise_url or "").strip()
        if url.endswith("/"):
            url = url[:-1]
        if url and not (url.startswith("http://") or url.startswith("https://")):
            print("⚠️ Invalid Flowise URL. It should start with http:// or https://")
        self.valves.flowise_url = url
        if not self.valves.flowise_url:
            print("⚠️ Please set your Flowise URL using the FLOWISE_API_URL environment variable")
        if not self.valves.flowise_api_key:
            print("⚠️ Please set your Flowise API key using the FLOWISE_API_KEY environment variable")
        self.session = requests.Session()
        retry = Retry(
            total=self.valves.retry_total,
            backoff_factor=self.valves.retry_backoff,
            status_forcelist=[429, 502, 503, 504],
            allowed_methods={"GET", "POST"},
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _get_request_timeout(self, stream_enabled: bool = False):
        if os.getenv("FLOWISE_TIMEOUT"):
            return (self.valves.timeout, self.valves.timeout)
        connect_t = self.valves.connect_timeout
        read_t = self.valves.read_timeout_stream if stream_enabled else self.valves.read_timeout
        return (connect_t, read_t)

    def _build_headers(self, stream_enabled: bool) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Authorization": f"Bearer {self.valves.flowise_api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": self.valves.user_agent,
        }
        headers["Accept"] = "text/event-stream; charset=utf-8" if stream_enabled else "application/json; charset=utf-8"
        return headers

    def pipes(self) -> List[Dict[str, str]]:
        if not self.valves.flowise_api_key or not self.valves.flowise_url:
            return [
                {
                    "id": "error",
                    "name": "❌ Missing API configuration - Please set FLOWISE_API_URL and FLOWISE_API_KEY",
                }
            ]

        try:
            headers = {"Authorization": f"Bearer {self.valves.flowise_api_key}", "Content-Type": "application/json"}
            params: Dict[str, str] = {}
            flow_type = (self.valves.flow_type_filter or "").strip()
            if flow_type and flow_type.upper() != "ALL":
                params["type"] = flow_type
            url = f"{self.valves.flowise_url}/api/v1/chatflows"
            t0 = time.perf_counter()
            response = self.session.get(
                url,
                headers=headers,
                params=params or None,
                timeout=self._get_request_timeout(stream_enabled=False),
            )
            response.raise_for_status()
            response.encoding = "utf-8"
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            chatflows = response.json()
            if self.valves.debug_mode:
                count = len(chatflows) if isinstance(chatflows, list) else "unknown"
                print(f"Debug - GET {response.request.url} status={response.status_code} count={count} elapsed_ms={elapsed_ms}")

            if not isinstance(chatflows, list):
                return [
                    {
                        "id": "error",
                        "name": "⚠️ Unexpected response when listing chatflows",
                    }
                ]
            return [
                {
                    "id": flow.get("id", ""),
                    "name": flow.get("name", "Unnamed Flow"),
                }
                for flow in chatflows
                if isinstance(flow, dict)
            ]

        except requests.exceptions.Timeout:
            return [
                {
                    "id": "timeout_error",
                    "name": "⏰ Connection timeout - Check your Flowise URL and network",
                }
            ]
        except requests.exceptions.ConnectionError:
            return [
                {
                    "id": "connection_error",
                    "name": "🔌 Connection failed - Is Flowise running?",
                }
            ]
        except Exception as e:
            error_msg = f"⚠️ Error loading chatflows: {str(e)}"
            if self.valves.debug_mode:
                print(f"Debug - pipes() error: {e}")
            return [
                {
                    "id": "error",
                    "name": error_msg,
                }
            ]

    async def emit_status(
        self,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]],
        level: str,
        message: str,
        done: bool = False,
    ):
        """Emit status updates to the UI"""
        current_time = time.time()
        if (
            __event_emitter__
            and self.valves.enable_status_indicator
            and (current_time - self.last_emit_time >= self.valves.emit_interval or done)
        ):
            try:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "status": "complete" if done else "in_progress",
                            "level": level,
                            "description": message,
                            "done": done,
                        },
                    }
                )
                self.last_emit_time = current_time
            except Exception as e:
                if self.valves.debug_mode:
                    print(f"Debug - emit_status error: {e}")

    def _process_message_content(self, message: dict) -> str:
        content = message.get("content", "")
        
        if isinstance(content, list):
            # Handle structured content (e.g., mixed text/image content)
            processed_content = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        processed_content.append(item.get("text", ""))
                    elif item.get("type") == "image_url":
                        # For now, we'll add a placeholder for images
                        processed_content.append("[Image content]")
                else:
                    processed_content.append(str(item))
            # Join with newlines to preserve semantic breaks when present
            return "\n\n".join([str(p) for p in processed_content if str(p)])
        
        return str(content) if content else ""

    

    def _split_messages_for_flowise(self, messages: List[dict], metadata: Optional[dict], body: Optional[dict]) -> (List[dict], dict):
        if not messages:
            return [], {}

        current_message = messages[-1]
        prior_messages = messages[:-1]

        return prior_messages, current_message

    def _build_history_payload(self, messages: List[dict]) -> List[Dict[str, str]]:
        history: List[Dict[str, str]] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = self._process_message_content(msg).strip()
            if not content:
                continue
            # Only map user/assistant roles into Flowise-visible history to maintain turns
            if role not in {"user", "assistant"}:
                continue
            # Normalize roles to Flowise history expectations
            if role not in {"user", "assistant"}:
                role = "user"
            flowise_role = "userMessage" if role == "user" else "apiMessage"
            history.append({"role": flowise_role, "content": content})
        return history

    def _trim_messages_for_history(self, messages: List[dict], limit: int) -> List[dict]:
        if limit <= 0 or not messages:
            return []
        filtered = [m for m in messages if m.get("role") in {"user", "assistant"}]
        if not filtered:
            return []
        tail = filtered[-limit:]
        return tail

    def _extract_uploads(self, body: dict, messages: List[dict]) -> List[Dict[str, Any]]:
        uploads: List[Dict[str, Any]] = []

        def normalize_upload(u: dict) -> Optional[Dict[str, Any]]:
            if not isinstance(u, dict):
                return None
            data_val = u.get("data") or u.get("url") or u.get("data_url") or u.get("base64") or u.get("b64")
            if not isinstance(data_val, str):
                return None
            is_data_uri = data_val.startswith("data:")
            is_http_uri = data_val.startswith("http://") or data_val.startswith("https://")
            if not is_data_uri and is_http_uri and not self.valves.allow_remote_file_urls:
                return None
            # Allow raw base64 strings when mime provided by user
            if not is_data_uri and not is_http_uri:
                mime_hint = u.get("mime") or u.get("mimetype")
                if isinstance(mime_hint, str) and "/" in mime_hint:
                    # Construct a data URL
                    data_val = f"data:{mime_hint};base64,{data_val}"
                    is_data_uri = True

            # Normalize type synonyms
            type_val = u.get("type") or self.valves.default_upload_type
            if type_val == "file":
                type_val = self.valves.default_upload_type
            elif type_val == "url" and is_http_uri:
                # keep as 'url' for Flowise if remote URL allowed
                type_val = "url" if self.valves.allow_remote_file_urls else self.valves.default_upload_type

            result: Dict[str, Any] = {
                "data": data_val,
                "type": type_val,
            }
            if u.get("name"):
                result["name"] = u.get("name")
            if u.get("mime") or u.get("mimetype"):
                result["mime"] = u.get("mime") or u.get("mimetype")
            return result

        top_level_uploads = body.get("uploads")
        if isinstance(top_level_uploads, list):
            for u in top_level_uploads:
                normalized = normalize_upload(u)
                if normalized:
                    uploads.append(normalized)

        for msg in messages:
            if isinstance(msg.get("uploads"), list):
                for u in msg.get("uploads", []):
                    normalized = normalize_upload(u)
                    if normalized:
                        uploads.append(normalized)
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type")
                    if item_type in {"file", "input_file", "data_url"}:
                        normalized = normalize_upload(item)
                        if normalized:
                            uploads.append(normalized)
                    elif item_type == "image_url":
                        image_url = item.get("image_url")
                        if isinstance(image_url, dict):
                            url_val = image_url.get("url")
                        else:
                            url_val = image_url
                        if isinstance(url_val, str):
                            # Prefer 'url' type for http(s) image URLs if allowed, fallback to default type
                            type_pref = "url" if (url_val.startswith("http://") or url_val.startswith("https://")) else self.valves.default_upload_type
                            candidate = {"data": url_val, "type": type_pref}
                            normalized = normalize_upload(candidate)
                            if normalized:
                                if "name" not in normalized:
                                    normalized["name"] = "image"
                                if "mime" not in normalized and url_val.startswith("data:"):
                                    try:
                                        mime_part = url_val.split(":", 1)[1].split(";", 1)[0]
                                        normalized["mime"] = mime_part
                                    except Exception:
                                        pass
                                uploads.append(normalized)

        return uploads

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
        __event_call__: Optional[Callable[[dict], Awaitable[dict]]] = None,
        __metadata__: Optional[dict] = None,
    ) -> Union[str, Generator, Iterator]:
        
        
        try:
            # Only reset status if there's no active request
            await self.emit_status(__event_emitter__, "info", "🚀 Initializing Flowise request...", False)

            if self.valves.debug_mode:
                print(f"\nDebug - Processing request:")
                print(f"Body: {json.dumps(body, indent=2, ensure_ascii=False)}")
                print(f"Metadata: {__metadata__}")

            # Validate configuration
            if not self.valves.flowise_api_key or not self.valves.flowise_url:
                error_msg = "❌ Missing Flowise configuration. Please check your API URL and key."
                await self.emit_status(__event_emitter__, "error", error_msg, True)
                return error_msg

            # Extract model ID and messages
            model_info = body.get("model", "")
            if "." in model_info:
                model_id = model_info.split(".", 1)[1]
            else:
                model_id = model_info

            messages = body.get("messages", [])
            if not messages:
                error_msg = "❌ No messages found in request"
                await self.emit_status(__event_emitter__, "error", error_msg, True)
                return error_msg

            # Determine prior messages and current message, pruning if this is an edit-resend
            prior_messages, current_message = self._split_messages_for_flowise(messages, __metadata__ or {}, body or {})
            # Process the current message (the one being sent as the question)
            question = self._process_message_content(current_message)
            
            if not question.strip():
                error_msg = "❌ Empty message content"
                await self.emit_status(__event_emitter__, "error", error_msg, True)
                return error_msg

            await self.emit_status(__event_emitter__, "info", "🔄 Sending request to Flowise...", False)

            # Prepare request data
            stream_enabled = body.get("stream", True)
            default_session_id = __metadata__.get("chat_id", f"session_{int(time.time())}") if __metadata__ else f"session_{int(time.time())}"

            # Start with base override config and merge user-provided overrides if present
            user_override_config = {}
            if isinstance(body.get("overrideConfig"), dict):
                user_override_config = body.get("overrideConfig", {})
            elif isinstance(body.get("flowise_override"), dict):
                # Alternate key some users might pass
                user_override_config = body.get("flowise_override", {})

            # Allow caller to define sessionId; fallback to our default if missing
            override_config: Dict[str, Any] = dict(user_override_config)
            if not override_config.get("sessionId"):
                override_config["sessionId"] = default_session_id
            # If not explicitly provided by caller, set override history flag based on valves
            if "isOverrideHistory" not in override_config:
                override_config["isOverrideHistory"] = self.valves.force_override_history

            # Build or passthrough history
            if isinstance(body.get("history"), list):
                # Trust caller-provided history to allow exact overwrite semantics
                history_payload = body.get("history")
            else:
                # Build history (excluding current message) for better context, if any
                if prior_messages:
                    trimmed_prior = self._trim_messages_for_history(prior_messages, self.valves.history_limit)
                    history_payload = self._build_history_payload(trimmed_prior)
                else:
                    history_payload = []

            request_session_id = override_config.get("sessionId", default_session_id)

            request_data: Dict[str, Any] = {
                "question": question,
                "overrideConfig": override_config,
                "streaming": stream_enabled,
            }
            # Provide top-level chatId for maximum compatibility with Flowise
            request_data["chatId"] = request_session_id
            # Optional fields like `form` and `humanInput` intentionally not forwarded to reduce payload noise
            if history_payload is not None:
                request_data["history"] = history_payload
            # Extract uploads from body/messages and pass through
            uploads_payload = self._extract_uploads(body, messages)
            if uploads_payload:
                request_data["uploads"] = uploads_payload
            # Note: do not forward metadata or user info to Flowise

            headers = self._build_headers(stream_enabled)

            if self.valves.debug_mode:
                print(f"Debug - Request URL: {self.valves.flowise_url}/api/v1/prediction/{model_id}")
                print(f"Debug - Request data: {json.dumps(request_data, indent=2, ensure_ascii=False)}")
                print(f"Debug - Headers: {headers}")
                print(f"Debug - Stream enabled: {stream_enabled}")
                print(f"Debug - Timeout (connect, read): {self._get_request_timeout(stream_enabled=stream_enabled)}")

            # Make the API request
            response = self.session.post(
                url=f"{self.valves.flowise_url}/api/v1/prediction/{model_id}",
                json=request_data,
                headers=headers,
                timeout=self._get_request_timeout(stream_enabled=stream_enabled),
                stream=stream_enabled
            )
            
            response.raise_for_status()
            response.encoding = "utf-8"

            if self.valves.debug_mode:
                print(f"Debug - Response status: {response.status_code}")
                print(f"Debug - Response headers: {dict(response.headers)}")

            await self.emit_status(__event_emitter__, "info", "✅ Receiving response from Flowise...", False)

            # Handle streaming response
            if stream_enabled:
                return self._handle_streaming_response(response, __event_emitter__)
            else:
                # Handle non-streaming response
                await self.emit_status(__event_emitter__, "info", "📝 Processing response...", False)
                
                try:
                    response_data = response.json()
                except json.JSONDecodeError:
                    await self.emit_status(__event_emitter__, "info", "✅ Response ready!", True)
                    return ""

                if self.valves.debug_mode:
                    print(f"Debug - Response data: {json.dumps(response_data, indent=2, ensure_ascii=False)}")

                if isinstance(response_data, dict) and "text" in response_data:
                    result = response_data["text"]
                    if isinstance(result, bytes):
                        result = result.decode("utf-8")
                    await self.emit_status(__event_emitter__, "info", "✅ Response ready!", True)
                    return result

                await self.emit_status(__event_emitter__, "info", "✅ Response ready!", True)
                return ""

        except requests.exceptions.Timeout:
            error_msg = f"⏰ Request timeout after {self.valves.timeout} seconds"
            await self.emit_status(__event_emitter__, "error", error_msg, True)
            return error_msg
            
        except requests.exceptions.ConnectionError:
            error_msg = "🔌 Connection failed - Is Flowise running and accessible?"
            await self.emit_status(__event_emitter__, "error", error_msg, True)
            return error_msg
            
        except requests.exceptions.HTTPError as e:
            error_msg = f"🚫 HTTP Error {response.status_code}: {e}"
            await self.emit_status(__event_emitter__, "error", error_msg, True)
            return error_msg
            
        except Exception as e:
            error_msg = f"❌ Unexpected error: {str(e)}"
            if self.valves.debug_mode:
                print(f"Debug - pipe() error: {e}")
            await self.emit_status(__event_emitter__, "error", error_msg, True)
            return error_msg

    def _handle_streaming_response(
        self, 
        response: requests.Response, 
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]]
    ) -> Generator[str, None, None]:
        
        try:
            for raw_line in response.iter_lines(decode_unicode=True, chunk_size=1):
                line = raw_line.strip() if isinstance(raw_line, str) else raw_line
                if self.valves.debug_mode and line:
                    print(f"Debug - Streaming line: {line}")
                
                if not line:
                    continue

                # Basic SSE: lines prefixed by 'data:' contain payload
                if isinstance(line, str) and line.startswith("data:"):
                    try:
                        # Parse the SSE data
                        json_data = line[5:].strip()  # Remove 'data:' prefix
                        if not json_data:
                            continue

                        data = json.loads(json_data)
                        
                        if self.valves.debug_mode:
                            print(f"Debug - Parsed streaming data: {json.dumps(data, indent=2, ensure_ascii=False)}")
                        
                        # Handle token events
                        if isinstance(data, dict) and data.get("event") == "token":
                            token = data.get("data", "")
                            if token:
                                if isinstance(token, bytes):
                                    token = token.decode("utf-8")
                                yield token
                        # Handle info/status events
                        elif isinstance(data, dict) and data.get("event") in {"start", "end", "error", "status"}:
                            desc = data.get("data") or data.get("message") or data.get("text") or ""
                            if __event_emitter__ and desc:
                                try:
                                    level = "error" if data.get("event") == "error" else "info"
                                    asyncio.create_task(self.emit_status(__event_emitter__, level, str(desc), data.get("event") == "end"))
                                except Exception:
                                    pass
                        # Ignore final text payloads; only stream token events
                        # Handle direct text response
                        elif isinstance(data, str):
                            yield data
                        # Ignore OpenAI-style delta formats not used by Flowise
                                
                    except json.JSONDecodeError as e:
                        if self.valves.debug_mode:
                            print(f"Debug - JSON decode error: {e} for line: {line}")
                        continue
                    except UnicodeDecodeError as e:
                        if self.valves.debug_mode:
                            print(f"Debug - Unicode decode error: {e}")
                        continue
            
            # Finalize status when streaming completes normally
            if __event_emitter__:
                try:
                    asyncio.create_task(self.emit_status(__event_emitter__, "info", "✅ Response complete", True))
                except Exception:
                    pass
                            
        except requests.exceptions.ReadTimeout as e:
            # Provide a clearer guidance for streaming read timeouts
            effective_timeout = self._get_request_timeout(stream_enabled=True)
            error_msg = (
                f"⏰ Streaming read timed out (connect, read)={effective_timeout}. "
                f"Increase FLOWISE_READ_TIMEOUT_STREAM or unset FLOWISE_TIMEOUT if set."
            )
            if self.valves.debug_mode:
                print(f"Debug - streaming read timeout: {e}")
            yield error_msg
        except Exception as e:
            error_msg = f"❌ Streaming error: {str(e)}"
            if self.valves.debug_mode:
                print(f"Debug - streaming error: {e}")
            yield error_msg