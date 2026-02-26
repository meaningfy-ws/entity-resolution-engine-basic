import asyncio
import logging

import redis
from linkml_runtime.dumpers import JSONDumper

from ere.adapters import AbstractResolver
from erspec.models.ere import ERERequest, EREResponse
from ere.services import AbstractPubSubResolutionService
from ere.adapters.utils import get_request_from_message

log = logging.getLogger(__name__)

_linkml_dumper = JSONDumper()  # Just to cache it


class RedisConnectionConfig:
    """
    Simple data class to hold Redis connection configuration.
    """

    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 0):
        self.host = host
        self.port = port
        self.db = db

    def __str__(self) -> str:
        return f'RedisConnectionConfig ( host: "{self.host}", port: "{self.port}", db: "{self.db}" )'


class RedisResolutionService(AbstractPubSubResolutionService):
    """
    An ERE resolution service that uses Redis as the publish-subscribe mechanism.

    This class should implement the methods to fetch requests from a Redis channel
    and push responses to another Redis channel. The actual resolution logic is
    delegated to the provided resolver.
    """

    def __init__(
        self,
        resolver: AbstractResolver = None,
        config_or_client: RedisConnectionConfig | redis.Redis = RedisConnectionConfig(),
    ):
        super().__init__(resolver)

        if isinstance(config_or_client, RedisConnectionConfig):
            self.config = config_or_client
            log.info(f"RedisResolutionService: connecting to {self.config}")
            self._redis_client = redis.Redis(
                host=self.config.host, port=self.config.port, db=self.config.db
            )
        else:
            log.info(
                f"RedisResolutionService: using existing redis client #{id(config_or_client)}"
            )
            conn_args = config_or_client.connection_pool.connection_kwargs
            log.debug(
                f"Redis client config: host={conn_args.get('host')}, port={conn_args.get('port')}, db={conn_args.get('db')}, unix_socket_path={conn_args.get('unix_socket_path')}"
            )
            self._redis_client = config_or_client

        self.character_encoding = "utf-8"

        self.request_channel_id = "ere_requests"
        self.response_channel_id = "ere_responses"

    async def _pull_request(self) -> ERERequest:
        log.debug(
            f"RedisResolutionService, Pulling request from channel: {self.request_channel_id}"
        )

        loop = asyncio.get_running_loop()
        _, raw_msg = await loop.run_in_executor(
            None,
            lambda: self._redis_client.brpop(
                self.request_channel_id, timeout=self.async_timeout
            ),
        )

        request = get_request_from_message(raw_msg, self.character_encoding)
        log.debug(f"RedisResolutionService, pulled request id: {request.ere_request_id}")
        return request

    def _push_response(self, response: EREResponse):
        log.debug(
            f"RedisResolutionService, pushing response id: {response.ere_request_id} to channel: {self.response_channel_id}"
        )
        msg_json_str = _linkml_dumper.dumps(response)
        self._redis_client.lpush(self.response_channel_id, msg_json_str)
        log.debug(f"RedisResolutionService, response id: {response.ere_request_id} sent")
