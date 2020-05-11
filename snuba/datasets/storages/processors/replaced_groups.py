import logging

from typing import Optional

from snuba import environment, settings
from snuba.clickhouse.processors import QueryProcessor
from snuba.clickhouse.query import Query
from snuba.clickhouse.query_dsl.accessors import get_project_ids_in_query
from snuba.datasets.errors_replacer import ReplacerState, get_projects_query_flags
from snuba.query.conditions import not_in_condition
from snuba.query.expressions import Column, FunctionCall, Literal
from snuba.request.request_settings import RequestSettings
from snuba.state import get_config
from snuba.util import is_condition
from snuba.utils.metrics.backends.wrapper import MetricsWrapper

CONSISTENCY_ENFORCER_PROCESSOR_ENABLED = "consistency_enforcer_processor_enabled"

logger = logging.getLogger(__name__)
metrics = MetricsWrapper(environment.metrics, "processors.replaced_groups")


class PostReplacementConsistencyEnforcer(QueryProcessor):
    """
    This processor tweaks the query to ensure that groups that have been manipulated
    by a replacer (like after a deletion) are excluded if they need to be.

    There is a period of time between the replacement executing its query and Clickhouse
    merging the rows to achieve consistency. During this period of time we either
    have to remove those rows manually or to run the query in FINAL mode.
    """

    def __init__(
        self, project_column: str, replacer_state_name: Optional[ReplacerState]
    ) -> None:
        self.__project_column = project_column
        # This is used to allow us to keep the replacement state in redis for multiple
        # replacers on multiple tables. replacer_state_name is part of the redis key.
        self.__replacer_state_name = replacer_state_name

    def process_query(self, query: Query, request_settings: RequestSettings) -> None:
        if request_settings.get_turbo():
            return

        activated = get_config(CONSISTENCY_ENFORCER_PROCESSOR_ENABLED, 0)
        try:
            project_ids = get_project_ids_in_query(query, self.__project_column)
        except Exception as e:
            if activated:
                raise e
            else:
                logger.warning("Failed to find project ids in the Query", exc_info=True)
                return

        set_final = False
        condition_to_add = None
        if project_ids:
            final, exclude_group_ids = get_projects_query_flags(
                list(project_ids), self.__replacer_state_name,
            )
            if not final and exclude_group_ids:
                # If the number of groups to exclude exceeds our limit, the query
                # should just use final instead of the exclusion set.
                max_group_ids_exclude = get_config(
                    "max_group_ids_exclude", settings.REPLACER_MAX_GROUP_IDS_TO_EXCLUDE
                )
                if len(exclude_group_ids) > max_group_ids_exclude:
                    set_final = True
                else:
                    condition_to_add = (
                        ["assumeNotNull", ["group_id"]],
                        "NOT IN",
                        exclude_group_ids,
                    )
                    query.add_condition_to_ast(
                        not_in_condition(
                            None,
                            FunctionCall(
                                None, "assumeNotNull", (Column(None, "group_id", None),)
                            ),
                            [Literal(None, p) for p in exclude_group_ids],
                        )
                    )
            else:
                set_final = final

        if activated:
            query.set_final(set_final)
            if condition_to_add:
                query.add_conditions([condition_to_add])
        else:
            # This is disabled. We assume the query was modified accordingly by the extension
            # and compare the results.
            # TODO: Swap all this with logger warnings after verifying this does not flood the logs
            log_issue = False
            if set_final != query.get_final():
                log_issue = True
                metrics.increment("mismatch.final")

            existing_groups_conditions = [
                c[2]
                for c in query.get_conditions() or []
                if is_condition(c)
                and c[0] == ["assumeNotNull", ["group_id"]]
                and c[1] == "NOT IN"
            ]

            if len(existing_groups_conditions) > 1:
                metrics.increment("mismatch.multiple_group_condition")
                logger.warning(
                    "Multiple group exclusion conditions in the Query", exc_info=True
                )
                return

            if (condition_to_add[2] if condition_to_add else None) != (
                existing_groups_conditions[0]
                if len(existing_groups_conditions) > 0
                else None
            ):
                metrics.increment(
                    "mismatch.group_id",
                    tags={
                        "extension": str(len(existing_groups_conditions) > 0),
                        "processor": str(condition_to_add is not None),
                    },
                )
                log_issue = True

            if log_issue:
                logger.warning(
                    "Discrepancy between project_extension and replacement consistency processor",
                    exc_info=True,
                )
            else:
                metrics.increment("match")