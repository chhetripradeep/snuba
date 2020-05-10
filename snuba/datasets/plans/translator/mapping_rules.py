from abc import ABC, abstractmethod
from typing import Any, Generic, Callable, Mapping, Optional, TypeVar, Tuple

from snuba.query.expressions import (
    Argument,
    Column,
    CurriedFunctionCall,
    Expression,
    ExpressionVisitor,
    FunctionCall,
    Lambda,
    Literal,
    SubscriptableReference,
)

TExpIn = TypeVar("TExpIn", bound=Expression)
TExpOut = TypeVar("TExpOut")


class SimpleExpressionMappingRule(ABC, Generic[TExpIn, TExpOut]):
    @abstractmethod
    def attemptMap(self, expression: TExpIn) -> Optional[TExpOut]:
        raise NotImplementedError


class StructuredExpressionMappingRule(ABC, Generic[TExpIn, TExpOut]):
    @abstractmethod
    def attemptMap(
        self, expression: TExpIn, children_translator: ExpressionVisitor[TExpOut]
    ) -> Optional[TExpOut]:
        raise NotImplementedError


ExpressionMatcher = Callable[
    [TExpIn], Tuple[bool, Mapping[str, Any], Mapping[str, TExpIn]]
]
StructuredRuleExecutor = Callable[
    [TExpIn, Mapping[str, Any], Mapping[str, TExpOut]], TExpOut,
]


class StructuredMatchingMappingRule(
    StructuredExpressionMappingRule[TExpIn, TExpOut], Generic[TExpIn, TExpOut]
):
    def __init__(
        self,
        matcher: ExpressionMatcher[TExpIn],
        executor: StructuredRuleExecutor[TExpIn, TExpOut],
    ) -> None:
        self.__matcher = matcher
        self.__executor = executor

    def attemptMap(
        self, expression: TExpIn, children_translator: ExpressionVisitor[TExpOut]
    ) -> Optional[TExpOut]:
        accepted_expression, parameters, to_translate = self.__matcher(expression)
        if not accepted_expression:
            return None
        translated_params = {
            param: exp.accept(children_translator)
            for param, exp in to_translate.items()
        }
        return self.__executor(expression, parameters, translated_params)


class ColumnMapper(SimpleExpressionMappingRule[Column, TExpOut], ABC):
    pass


class LiteralMapper(SimpleExpressionMappingRule[Literal, TExpOut], ABC):
    pass


class ArgumentMapper(SimpleExpressionMappingRule[Argument, TExpOut], ABC):
    pass


class FunctionMapper(StructuredExpressionMappingRule[FunctionCall, TExpOut], ABC):
    pass


class CurriedFunctionMapper(
    StructuredExpressionMappingRule[CurriedFunctionCall, TExpOut], ABC
):
    pass


class SubscriptableMapper(
    StructuredExpressionMappingRule[SubscriptableReference, TExpOut], ABC
):
    pass


class LambdaMapper(StructuredExpressionMappingRule[Lambda, TExpOut], ABC):
    pass
