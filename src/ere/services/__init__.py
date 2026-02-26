"""
Abstract definitions for the ERE service
"""

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from concurrent.futures import Executor, ThreadPoolExecutor
from threading import Thread

from ere.adapters import AbstractResolver
from erspec.models.ere import ERERequest, EREResponse

log = logging.getLogger(__name__)


class AbstractService(ABC):
    """
    In general, an ERE service can be :meth:`run` or started in a background thread using :meth:`start`.
    """

    def __init__(self):
        """

        ## Attributes

        - is_running: A read-only boolean flag indicating whether the service is running.
          This is set by :meth:`run` (and hence, by :meth:`start`) and reset by :meth:`stop`.
                Concrete implementations should check this flag to decide whether to keep running.

        - async_timeout: The timeout (in seconds) for waiting upon blocking asynchronous calls
                made during the service lifecycle (eg, :meth:`_pull_request`). This ensures that
                the service (eg, a service loop) can periodically check whether it was stopped and
                exit cleanly. It mainly affects how long it takes to stop the service and how much
                CPU overhead the service causes (eg, by waking often in a service loop). You should
                be fine with the default value, but cases like tests can benefit from a lower value.
        """

        self.async_timeout: float = 3
        self._thread: Thread = None
        # To back is_running, it's set/reset by run()/stop()
        self._is_running: bool = False

    @abstractmethod
    def run(self):
        """
        Runs the service and blocks until it's stopped by some external event, such as SIGINT/SIGTERM.

        This is supposed to be used in situations like a CLI wrapper. The alternative (eg, in tests) is
        to run the service in a background thread, which is available from :meth:`start`.

        The default implementation just sets an internal flag to make :attr:`is_running` return True.
        This implies that a concrete implementation should call this before doing the actual running.
        """

        if self._is_running:
            raise RuntimeError(
                f"{self.__class__.__name__}.run(): service is already running"
            )

        log.info(f"Entering {self.__class__.__name__}.run()")
        self._is_running = True

    def start(self):
        """
        Starts the service, by calling :meth:`run` in a background thread.

        If your service implementation has special things to do before thread wrapping, you
        should call this method (or better, do your own things in :meth:`run`)
        """

        def runner():
            # The background thread needs its own event loop, in order to not have interference
            # from the main thread.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                # loop.run_until_complete ( self.run() )
                self.run()
            finally:
                loop.close()

        log.info(f"Starting {self.__class__.__name__} in the background")
        # Unfortunately, components like pytest seems to ignore daemon mode, but having it doesn't
        # hurt.
        #
        self._thread = Thread(target=runner, daemon=True)
        self._thread.start()
        # TODO: wait until the service is really started?
        log.info(f"{self.__class__.__name__} started in the background")

    def stop(self):
        if not self._is_running:
            log.warning(
                f"{self.__class__.__name__}.stop(): service is not running, ignoring stop request"
            )
            return

        log.info(f"Stopping {self.__class__.__name__}")
        self._is_running = False

        if not self._thread:
            # It was started in the foreground by calling run(), so we're done
            log.info(f"{self.__class__.__name__} stopped")
            return

        self._thread.join(timeout=self.async_timeout + 1.0)
        if self._thread.is_alive():
            log.warning(
                f"{self.__class__.__name__}.stop(): background thread did not stop within the configured timeout"
            )
        else:
            log.info(f"{self.__class__.__name__} stopped")

        self._thread = None

    @property
    def is_running(self) -> bool:
        return self._is_running


class AbstractPubSubResolutionService(AbstractService):
    """
    An abstract ERE resolution service that works in a publish-subscribe fashion.

    This is a skeleton for concrete implementations that base their service on
    fetching requests from some source (a channel, a message queue, etc) and pushing
    responses to some sink (a channel, a message queue, etc).

    As such, it delegates the actual resolution to an :class:`AbstractResolver`, and
    we wrap it with placeholders and defaults to manage the publish-subscribe cycle
    in asynchronous/parallel fashion. See below for details.


    ## Attributes

    - resolver: An :class:`AbstractResolver` instance that does the actual resolution work.

    - parallelism: The number of parallel workers to use for processing requests.
            By default, it uses the number of CPU cores.

    - executor_type: The type of executor to use for parallel processing. By default, it
            uses :class:`ThreadPoolExecutor`. :class:`InterpreterPoolExecutor` should be better
            for CPU-bound tasks, but we have experienced various problems with it (eg, resolution
            workers not starting).
    """

    def __init__(self, resolver: AbstractResolver = None):
        super().__init__()
        self.resolver: AbstractResolver = resolver
        self.parallelism: int = os.cpu_count()
        self.executor_type: Executor = ThreadPoolExecutor

    @abstractmethod
    async def _pull_request(self) -> ERERequest:
        """
        Pulls a request from a request channel or alike resource.

        This is an abstract placeholder to be implemented by concrete subclasses.
        """

    @abstractmethod
    def _push_response(self, response: EREResponse):
        """
        Pushes a response to a response channel or alike resource.

        This is an abstract placeholder to be implemented by concrete subclasses.
        """

    def run(self):
        super().run()  # Sets is_running to True
        asyncio.run(self._service_loop())

    async def _service_loop(self):
        """
        The service loop. The default implementation keeps pulling requests, sending them
        to the delegate resolver and pushing the responses.

        This is based on:
        - Calling the :meth:`_pull_request` asynchronously
        - Sending requests to the delegate resolver in parallel, using the configured
          :attr:`executor_type` and :attr:`parallelism`, and :meth:`_process_push_helper`
        - Repeating, while :meth:`_process_push_helper` pushes responses in parallel (see it)

        TODO: The input queue isn't bounded. Usually, this can be set in the implementing
        subsystem (eg, Redis). In future, we may want to add semaphore-based limiting.
        """

        try:
            with self.executor_type(max_workers=self.parallelism) as executor:
                log.debug(
                    f"PubSubResolutionService: starting service loop with parallelism: {self.parallelism}, executor type: {self.executor_type.__name__}"
                )
                while self._is_running:
                    # We need this to allow for periodically checking if we were stopped
                    try:
                        request = await asyncio.wait_for(
                            self._pull_request(), timeout=self.async_timeout
                        )
                        if request is None:
                            continue  # timeout or shutdown
                        log.debug(
                            f"PubSubResolutionService: dispatching request id: {request.ere_request_id}"
                        )
                        executor.submit(self._process_push_helper, request)
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            # TODO: graceful shutdown (ie, synch with executor)
            log.info("Service loop cancelled, shutting down.")

    def _process_push_helper(self, request: ERERequest):
        """
        Helper used by :meth:`_service_loop` to submit a request to the delegate resolver
        and push its response to :meth:`_push_response`.

        Since this method is passed to the service's executor, both the two steps above
        are a sequence that is run in parallel, while :meth:`_service_loop` keeps pulling
        requests and dispatching them to this method.
        """

        log.debug(
            f"Service: sending request id: {request.ere_request_id} to the resolver"
        )
        response = self.resolver.process_request(request)
        log.debug(
            f"Service: got response for request id: {request.ere_request_id} from the resolver, pushing it back"
        )
        self._push_response(response)
