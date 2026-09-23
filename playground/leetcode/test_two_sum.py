"""两数之和的测试。本地跑 pytest,比网页提交快得多,还逼你想边界。"""
import pytest

from two_sum import two_sum, two_sum_brute


# 两个解法应该给出同样的答案,所以同一套用例参数化跑两遍
@pytest.mark.parametrize("solve", [two_sum, two_sum_brute])
class TestTwoSum:
    def test_basic(self, solve):
        assert solve([2, 7, 11, 15], 9) == [0, 1]

    def test_answer_at_end(self, solve):
        assert solve([3, 2, 4], 6) == [1, 2]

    def test_same_value_twice(self, solve):
        # 边界:两个元素值相同,不能把自己用两次
        assert solve([3, 3], 6) == [0, 1]

    def test_negative_numbers(self, solve):
        assert solve([-3, 4, 3, 90], 0) == [0, 2]

    def test_no_answer(self, solve):
        assert solve([1, 2, 3], 100) == []
