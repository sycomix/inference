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

import asyncio
import itertools
import time
from dataclasses import dataclass
from logging import getLogger
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    Dict,
    Iterator,
    List,
    Optional,
    Union,
)

import xoscar as xo

from ..core import ModelActor
from .resource import ResourceStatus
from .utils import (
    build_replica_model_uid,
    iter_replica_model_uid,
    log_async,
    log_sync,
    parse_replica_model_uid,
)

if TYPE_CHECKING:
    from .worker import WorkerActor

logger = getLogger(__name__)


DEFAULT_NODE_TIMEOUT = 30


@dataclass
class WorkerStatus:
    update_time: float
    status: Dict[str, ResourceStatus]


@dataclass
class ReplicaInfo:
    replica: int
    scheduler: Iterator


class SupervisorActor(xo.Actor):
    def __init__(self):
        super().__init__()
        self._worker_address_to_worker: Dict[str, xo.ActorRefType["WorkerActor"]] = {}
        self._replica_model_uid_to_worker: Dict[
            str, xo.ActorRefType["WorkerActor"]
        ] = {}
        self._model_uid_to_replica_info: Dict[str, ReplicaInfo] = {}
        self._worker_status: Dict[str, WorkerStatus] = {}

    @classmethod
    def uid(cls) -> str:
        return "supervisor"

    async def __post_create__(self):
        self._check_dead_nodes_task = asyncio.create_task(self._check_dead_nodes())

    async def __pre_destroy__(self):
        self._check_dead_nodes_task.cancel()

    async def _choose_worker(self) -> xo.ActorRefType["WorkerActor"]:
        # TODO: better allocation strategy.
        min_running_model_count = None
        target_worker = None
        for worker in self._worker_address_to_worker.values():
            running_model_count = await worker.get_model_count()
            if (
                min_running_model_count is None
                or running_model_count < min_running_model_count
            ):
                min_running_model_count = running_model_count
                target_worker = worker

        if target_worker:
            return target_worker

        raise RuntimeError("No available worker found")

    @log_sync(logger=logger)
    def list_model_registrations(self, model_type: str) -> List[Dict[str, Any]]:
        if model_type != "LLM":
            raise ValueError(f"Unsupported model type: {model_type}")
        from ..model.llm import BUILTIN_LLM_FAMILIES, get_user_defined_llm_families

        ret = [
            {"model_name": f.model_name, "is_builtin": True}
            for f in BUILTIN_LLM_FAMILIES
        ]
        user_defined_llm_families = get_user_defined_llm_families()
        ret.extend(
            [
                {"model_name": f.model_name, "is_builtin": False}
                for f in user_defined_llm_families
            ]
        )

        def sort_helper(item):
            assert isinstance(item["model_name"], str)
            return item.get("model_name").lower()

        ret.sort(key=sort_helper)

        return ret

    @log_sync(logger=logger)
    def get_model_registration(
        self, model_type: str, model_name: str
    ) -> Dict[str, Any]:
        if model_type != "LLM":
            raise ValueError(f"Unsupported model type: {model_type}")
        from ..model.llm import BUILTIN_LLM_FAMILIES, get_user_defined_llm_families

        for f in BUILTIN_LLM_FAMILIES + get_user_defined_llm_families():
            if f.model_name == model_name:
                return f

        raise ValueError(f"Model {model_name} not found")

    @log_async(logger=logger)
    async def register_model(self, model_type: str, model: str, persist: bool):
        if model_type != "LLM":
            raise ValueError(f"Unsupported model type: {model_type}")
        from ..model.llm import LLMFamilyV1, register_llm

        llm_family = LLMFamilyV1.parse_raw(model)
        register_llm(llm_family, persist)

        if not self.is_local_deployment:
            for worker in self._worker_address_to_worker.values():
                await worker.register_model(model_type, model, persist)

    @log_async(logger=logger)
    async def unregister_model(self, model_type: str, model_name: str):
        if model_type != "LLM":
            raise ValueError(f"Unsupported model type: {model_type}")
        from ..model.llm import unregister_llm

        unregister_llm(model_name)

        if not self.is_local_deployment:
            for worker in self._worker_address_to_worker.values():
                await worker.unregister_model(model_name)

    async def launch_builtin_model(
        self,
        model_uid: str,
        model_name: str,
        model_size_in_billions: Optional[int],
        model_format: Optional[str],
        quantization: Optional[str],
        model_type: Optional[str],
        replica: int = 1,
        n_gpu: Optional[Union[int, str]] = "auto",
        **kwargs,
    ) -> AsyncGenerator:
        logger.debug(
            'Enter launch_builtin_model, model_uid: %s, model_name: %s, model_size: %s, model_format: %s, quantization: %s, replica: %s',
            model_uid,
            model_name,
            str(model_size_in_billions) if model_size_in_billions else "",
            model_format,
            quantization,
            replica,
        )

        async def _launch_one_model(_replica_model_uid):
            if _replica_model_uid in self._replica_model_uid_to_worker:
                raise ValueError(
                    f"Model is already in the model list, uid: {_replica_model_uid}"
                )

            nonlocal model_type
            worker_ref = await self._choose_worker()
            # LLM as default for compatibility
            model_type = model_type or "LLM"
            yield worker_ref.launch_builtin_model(
                model_uid=_replica_model_uid,
                model_name=model_name,
                model_size_in_billions=model_size_in_billions,
                model_format=model_format,
                quantization=quantization,
                model_type=model_type,
                n_gpu=n_gpu,
                **kwargs,
            )
            # TODO: not protected.
            self._replica_model_uid_to_worker[_replica_model_uid] = worker_ref

        if model_uid in self._model_uid_to_replica_info:
            raise ValueError(f"Model is already in the model list, uid: {model_uid}")
        # Set replica info first for exception handler to terminate model.
        self._model_uid_to_replica_info[model_uid] = ReplicaInfo(
            replica=replica, scheduler=itertools.cycle(range(replica))
        )
        try:
            for rep_model_uid in iter_replica_model_uid(model_uid, replica):
                yield _launch_one_model(rep_model_uid)
        except Exception:
            # terminate_model will remove the replica info.
            await self.terminate_model(model_uid, suppress_exception=True)
            raise
        raise xo.Return(model_uid)

    async def _check_dead_nodes(self):
        while True:
            dead_nodes = []
            for address, status in self._worker_status.items():
                if time.time() - status.update_time > DEFAULT_NODE_TIMEOUT:
                    dead_models = [
                        model_uid
                        for model_uid in self._replica_model_uid_to_worker
                        if (
                            self._replica_model_uid_to_worker[model_uid].address
                            == address
                        )
                    ]
                    logger.error(
                        "Worker timeout. address: %s, influenced models: %s",
                        address,
                        dead_models,
                    )
                    dead_nodes.append(address)

            for address in dead_nodes:
                self._worker_status.pop(address)
                self._worker_address_to_worker.pop(address)
            await asyncio.sleep(5)

    @log_async(logger=logger)
    async def terminate_model(self, model_uid: str, suppress_exception=False):
        async def _terminate_one_model(_replica_model_uid):
            if _replica_model_uid not in self._replica_model_uid_to_worker:
                raise ValueError(
                    f"Model not found in the model list, uid: {_replica_model_uid}"
                )

            worker_ref = self._replica_model_uid_to_worker[_replica_model_uid]
            await worker_ref.terminate_model(model_uid=_replica_model_uid)
            del self._replica_model_uid_to_worker[_replica_model_uid]

        if model_uid not in self._model_uid_to_replica_info:
            raise ValueError(f"Model not found in the model list, uid: {model_uid}")
        replica_info = self._model_uid_to_replica_info[model_uid]
        for rep_model_uid in iter_replica_model_uid(model_uid, replica_info.replica):
            try:
                await _terminate_one_model(rep_model_uid)
            except Exception:
                if not suppress_exception:
                    raise
        self._model_uid_to_replica_info.pop(model_uid, None)

    @log_async(logger=logger)
    async def get_model(self, model_uid: str) -> xo.ActorRefType["ModelActor"]:
        if model_uid not in self._model_uid_to_replica_info:
            raise ValueError(f"Model not found in the model list, uid: {model_uid}")
        replica_info = self._model_uid_to_replica_info[model_uid]
        replica_model_uid = build_replica_model_uid(
            model_uid, replica_info.replica, next(replica_info.scheduler)
        )
        if replica_model_uid not in self._replica_model_uid_to_worker:
            raise ValueError(
                f"Model not found in the model list, uid: {replica_model_uid}"
            )

        worker_ref = self._replica_model_uid_to_worker[replica_model_uid]
        return await worker_ref.get_model(model_uid=replica_model_uid)

    @log_async(logger=logger)
    async def describe_model(self, model_uid: str) -> Dict[str, Any]:
        if model_uid not in self._model_uid_to_replica_info:
            raise ValueError(f"Model not found in the model list, uid: {model_uid}")
        replica_info = self._model_uid_to_replica_info[model_uid]
        # Use rep id 0 to instead of next(replica_info.scheduler) to avoid
        # consuming the generator.
        replica_model_uid = build_replica_model_uid(model_uid, replica_info.replica, 0)
        if replica_model_uid not in self._replica_model_uid_to_worker:
            raise ValueError(
                f"Model not found in the model list, uid: {replica_model_uid}"
            )

        worker_ref = self._replica_model_uid_to_worker[replica_model_uid]
        info = await worker_ref.describe_model(model_uid=replica_model_uid)
        info["replica"] = replica_info.replica
        return info

    @log_async(logger=logger)
    async def list_models(self) -> Dict[str, Dict[str, Any]]:
        ret = {}
        for worker in self._worker_address_to_worker.values():
            ret.update(await worker.list_models())
        return {parse_replica_model_uid(k)[0]: v for k, v in ret.items()}

    @log_sync(logger=logger)
    def is_local_deployment(self) -> bool:
        # TODO: temporary.
        return (
            len(self._worker_address_to_worker) == 1
            and list(self._worker_address_to_worker)[0] == self.address
        )

    @log_async(logger=logger)
    async def add_worker(self, worker_address: str):
        from .worker import WorkerActor

        assert worker_address not in self._worker_address_to_worker

        worker_ref = await xo.actor_ref(address=worker_address, uid=WorkerActor.uid())
        self._worker_address_to_worker[worker_address] = worker_ref
        logger.info("Worker %s has been added successfully", worker_address)

    async def report_worker_status(
        self, worker_address: str, status: Dict[str, ResourceStatus]
    ):
        self._worker_status[worker_address] = WorkerStatus(
            update_time=time.time(), status=status
        )
