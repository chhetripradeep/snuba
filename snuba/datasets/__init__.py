from typing import Any, Optional, Mapping, Sequence

from snuba.datasets.dataset_schemas import DatasetSchemas
from snuba.datasets.table_storage import KafkaFedTableWriter
from snuba.query.extensions import get_time_limit
from snuba.util import escape_col


class Dataset(object):
    """
    A Dataset defines the complete set of data sources, schemas, and
    transformations that are required to:
        - Consume, transform, and insert data payloads from Kafka into Clickhouse.
        - Define how Snuba API queries are transformed into SQL.

    This is the the initial boilerplate. schema and processor will come.
    """

    def __init__(self,
            dataset_schemas: DatasetSchemas,
            *,
            table_writer: Optional[KafkaFedTableWriter],
            default_replacement_topic: Optional[str] = None,
            default_commit_log_topic: Optional[str] = None):
        self.__dataset_schemas = dataset_schemas
        self.__table_writer = table_writer
        self.__default_replacement_topic = default_replacement_topic
        self.__default_commit_log_topic = default_commit_log_topic

    def get_dataset_schemas(self) -> DatasetSchemas:
        return self.__dataset_schemas

    def get_table_writer(self) -> KafkaFedTableWriter:
        table_writer = self.__table_writer
        assert table_writer, "This dataset does not support writing"
        return table_writer

    def default_conditions(self):
        """
        Return a list of the default conditions that should be applied to all
        queries on this dataset.
        """
        return []

    def get_extensions_conditions(
        self,
        extensions: Mapping[str, Mapping[str, Any]],
    ) -> Sequence[Any]:
        return []

    def get_default_replacement_topic(self) -> Optional[str]:
        return self.__default_replacement_topic

    def get_default_commit_log_topic(self) -> Optional[str]:
        return self.__default_commit_log_topic

    def column_expr(self, column_name, body):
        """
        Return an expression for the column name. Handle special column aliases
        that evaluate to something else.
        """
        return escape_col(column_name)

    def get_query_schema(self):
        raise NotImplementedError('dataset does not support queries')

    def get_prewhere_keys(self) -> Sequence[str]:
        """
        Returns the keys that will be upgraded from a WHERE condition to a PREWHERE.

        This is an ordered list, from highest priority to lowest priority. So, a column at index 1 will be upgraded
        before a column at index 2. This is relevant when we have a maximum number of prewhere keys.
        """
        return []


class TimeSeriesDataset(Dataset):
    def __init__(self, *args,
            dataset_schemas: DatasetSchemas,
            time_group_columns: Mapping[str, str],
            timestamp_column: str,
            **kwargs):
        super().__init__(*args, dataset_schemas=dataset_schemas, **kwargs)
        # Convenience columns that evaluate to a bucketed time. The bucketing
        # depends on the granularity parameter.
        # The bucketed time column names cannot be overlapping with existing
        # schema columns
        read_schema = dataset_schemas.get_read_schema()
        if read_schema:
            for bucketed_column in time_group_columns.keys():
                assert \
                    bucketed_column not in read_schema.get_columns(), \
                    f"Bucketed column {bucketed_column} is already defined in the schema"
        self.__time_group_columns = time_group_columns
        self.__timestamp_column = timestamp_column

    def __time_expr(self, column_name: str, granularity: int) -> str:
        real_column = self.__time_group_columns[column_name]
        template = {
            3600: 'toStartOfHour({column})',
            60: 'toStartOfMinute({column})',
            86400: 'toDate({column})',
        }.get(granularity, 'toDateTime(intDiv(toUInt32({column}), {granularity}) * {granularity})')
        return template.format(column=real_column, granularity=granularity)

    def get_extensions_conditions(
        self,
        extensions: Mapping[str, Mapping[str, Any]],
    ) -> Sequence[Any]:
        """
        This method is temporary. As soon as we are done with #456
        the processing would happen the other way around:
        the dataset will provide extensions and api.py (not directly)
        will run the processing.
        Passing extensions instead of the query because I want to keep
        its scope limited.
        """
        from_date, to_date = get_time_limit(extensions['timeseries'])
        return [
            (self.__timestamp_column, '>=', from_date.isoformat()),
            (self.__timestamp_column, '<', to_date.isoformat()),
        ]

    def column_expr(self, column_name, body):
        if column_name in self.__time_group_columns:
            return self.__time_expr(column_name, body['granularity'])
        else:
            return super().column_expr(column_name, body)
