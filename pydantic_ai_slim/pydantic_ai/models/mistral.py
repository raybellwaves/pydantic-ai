from __future__ import annotations as _annotations

import os
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import chain
from typing import Any, Callable, Literal, Union

from httpx import AsyncClient as AsyncHTTPClient, Timeout
from typing_extensions import assert_never

from .. import UnexpectedModelBehavior
from .._utils import now_utc as _now_utc
from ..messages import (
    ArgsJson,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponsePart,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from ..result import Usage
from ..settings import ModelSettings
from ..tools import ToolDefinition
from . import (
    AgentModel,
    EitherStreamedResponse,
    Model,
    StreamStructuredResponse,
    StreamTextResponse,
    cached_async_http_client,
)

try:
    from json_repair import repair_json
    from mistralai import (
        UNSET,
        CompletionChunk as MistralCompletionChunk,
        Content as MistralContent,
        ContentChunk as MistralContentChunk,
        FunctionCall as MistralFunctionCall,
        Mistral,
        OptionalNullable as MistralOptionalNullable,
        TextChunk as MistralTextChunk,
        ToolChoiceEnum as MistralToolChoiceEnum,
    )
    from mistralai.models import (
        ChatCompletionResponse as MistralChatCompletionResponse,
        CompletionEvent as MistralCompletionEvent,
        Messages as MistralMessages,
        Tool as MistralTool,
        ToolCall as MistralToolCall,
    )
    from mistralai.models.assistantmessage import AssistantMessage as MistralAssistantMessage
    from mistralai.models.function import Function as MistralFunction
    from mistralai.models.systemmessage import SystemMessage as MistralSystemMessage
    from mistralai.models.toolmessage import ToolMessage as MistralToolMessage
    from mistralai.models.usermessage import UserMessage as MistralUserMessage
    from mistralai.types.basemodel import Unset as MistralUnset
    from mistralai.utils.eventstreaming import EventStreamAsync as MistralEventStreamAsync
except ImportError as e:
    raise ImportError(
        'Please install `mistral` to use the Mistral model, '
        "you can use the `mistral` optional group — `pip install 'pydantic-ai-slim[mistral]'`"
    ) from e

NamedMistralModels = Literal[
    'mistral-large-latest', 'mistral-small-latest', 'codestral-latest', 'mistral-moderation-latest'
]
"""Latest / most popular named Mistral models."""

MistralModelName = Union[NamedMistralModels, str]
"""Possible Mistral model names.

Since Mistral supports a variety of date-stamped models, we explicitly list the most popular models but
allow any name in the type hints.
Since [the Mistral docs](https://docs.mistral.ai/getting-started/models/models_overview/) for a full list.
"""


@dataclass(init=False)
class MistralModel(Model):
    """A model that uses Mistral.

    Internally, this uses the [Mistral Python client](https://github.com/mistralai/client-python) to interact with the API.

    [API Documentation](https://docs.mistral.ai/)
    """

    model_name: MistralModelName
    client: Mistral = field(repr=False)

    def __init__(
        self,
        model_name: MistralModelName,
        *,
        api_key: str | Callable[[], str | None] | None = None,
        client: Mistral | None = None,
        http_client: AsyncHTTPClient | None = None,
    ):
        """Initialize a Mistral model.

        Args:
            model_name: The name of the model to use.
            api_key: The API key to use for authentication, if unset uses `MISTRAL_API_KEY` environment variable.
            client: An existing `Mistral` client to use, if provided, `api_key` and `http_client` must be `None`.
            http_client: An existing `httpx.AsyncClient` to use for making HTTP requests.
        """
        self.model_name = model_name

        if client is not None:
            assert http_client is None, 'Cannot provide both `mistral_client` and `http_client`'
            assert api_key is None, 'Cannot provide both `mistral_client` and `api_key`'
            self.client = client
        else:
            api_key = os.getenv('MISTRAL_API_KEY') if api_key is None else api_key
            self.client = Mistral(api_key=api_key, async_client=http_client or cached_async_http_client())

    async def agent_model(
        self,
        *,
        function_tools: list[ToolDefinition],
        allow_text_result: bool,
        result_tools: list[ToolDefinition],
    ) -> AgentModel:
        """Create an agent model, this is called for each step of an agent run from Pydantic AI call."""
        return MistralAgentModel(
            self.client,
            self.model_name,
            allow_text_result,
            function_tools,
            result_tools,
        )

    def name(self) -> str:
        return f'mistral:{self.model_name}'


@dataclass
class MistralAgentModel(AgentModel):
    """Implementation of `AgentModel` for Mistral models."""

    client: Mistral
    model_name: str
    allow_text_result: bool
    function_tools: list[ToolDefinition]
    result_tools: list[ToolDefinition]
    json_mode_schema_prompt: str = """Answer in JSON Object, respect the format:\n```\n{schema}\n```\n"""

    async def request(
        self, messages: list[ModelMessage], model_settings: ModelSettings | None
    ) -> tuple[ModelResponse, Usage]:
        """Make a non-streaming request to the model from Pydantic AI call."""
        response = await self._completions_create(messages, model_settings)
        return self._process_response(response), _map_usage(response)

    @asynccontextmanager
    async def request_stream(
        self, messages: list[ModelMessage], model_settings: ModelSettings | None
    ) -> AsyncIterator[EitherStreamedResponse]:
        """Make a streaming request to the model from Pydantic AI call."""
        response = await self._stream_completions_create(messages, model_settings)
        async with response:
            yield await self._process_streamed_response(self.result_tools, response)

    async def _completions_create(
        self, messages: list[ModelMessage], model_settings: ModelSettings | None
    ) -> MistralChatCompletionResponse:
        """Make a non-streaming request to the model."""
        model_settings = model_settings or {}
        response = await self.client.chat.complete_async(
            model=str(self.model_name),
            messages=list(chain(*(self._map_message(m) for m in messages))),
            n=1,
            tools=self._map_function_and_result_tools_definition() or UNSET,
            tool_choice=self._get_tool_choice(),
            stream=False,
            max_tokens=model_settings.get('max_tokens', UNSET),
            temperature=model_settings.get('temperature', UNSET),
            top_p=model_settings.get('top_p', 1),
            timeout_ms=self._get_timeout_ms(model_settings.get('timeout')),
        )
        assert response, 'A unexpected empty response from Mistral.'
        return response

    async def _stream_completions_create(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
    ) -> MistralEventStreamAsync[MistralCompletionEvent]:
        """Create a streaming completion request to the Mistral model."""
        response: MistralEventStreamAsync[MistralCompletionEvent] | None
        mistral_messages = list(chain(*(self._map_message(m) for m in messages)))

        model_settings = model_settings or {}

        if self.result_tools and self.function_tools or self.function_tools:
            # Function Calling Mode
            response = await self.client.chat.stream_async(
                model=str(self.model_name),
                messages=mistral_messages,
                n=1,
                tools=self._map_function_and_result_tools_definition() or UNSET,
                tool_choice=self._get_tool_choice(),
                temperature=model_settings.get('temperature', UNSET),
                top_p=model_settings.get('top_p', 1),
                max_tokens=model_settings.get('max_tokens', UNSET),
                timeout_ms=self._get_timeout_ms(model_settings.get('timeout')),
            )

        elif self.result_tools:
            # Json Mode
            parameters_json_schemas = [tool.parameters_json_schema for tool in self.result_tools]

            user_output_format_message = self._generate_user_output_format(parameters_json_schemas)
            mistral_messages.append(user_output_format_message)
            response = await self.client.chat.stream_async(
                model=str(self.model_name),
                messages=mistral_messages,
                response_format={'type': 'json_object'},
                stream=True,
            )

        else:
            # Stream Mode
            response = await self.client.chat.stream_async(
                model=str(self.model_name),
                messages=mistral_messages,
                stream=True,
            )
        assert response, 'A unexpected empty response from Mistral.'
        return response

    def _get_tool_choice(self) -> MistralToolChoiceEnum | None:
        """Get tool choice for the model.

        - "auto": Default mode. Model decides if it uses the tool or not.
        - "any": Select any tool.
        - "none": Prevents tool use.
        - "required": Forces tool use.
        """
        if not self.function_tools and not self.result_tools:
            return None
        elif not self.allow_text_result:
            return 'required'
        else:
            return 'auto'

    def _map_function_and_result_tools_definition(self) -> list[MistralTool] | None:
        """Map function and result tools to MistralTool format.

        Returns None if both function_tools and result_tools are empty.
        """
        all_tools: list[ToolDefinition] = self.function_tools + self.result_tools
        tools = [
            MistralTool(
                function=MistralFunction(name=r.name, parameters=r.parameters_json_schema, description=r.description)
            )
            for r in all_tools
        ]
        return tools if tools else None

    @staticmethod
    def _process_response(response: MistralChatCompletionResponse) -> ModelResponse:
        """Process a non-streamed response, and prepare a message to return."""
        if response.created:
            timestamp = datetime.fromtimestamp(response.created, tz=timezone.utc)
        else:
            timestamp = _now_utc()

        assert response.choices, 'Unexpected empty response choice.'
        choice = response.choices[0]
        content = choice.message.content
        tool_calls = choice.message.tool_calls

        parts: list[ModelResponsePart] = []
        if text := _map_content(content):
            parts.append(TextPart(text))

        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                tool = _map_mistral_to_pydantic_tool_call(tool_call)
                parts.append(tool)

        return ModelResponse(parts, timestamp=timestamp)

    @staticmethod
    async def _process_streamed_response(
        result_tools: list[ToolDefinition],
        response: MistralEventStreamAsync[MistralCompletionEvent],
    ) -> EitherStreamedResponse:
        """Process a streamed response, and prepare a streaming response to return."""
        start_usage = Usage()

        # Iterate until we get either `tool_calls` or `content` from the first chunk.
        while True:
            try:
                event = await response.__anext__()
                chunk = event.data
            except StopAsyncIteration as e:
                raise UnexpectedModelBehavior('Streamed response ended without content or tool calls') from e

            start_usage += _map_usage(chunk)

            if chunk.created:
                timestamp = datetime.fromtimestamp(chunk.created, tz=timezone.utc)
            else:
                timestamp = _now_utc()

            if chunk.choices:
                delta = chunk.choices[0].delta
                content = _map_content(delta.content)

                tool_calls: list[MistralToolCall] | None = None
                if delta.tool_calls:
                    tool_calls = delta.tool_calls

                if tool_calls or content and result_tools:
                    return MistralStreamStructuredResponse(
                        {c.id if c.id else 'null': c for c in tool_calls or []},
                        {c.name: c for c in result_tools},
                        response,
                        content,
                        timestamp,
                        start_usage,
                    )

                elif content:
                    return MistralStreamTextResponse(content, response, timestamp, start_usage)

    @staticmethod
    def _map_to_mistral_tool_call(t: ToolCallPart) -> MistralToolCall:
        """Maps a pydantic-ai ToolCall to a MistralToolCall."""
        if isinstance(t.args, ArgsJson):
            return MistralToolCall(
                id=t.tool_call_id,
                type='function',
                function=MistralFunctionCall(name=t.tool_name, arguments=t.args.args_json),
            )
        else:
            return MistralToolCall(
                id=t.tool_call_id,
                type='function',
                function=MistralFunctionCall(name=t.tool_name, arguments=t.args.args_dict),
            )

    def _generate_user_output_format(self, schemas: list[dict[str, Any]]) -> MistralUserMessage:
        """Get a message with an example of the expected output format."""
        examples: list[dict[str, Any]] = []
        for schema in schemas:
            typed_dict_definition: dict[str, Any] = {}
            for key, value in schema.get('properties', {}).items():
                typed_dict_definition[key] = self._get_python_type(value)
            examples.append(typed_dict_definition)

        example_schema = examples[0] if len(examples) == 1 else examples
        return MistralUserMessage(content=self.json_mode_schema_prompt.format(schema=example_schema))

    @classmethod
    def _get_python_type(cls, value: dict[str, Any]) -> str:
        """Return a string representation of the Python type for a single JSON schema property.

        This function handles recursion for nested arrays/objects and `anyOf`.
        """
        # 1) Handle anyOf first, because it's a different schema structure
        if any_of := value.get('anyOf'):
            # Simplistic approach: pick the first option in anyOf
            # (In reality, you'd possibly want to merge or union types)
            return f'Optional[{cls._get_python_type(any_of[0])}]'

        # 2) If we have a top-level "type" field
        value_type = value.get('type')
        if not value_type:
            # No explicit type; fallback
            return 'Any'

        # 3) Direct simple type mapping (string, integer, float, bool, None)
        if value_type in SIMPLE_JSON_TYPE_MAPPING and value_type != 'array' and value_type != 'object':
            return SIMPLE_JSON_TYPE_MAPPING[value_type]

        # 4) Array: Recursively get the item type
        if value_type == 'array':
            items = value.get('items', {})
            return f'list[{cls._get_python_type(items)}]'

        # 5) Object: Check for additionalProperties
        if value_type == 'object':
            additional_properties = value.get('additionalProperties', {})
            additional_properties_type = additional_properties.get('type')
            if (
                additional_properties_type in SIMPLE_JSON_TYPE_MAPPING
                and additional_properties_type != 'array'
                and additional_properties_type != 'object'
            ):
                # dict[str, bool/int/float/etc...]
                return f'dict[str, {SIMPLE_JSON_TYPE_MAPPING[additional_properties_type]}]'
            elif additional_properties_type == 'array':
                array_items = additional_properties.get('items', {})
                return f'dict[str, list[{cls._get_python_type(array_items)}]]'
            elif additional_properties_type == 'object':
                # nested dictionary of unknown shape
                return 'dict[str, dict[str, Any]]'
            else:
                # If no additionalProperties type or something else, default to a generic dict
                return 'dict[str, Any]'

        # 6) Fallback
        return 'Any'

    @staticmethod
    def _get_timeout_ms(timeout: Timeout | float | None) -> int | None:
        """Convert a timeout to milliseconds."""
        if timeout is None:
            return None
        if isinstance(timeout, float):
            return int(1000 * timeout)
        raise NotImplementedError('Timeout object is not yet supported for MistralModel.')

    @classmethod
    def _map_user_message(cls, message: ModelRequest) -> Iterable[MistralMessages]:
        for part in message.parts:
            if isinstance(part, SystemPromptPart):
                yield MistralSystemMessage(content=part.content)
            elif isinstance(part, UserPromptPart):
                yield MistralUserMessage(content=part.content)
            elif isinstance(part, ToolReturnPart):
                yield MistralToolMessage(
                    tool_call_id=part.tool_call_id,
                    content=part.model_response_str(),
                )
            elif isinstance(part, RetryPromptPart):
                if part.tool_name is None:
                    yield MistralUserMessage(content=part.model_response())
                else:
                    yield MistralToolMessage(
                        tool_call_id=part.tool_call_id,
                        content=part.model_response(),
                    )
            else:
                assert_never(part)

    @classmethod
    def _map_message(cls, message: ModelMessage) -> Iterable[MistralMessages]:
        """Just maps a `pydantic_ai.Message` to a `MistralMessage`."""
        if isinstance(message, ModelRequest):
            yield from cls._map_user_message(message)
        elif isinstance(message, ModelResponse):
            content_chunks: list[MistralContentChunk] = []
            tool_calls: list[MistralToolCall] = []

            for part in message.parts:
                if isinstance(part, TextPart):
                    content_chunks.append(MistralTextChunk(text=part.content))
                elif isinstance(part, ToolCallPart):
                    tool_calls.append(cls._map_to_mistral_tool_call(part))
                else:
                    assert_never(part)
            yield MistralAssistantMessage(content=content_chunks, tool_calls=tool_calls)
        else:
            assert_never(message)


@dataclass
class MistralStreamTextResponse(StreamTextResponse):
    """Implementation of `StreamTextResponse` for Mistral models."""

    _first: str | None
    _response: MistralEventStreamAsync[MistralCompletionEvent]
    _timestamp: datetime
    _usage: Usage
    _buffer: list[str] = field(default_factory=list, init=False)

    async def __anext__(self) -> None:
        if self._first is not None and len(self._first) > 0:
            self._buffer.append(self._first)
            self._first = None
            return None

        chunk = await self._response.__anext__()
        self._usage += _map_usage(chunk.data)

        try:
            choice = chunk.data.choices[0]
        except IndexError:
            raise StopAsyncIteration()

        content = choice.delta.content
        if choice.finish_reason is None:
            assert content is not None, f'Expected delta with content, invalid chunk: {chunk!r}'

        if text := _map_content(content):
            self._buffer.append(text)

    def get(self, *, final: bool = False) -> Iterable[str]:
        yield from self._buffer
        self._buffer.clear()

    def usage(self) -> Usage:
        return self._usage

    def timestamp(self) -> datetime:
        return self._timestamp


@dataclass
class MistralStreamStructuredResponse(StreamStructuredResponse):
    """Implementation of `StreamStructuredResponse` for Mistral models."""

    _function_tools: dict[str, MistralToolCall]
    _result_tools: dict[str, ToolDefinition]
    _response: MistralEventStreamAsync[MistralCompletionEvent]
    _delta_content: str | None
    _timestamp: datetime
    _usage: Usage

    async def __anext__(self) -> None:
        chunk = await self._response.__anext__()
        self._usage += _map_usage(chunk.data)

        try:
            choice = chunk.data.choices[0]

        except IndexError:
            raise StopAsyncIteration()

        if choice.finish_reason is not None:
            raise StopAsyncIteration()

        content = choice.delta.content
        if self._result_tools:
            if text := _map_content(content):
                self._delta_content = (self._delta_content or '') + text

    def get(self, *, final: bool = False) -> ModelResponse:
        calls: list[ModelResponsePart] = []
        if self._function_tools and self._result_tools or self._function_tools:
            for tool_call in self._function_tools.values():
                tool = _map_mistral_to_pydantic_tool_call(tool_call)
                calls.append(tool)

        elif self._delta_content and self._result_tools:
            # NOTE: Params set for the most efficient and fastest way.
            output_json = repair_json(self._delta_content, return_objects=True, skip_json_loads=True)
            assert isinstance(
                output_json, dict
            ), f'Expected repair_json as type dict, invalid type: {type(output_json)}'

            if output_json:
                for result_tool in self._result_tools.values():
                    # NOTE: Additional verification to prevent JSON validation to crash in `result.py`
                    # Ensures required parameters in the JSON schema are respected, especially for stream-based return types.
                    # For example, `return_type=list[str]` expects a 'response' key with value type array of str.
                    # when `{"response":` then `repair_json` sets `{"response": ""}` (type not found default str)
                    # when `{"response": {` then `repair_json` sets `{"response": {}}` (type found)
                    # This ensures it's corrected to `{"response": {}}` and other required parameters and type.
                    if not self._validate_required_json_shema(output_json, result_tool.parameters_json_schema):
                        continue

                    tool = ToolCallPart.from_dict(
                        tool_name=result_tool.name,
                        args_dict=output_json,
                    )
                    calls.append(tool)

        return ModelResponse(calls, timestamp=self._timestamp)

    def usage(self) -> Usage:
        return self._usage

    def timestamp(self) -> datetime:
        return self._timestamp

    @staticmethod
    def _validate_required_json_shema(json_dict: dict[str, Any], json_schema: dict[str, Any]) -> bool:
        """Validate that all required parameters in the JSON schema are present in the JSON dictionary."""
        required_params = json_schema.get('required', [])
        properties = json_schema.get('properties', {})

        for param in required_params:
            if param not in json_dict:
                return False

            param_schema = properties.get(param, {})
            param_type = param_schema.get('type')
            param_items_type = param_schema.get('items', {}).get('type')

            if param_type == 'array' and param_items_type:
                if not isinstance(json_dict[param], list):
                    return False
                for item in json_dict[param]:
                    if not isinstance(item, VALIDE_JSON_TYPE_MAPPING[param_items_type]):
                        return False
            elif param_type and not isinstance(json_dict[param], VALIDE_JSON_TYPE_MAPPING[param_type]):
                return False

            if isinstance(json_dict[param], dict) and 'properties' in param_schema:
                nested_schema = param_schema
                if not MistralStreamStructuredResponse._validate_required_json_shema(json_dict[param], nested_schema):
                    return False

        return True


VALIDE_JSON_TYPE_MAPPING: dict[str, Any] = {
    'string': str,
    'integer': int,
    'number': float,
    'boolean': bool,
    'array': list,
    'object': dict,
    'null': type(None),
}

SIMPLE_JSON_TYPE_MAPPING = {
    'string': 'str',
    'integer': 'int',
    'number': 'float',
    'boolean': 'bool',
    'array': 'list',
    'null': 'None',
}


def _map_mistral_to_pydantic_tool_call(tool_call: MistralToolCall) -> ToolCallPart:
    """Maps a MistralToolCall to a ToolCall."""
    tool_call_id = tool_call.id or None
    func_call = tool_call.function

    if isinstance(func_call.arguments, str):
        return ToolCallPart.from_json(
            tool_name=func_call.name,
            args_json=func_call.arguments,
            tool_call_id=tool_call_id,
        )
    else:
        return ToolCallPart.from_dict(
            tool_name=func_call.name, args_dict=func_call.arguments, tool_call_id=tool_call_id
        )


def _map_usage(response: MistralChatCompletionResponse | MistralCompletionChunk) -> Usage:
    """Maps a Mistral Completion Chunk or Chat Completion Response to a Usage."""
    if response.usage:
        return Usage(
            request_tokens=response.usage.prompt_tokens,
            response_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
            details=None,
        )
    else:
        return Usage()


def _map_content(content: MistralOptionalNullable[MistralContent]) -> str | None:
    """Maps the delta content from a Mistral Completion Chunk to a string or None."""
    result: str | None = None

    if isinstance(content, MistralUnset) or not content:
        result = None
    elif isinstance(content, list):
        for chunk in content:
            if isinstance(chunk, MistralTextChunk):
                result = result or '' + chunk.text
            else:
                assert False, f'Other data types like (Image, Reference) are not yet supported,  got {type(chunk)}'
    elif isinstance(content, str):
        result = content

    # Note: Check len to handle potential mismatch between function calls and responses from the API. (`msg: not the same number of function class and reponses`)
    if result and len(result) == 0:
        result = None

    return result
