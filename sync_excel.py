# -*- coding: utf-8 -*-
"""
sync_excel.py - Excel ↔ 数据库双向同步

设计说明（解决"每次运行都重复推送"的根因）：
- sync_from_excel()：启动时把船舶完整性看板.xlsx 的记录【增量】导入数据库。
  按唯一键（系统名+描述+发起时间）判重：
    * 已存在的记录 → 只更新业务字段，绝不覆盖 notify_level / push_tag（推送状态得以保留）
    * 新记录       → 才插入
  人员台账、系统清单是配置表，每次全量刷新（以 Excel 为准）。
- sync_to_excel()：把数据库最新数据【全量写回】船舶完整性看板.xlsx（每分钟由 monitor 调用）。
  覆盖已有单元格值、保留表头与样式；Excel 被占用时跳过本次，下一轮自动重试。
"""
import os

import openpyxl

import db
from config import BASE_DIR

RECORDS_XLSX = os.path.join(BASE_DIR, "船舶完整性看板.xlsx")
# V1.5.3 改名兼容：老环境里可能还留着「收尾看板.xlsx」，存在就继续沿用它，
# 避免改名后历史看板 Excel 突然"失联"（数据一直在数据库，这里只是回写用的文件）。
_LEGACY_RECORDS_XLSX = os.path.join(BASE_DIR, "收尾看板.xlsx")
if (not os.path.exists(RECORDS_XLSX)) and os.path.exists(_LEGACY_RECORDS_XLSX):
    RECORDS_XLSX = _LEGACY_RECORDS_XLSX
STAFF_XLSX = os.path.join(BASE_DIR, "人员台账.xlsx")

# Excel 表头名 → 数据库字段名
# 责任人列统一存"公司-一级-二级-作业区-姓名-电话"6段台账格式（电话不再单列）
COL_MAP = {
    "系统": "sys_name",
    "异常描述": "description",
    "调试责任人": "debug_users",
    "安装作业区": "debug_sup",
    "安装责任人": "install_user",
    "作业长": "area_leader",
    "部门长": "dept_leader",
    "发起时间": "submit_time",
    "发起照片": "photo_path",
    "状态": "status",
    "反馈说明": "fb_desc",
    "反馈照片": "fb_photo",
    "反馈时间": "fb_time",
    "闭环时间": "close_time",
    "推送标记": "push_tag",
}

# 用于增量判重的字段
KEY_FIELDS = ("sys_name", "description", "submit_time")


# ================= 工具 =================

def _load_sheet(path, sheet_name=None):
    """打开 Excel 返回 (workbook, sheet)；文件不存在返回 (None, None)"""
    if not os.path.exists(path):
        return None, None
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
    return wb, ws


def _read_header_list(ws) -> list:
    """返回第一行表头（按列号顺序），空单元格为空字符串"""
    return [str(ws.cell(row=1, column=c).value).strip() if ws.cell(row=1, column=c).value else ""
            for c in range(1, ws.max_column + 1)]


def _cell(ws, row, col):
    return ws.cell(row=row, column=col).value


def _normalize_status(v):
    s = (v or "").strip()
    return s if s else "未完成"


# ================= ① Excel → 数据库（增量导入） =================

def sync_from_excel(force: bool = False) -> int:
    """把船舶完整性看板.xlsx 的记录增量导入数据库，返回新增条数。
    force=True 时先清空 records 再全量导入（推送状态会重置，谨慎使用）。"""
    if not os.path.exists(RECORDS_XLSX):
        print("[同步] 未找到 船舶完整性看板.xlsx，跳过记录导入")
        return 0

    if force:
        mode = db.resolve_mode()
        conn = db.get_conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM records")
            conn.commit()
        finally:
            conn.close()
        print("[同步] --force：records 表已清空，将从 Excel 全量重新导入（推送状态会被重置）")

    wb, ws = _load_sheet(RECORDS_XLSX, "收尾数据")
    if ws is None:
        return 0
    header_list = _read_header_list(ws)

    added = 0
    updated = 0
    skipped = 0
    for row_idx in range(2, ws.max_row + 1):
        # 只取映射表里关心的列
        record = {}
        for c, hname in enumerate(header_list, start=1):
            if hname in COL_MAP:
                record[COL_MAP[hname]] = _cell(ws, row_idx, c)
        sys_name = str(record.get("sys_name") or "").strip()
        if not sys_name:
            skipped += 1
            continue  # 空行或无系统名

        description = str(record.get("description") or "").strip()
        submit_time = str(record.get("submit_time") or "").strip()

        existing = db.get_record_by_key(sys_name, description, submit_time)
        if existing:
            # 已存在：更新业务字段，保留 notify_level / push_tag（防止重复推送）
            db.update_record_fields(existing["id"], {
                "debug_users": str(record.get("debug_users") or "").strip(),
                "debug_sup": str(record.get("debug_sup") or "").strip(),
                "install_user": str(record.get("install_user") or "").strip(),
                "area_leader": str(record.get("area_leader") or "").strip(),
                "dept_leader": str(record.get("dept_leader") or "").strip(),
                "photo_path": str(record.get("photo_path") or "").strip(),
                "status": _normalize_status(record.get("status")),
                "fb_desc": str(record.get("fb_desc") or "").strip(),
                "fb_photo": str(record.get("fb_photo") or "").strip(),
                "fb_time": str(record.get("fb_time") or "").strip(),
                "close_time": str(record.get("close_time") or "").strip(),
            })
            updated += 1
        else:
            db.add_record(
                sys_name=sys_name,
                description=description,
                debug_users=str(record.get("debug_users") or "").strip(),
                debug_phones="",  # 电话不单列，包含在6段台账格式中
                debug_sup=str(record.get("debug_sup") or "").strip(),
                install_user=str(record.get("install_user") or "").strip(),
                area_leader=str(record.get("area_leader") or "").strip(),
                dept_leader=str(record.get("dept_leader") or "").strip(),
                submit_time=submit_time,
                photo_path=str(record.get("photo_path") or "").strip(),
                push_tag=str(record.get("push_tag") or "0").strip() or "0",
                submitted_by="",  # Excel 导入的旧数据无提交人
                ship_id=1,        # 本地 Excel 同步的是默认看板（2555船）
            )
            added += 1

    print(f"[同步] Excel→数据库：新增 {added} 条，更新 {updated} 条，跳过空行 {skipped} 条（推送状态已保留）")
    return added


def sync_sys_staff():
    """系统清单 + 人员台账全量刷新（配置表，以 Excel 为准）"""
    if not os.path.exists(RECORDS_XLSX):
        return
    wb, ws = _load_sheet(RECORDS_XLSX, "系统清单")
    if ws is not None:
        names = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row and row[0] and str(row[0]).strip():
                names.append(str(row[0]).strip())
        db.import_ship_systems(1, names)   # 默认看板的系统清单
        print(f"[同步] 系统清单：{len(names)} 个")

    if not os.path.exists(STAFF_XLSX):
        # 人员台账.xlsx 不存在时：改从船海智云通讯录缓存（chz_users.json）填充人员表，
        # 实现"删掉 Excel、只用界面添加人员"，人员来源 = 船海智云通讯录
        try:
            import chz_push
            users = chz_push.ensure_users()
            rows = [{"公司": "", "一级部门": "", "二级部门": "", "作业区": "",
                     "姓名": (u.get("name") or "").strip(), "电话": p}
                    for p, u in users.items() if u.get("name") or u.get("code")]
            rows = [r for r in rows if r["电话"] and r["姓名"]]
            db.import_staff(rows)
            print(f"[同步] 未找到 人员台账.xlsx，已改用船海智云通讯录填充人员：{len(rows)} 人")
        except Exception as e:
            print(f"[同步] 未找到 人员台账.xlsx，且船海智云通讯录不可用（{e}），跳过人员导入")
        return
    wb2, ws2 = _load_sheet(STAFF_XLSX)
    if ws2 is not None:
        header_list = _read_header_list(ws2)
        col = {h: i + 1 for i, h in enumerate(header_list)}
        rows = []
        for row_idx in range(2, ws2.max_row + 1):
            name = ws2.cell(row=row_idx, column=col.get("姓名", 1)).value
            if not name or not str(name).strip():
                continue
            rows.append({
                "姓名": str(name).strip(),
                "公司": str(ws2.cell(row=row_idx, column=col.get("公司", 2)).value or "").strip(),
                "一级部门": str(ws2.cell(row=row_idx, column=col.get("一级部门", 3)).value or "").strip(),
                "二级部门": str(ws2.cell(row=row_idx, column=col.get("二级部门", 4)).value or "").strip(),
                "作业区": str(ws2.cell(row=row_idx, column=col.get("作业区", 5)).value or "").strip(),
                "电话": str(ws2.cell(row=row_idx, column=col.get("电话", 6)).value or "").strip(),
            })
        db.import_staff(rows)
        print(f"[同步] 人员台账：{len(rows)} 人")


# ================= ② 数据库 → Excel（定期写回） =================

def sync_to_excel() -> int:
    """把数据库最新记录写回船舶完整性看板.xlsx（保留表头与样式），返回写回条数。
    返回 -1 = Excel 被占用；-2 = 其他错误"""
    if not os.path.exists(RECORDS_XLSX):
        return -2
    try:
        wb = openpyxl.load_workbook(RECORDS_XLSX)  # 保留格式
    except PermissionError:
        print("[同步] 船舶完整性看板.xlsx 正被 Excel 打开，跳过本次写回（下一轮自动重试）")
        return -1
    except Exception as e:
        print(f"[同步] 打开船舶完整性看板.xlsx 失败: {e}")
        return -2

    try:
        ws = wb["收尾数据"] if "收尾数据" in wb.sheetnames else wb.active
        header_list = _read_header_list(ws)
        records = db.get_records_by_ship(1)   # 本地 Excel 只对应默认看板

        # 覆盖写：保留已有单元格样式，多余行只清空值不清样式
        total_rows = max(ws.max_row, 1 + len(records))
        for r_idx in range(2, total_rows + 1):
            data = records[r_idx - 2] if (r_idx - 2) < len(records) else None
            for c in range(1, len(header_list) + 1):
                cell = ws.cell(row=r_idx, column=c)
                hname = header_list[c - 1]
                field = COL_MAP.get(hname)
                if data is None:
                    cell.value = None
                elif field:
                    cell.value = data.get(field)
                else:
                    cell.value = None  # Excel 有、映射表无的列留空（不覆盖）

        wb.save(RECORDS_XLSX)
        print(f"[同步] 数据库→Excel：已写回 {len(records)} 条")
        return len(records)
    except PermissionError:
        print("[同步] 写回船舶完整性看板.xlsx 时被占用，跳过本次（下一轮自动重试）")
        return -1
    except Exception as e:
        print(f"[同步] 写回船舶完整性看板.xlsx 失败: {e}")
        return -2
    finally:
        # V1.3.2：显式丢弃工作簿对象，配合调用方 gc.collect() 及时归还内存
        try:
            del ws, wb
        except Exception:
            pass
