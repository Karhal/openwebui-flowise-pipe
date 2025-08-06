"""
title: Flowise Integration for OpenWebUI
version: 1.0.0
description: Flowise integration with dynamic model loading, streaming, and UI feedback
Requirements:
  - Flowise API URL (set via FLOWISE_API_URL)
  - Flowise API Key (set via FLOWISE_API_KEY)
"""

from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List, Union, Generator, Iterator, Callable, Awaitable
import requests
import json
import os
import time
import asyncio


class Pipe:
    class Valves(BaseModel):
        flowise_url: str = Field(
            default=os.getenv("FLOWISE_API_URL", "http://localhost:3001"),
            description="Flowise API base URL",
        )
        flowise_api_key: str = Field(
            default=os.getenv("FLOWISE_API_KEY", ""),
            description="Flowise API key for authentication",
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
            default=120,
            description="Request timeout in seconds"
        )
        debug_mode: bool = Field(
            default=False,
            description="Enable debug logging"
        )

    def __init__(self):
        self.type = "manifold"
        self.id = "flowise"
        self.name = "Flowise Integration"
        self.valves = self.Valves()
        self.last_emit_time = 0

        # Validate required settings
        if not self.valves.flowise_url:
            print("⚠️ Please set your Flowise URL using the FLOWISE_API_URL environment variable")
        if not self.valves.flowise_api_key:
            print("⚠️ Please set your Flowise API key using the FLOWISE_API_KEY environment variable")

    def pipes(self) -> List[Dict[str, str]]:
        """Dynamically load all available Flowise chatflows"""
        if not self.valves.flowise_api_key or not self.valves.flowise_url:
            return [
                {
                    "id": "error",
                    "name": "❌ Missing API configuration - Please set FLOWISE_API_URL and FLOWISE_API_KEY",
                }
            ]

        try:
            headers = {
                "Authorization": f"Bearer {self.valves.flowise_api_key}",
                "Content-Type": "application/json; charset=utf-8",
            }

            # Fetch all chatflows
            response = requests.get(
                f"{self.valves.flowise_url}/api/v1/chatflows",
                headers=headers,
                timeout=self.valves.timeout
            )
            response.raise_for_status()
            response.encoding = "utf-8"
            
            chatflows = response.json()
            
            if not chatflows:
                return [
                    {
                        "id": "no_flows",
                        "name": "ℹ️ No chatflows found in your Flowise instance",
                    }
                ]

            # Return available chatflows
            available_pipes = []
            for flow in chatflows:
                flow_name = flow.get("name", "Unnamed Flow")
                flow_id = flow.get("id", "")
                
                # Add emoji based on flow type or status
                if "agent" in flow_name.lower():
                    emoji = "🤖"
                elif "chat" in flow_name.lower():
                    emoji = "💬"
                else:
                    emoji = "🔄"
                
                available_pipes.append({
                    "id": flow_id,
                    "name": f"{emoji} {flow_name}",
                })

            return available_pipes

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
        """Process message content, handling both text and structured content"""
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
            return " ".join(processed_content)
        
        return str(content) if content else ""

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
        __event_call__: Optional[Callable[[dict], Awaitable[dict]]] = None,
        __metadata__: Optional[dict] = None,
    ) -> Union[str, Generator, Iterator]:
        """Pipe processing with streaming and UI feedback"""
        
        try:
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

            # Process the current message
            current_message = messages[-1]
            question = self._process_message_content(current_message)
            
            if not question.strip():
                error_msg = "❌ Empty message content"
                await self.emit_status(__event_emitter__, "error", error_msg, True)
                return error_msg

            await self.emit_status(__event_emitter__, "info", "🔄 Sending request to Flowise...", False)

            # Prepare request data
            stream_enabled = body.get("stream", True)
            session_id = __metadata__.get("chat_id", f"session_{int(time.time())}") if __metadata__ else f"session_{int(time.time())}"

            request_data = {
                "question": question,
                "overrideConfig": {
                    "sessionId": session_id
                },
                "streaming": stream_enabled,
            }

            headers = {
                "Authorization": f"Bearer {self.valves.flowise_api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "text/event-stream; charset=utf-8" if stream_enabled else "application/json; charset=utf-8",
            }

            if self.valves.debug_mode:
                print(f"Debug - Request URL: {self.valves.flowise_url}/api/v1/prediction/{model_id}")
                print(f"Debug - Request data: {json.dumps(request_data, indent=2, ensure_ascii=False)}")
                print(f"Debug - Headers: {headers}")
                print(f"Debug - Stream enabled: {stream_enabled}")

            # Make the API request
            response = requests.post(
                url=f"{self.valves.flowise_url}/api/v1/prediction/{model_id}",
                json=request_data,
                headers=headers,
                timeout=self.valves.timeout,
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
                    # If JSON parsing fails, try to return raw text
                    text_response = response.text
                    if self.valves.debug_mode:
                        print(f"Debug - Raw response (not JSON): {text_response}")
                    await self.emit_status(__event_emitter__, "info", "✅ Response ready!", True)
                    return text_response
                
                if self.valves.debug_mode:
                    print(f"Debug - Response data: {json.dumps(response_data, indent=2, ensure_ascii=False)}")
                
                # Handle different response formats
                if isinstance(response_data, dict):
                    # Check for text field
                    if "text" in response_data:
                        result = response_data["text"]
                        if isinstance(result, bytes):
                            result = result.decode("utf-8")
                        await self.emit_status(__event_emitter__, "info", "✅ Response ready!", True)
                        return result
                    # Check for message field
                    elif "message" in response_data:
                        result = response_data["message"]
                        if isinstance(result, bytes):
                            result = result.decode("utf-8")
                        await self.emit_status(__event_emitter__, "info", "✅ Response ready!", True)
                        return result
                    # Check for content field
                    elif "content" in response_data:
                        result = response_data["content"]
                        if isinstance(result, bytes):
                            result = result.decode("utf-8")
                        await self.emit_status(__event_emitter__, "info", "✅ Response ready!", True)
                        return result
                    # Check for response field
                    elif "response" in response_data:
                        result = response_data["response"]
                        if isinstance(result, bytes):
                            result = result.decode("utf-8")
                        await self.emit_status(__event_emitter__, "info", "✅ Response ready!", True)
                        return result
                elif isinstance(response_data, str):
                    # Direct string response
                    await self.emit_status(__event_emitter__, "info", "✅ Response ready!", True)
                    return response_data
                
                # Fallback: convert to string
                result = str(response_data)
                await self.emit_status(__event_emitter__, "warning", "⚠️ Unexpected response format - returning as string", True)
                return result

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
        """Handle streaming response from Flowise"""
        
        try:
            for line in response.iter_lines(decode_unicode=True, chunk_size=1):
                if self.valves.debug_mode and line:
                    print(f"Debug - Streaming line: {line}")
                
                if line and line.startswith("data:"):
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
                        # Handle final response
                        elif isinstance(data, dict) and "text" in data:
                            final_text = data["text"]
                            if isinstance(final_text, bytes):
                                final_text = final_text.decode("utf-8")
                            yield final_text
                        # Handle direct text response
                        elif isinstance(data, str):
                            yield data
                                
                    except json.JSONDecodeError as e:
                        if self.valves.debug_mode:
                            print(f"Debug - JSON decode error: {e} for line: {line}")
                        continue
                    except UnicodeDecodeError as e:
                        if self.valves.debug_mode:
                            print(f"Debug - Unicode decode error: {e}")
                        continue
                        
        except Exception as e:
            error_msg = f"❌ Streaming error: {str(e)}"
            if self.valves.debug_mode:
                print(f"Debug - streaming error: {e}")
            yield error_msg