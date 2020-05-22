from abc import ABC, abstractmethod

from snuba.clickhouse.query import Expression
from snuba.query.expressions import ExpressionVisitor
from snuba.query.expressions import FunctionCall


class SnubaClickhouseTranslator(ExpressionVisitor[Expression], ABC):
    """
    Convenience interface that defines a translator from Snuba AST to
    Clickhouse AST.
    """

    pass


class SnubaClickhouseStrictTranslator(SnubaClickhouseTranslator):
    """
    Type strict Snuba to Clickhouse translator.

    When translating an expression from an AST to another one wev cannot assume that
    any expression type of the first AST can be translated into any type of expression
    in the second AST in any context.
    An example is the inner function of a CurriedFunction. That is not of type Expression
    but it has to be a FunctionCall. So whatever translates the inner function is forced
    to produce a FunctionCall or the AST will be broken.

    The exact constraints of which valid translations are possible depend on the two ASTs.
    In the Snuba to Clickhouse translation there is one constraint only (the CurriedFunctionCall
    outlined above).

    This interface is meant to only capture this requirement so that all of its implementations
    have to provide a way to guarantee this integrity constraint. In this specific case the only
    constraint is that an implementation need to implement a method translates a FunctionCall
    into another FunctionCall (no other result allowed).

    This class is supposed to evolve if, when adding new nodes to the AST, more such integrity
    constraints are introduced.
    """

    @abstractmethod
    def translate_function_strict(self, exp: FunctionCall) -> FunctionCall:
        raise NotImplementedError
