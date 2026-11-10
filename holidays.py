# -*- coding: utf-8 -*-
"""
holidays.py - 中国法定节假日与工作日判断模块

用途：超时提醒按「工作日」计时——周六日与法定节假日不计时，周末补班日算工作日。

数据来源：国务院办公厅每年发布的《关于XX年部分节假日安排的通知》
  - 2025 年：2024-11-12 发布
  - 2026 年：2025-11-04 发布

★ 每年更新方法（无需改代码）：
  每年 11 月国务院发布新一年的放假安排后，编辑本目录下的 holidays.json，
  把新一年的 holidays（放假日）和 workdays（周末补班日）加进去即可。
  格式见 holidays.json 内示例。holidays.json 不存在或损坏时，自动使用本文件内置数据。

用法：
    python holidays.py --show 2026            # 查看某年放假安排
    python holidays.py --check 2026-02-15     # 判断某天是否工作日
"""
import datetime
import json
import os

# ============ 内置默认数据（兜底；holidays.json 存在时优先使用并合并） ============
# holidays = 放假日（含调休连休形成的所有放假日期）
# workdays = 周末补班日（这些日子虽然是周六日，但要上班，算工作日）
DEFAULT_HOLIDAYS = {
    "2025": {
        "holidays": [
            "2025-01-01",
            "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
            "2025-02-01", "2025-02-02", "2025-02-03", "2025-02-04",
            "2025-04-04", "2025-04-05", "2025-04-06",
            "2025-05-01", "2025-05-02", "2025-05-03", "2025-05-04", "2025-05-05",
            "2025-05-31", "2025-06-01", "2025-06-02",
            "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-04",
            "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08",
        ],
        "workdays": [
            "2025-01-26", "2025-02-08", "2025-04-27", "2025-09-28", "2025-10-11",
        ],
    },
    "2026": {
        "holidays": [
            "2026-01-01", "2026-01-02", "2026-01-03",
            "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18",
            "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
            "2026-04-04", "2026-04-05", "2026-04-06",
            "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
            "2026-06-19", "2026-06-20", "2026-06-21",
            "2026-09-25", "2026-09-26", "2026-09-27",
            "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
            "2026-10-05", "2026-10-06", "2026-10-07",
        ],
        "workdays": [
            "2026-01-04", "2026-02-14", "2026-02-28", "2026-05-09",
            "2026-09-20", "2026-10-10",
        ],
    },
}

# 外置数据文件（与程序数据目录同目录），每年国务院发布后更新此文件即可
from config import BASE_DIR
_JSON_PATH = os.path.join(BASE_DIR, "holidays.json")


def _load():
    data = {y: dict(v) for y, v in DEFAULT_HOLIDAYS.items()}
    try:
        with open(_JSON_PATH, "r", encoding="utf-8") as f:
            ext = json.load(f)
        for year, v in ext.items():
            if isinstance(v, dict) and "holidays" in v:
                data[str(year)] = {
                    "holidays": [str(d) for d in v["holidays"]],
                    "workdays": [str(d) for d in v.get("workdays", [])],
                }
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[holidays] 读取 holidays.json 失败（使用内置数据）: {e}")
    return data


HOLIDAYS = _load()
# 预生成 set 加速判断
_HOLIDAY_SET = {d for v in HOLIDAYS.values() for d in v.get("holidays", [])}
_WORKDAY_SET = {d for v in HOLIDAYS.values() for d in v.get("workdays", [])}


def is_workday(dt):
    """判断某天是否为工作日：周末补班日算工作日；法定/调休假日不算；其余周一~周五为工作日"""
    d = dt.date().isoformat()
    if d in _WORKDAY_SET:
        return True
    if d in _HOLIDAY_SET:
        return False
    return dt.weekday() < 5


def work_hours_between(start_dt, end_dt):
    """计算两个时间之间的『工作日时长』（小时）。周六日与法定节假日不计入，周末补班日计入。

    规则示例：周五 18:00 发起 → 周一 09:00 = 周五 6h + 周一 9h = 15h（周六周日不计）
    """
    if not start_dt or not end_dt or end_dt <= start_dt:
        return 0.0
    total = 0.0
    cur = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    one_day = datetime.timedelta(days=1)
    while cur < end_dt:
        seg_start = max(cur, start_dt)
        seg_end = min(cur + one_day, end_dt)
        if seg_end > seg_start and is_workday(cur):
            total += (seg_end - seg_start).total_seconds() / 3600.0
        cur += one_day
    return round(total, 2)


def is_holiday_date(date_str):
    """判断某个 'YYYY-MM-DD' 字符串是否为放假日"""
    return date_str in _HOLIDAY_SET


def is_workday_date(date_str):
    """判断某个 'YYYY-MM-DD' 字符串是否为周末补班日"""
    return date_str in _WORKDAY_SET


def main():
    import sys
    args = sys.argv[1:]
    if "--show" in args:
        idx = args.index("--show")
        if idx + 1 < len(args):
            year = args[idx + 1]
            v = HOLIDAYS.get(year)
            if not v:
                print(f"没有 {year} 年的数据（已内置: {', '.join(sorted(HOLIDAYS))}）")
                return
            print(f"===== {year} 年放假安排 =====")
            print("放假日期:", ", ".join(v["holidays"]))
            print("周末补班: ", ", ".join(v["workdays"]))
            return
    if "--check" in args:
        idx = args.index("--check")
        if idx + 1 < len(args):
            ds = args[idx + 1]
            try:
                dt = datetime.datetime.strptime(ds, "%Y-%m-%d")
            except ValueError:
                print(f"日期格式错误: {ds}（应为 YYYY-MM-DD）")
                return
            print(f"{ds} → {'工作日' if is_workday(dt) else '休息日（周六日/法定节假日）'}")
            return
    print("用法: python holidays.py --show 2026 | --check 2026-02-15")


if __name__ == "__main__":
    main()
