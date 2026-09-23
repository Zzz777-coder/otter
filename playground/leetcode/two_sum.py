"""LeetCode 1. 两数之和 (Two Sum)

给定整数数组 nums 和目标值 target,返回和为 target 的两个元素的下标。
假设有且仅有一个答案,且同一元素不能使用两次。
"""


def two_sum_brute(nums: list[int], target: int) -> list[int]:
    """解法一:暴力双重循环 O(n^2)。先能对,再谈快。"""
    n = len(nums)
    for i in range(n):
        for j in range(i + 1, n):
            if nums[i] + nums[j] == target:
                return [i, j]
    return []


def two_sum(nums: list[int], target: int) -> list[int]:
    """解法二:哈希表一次遍历 O(n)。

    关键转变:
      暴力是"对每个 i,回头找能和它配对的 j"(回头找 = 慢)。
      改成"边走边把见过的数存进字典,当前数 x 只需问:
      target - x 这个补数之前出现过吗?" 查字典是 O(1)。
    """
    seen: dict[int, int] = {}  # 值 -> 下标
    for i, x in enumerate(nums):
        need = target - x
        if need in seen:
            return [seen[need], i]
        seen[x] = i
    return []


if __name__ == "__main__":
    print(two_sum_brute([2, 7, 11, 15], 9))
    print(two_sum([2, 7, 11, 15], 9))
