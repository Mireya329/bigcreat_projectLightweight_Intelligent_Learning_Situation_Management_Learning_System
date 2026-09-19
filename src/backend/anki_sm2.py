# -*- coding: utf-8 -*-
"""SM-2 间隔重复算法（Anki 内核移植）。

对外只暴露一个函数 review()：给它"卡片当前状态 + 这次的回忆质量"，
返回"更新后的状态 + 下次到期日"。不碰数据库，纯函数——好测。

回忆质量 quality（0~5）：
    0  完全想不起来      1  错了但看到答案有印象
    2  想起来了但很勉强   3  想起来了，费了点劲
    4  想起来了，稍有犹豫 5  脱口而出
"""

MIN_EF = 1.3


def review(ef, interval_days, repetitions, quality, today):
    """SM-2 核心。

    参数：
        ef, interval_days, repetitions  卡片当前状态
        quality    0~5 的回忆质量
        today      datetime.date，今天
    返回：
        dict：新的 ef / interval_days / repetitions / due_date
    """
    if not 0 <= quality <= 5:
        raise ValueError("quality 必须在 0~5 之间")

    # 1. 更新难度因子（答对答错都要更新）
    new_ef = ef + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    new_ef = max(MIN_EF, round(new_ef, 2))          # 有下限，不能无限变难

    # 2. 更新间隔与连续次数
    if quality < 3:                                  # 忘了：从头来过
        new_reps = 0
        new_interval = 1
    else:
        if repetitions == 0:
            new_interval = 1
        elif repetitions == 1:
            new_interval = 6
        else:
            new_interval = round(interval_days * new_ef)
        new_reps = repetitions + 1

    # 3. 算下次到期
    from datetime import timedelta
    return {
        "ef": new_ef,
        "interval_days": new_interval,
        "repetitions": new_reps,
        "due_date": (today + timedelta(days=new_interval)).isoformat(),
    }


# ---- 自测：用已知值验证算法正确性 ----
if __name__ == "__main__":
    from datetime import date
    t = date(2026, 9, 19)

    # 新卡，答对（q=4）→ 隔 1 天，ef 不变 2.5
    print(review(2.5, 0, 0, 4, t))
    # 期望 {'ef': 2.5, 'interval_days': 1, 'repetitions': 1, 'due_date': '2026-09-20'}

    # 第二次答对 → 隔 6 天
    print(review(2.5, 1, 1, 4, t))
    # 期望 interval_days=6, repetitions=2

    # 第三次答对（q=3，有点吃力）→ 间隔 = 上次间隔 × 新的 ef
    # 注意用的是【更新后】的 ef（2.36 → 2.22），不是旧的：
    #   6 × 2.22 = 13.32 → 13 天
    # （如果误用旧 ef 会得到 14，这是 SM-2 最容易写错的地方）
    print(review(2.36, 6, 2, 3, t))
    # 期望 {'ef': 2.22, 'interval_days': 13, 'repetitions': 3, ...}

    # 答错 → 打回 1 天，连续次数清零
    print(review(2.5, 15, 3, 1, t))
    # 期望 interval_days=1, repetitions=0
