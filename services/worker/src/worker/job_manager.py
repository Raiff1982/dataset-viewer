# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

import logging
from http import HTTPStatus
from typing import Optional

from libcommon.config import CommonConfig
from libcommon.exceptions import (
    CustomError,
    DatasetNotFoundError,
    JobManagerCrashedError,
    JobManagerExceededMaximumDurationError,
    ResponseAlreadyComputedError,
    TooBigContentError,
    UnexpectedError,
)
from libcommon.processing_graph import ProcessingGraph, ProcessingStep
from libcommon.simple_cache import (
    CachedArtifactError,
    DoesNotExist,
    get_response_without_content_params,
    upsert_response_params,
)
from libcommon.state import DatasetState
from libcommon.utils import JobInfo, JobParams, Priority, orjson_dumps

from worker.config import AppConfig, WorkerConfig
from worker.job_runner import JobRunner

# List of error codes that should trigger a retry.
ERROR_CODES_TO_RETRY: list[str] = ["ClientConnectionError"]


class JobManager:
    """
    A job manager is a class that handles a job runner compute, for a specific processing step.

    Args:
        job_info (:obj:`JobInfo`):
            The job to process. It contains the job_id, the job type, the dataset, the revision, the config,
            the split and the priority level.
        common_config (:obj:`CommonConfig`):
            The common config.
        processing_step (:obj:`ProcessingStep`):
            The processing step to process.
    """

    job_id: str
    job_params: JobParams
    priority: Priority
    worker_config: WorkerConfig
    common_config: CommonConfig
    processing_step: ProcessingStep
    processing_graph: ProcessingGraph
    job_runner: JobRunner

    def __init__(
        self,
        job_info: JobInfo,
        app_config: AppConfig,
        job_runner: JobRunner,
        processing_graph: ProcessingGraph,
    ) -> None:
        self.job_info = job_info
        self.job_type = job_info["type"]
        self.job_id = job_info["job_id"]
        self.priority = job_info["priority"]
        self.job_params = job_info["params"]
        self.common_config = app_config.common
        self.worker_config = app_config.worker
        self.job_runner = job_runner
        self.processing_graph = processing_graph
        self.processing_step = self.job_runner.processing_step
        self.setup()

    def setup(self) -> None:
        job_type = self.job_runner.get_job_type()
        if self.processing_step.job_type != job_type:
            raise ValueError(
                f"The processing step's job type is {self.processing_step.job_type}, but the job manager only"
                f" processes {job_type}"
            )
        if self.job_type != job_type:
            raise ValueError(
                f"The submitted job type is {self.job_type}, but the job manager only processes {job_type}"
            )

    def __str__(self) -> str:
        return f"JobManager(job_id={self.job_id} dataset={self.job_params['dataset']} job_info={self.job_info}"

    def log(self, level: int, msg: str) -> None:
        logging.log(level=level, msg=f"[{self.processing_step.job_type}] {msg}")

    def debug(self, msg: str) -> None:
        self.log(level=logging.DEBUG, msg=msg)

    def info(self, msg: str) -> None:
        self.log(level=logging.INFO, msg=msg)

    def warning(self, msg: str) -> None:
        self.log(level=logging.WARNING, msg=msg)

    def exception(self, msg: str) -> None:
        self.log(level=logging.ERROR, msg=msg)

    def critical(self, msg: str) -> None:
        self.log(level=logging.CRITICAL, msg=msg)

    def run(self) -> bool:
        try:
            self.info(f"compute {self}")
            result = self.process()
        except Exception:
            self.exception(f"error while computing {self}")
            result = False
        self.backfill()
        return result

    def raise_if_parallel_response_exists(self, parallel_cache_kind: str, parallel_job_version: int) -> None:
        try:
            existing_response = get_response_without_content_params(
                kind=parallel_cache_kind,
                job_params=self.job_params,
            )

            if (
                existing_response["http_status"] == HTTPStatus.OK
                and existing_response["job_runner_version"] == parallel_job_version
                and existing_response["progress"] == 1.0  # completed response
                and existing_response["dataset_git_revision"] == self.job_params["revision"]
            ):
                raise ResponseAlreadyComputedError(
                    f"Response has already been computed and stored in cache kind: {parallel_cache_kind}. Compute will"
                    " be skipped."
                )
        except DoesNotExist:
            logging.debug(f"no cache found for {parallel_cache_kind}.")

    def process(
        self,
    ) -> bool:
        try:
            try:
                self.job_runner.pre_compute()
                parallel_job_runner = self.job_runner.get_parallel_job_runner()
                if parallel_job_runner:
                    self.raise_if_parallel_response_exists(
                        parallel_cache_kind=parallel_job_runner["job_type"],
                        parallel_job_version=parallel_job_runner["job_runner_version"],
                    )

                job_result = self.job_runner.compute()
                content = job_result.content

                # Validate content size
                if len(orjson_dumps(content)) > self.worker_config.content_max_bytes:
                    raise TooBigContentError(
                        "The computed response content exceeds the supported size in bytes"
                        f" ({self.worker_config.content_max_bytes})."
                    )
            finally:
                # ensure the post_compute hook is called even if the compute raises an exception
                self.job_runner.post_compute()
            upsert_response_params(
                kind=self.processing_step.cache_kind,
                job_params=self.job_params,
                content=content,
                http_status=HTTPStatus.OK,
                job_runner_version=self.job_runner.get_job_runner_version(),
                dataset_git_revision=self.job_params["revision"],
                progress=job_result.progress,
            )
            self.debug(
                f"dataset={self.job_params['dataset']} revision={self.job_params['revision']} job_info={self.job_info}"
                " is valid, cache updated"
            )
            return True
        except DatasetNotFoundError:
            # To avoid filling the cache, we don't save this error. Otherwise, DoS is possible.
            self.debug(f"the dataset={self.job_params['dataset']} could not be found, don't update the cache")
            return False
        except CachedArtifactError as err:
            # A previous step (cached artifact required by the job runner) is an error. We copy the cached entry,
            # so that users can see the underlying error (they are not interested in the internals of the graph).
            # We add an entry to details: "copied_from_artifact", with its identification details, to have a chance
            # to debug if needed.
            upsert_response_params(
                kind=self.processing_step.cache_kind,
                job_params=self.job_params,
                job_runner_version=self.job_runner.get_job_runner_version(),
                dataset_git_revision=self.job_params["revision"],
                # TODO: should we manage differently arguments above ^ and below v?
                content=err.cache_entry_with_details["content"],
                http_status=err.cache_entry_with_details["http_status"],
                error_code=err.cache_entry_with_details["error_code"],
                details=err.enhanced_details,
            )
            self.debug(f"response for job_info={self.job_info} had an error from a previous step, cache updated")
            return False
        except Exception as err:
            e = err if isinstance(err, CustomError) else UnexpectedError(str(err), err)
            upsert_response_params(
                kind=self.processing_step.cache_kind,
                job_params=self.job_params,
                job_runner_version=self.job_runner.get_job_runner_version(),
                dataset_git_revision=self.job_params["revision"],
                # TODO: should we manage differently arguments above ^ and below v?
                content=dict(e.as_response()),
                http_status=e.status_code,
                error_code=e.code,
                details=dict(e.as_response_with_cause()),
            )
            self.debug(f"response for job_info={self.job_info} had an error, cache updated")
            return False

    def backfill(self) -> None:
        """Evaluate the state of the dataset and backfill the cache if necessary."""
        DatasetState(
            dataset=self.job_params["dataset"],
            revision=self.job_params["revision"],
            processing_graph=self.processing_graph,
            error_codes_to_retry=ERROR_CODES_TO_RETRY,
            priority=self.priority,
        ).backfill()

    def set_crashed(self, message: str, cause: Optional[BaseException] = None) -> None:
        error = JobManagerCrashedError(message=message, cause=cause)
        upsert_response_params(
            kind=self.processing_step.cache_kind,
            job_params=self.job_params,
            content=dict(error.as_response()),
            http_status=error.status_code,
            error_code=error.code,
            details=dict(error.as_response_with_cause()),
            job_runner_version=self.job_runner.get_job_runner_version(),
            dataset_git_revision=self.job_params["revision"],
        )
        logging.debug(
            "response for"
            f" dataset={self.job_params['dataset']} revision={self.job_params['revision']} job_info={self.job_info}"
            " had an error (crashed), cache updated"
        )

    def set_exceeded_maximum_duration(self, message: str, cause: Optional[BaseException] = None) -> None:
        error = JobManagerExceededMaximumDurationError(message=message, cause=cause)
        upsert_response_params(
            kind=self.processing_step.cache_kind,
            job_params=self.job_params,
            content=dict(error.as_response()),
            http_status=error.status_code,
            error_code=error.code,
            details=dict(error.as_response_with_cause()),
            job_runner_version=self.job_runner.get_job_runner_version(),
            dataset_git_revision=self.job_params["revision"],
        )
        logging.debug(
            "response for"
            f" dataset={self.job_params['dataset']} revision={self.job_params['revision']} job_info={self.job_info}"
            " had an error (exceeded maximum duration), cache updated"
        )
