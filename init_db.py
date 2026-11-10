# -*- coding: utf-8 -*-
"""
init_db.py - 数据库一键初始化 + Excel 增量迁移

功能：
1. 自动建库建表（MySQL 自动创建数据库；SQLite 自动生成文件）
   - records（收尾任务）、sys_list（系统清单）、staff（人员台账）、users（注册用户）
   - 旧数据库自动增量迁移：补 submitted_by 列、补 users 表
2. 把收尾看板.xlsx 的记录【增量】导入数据库：
   - 按唯一键（系统名+描述+发起时间）判重，已存在只更新业务字段
   - 推送状态（notify_level / push_tag）永远保留 → 不会重复推送
   - 自动跳过空行和未映射的列（如历史遗留的"调试电话/安装电话"）
3. 导入系统清单、人员台账（配置表，全量刷新）
   - 责任人列为6段台账格式（公司-一级-二级-作业区-姓名-电话），电话不再单列

用法：
    python init_db.py          # 初始化/增量导入（日常双击启动就用它，安全）
    python init_db.py --force  # 清空 records 后从 Excel 全量重建（推送状态会重置，仅数据错乱时用）
"""
import sys

import db
from sync_excel import sync_from_excel, sync_sys_staff


def main():
    force = "--force" in sys.argv
    print("=" * 55)
    print("  船舶收尾异常管理看板 - 数据库初始化")
    print("  数据库模式: " + db.resolve_mode())
    print("=" * 55)

    db.init_db()
    sync_from_excel(force)
    sync_sys_staff()

    print("\n✅ 初始化完成！当前状态：")
    print(f"   收尾记录: {db.count_records()} 条")
    print(f"   系统清单: {len(db.get_system_list())} 个")
    print(f"   人员台账: {len(db.get_staff_list())} 人")
    print()
    if force:
        print("⚠️  注意：本次使用了 --force，所有记录的推送状态已被重置。")
    print("提示：正式上线请编辑 config.json → db.type 改为 \"mysql\"，并填写连接信息。")


if __name__ == "__main__":
    main()
