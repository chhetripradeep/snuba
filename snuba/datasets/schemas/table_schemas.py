from abc import ABC, abstractmethod
from typing import Callable, Mapping, Optional, Sequence

from snuba import settings
from snuba.clickhouse.columns import ColumnSet
from snuba.datasets.schemas import Schema, local_dataset_mode


class TableSchema(Schema, ABC):
    """
    Represent a table-like schema. This means it represents either
    a Clickhouse table, a Clickhouse view or a Materialized view.

    Specifically a TableSchema is something we can read from through
    a simple select and that provides DDL operations.
    """

    TEST_TABLE_PREFIX = "test_"

    def __init__(self,
        *,
        columns: ColumnSet,
        local_table_name: str,
        dist_table_name: str,
        migration_function: Optional[Callable[[str, Mapping[str, str]], Sequence[str]]]=None,
    ):
        super().__init__(
            columns=columns,
        )
        self.__migration_function = migration_function if migration_function else lambda table, schema: []
        self.__local_table_name = local_table_name
        self.__dist_table_name = dist_table_name

    def get_from_clause(self) -> str:
        """
        In this abstraction the from clause is just the same
        table we refer to for writes.
        """
        return self.get_table_name()

    def _make_test_table(self, table_name: str) -> str:
        return table_name if not settings.TESTING else "%s%s" % (self.TEST_TABLE_PREFIX, table_name)

    def get_local_table_name(self) -> str:
        """
        This returns the local table name for a distributed environment.
        It is supposed to be used in DDL commands and for maintenance.
        """
        return self._make_test_table(self.__local_table_name)

    def get_table_name(self) -> str:
        """
        This represents the table we interact with to send queries to Clickhouse.
        In distributed mode this will be a distributed table. In local mode it is a local table.
        """
        table_name = self.__local_table_name if local_dataset_mode() else self.__dist_table_name
        return self._make_test_table(table_name)

    def _get_table_definition(self, name: str, engine: str) -> str:
        return """
        CREATE TABLE IF NOT EXISTS %(name)s (%(columns)s) ENGINE = %(engine)s""" % {
            'columns': self.get_columns().for_schema(),
            'engine': engine,
            'name': name,
        }

    def get_local_table_definition(self) -> str:
        return self._get_table_definition(
            self.get_local_table_name(),
            self._get_local_engine()
        )

    @abstractmethod
    def _get_local_engine(self) -> str:
        raise NotImplementedError

    def get_local_drop_table_statement(self) -> str:
        return "DROP TABLE IF EXISTS %s" % self.get_local_table_name()

    def get_migration_statements(
        self
    ) -> Callable[[str, Mapping[str, str]], Sequence[str]]:
        return self.__migration_function


class WritableTableSchema(TableSchema):
    """
    This class identifies a subset of TableSchemas we can write onto.
    While it does not provide any functionality by itself, it is used
    to allow the type checker to prevent us from returning a read only
    schema from DatasetSchemas.
    """
    pass


class MergeTreeSchema(WritableTableSchema):

    def __init__(self,
        *,
        columns: ColumnSet,
        local_table_name: str,
        dist_table_name: str,
        order_by: str,
        partition_by: str,
        sample_expr: Optional[str]=None,
        settings: Optional[Mapping[str, str]]=None,
        migration_function: Optional[Callable[[str, Mapping[str, str]], Sequence[str]]]=None,
    ):
        super(MergeTreeSchema, self).__init__(
            columns=columns,
            local_table_name=local_table_name,
            dist_table_name=dist_table_name,
            migration_function=migration_function)
        self.__order_by = order_by
        self.__partition_by = partition_by
        self.__sample_expr = sample_expr
        self.__settings = settings

    def _get_engine_type(self) -> str:
        return "MergeTree()"

    def _get_local_engine(self) -> str:
        partition_by_clause = ("PARTITION BY %s" %
            self.__partition_by) if self.__partition_by else ''

        sample_clause = ("SAMPLE BY %s" %
            self.__sample_expr) if self.__sample_expr else ''

        if self.__settings:
            settings_list = ["%s=%s" % (k, v) for k, v in self.__settings.items()]
            settings_clause = "SETTINGS %s" % ", ".join(settings_list)
        else:
            settings_clause = ''

        return """
            %(engine_type)s
            %(partition_by_clause)s
            ORDER BY %(order_by)s
            %(sample_clause)s
            %(settings_clause)s;""" % {
            'engine_type': self._get_engine_type(),
            'order_by': self.__order_by,
            'partition_by_clause': partition_by_clause,
            'sample_clause': sample_clause,
            'settings_clause': settings_clause,
        }


class ReplacingMergeTreeSchema(MergeTreeSchema):

    def __init__(self,
        *,
        columns: ColumnSet,
        local_table_name: str,
        dist_table_name: str,
        order_by: str,
        partition_by: str,
        version_column: str,
        sample_expr: Optional[str]=None,
        settings: Optional[Mapping[str, str]]=None,
        migration_function: Optional[Callable[[str, Mapping[str, str]], Sequence[str]]]=None,
    ) -> None:
        super(ReplacingMergeTreeSchema, self).__init__(
            columns=columns,
            local_table_name=local_table_name,
            dist_table_name=dist_table_name,
            order_by=order_by,
            partition_by=partition_by,
            sample_expr=sample_expr,
            settings=settings,
            migration_function=migration_function)
        self.__version_column = version_column

    def _get_engine_type(self) -> str:
        return "ReplacingMergeTree(%s)" % self.__version_column


class SummingMergeTreeSchema(MergeTreeSchema):

    def _get_engine_type(self) -> str:
        return "SummingMergeTree()"


class MaterializedViewSchema(TableSchema):

    def __init__(
            self,
            *,
            columns: ColumnSet,
            local_materialized_view_name: str,
            dist_materialized_view_name: str,
            query: str,
            local_source_table_name: str,
            local_destination_table_name: str,
            dist_source_table_name: str,
            dist_destination_table_name: str,
            migration_function: Optional[Callable[[str, Mapping[str, str]], Sequence[str]]] = None) -> None:
        super().__init__(
            columns=columns,
            local_table_name=local_materialized_view_name,
            dist_table_name=dist_materialized_view_name,
            migration_function=migration_function,
        )

        # Make sure the caller has provided a source_table_name in the query
        assert query % {'source_table_name': local_source_table_name} != query

        self.__query = query
        self.__local_source_table_name = local_source_table_name
        self.__local_destination_table_name = local_destination_table_name
        self.__dist_source_table_name = dist_source_table_name
        self.__dist_destination_table_name = dist_destination_table_name

    def __get_local_source_table_name(self) -> str:
        return self._make_test_table(self.__local_source_table_name)

    def __get_local_destination_table_name(self) -> str:
        return self._make_test_table(self.__local_destination_table_name)

    def _get_local_engine(self) -> str:
        pass

    def __get_table_definition(self, name: str, source_table_name: str, destination_table_name: str) -> str:
        return """
        CREATE MATERIALIZED VIEW IF NOT EXISTS %(name)s TO %(destination_table_name)s (%(columns)s) AS %(query)s""" % {
            'name': name,
            'destination_table_name': destination_table_name,
            'columns': self.get_columns().for_schema(),
            'query': self.__query,
        } % {
            'source_table_name': source_table_name,
        }

    def get_local_table_definition(self) -> str:
        return self.__get_table_definition(
            self.get_local_table_name(),
            self.__get_local_source_table_name(),
            self.__get_local_destination_table_name(),
        )