"""Port: a resolver turns ERE requests into ERE responses (transport-independent)."""

from abc import abstractmethod
from typing import Protocol

from erspec.models.ere import ERERequest, EREResponse


class AbstractResolver(Protocol):
    """
    ERE resolver abstraction.

    An ERE resolver deals with the core of the job, i.e., it takes requests like
    :class:`erspec.models.ere.ERERequest` and computes results for them.
    A resolver doesn't deal with aspects like networking or asynchronous processing, these
    are concerns for services and entrypoints, which wrap around resolvers.

    As you can see, it makes sense to define resolvers as :class:`Protocol` classes, so that,
    for instance, even a simple lambda could be used as a resolver.
    """

    @abstractmethod
    def process_request(self, request: ERERequest) -> EREResponse:
        """
        Resolve an entity resolution request, returning the corresponding response.

        This only concerns the resolution logic, leaving out aspects like transport or
        asynchronous processing.

        This should take care of wrapping exceptions into ErrorResponse results.
        """

    def __call__(self, request: ERERequest) -> EREResponse:
        return self.process_request(request)
