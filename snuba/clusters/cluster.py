from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Generic, Mapping, Optional, Sequence, Set, TypeVar


from snuba import settings
from snuba.clickhouse.escaping import escape_string
from snuba.clickhouse.http import HTTPBatchWriter
from snuba.clickhouse.native import ClickhousePool, NativeDriverReader
from snuba.clickhouse.sql import SqlQuery
from snuba.clusters.storage_sets import StorageSetKey
from snuba.reader import Reader, TQuery
from snuba.writer import BatchWriter, WriterTableRow


@dataclass(frozen=True)
class StorageNode:
    host_name: str
    port: int
    shard: Optional[int] = None
    replica: Optional[int] = None


TWriterOptions = TypeVar("TWriterOptions")


class Cluster(ABC, Generic[TQuery, TWriterOptions]):
    """
    A cluster is responsible for managing a collection of database nodes.

    Clusters are configurable, and will be instantiated based on user defined settings.

    Each storage must be mapped to a cluster via a storage set, which defines
    the storages that must be located on the same cluster.

    In future, clusters will also be responsible for co-ordinating commands that
    need to be run on multiple hosts that are colocated within the same cluster.
    The cluster will expose methods for:
        - bootstrap
        - migrate
        - cleanup
        - optimize
    """

    def __init__(self, storage_sets: Set[str]):
        self.__storage_sets = storage_sets

    def get_storage_set_keys(self) -> Set[StorageSetKey]:
        return {StorageSetKey(storage_set) for storage_set in self.__storage_sets}

    @abstractmethod
    def get_reader(self) -> Reader[TQuery]:
        raise NotImplementedError

    @abstractmethod
    def get_writer(
        self,
        table_name: str,
        encoder: Callable[[WriterTableRow], bytes],
        options: TWriterOptions,
        chunk_size: Optional[int],
    ) -> BatchWriter:
        raise NotImplementedError


ClickhouseWriterOptions = Optional[Mapping[str, Any]]


class ClickhouseCluster(Cluster[SqlQuery, ClickhouseWriterOptions]):
    """
    ClickhouseCluster provides a reader, writer and Clickhouse connections that are
    shared by all storages located on the cluster
    """

    def __init__(
        self,
        host: str,
        port: int,
        http_port: int,
        storage_sets: Set[str],
        single_node: bool,
        # The cluster name if single_node is set to False
        cluster_name: Optional[str] = None,
    ):
        super().__init__(storage_sets)
        if not single_node:
            assert cluster_name
        self.__host = host
        self.__port = port
        self.__http_port = http_port
        self.__single_node = single_node
        self.__clickhouse_rw = ClickhousePool(host, port)
        self.__clickhouse_ro = ClickhousePool(
            host, port, client_settings={"readonly": True},
        )
        self.__reader = NativeDriverReader(self.__clickhouse_ro)
        self.__cluster_name = cluster_name

    def __str__(self) -> str:
        return f"{self.__host}:{self.__port}"

    def get_clickhouse_rw(self) -> ClickhousePool:
        return self.__clickhouse_rw

    def get_clickhouse_ro(self) -> ClickhousePool:
        return self.__clickhouse_ro

    def get_reader(self) -> Reader[SqlQuery]:
        return self.__reader

    def get_writer(
        self,
        table_name: str,
        encoder: Callable[[WriterTableRow], bytes],
        options: ClickhouseWriterOptions,
        chunk_size: Optional[int],
    ) -> BatchWriter:
        return HTTPBatchWriter(
            table_name, self.__host, self.__http_port, encoder, options, chunk_size
        )

    def get_storage_nodes(self) -> Sequence[StorageNode]:
        if self.__single_node:
            return [StorageNode(self.__host, self.__port)]
        else:
            # Get the nodes from system.clusters
            return [
                StorageNode(*host)
                for host in self.get_clickhouse_ro().execute(
                    f"select host_name, port, shard_num, replica_num from system.clusters where cluster={escape_string(self.__cluster_name)}"
                )
            ]


CLUSTERS = [
    ClickhouseCluster(
        host=cluster["host"],
        port=cluster["port"],
        http_port=cluster["http_port"],
        storage_sets=cluster["storage_sets"],
        single_node=cluster["single_node"],
        cluster_name=cluster["cluster_name"] if "cluster_name" in cluster else None,
    )
    for cluster in settings.CLUSTERS
]

_registered_storage_sets = [
    storage_set
    for cluster in CLUSTERS
    for storage_set in cluster.get_storage_set_keys()
]

_unique_registered_storage_sets = set(_registered_storage_sets)

assert len(_registered_storage_sets) == len(
    _unique_registered_storage_sets
), "Storage set registered to more than one cluster"

assert (
    set(StorageSetKey) == _unique_registered_storage_sets
), "All storage sets must be assigned to a cluster"

# Map all storages to clusters via storage sets
_STORAGE_SET_CLUSTER_MAP = {
    storage_set: cluster
    for cluster in CLUSTERS
    for storage_set in cluster.get_storage_set_keys()
}


def get_cluster(storage_set_key: StorageSetKey) -> ClickhouseCluster:
    return _STORAGE_SET_CLUSTER_MAP[storage_set_key]
