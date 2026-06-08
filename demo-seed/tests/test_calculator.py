import pytest
from src.calculator import add, subtract, multiply, divide


def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0


def test_subtract():
    assert subtract(10, 4) == 6


def test_multiply():
    assert multiply(3, 5) == 15


def test_divide():
    assert divide(10, 2) == 5.0


def test_divide_by_zero_raises():
    with pytest.raises(ZeroDivisionError):
        divide(1, 0)


def test_integration_chain():
    result = add(multiply(2, 3), subtract(10, 4))
    assert result == 12
