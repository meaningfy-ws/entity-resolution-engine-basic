import redis
from linkml_runtime.dumpers import JSONDumper
from redis.exceptions import ConnectionError, TimeoutError

from ere.adapters.utils import get_response_from_message
from ere.services.redis import RedisConnectionConfig, log

_linkml_dumper = JSONDumper()  # Just to cache it

from abc import ABC, abstractmethod
from collections.abc import Generator

from erspec.models.ere import ERERequest, EREResponse


class AbstractClient(ABC):
    """
    Abstraction of a client to access with an ERE instance.
    """

    @abstractmethod
    def push_request(self, request: ERERequest):
        """
        Pushes a request to the request channel of the ERE system.

        See the ERE Contract document for details.
        """

    @abstractmethod
    def subscribe_responses(self) -> Generator[EREResponse, None, None]:
        """
        Subscribes to the response channel.

        This is a generator that yields responses as the implementation publishes them
        to the response channel.
        """

class RedisEREClient(AbstractClient):
    """
    A simple ERE client that interacts with a RedisResolutionService.

    """

    def __init__(
        self,
        config_or_client: RedisConnectionConfig | redis.Redis = RedisConnectionConfig(),
    ):
        if isinstance(config_or_client, RedisConnectionConfig):
            self.config = config_or_client
            log.info(f"RedisEREClient: connecting to {self.config}")
            self._redis_client = redis.Redis(
                host=self.config.host, port=self.config.port, db=self.config.db
            )
        else:
            log.info(
                f"RedisEREClient: using existing redis client #{id(config_or_client)}"
            )
            conn_args = config_or_client.connection_pool.connection_kwargs
            log.debug(
                f"Redis client config: host={conn_args.get('host')}, port={conn_args.get('port')}, db={conn_args.get('db')}, unix_socket_path={conn_args.get('unix_socket_path')}"
            )
            self._redis_client = config_or_client

        self.character_encoding = "utf-8"

        self.request_channel_id = "ere_requests"
        self.response_channel_id = "ere_responses"

    def push_request(self, request: ERERequest):
        log.debug(
            f"Redis ERE client, pushing request id: {request.ere_request_id} to channel: {self.request_channel_id}"
        )
        msg_json_str = _linkml_dumper.dumps(request)
        self._redis_client.lpush(self.request_channel_id, msg_json_str)
        log.debug(f"Redis ERE client, request id: {request.ere_request_id} sent")

    def subscribe_responses(self) -> Generator[EREResponse, None, None]:
        while True:
            try:
                log.debug(
                    f"Redis ERE client, waiting for response on channel: {self.response_channel_id}"
                )
                _, raw_msg = self._redis_client.brpop(self.response_channel_id)
                response = get_response_from_message(raw_msg, self.character_encoding)
                log.debug(
                    f"Redis ERE client, received response id: {response.ere_request_id}"
                )
                yield response
            except (ConnectionError, TimeoutError) as ex:
                log.error(
                    f"Redis ERE client, ending subscribe_responses() due to connection issue: {ex}"
                )
                raise
