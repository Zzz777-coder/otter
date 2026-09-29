"""按出行天数生成行李清单。"""


def packing_list(days: int) -> list:
    """根据旅行天数返回行李清单列表。

    days: 出行天数,需为正整数。
    """
    if not isinstance(days, int) or isinstance(days, bool):
        raise TypeError("days 必须是整数")
    if days <= 0:
        raise ValueError("days 必须大于 0")

    items = [
        "护照与签证复印件",
        "手机与充电器",
        "充电宝",
        "现金与信用卡",
        "常备药品",
    ]

    # 衣物按天数准备,超过 5 天按 5 套计算并安排洗衣
    outfit_count = min(days, 5)
    items += [f"换洗衣物 x{outfit_count}", f"内衣袜子 x{outfit_count + 1}"]

    if days >= 3:
        items += ["轻便折叠伞", "舒适步行鞋"]
    if days >= 5:
        items += ["转换插头", "便携洗衣液"]
    if days >= 7:
        items += ["备用腰包", "常用护理用品"]

    return items


if __name__ == "__main__":
    for day in (1, 3, 7):
        print(f"--- {day} 天行程 ---")
        for entry in packing_list(day):
            print(f"- {entry}")
