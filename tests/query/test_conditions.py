from snuba.query.conditions import BasicCondition, AndCondition, Operator, OrCondition
from snuba.query.expressions import AliasedExpression, FunctionCall, Column, Expression


def test_expressions_from_basic_condition() -> None:
    """
    Iterates over the expressions in a basic condition
    """

    c = Column("c1", "t1")
    f1 = FunctionCall("f", [c])
    c2 = Column("c2", "t1")

    condition = BasicCondition(f1, Operator.EQ, c2)
    ret = list(condition.get_expressions())
    expected = [f1, c, c2]

    assert ret == expected


def test_aliased_expressions_from_basic_condition() -> None:
    """
    Iterates over the expressions in a basic condition when those expressions
    are aliased
    """

    c = Column("c1", "t1")
    f1 = FunctionCall("f", [c])
    al1 = AliasedExpression("a", f1)
    c2 = Column("c2", "t1")
    al2 = AliasedExpression("a", c2)

    condition = BasicCondition(al1, Operator.EQ, al2)
    ret = list(condition.get_expressions())
    expected = [al1, f1, c, al2, c2]

    assert ret == expected


def test_map_expressions_in_basic_condition() -> None:
    """
    Change the column name over the expressions in a basic condition
    """
    c = Column("c1", "t1")
    f1 = FunctionCall("f", [c])
    c2 = Column("c2", "t1")

    c3 = Column("c3", "t1")

    def replace_col(e: Expression) -> Expression:
        if isinstance(e, Column) and e.column_name == "c1":
            return c3
        return e

    condition = BasicCondition(f1, Operator.EQ, c2)
    condition.get_expressions().transform(replace_col)
    ret = list(condition.get_expressions())
    expected = [f1, c3, c2]

    assert ret == expected


def test_nested_simple_condition() -> None:
    """
    Iterates and maps expressions over a complex Condition:
    (A=B OR A=B) AND (A=B OR A=B)
    """

    c1 = Column("c1", "t1")
    c2 = Column("c2", "t1")
    co1 = BasicCondition(c1, Operator.EQ, c2)

    c3 = Column("c1", "t1")
    c4 = Column("c2", "t1")
    co2 = BasicCondition(c3, Operator.EQ, c4)
    or1 = OrCondition([co1, co2])

    c5 = Column("c1", "t1")
    c6 = Column("c2", "t1")
    co4 = BasicCondition(c5, Operator.EQ, c6)

    c7 = Column("c1", "t1")
    c8 = Column("c2", "t1")
    co5 = BasicCondition(c7, Operator.EQ, c8)
    or2 = OrCondition([co4, co5])
    and1 = AndCondition([or1, or2])

    ret = list(and1.get_expressions())
    expected = [c1, c2, c3, c4, c5, c6, c7, c8]
    assert ret == expected

    cX = Column("cX", "t1")

    def replace_col(e: Expression) -> Expression:
        if isinstance(e, Column) and e.column_name == "c2":
            return cX
        return e

    and1.get_expressions().transform(replace_col)
    ret = list(and1.get_expressions())
    expected = [c1, cX, c3, cX, c5, cX, c7, cX]
    assert ret == expected