"""验收用例：一过一挂——挂的那个用于验证沙箱能捕获失败信息而不是直接崩。"""

from app import add


def test_add_works():
    assert add(1, 2) == 3


def test_this_must_fail():
    assert 1 == 2, "故意失败：验证沙箱能捕获 traceback"
