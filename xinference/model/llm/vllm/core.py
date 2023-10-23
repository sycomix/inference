# Copyright 2022-2023 XProbe Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import time
import uuid
from typing import TYPE_CHECKING, AsyncGenerator, Dict, List, Optional, TypedDict, Union

from ....types import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessage,
    Completion,
    CompletionChoice,
    CompletionChunk,
    CompletionUsage,
)
from .. import LLM, LLMFamilyV1, LLMSpecV1
from ..utils import ChatModelMixin

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from vllm.outputs import RequestOutput


class VLLMModelConfig(TypedDict, total=False):
    tokenizer_mode: Optional[str]
    trust_remote_code: bool
    tensor_parallel_size: int
    block_size: int
    swap_space: int  # GiB
    gpu_memory_utilization: float
    max_num_batched_tokens: int
    max_num_seqs: int


class VLLMGenerateConfig(TypedDict, total=False):
    n: int
    best_of: Optional[int]
    presence_penalty: float
    frequency_penalty: float
    temperature: float
    top_p: float
    max_tokens: int
    stop: Optional[Union[str, List[str]]]
    stream: bool  # non-sampling param, should not be passed to the engine.


try:
    import vllm  # noqa: F401

    VLLM_INSTALLED = True
except ImportError:
    VLLM_INSTALLED = False

VLLM_SUPPORTED_MODELS = ["llama-2", "baichuan", "internlm-16k"]
VLLM_SUPPORTED_CHAT_MODELS = [
    "llama-2-chat",
    "vicuna-v1.3",
    "vicuna-v1.5",
    "baichuan-chat",
    "internlm-chat-7b",
    "internlm-chat-8k",
    "internlm-chat-20b",
]


class VLLMModel(LLM):
    def __init__(
        self,
        model_uid: str,
        model_family: "LLMFamilyV1",
        model_spec: "LLMSpecV1",
        quantization: str,
        model_path: str,
        model_config: Optional[VLLMModelConfig],
    ):
        super().__init__(model_uid, model_family, model_spec, quantization, model_path)
        self._model_config = self._sanitize_model_config(model_config)
        self._engine = None

    def load(self):
        try:
            from vllm.engine.arg_utils import AsyncEngineArgs
            from vllm.engine.async_llm_engine import AsyncLLMEngine
        except ImportError:
            error_message = "Failed to import module 'vllm'"
            installation_guide = [
                "Please make sure 'vllm' is installed. ",
                "You can install it by `pip install vllm`\n",
            ]

            raise ImportError(f"{error_message}\n\n{''.join(installation_guide)}")

        engine_args = AsyncEngineArgs(model=self.model_path, **self._model_config)
        self._engine = AsyncLLMEngine.from_engine_args(engine_args)

    def _sanitize_model_config(
        self, model_config: Optional[VLLMModelConfig]
    ) -> VLLMModelConfig:
        if model_config is None:
            model_config = VLLMModelConfig()

        cuda_count = self._get_cuda_count()

        model_config.setdefault("tokenizer_mode", "auto")
        model_config.setdefault("trust_remote_code", False)
        model_config.setdefault("tensor_parallel_size", cuda_count)
        model_config.setdefault("block_size", 16)
        model_config.setdefault("swap_space", 4)
        model_config.setdefault("gpu_memory_utilization", 0.90)
        model_config.setdefault("max_num_batched_tokens", 2560)
        model_config.setdefault("max_num_seqs", 256)

        return model_config

    @staticmethod
    def _sanitize_generate_config(
        generate_config: Optional[Dict] = None,
    ) -> VLLMGenerateConfig:
        if not generate_config:
            generate_config = {}

        sanitized = VLLMGenerateConfig()
        sanitized.setdefault("n", generate_config.get("n", 1))
        sanitized.setdefault("best_of", generate_config.get("best_of", None))
        sanitized.setdefault(
            "presence_penalty", generate_config.get("presence_penalty", 0.0)
        )
        sanitized.setdefault(
            "frequency_penalty", generate_config.get("frequency_penalty", 0.0)
        )
        sanitized.setdefault("temperature", generate_config.get("temperature", 1.0))
        sanitized.setdefault("top_p", generate_config.get("top_p", 1.0))
        sanitized.setdefault("max_tokens", generate_config.get("max_tokens", 16))
        sanitized.setdefault("stop", generate_config.get("stop", None))
        sanitized.setdefault("stream", generate_config.get("stream", None))

        return sanitized

    @classmethod
    def match(
        cls, llm_family: "LLMFamilyV1", llm_spec: "LLMSpecV1", quantization: str
    ) -> bool:
        if not cls._has_cuda_device():
            return False
        if not cls._is_linux():
            return False
        if quantization != "none":
            return False
        if llm_spec.model_format != "pytorch":
            return False
        if llm_family.model_name not in VLLM_SUPPORTED_MODELS:
            return False
        return False if "generate" not in llm_family.model_ability else VLLM_INSTALLED

    @staticmethod
    def _convert_request_output_to_completion_chunk(
        request_id: str, model: str, request_output: "RequestOutput"
    ) -> CompletionChunk:
        choices: List[CompletionChoice] = [
            CompletionChoice(
                text=output.text,
                index=output.index,
                logprobs=None,  # TODO: support logprobs.
                finish_reason=output.finish_reason,
            )
            for output in request_output.outputs
        ]
        return CompletionChunk(
            id=request_id,
            object="text_completion",
            created=int(time.time()),
            model=model,
            choices=choices,
        )

    @staticmethod
    def _convert_request_output_to_completion(
        request_id: str, model: str, request_output: "RequestOutput"
    ) -> Completion:
        choices = [
            CompletionChoice(
                text=output.text,
                index=output.index,
                logprobs=None,  # TODO: support logprobs.
                finish_reason=output.finish_reason,
            )
            for output in request_output.outputs
        ]
        prompt_tokens = len(request_output.prompt_token_ids)
        completion_tokens = sum(
            len(output.token_ids) for output in request_output.outputs
        )
        usage = CompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )
        return Completion(
            id=request_id,
            object="text_completion",
            created=int(time.time()),
            model=model,
            choices=choices,
            usage=usage,
        )

    async def async_generate(
        self,
        prompt: str,
        generate_config: Optional[Dict] = None,
    ) -> Union[Completion, AsyncGenerator[CompletionChunk, None]]:
        try:
            from vllm.sampling_params import SamplingParams
        except ImportError:
            error_message = "Failed to import module 'vllm'"
            installation_guide = [
                "Please make sure 'vllm' is installed. ",
                "You can install it by `pip install vllm`\n",
            ]

            raise ImportError(f"{error_message}\n\n{''.join(installation_guide)}")

        sanitized_generate_config = self._sanitize_generate_config(generate_config)
        logger.debug(
            "Enter generate, prompt: %s, generate config: %s", prompt, generate_config
        )

        stream = sanitized_generate_config.pop("stream")
        sampling_params = SamplingParams(**sanitized_generate_config)
        request_id = str(uuid.uuid1())

        assert self._engine is not None
        results_generator = self._engine.generate(prompt, sampling_params, request_id)

        async def stream_results() -> AsyncGenerator[CompletionChunk, None]:
            previous_texts = [""] * sanitized_generate_config["n"]
            async for _request_output in results_generator:
                chunk = self._convert_request_output_to_completion_chunk(
                    request_id=request_id,
                    model=self.model_uid,
                    request_output=_request_output,
                )
                for i, choice in enumerate(chunk["choices"]):
                    delta = choice["text"][len(previous_texts[i]) :]
                    previous_texts[i] = choice["text"]
                    choice["text"] = delta
                yield chunk

        if stream:
            return stream_results()
        final_output = None
        async for request_output in results_generator:
            final_output = request_output

        assert final_output is not None
        return self._convert_request_output_to_completion(
            request_id, model=self.model_uid, request_output=final_output
        )


class VLLMChatModel(VLLMModel, ChatModelMixin):
    @classmethod
    def match(
        cls, llm_family: "LLMFamilyV1", llm_spec: "LLMSpecV1", quantization: str
    ) -> bool:
        if quantization != "none":
            return False
        if llm_spec.model_format != "pytorch":
            return False
        if llm_family.model_name not in VLLM_SUPPORTED_CHAT_MODELS:
            return False
        return False if "chat" not in llm_family.model_ability else VLLM_INSTALLED

    def _sanitize_chat_config(
        self,
        generate_config: Optional[Dict] = None,
    ) -> Dict:
        if not generate_config:
            generate_config = {}
        if self.model_family.prompt_style and self.model_family.prompt_style.stop:
            generate_config.setdefault(
                "stop", self.model_family.prompt_style.stop.copy()
            )
        return generate_config

    async def async_chat(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        chat_history: Optional[List[ChatCompletionMessage]] = None,
        generate_config: Optional[Dict] = None,
    ) -> Union[ChatCompletion, AsyncGenerator[ChatCompletionChunk, None]]:
        assert self.model_family.prompt_style is not None
        prompt_style = self.model_family.prompt_style.copy()
        if system_prompt:
            prompt_style.system_prompt = system_prompt
        chat_history = chat_history or []
        full_prompt = self.get_prompt(prompt, chat_history, prompt_style)

        sanitized = self._sanitize_chat_config(generate_config)
        stream = sanitized["stream"]

        if stream:
            agen = await self.async_generate(full_prompt, sanitized)
            assert isinstance(agen, AsyncGenerator)
            return self._async_to_chat_completion_chunks(agen)
        else:
            c = await self.async_generate(full_prompt, sanitized)
            assert not isinstance(c, AsyncGenerator)
            return self._to_chat_completion(c)
