from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Optional, Sequence, TypeVar, Union

from snuba.clickhouse.translator.snuba import SnubaClickhouseTranslator
from snuba.datasets.plans.translator.mapper import ExpressionMapper
from snuba.clickhouse.translator.rules import (
    ColumnMapper,
    ArgumentMapper,
    LiteralMapper,
    FunctionCallMapper,
    CurriedFunctionCallMapper,
    SubscriptableReferenceMapper,
    LambdaMapper,
)
from snuba.query.expressions import (
    Column,
    Argument,
    CurriedFunctionCall,
    FunctionCall,
    Lambda,
    Literal,
    SubscriptableReference,
)

TExpIn = TypeVar("TExpIn")
TExpOut = TypeVar("TExpOut")


@dataclass(frozen=True)
class TranslationRules:
    """
    Represents the set of rules to be used to configure a RuleBasedTranslator.
    It encapsulates different sequences of rules. Each one produces a different
    expression type, this is because, in a Clickhouse AST, several nodes have children
    of a specific type, so we need strictly typed rules that produce those specific
    types to guarantee we produce a valid AST.

    This is parametric with respect to the input expression type so we will be able to
    to use this translator to either translate a Snuba expression into a clickhouse
    expression as well as to transform a Clickhouse expression into another one, for
    query processors.
    The downside of keeping TExpIn parametric instead of hardcoding the Snuba expression
    is that all ExpressionMapper have to take in the same type even in cases where we
    could be stricter. Being able to translate both ASTs with this abstractions seem
    to be a reasonable tradeoff.
    """

    columns: Sequence[ColumnMapper] = field(default_factory=list)
    literals: Sequence[LiteralMapper] = field(default_factory=list)
    functions: Sequence[FunctionCallMapper] = field(default_factory=list)
    curried_functions: Sequence[CurriedFunctionCallMapper] = field(default_factory=list)
    subscriptables: Sequence[SubscriptableReferenceMapper] = field(default_factory=list)
    lambdas: Sequence[LambdaMapper] = field(default_factory=list)
    arguments: Sequence[ArgumentMapper] = field(default_factory=list)

    def concat(self, spec: TranslationRules) -> TranslationRules:
        return TranslationRules(
            columns=[*self.columns, *spec.columns],
            literals=[*self.literals, *spec.literals],
            functions=[*self.functions, *spec.functions],
            curried_functions=[*self.curried_functions, *spec.curried_functions],
            subscriptables=[*self.subscriptables, *spec.subscriptables],
            lambdas=[*self.lambdas, *spec.lambdas],
            arguments=[*self.arguments, *spec.arguments],
        )


class SnubaClickhouseRulesTranslator(SnubaClickhouseTranslator):
    """
    Translates an expression into an clickhouse query expression.

    The translation of every node in the expression is performed by a series of rules
    that extend ExpressionMapper.
    Rules are applied in sequence. Given an expression, the first valid rule for such
    expression is applied and the result is returned. If no rule can translate such
    expression an exception is raised.
    A rule can delegate the translation of its children back to this translator.

    Each rule only has context around the expression provided and its children. It does
    not have general context around the query or around the expression's ancestors in
    the AST.
    This approach implies that, while rules are specific to the relationship between
    dataset (later entities) and storage, this class keeps the responsibility of
    orchestrating the translation process.

    It is possible to compose different, independently defined, sets of rules that are
    applied in a single pass over the AST.
    This allows us to support joins and multi-step translations (for multi table
    storages) as an example:
    Joins can be supported by simply concatenating rule sets associated with each storage.
    Multi-step (still TODO) translations can be supported by applying a second sequence of
    rules to the result of the first one for each node in the expression to be translated.
    """

    def __init__(self, translation_rules: TranslationRules) -> None:
        default_rules = TranslationRules(
            columns=[DefaultColumnMapper()],
            literals=[DefaultLiteralMapper()],
            functions=[DefaultFunctionMapper()],
            curried_functions=[DefaultCurriedFunctionMapper()],
            subscriptables=[DefaultSubscriptableMapper()],
            lambdas=[DefaultLambdaMapper()],
            arguments=[DefaultArgumentMapper()],
        )
        self.__translation_rules = translation_rules.concat(default_rules)

    def translate_column(self, exp: Column) -> Union[Column, Literal, FunctionCall]:
        return self.__map_expression(exp, self.__translation_rules.columns)

    def translate_literal(self, exp: Literal) -> Union[Literal]:
        return self.__map_expression(exp, self.__translation_rules.literals)

    def translate_function_call(self, exp: FunctionCall,) -> FunctionCall:
        return self.__map_expression(exp, self.__translation_rules.functions)

    def translate_curried_function_call(
        self, exp: CurriedFunctionCall
    ) -> CurriedFunctionCall:
        return self.__map_expression(exp, self.__translation_rules.curried_functions)

    def translate_subscriptable_reference(
        self, exp: SubscriptableReference
    ) -> Union[FunctionCall, SubscriptableReference]:
        return self.__map_expression(exp, self.__translation_rules.subscriptables)

    def translate_lambda(self, exp: Lambda) -> Lambda:
        return self.__map_expression(exp, self.__translation_rules.lambdas)

    def translate_argument(self, exp: Argument) -> Argument:
        return self.__map_expression(exp, self.__translation_rules.arguments)

    def __map_expression(
        self,
        exp: TExpIn,
        rules: Sequence[ExpressionMapper[TExpIn, TExpOut, SnubaClickhouseTranslator]],
    ) -> TExpOut:
        for r in rules:
            ret = r.attempt_map(exp, self)
            if ret is not None:
                return ret
        raise ValueError(f"Cannot map expression {exp}")


class DefaultColumnMapper(ColumnMapper):
    def attempt_map(
        self, expression: Column, children_translator: SnubaClickhouseTranslator,
    ) -> Optional[Column]:
        return deepcopy(expression)


class DefaultLiteralMapper(LiteralMapper):
    def attempt_map(
        self, expression: Literal, children_translator: SnubaClickhouseTranslator,
    ) -> Optional[Literal]:
        return deepcopy(expression)


class DefaultArgumentMapper(ArgumentMapper):
    def attempt_map(
        self, expression: Argument, children_translator: SnubaClickhouseTranslator,
    ) -> Optional[Argument]:
        return deepcopy(expression)


class DefaultFunctionMapper(FunctionCallMapper):
    def attempt_map(
        self,
        expression: Union[CurriedFunctionCall, FunctionCall],
        children_translator: SnubaClickhouseTranslator,
    ) -> Optional[FunctionCall]:
        if not isinstance(expression, FunctionCall):
            return None

        return replace(
            expression,
            parameters=tuple(
                children_translator.translate_expression(p)
                for p in expression.parameters
            ),
        )


class DefaultCurriedFunctionMapper(CurriedFunctionCallMapper):
    def attempt_map(
        self,
        expression: CurriedFunctionCall,
        children_translator: SnubaClickhouseTranslator,
    ) -> Optional[CurriedFunctionCall]:
        if not isinstance(expression, CurriedFunctionCall):
            return None

        return CurriedFunctionCall(
            alias=expression.alias,
            internal_function=children_translator.translate_function_call(
                expression.internal_function
            ),
            parameters=tuple(
                children_translator.translate_expression(p)
                for p in expression.parameters
            ),
        )


class DefaultSubscriptableMapper(SubscriptableReferenceMapper):
    def attempt_map(
        self,
        expression: SubscriptableReference,
        children_translator: SnubaClickhouseTranslator,
    ) -> Optional[SubscriptableReference]:
        column = children_translator.translate_column(expression.column)
        assert isinstance(column, Column)
        key = children_translator.translate_literal(expression.key)
        assert isinstance(key, Literal)
        return SubscriptableReference(alias=expression.alias, column=column, key=key,)


class DefaultLambdaMapper(LambdaMapper):
    def attempt_map(
        self, expression: Lambda, children_translator: SnubaClickhouseTranslator,
    ) -> Optional[Lambda]:
        return replace(
            expression,
            transformation=children_translator.translate_expression(
                expression.transformation
            ),
        )
