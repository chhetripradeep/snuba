from abc import ABC, abstractmethod
from typing import Any, Generic, Mapping, TypeVar

from snuba.query.query import Query
from snuba.request.request_settings import RequestSettings


class QueryProcessor(ABC):
    """
    A transformation applied to a Query. This depends on the query structure and
    on the request settings. No additional context is provided.
    This transformation mutates the Query class in place.
    """

    @abstractmethod
    def process_query(self,
        query: Query,
        request_settings: RequestSettings,
    ) -> None:
        # TODO: Now the query is moved around through the Request object, which
        # is frozen (and it should be), thus the Query itself is mutable since
        # we cannot reassign it.
        # Ideally this should return a query insteadof assuming it mutates the
        # existing one in place. We can move towards an immutable structure
        # after changing Request.
        raise NotImplementedError


ExtensionData = Mapping[str, Any]


class ExtensionQueryProcessor:
    """
    Common parent class for all the extension processors.
    Extension processors are provided by the QueryExtensions for a dataset,
    they are fed with the raw extension data and can make changes to query
    and settings.

    Extension processors are executed very early in the query parsing phase,
    this happens right after schema validation.
    """

    @abstractmethod
    def process_query(
            self, query: Query,
            extension_data: ExtensionData,
            request_settings: RequestSettings,
    ) -> None:
        raise NotImplementedError
