# -*- coding: utf-8 -*-
"""
db.py - 统一数据库访问层（MySQL / SQLite 双模式）

设计说明：
- type = "auto"（默认）：优先连接 MySQL；连不上时自动降级为本地 SQLite 文件，
  零依赖即可运行，适合演示和单机使用。
- type = "mysql"：强制使用 MySQL（正式上线，支撑上万人规模）。
- type = "sqlite"：强制使用 SQLite。

表结构（两种数据库完全一致）：
  records        收尾任务主表（含超时提醒等级 notify_level，持久化防止重启重复推送）
  ships          收尾看板表（每个产品船一个看板：名称/船号/webhook/创建人）
  ship_systems   各看板的系统清单（看板独立维护）
  staff          人员台账
  users          注册用户表（手机号登录、三级权限）
  dispatch_log   派工日志表（完整派工链：谁派给谁、时间、备注，每次派工追加一条）

多看板说明：
- 系统启动时自动创建默认看板（2555船，webhook 取自 config.json 的 webhook_url），
  历史记录自动归入默认看板。
- 超级管理员可在界面上新建看板：填写看板名称、船号、企微 webhook，
  并上传系统清单 Excel（仅一列，第一行为"名称"，其余每行一个系统名）。
- 每张看板的记录相互独立，系统清单相互独立，推送使用各自 webhook。
"""
import json
import os
import sqlite3
from datetime import datetime

import pymysql

from config import CONFIG, BASE_DIR

DB_CFG = CONFIG["db"]

# 表结构 DDL（两种数据库差异仅为自增主键写法）
DDL = {
    "mysql": """
CREATE TABLE IF NOT EXISTS records (
    id INT AUTO_INCREMENT PRIMARY KEY,
    sys_name VARCHAR(100) NOT NULL DEFAULT '',
    description TEXT,
    debug_users TEXT,
    debug_phones TEXT,
    debug_sup TEXT,
    install_user TEXT,
    area_leader TEXT,
    dept_leader TEXT,
    submit_time VARCHAR(32),
    photo_path TEXT,
    status VARCHAR(20) DEFAULT '未完成',
    fb_desc TEXT,
    fb_photo TEXT,
    fb_time VARCHAR(32),
    close_time VARCHAR(32),
    push_tag VARCHAR(30) DEFAULT '0',
    notify_level INT DEFAULT 0,
    submitted_by VARCHAR(30) DEFAULT '',
    ship_id INT DEFAULT 1,
    dispatch_user TEXT,
    dispatch_phone VARCHAR(30) DEFAULT '',
    dispatch_by VARCHAR(100) DEFAULT '',
    dispatch_time VARCHAR(32),
    dispatch_note TEXT,
    extra_users TEXT,
    notified_phones TEXT,
    extra_responsible TEXT
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ships (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    ship_no VARCHAR(30) DEFAULT '',
    webhook VARCHAR(500) DEFAULT '',
    is_default INT DEFAULT 0,
    created_at VARCHAR(32),
    created_by VARCHAR(30) DEFAULT '',
    biz VARCHAR(20) DEFAULT 'debug',
    btype VARCHAR(20) DEFAULT '',
    def_debug_sup TEXT DEFAULT '',
    def_ministers TEXT DEFAULT ''
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ship_systems (
    id INT AUTO_INCREMENT PRIMARY KEY,
    ship_id INT NOT NULL,
    name VARCHAR(100) NOT NULL
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS staff (
    id INT AUTO_INCREMENT PRIMARY KEY,
    company VARCHAR(100),
    dept1 VARCHAR(100),
    dept2 VARCHAR(100),
    area VARCHAR(100),
    name VARCHAR(100),
    phone VARCHAR(30)
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    phone VARCHAR(30) NOT NULL,
    password VARCHAR(255) NOT NULL,
    name VARCHAR(100) DEFAULT '',
    emp_no VARCHAR(50) DEFAULT '',
    role VARCHAR(30) DEFAULT 'debug_user',
    status VARCHAR(20) DEFAULT 'pending',
    created_at VARCHAR(32),
    approved_by VARCHAR(100) DEFAULT '',
    biz VARCHAR(20) DEFAULT 'debug',
    UNIQUE KEY uk_phone (phone)
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS area_defaults (
    id INT AUTO_INCREMENT PRIMARY KEY,
    ship_id INT NOT NULL,
    area_name VARCHAR(50) NOT NULL,
    sup_value VARCHAR(500) DEFAULT '',
    foreman_value VARCHAR(500) DEFAULT '',
    is_preset INT DEFAULT 1,
    ministers VARCHAR(1000) DEFAULT ''
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS login_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(100),
    ip VARCHAR(64),
    success INT DEFAULT 0,
    detail VARCHAR(255) DEFAULT '',
    created_at VARCHAR(32),
    biz VARCHAR(20) DEFAULT 'debug'
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS dispatch_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    rid INT NOT NULL,
    from_user VARCHAR(100) DEFAULT '',
    to_user VARCHAR(100) DEFAULT '',
    to_phone VARCHAR(30) DEFAULT '',
    note TEXT,
    created_at VARCHAR(32)
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS urge_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    rid INT NOT NULL,
    actor_phone VARCHAR(30) DEFAULT '',
    actor_name VARCHAR(100) DEFAULT '',
    target_phones TEXT,
    target_names VARCHAR(500) DEFAULT '',
    created_at VARCHAR(32)
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS sixs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    ship_no VARCHAR(50) DEFAULT '',
    scorer VARCHAR(100) DEFAULT '',
    picks TEXT,
    deduct INT DEFAULT 0,
    score INT DEFAULT 0,
    photos TEXT,
    ctime VARCHAR(32),
    week_start VARCHAR(10) DEFAULT ''
) DEFAULT CHARSET=utf8mb4;
""",
    "sqlite": """
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sys_name TEXT DEFAULT '',
    description TEXT,
    debug_users TEXT,
    debug_phones TEXT,
    debug_sup TEXT,
    install_user TEXT,
    area_leader TEXT,
    dept_leader TEXT,
    submit_time TEXT,
    photo_path TEXT,
    status TEXT DEFAULT '未完成',
    fb_desc TEXT,
    fb_photo TEXT,
    fb_time TEXT,
    close_time TEXT,
    push_tag TEXT DEFAULT '0',
    notify_level INTEGER DEFAULT 0,
    submitted_by TEXT DEFAULT '',
    ship_id INTEGER DEFAULT 1,
    dispatch_user TEXT,
    dispatch_phone TEXT DEFAULT '',
    dispatch_by TEXT DEFAULT '',
    dispatch_time TEXT,
    dispatch_note TEXT,
    extra_users TEXT,
    notified_phones TEXT,
    extra_responsible TEXT
);

CREATE TABLE IF NOT EXISTS ships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    ship_no TEXT DEFAULT '',
    webhook TEXT DEFAULT '',
    is_default INTEGER DEFAULT 0,
    created_at TEXT,
    created_by TEXT DEFAULT '',
    biz TEXT DEFAULT 'debug',
    btype TEXT DEFAULT '',
    def_debug_sup TEXT DEFAULT '',
    def_ministers TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ship_systems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ship_id INTEGER NOT NULL,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT,
    dept1 TEXT,
    dept2 TEXT,
    area TEXT,
    name TEXT,
    phone TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL,
    name TEXT DEFAULT '',
    emp_no TEXT DEFAULT '',
    role TEXT DEFAULT 'debug_user',
    status TEXT DEFAULT 'pending',
    created_at TEXT,
    approved_by TEXT DEFAULT '',
    biz TEXT DEFAULT 'debug'
);

CREATE TABLE IF NOT EXISTS area_defaults (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ship_id INTEGER NOT NULL,
    area_name TEXT NOT NULL,
    sup_value TEXT DEFAULT '',
    foreman_value TEXT DEFAULT '',
    is_preset INTEGER DEFAULT 1,
    ministers TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS login_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    ip TEXT,
    success INTEGER DEFAULT 0,
    detail TEXT DEFAULT '',
    created_at TEXT,
    biz TEXT DEFAULT 'debug'
);

CREATE TABLE IF NOT EXISTS dispatch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rid INTEGER NOT NULL,
    from_user TEXT DEFAULT '',
    to_user TEXT DEFAULT '',
    to_phone TEXT DEFAULT '',
    note TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS urge_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rid INTEGER NOT NULL,
    actor_phone TEXT DEFAULT '',
    actor_name TEXT DEFAULT '',
    target_phones TEXT,
    target_names TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS sixs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ship_no TEXT DEFAULT '',
    scorer TEXT DEFAULT '',
    picks TEXT,
    deduct INTEGER DEFAULT 0,
    score INTEGER DEFAULT 0,
    photos TEXT,
    ctime TEXT,
    week_start TEXT DEFAULT ''
);
"""
}

_mode = None  # 'mysql' | 'sqlite'（解析一次缓存）


def resolve_mode() -> str:
    """确定当前使用的数据库模式"""
    global _mode
    if _mode:
        return _mode
    t = DB_CFG.get("type", "auto")
    if t == "sqlite":
        _mode = "sqlite"
    elif t == "mysql":
        _mode = "mysql"  # 强制 MySQL，连接失败时由调用方处理
    else:  # auto：探测 MySQL，失败降级 SQLite
        try:
            conn = pymysql.connect(
                host=DB_CFG.get("host", "127.0.0.1"),
                port=int(DB_CFG.get("port", 3306)),
                user=DB_CFG.get("user", "root"),
                password=DB_CFG.get("password", ""),
                database=DB_CFG.get("database", "shipyard"),
                charset="utf8mb4",
                connect_timeout=3,
            )
            conn.close()
            _mode = "mysql"
            print("[数据库] 已连接 MySQL：" + DB_CFG.get("host", "127.0.0.1") + "/" + DB_CFG.get("database", "shipyard"))
        except Exception:
            _mode = "sqlite"
            print("[数据库] 未检测到 MySQL，已自动降级为内置 SQLite（演示/单机模式，无需安装任何东西）")
    return _mode


def _sqlite_path() -> str:
    return os.path.join(BASE_DIR, "shipyard.db")


def get_conn():
    """获取数据库连接（用完必须 close）"""
    mode = resolve_mode()
    if mode == "sqlite":
        conn = sqlite3.connect(_sqlite_path())
        conn.row_factory = sqlite3.Row
        return conn
    return pymysql.connect(
        host=DB_CFG.get("host", "127.0.0.1"),
        port=int(DB_CFG.get("port", 3306)),
        user=DB_CFG.get("user", "root"),
        password=DB_CFG.get("password", ""),
        database=DB_CFG.get("database", "shipyard"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def init_db():
    """建库建表（幂等，可重复执行）。MySQL 模式下若库不存在则自动创建。"""
    mode = resolve_mode()
    if mode == "mysql":
        try:
            conn = pymysql.connect(
                host=DB_CFG.get("host", "127.0.0.1"),
                port=int(DB_CFG.get("port", 3306)),
                user=DB_CFG.get("user", "root"),
                password=DB_CFG.get("password", ""),
                charset="utf8mb4",
                autocommit=True,
            )
            with conn.cursor() as cur:
                cur.execute("CREATE DATABASE IF NOT EXISTS `%s` DEFAULT CHARSET=utf8mb4" % DB_CFG["database"])
            conn.close()
        except Exception as e:
            raise RuntimeError("MySQL 连接失败，请检查 config.json 数据库配置（host/user/password/database）。错误：" + str(e))
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.executescript(DDL[mode])
        conn.commit()
        # 增量迁移：为旧数据库补列/补表（幂等，已有则跳过）
        _migrate(cur, mode)
        conn.commit()
    finally:
        conn.close()
    ensure_default_ship()
    print(f"[数据库] 表结构就绪（{mode}）")


def ensure_default_ship() -> int:
    """确保存在默认看板（2555船），并把无归属的记录归入默认看板。返回默认看板 id。"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM ships WHERE is_default = 1 ORDER BY id ASC LIMIT 1")
        row = cur.fetchone()
        if row:
            default_id = int(row[0])
        else:
            cur.execute(
                "INSERT INTO ships (name, ship_no, webhook, is_default, created_at, created_by) "
                "VALUES (" + _ph(6, mode) + ")",
                ("2555船手尾看板", "2555", CONFIG.get("webhook_url", ""), 1,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "system"))
            conn.commit()
            default_id = int(cur.lastrowid)
            print("[数据库] 已创建默认看板：2555船手尾看板")
        # 无归属记录归入默认看板
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("UPDATE records SET ship_id = " + ph + " WHERE ship_id IS NULL OR ship_id = 0", (default_id,))
        conn.commit()
        return default_id
    finally:
        conn.close()


# ============ 工具 ============

def _ph(n: int, mode: str) -> str:
    """生成 n 个占位符：MySQL 用 %s，SQLite 用 ?"""
    p = "%s" if mode == "mysql" else "?"
    return ", ".join([p] * n)


def _rows_to_dicts(rows) -> list:
    """sqlite3.Row / pymysql DictCursor 统一转普通 dict 列表"""
    if rows is None:
        return []
    return [dict(r) for r in rows]


def _migrate(cur, mode: str):
    """增量迁移：为旧数据库补列/补表（幂等，已有则跳过）"""
    def _col_exists(table: str, col: str) -> bool:
        if mode == "sqlite":
            cur.execute(f"PRAGMA table_info({table})")
            return any(row[1] == col for row in cur.fetchall())
        else:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
                (DB_CFG.get("database", "shipyard"), table, col))
            return int(cur.fetchone()[0]) > 0

    def _table_exists(table: str) -> bool:
        if mode == "sqlite":
            cur.execute(f"SELECT count(*) FROM sqlite_master WHERE type='table' AND name='{table}'")
            return int(cur.fetchone()[0]) > 0
        else:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema=%s AND table_name=%s",
                (DB_CFG.get("database", "shipyard"), table))
            return int(cur.fetchone()[0]) > 0

    # V1.5.3：login_logs 表补 biz 列（两块看板的登录日志各自独立；老日志全部归调试业务）
    if _table_exists("login_logs") and not _col_exists("login_logs", "biz"):
        sql = "ALTER TABLE login_logs ADD COLUMN biz VARCHAR(20) DEFAULT 'debug'" if mode == "mysql" \
            else "ALTER TABLE login_logs ADD COLUMN biz TEXT DEFAULT 'debug'"
        cur.execute(sql)
        print("[数据库] 已迁移：login_logs 表新增 biz 列（默认 debug）")

    # V1.5.3：users 表补 biz 列（两个看板的用户互不相通；老账号全部归调试业务）
    if _table_exists("users") and not _col_exists("users", "biz"):
        sql = "ALTER TABLE users ADD COLUMN biz VARCHAR(20) DEFAULT 'debug'" if mode == "mysql" \
            else "ALTER TABLE users ADD COLUMN biz TEXT DEFAULT 'debug'"
        cur.execute(sql)
        print("[数据库] 已迁移：users 表新增 biz 列（默认 debug）")

    # V1.5.3：ships 表补 biz 列（业务档：debug=调试看板 / safety=安全隐患看板）
    # 老库全部按 'debug' 认领，不影响任何既有看板；安全隐患看板是新建的一条记录
    if _table_exists("ships") and not _col_exists("ships", "biz"):
        sql = "ALTER TABLE ships ADD COLUMN biz VARCHAR(20) DEFAULT 'debug'" if mode == "mysql" \
            else "ALTER TABLE ships ADD COLUMN biz TEXT DEFAULT 'debug'"
        cur.execute(sql)
        print("[数据库] 已迁移：ships 表新增 biz 列（默认 debug）")

    # records 表补 submitted_by 列
    if _table_exists("records") and not _col_exists("records", "submitted_by"):
        sql = "ALTER TABLE records ADD COLUMN submitted_by VARCHAR(30) DEFAULT ''" if mode == "mysql" \
            else "ALTER TABLE records ADD COLUMN submitted_by TEXT DEFAULT ''"
        cur.execute(sql)
        print("[数据库] 已迁移：records 表新增 submitted_by 列")

    # records 表补 ship_id 列（多看板归属）
    if _table_exists("records") and not _col_exists("records", "ship_id"):
        sql = "ALTER TABLE records ADD COLUMN ship_id INT DEFAULT 1" if mode == "mysql" \
            else "ALTER TABLE records ADD COLUMN ship_id INTEGER DEFAULT 1"
        cur.execute(sql)
        print("[数据库] 已迁移：records 表新增 ship_id 列")

    # records 表补派工字段（安装责任人派工给安装个人）
    _dispatch_cols = [
        ("dispatch_user", "ALTER TABLE records ADD COLUMN dispatch_user TEXT"),
        ("dispatch_phone", "ALTER TABLE records ADD COLUMN dispatch_phone VARCHAR(30) DEFAULT ''"),
        ("dispatch_by", "ALTER TABLE records ADD COLUMN dispatch_by VARCHAR(100) DEFAULT ''"),
        ("dispatch_time", "ALTER TABLE records ADD COLUMN dispatch_time VARCHAR(32)"),
        ("dispatch_note", "ALTER TABLE records ADD COLUMN dispatch_note TEXT"),
    ]
    for _col, _sql in _dispatch_cols:
        if _table_exists("records") and not _col_exists("records", _col):
            cur.execute(_sql)
            print(f"[数据库] 已迁移：records 表新增 {_col} 列")

    # records 表补 V1.2 字段：追加抄送人 / 实际推送号码留痕
    _v12_cols = [
        ("extra_users", "ALTER TABLE records ADD COLUMN extra_users TEXT"),
        ("notified_phones", "ALTER TABLE records ADD COLUMN notified_phones TEXT"),
    ]
    for _col, _sql in _v12_cols:
        if _table_exists("records") and not _col_exists("records", _col):
            cur.execute(_sql)
            print(f"[数据库] 已迁移：records 表新增 {_col} 列")

    # records 表补显式作业区/部门列（快速选作业区时直接存储名称，不再依赖台账反查）
    _area_cols = [
        ("area_name", "ALTER TABLE records ADD COLUMN area_name VARCHAR(50) DEFAULT ''"),
        ("dept_name", "ALTER TABLE records ADD COLUMN dept_name VARCHAR(50) DEFAULT ''"),
    ]
    for _col, _sql in _area_cols:
        if _table_exists("records") and not _col_exists("records", _col):
            cur.execute(_sql)
            print(f"[数据库] 已迁移：records 表新增 {_col} 列")

    # V1.17.1：records 表补「场地位置」列（安全隐患看板「场地」分类下提交隐患时选发生场地）
    if _table_exists("records") and not _col_exists("records", "place_name"):
        _sql_place = ("ALTER TABLE records ADD COLUMN place_name VARCHAR(50) DEFAULT ''"
                      if mode == "mysql" else "ALTER TABLE records ADD COLUMN place_name TEXT DEFAULT ''")
        cur.execute(_sql_place)
        print("[数据库] 已迁移：records 表新增 place_name 列（场地位置）")

    # V1.3 需求2：records 表补「追加责任人」字段（手尾发出后从 users 表追加的责任人，参与全部推送）
    if _table_exists("records") and not _col_exists("records", "extra_responsible"):
        sql = "ALTER TABLE records ADD COLUMN extra_responsible TEXT" if mode == "mysql" \
            else "ALTER TABLE records ADD COLUMN extra_responsible TEXT"
        cur.execute(sql)
        print("[数据库] 已迁移：records 表新增 extra_responsible 列")

    # V1.3 需求1：area_defaults 表补「部长」列（部长=部门长，独立于责任单位，可多个）
    if _table_exists("area_defaults") and not _col_exists("area_defaults", "ministers"):
        sql = "ALTER TABLE area_defaults ADD COLUMN ministers VARCHAR(1000) DEFAULT ''" if mode == "mysql" \
            else "ALTER TABLE area_defaults ADD COLUMN ministers TEXT DEFAULT ''"
        cur.execute(sql)
        print("[数据库] 已迁移：area_defaults 表新增 ministers 列")

    # V1.3 需求1/2：users 表补「工号」列（姓名/手机号/工号三合一检索的前提）
    if _table_exists("users") and not _col_exists("users", "emp_no"):
        sql = "ALTER TABLE users ADD COLUMN emp_no VARCHAR(50) DEFAULT ''" if mode == "mysql" \
            else "ALTER TABLE users ADD COLUMN emp_no TEXT DEFAULT ''"
        cur.execute(sql)
        print("[数据库] 已迁移：users 表新增 emp_no 列")

    # ========== V1.6 A1：ships 表补「看板类型」列（产品船 / 地面场地 / 其他） ==========
    # 老看板一律认领为「产品船」，不影响既有船的船号与任何历史记录。
    if _table_exists("ships") and not _col_exists("ships", "btype"):
        sql = "ALTER TABLE ships ADD COLUMN btype VARCHAR(20) DEFAULT ''" if mode == "mysql" \
            else "ALTER TABLE ships ADD COLUMN btype TEXT DEFAULT ''"
        cur.execute(sql)
        print("[数据库] 已迁移：ships 表新增 btype 列（看板类型，默认产品船）")

    # V1.16.10：ships 表补「默认调试上级负责人 / 默认部长」字段（看板级默认，打开发起弹窗时自动带出）
    _ship_def_cols = [
        ("def_debug_sup", "ALTER TABLE ships ADD COLUMN def_debug_sup VARCHAR(1000) DEFAULT ''" if mode == "mysql" \
            else "ALTER TABLE ships ADD COLUMN def_debug_sup TEXT DEFAULT ''"),
        ("def_ministers", "ALTER TABLE ships ADD COLUMN def_ministers VARCHAR(1000) DEFAULT ''" if mode == "mysql" \
            else "ALTER TABLE ships ADD COLUMN def_ministers TEXT DEFAULT ''"),
    ]
    for _col, _sql in _ship_def_cols:
        if _table_exists("ships") and not _col_exists("ships", _col):
            cur.execute(_sql)
            print("[数据库] 已迁移：ships 表新增 {} 列".format(_col))

    # ========== V1.10：highlights 表补「审批人清单」列（提交人自选审批人） ==========
    # 存 JSON：[{"name":"张三","phone":"138..."}]；为空表示老数据（任何管理角色都能审）
    if _table_exists("highlights") and not _col_exists("highlights", "approvers"):
        cur.execute("ALTER TABLE highlights ADD COLUMN approvers TEXT")
        print("[数据库] 已迁移：highlights 表新增 approvers 列（审批人清单）")

    # ========== V1.6 A2：records 表补「记录类型 / 审批」列 ==========
    # rec_type        隐患整改 / 违章处罚 / 5S
    # approval_status '' 无需审批 | 审批中 | 已通过 | 已驳回 | 无需审批(他人已通过)
    # approvers       审批人清单快照（JSON：姓名/电话/角色）
    # approved_by / approve_time / approve_note / handle_way / fine_amount
    _v16_cols = [
        ("rec_type", "ALTER TABLE records ADD COLUMN rec_type {} DEFAULT ''".format(
            "VARCHAR(20)" if mode == "mysql" else "TEXT")),
        ("approval_status", "ALTER TABLE records ADD COLUMN approval_status {} DEFAULT ''".format(
            "VARCHAR(20)" if mode == "mysql" else "TEXT")),
        ("approvers", "ALTER TABLE records ADD COLUMN approvers TEXT"),
        ("approved_by", "ALTER TABLE records ADD COLUMN approved_by {} DEFAULT ''".format(
            "VARCHAR(100)" if mode == "mysql" else "TEXT")),
        ("approve_time", "ALTER TABLE records ADD COLUMN approve_time {} DEFAULT ''".format(
            "VARCHAR(32)" if mode == "mysql" else "TEXT")),
        ("approve_note", "ALTER TABLE records ADD COLUMN approve_note TEXT"),
        ("handle_way", "ALTER TABLE records ADD COLUMN handle_way {} DEFAULT ''".format(
            "VARCHAR(20)" if mode == "mysql" else "TEXT")),
        ("fine_amount", "ALTER TABLE records ADD COLUMN fine_amount {} DEFAULT ''".format(
            "VARCHAR(20)" if mode == "mysql" else "TEXT")),
        # V1.16：安全隐患看板「手填截止时间」+ 过点提醒一次 的标记
        ("deadline", "ALTER TABLE records ADD COLUMN deadline {} DEFAULT ''".format(
            "VARCHAR(32)" if mode == "mysql" else "TEXT")),
        ("deadline_push", "ALTER TABLE records ADD COLUMN deadline_push {} DEFAULT ''".format(
            "VARCHAR(16)" if mode == "mysql" else "TEXT")),
    ]
    for _col, _sql in _v16_cols:
        if _table_exists("records") and not _col_exists("records", _col):
            cur.execute(_sql)
            print("[数据库] 已迁移：records 表新增 {} 列".format(_col))

    # V1.16.7：6S 评分记录记下打分人手机号 —— 用于「只有本人 + 固定超管能删」
    _sixs_v167 = [
        ("creator", "ALTER TABLE sixs ADD COLUMN creator {} DEFAULT ''".format(
            "VARCHAR(32)" if mode == "mysql" else "TEXT")),
        # V1.51：所属周（周一 YYYY-MM-DD）。只加列、不回填 —— 历史记录读取时
        # 由 app._sixs_week_of() 从 ctime 派生，零数据改动、零迁移风险。
        ("week_start", "ALTER TABLE sixs ADD COLUMN week_start {} DEFAULT ''".format(
            "VARCHAR(10)" if mode == "mysql" else "TEXT")),
    ]
    for _col, _sql in _sixs_v167:
        if _table_exists("sixs") and not _col_exists("sixs", _col):
            cur.execute(_sql)
            print("[数据库] 已迁移：sixs 表新增 {} 列".format(_col))

    # ========== V1.6 A5：亮点展示表 ==========
    # kind  亮点 / 问题（问题=需要整改的，由上级审核定奖罚）
    # status 待审核 / 已通过 / 已驳回
    if mode == "mysql":
        cur.execute("""
        CREATE TABLE IF NOT EXISTS highlights (
            id INT AUTO_INCREMENT PRIMARY KEY,
            ship_id INT NOT NULL,
            kind VARCHAR(20) DEFAULT 'highlight',
            title VARCHAR(200) DEFAULT '',
            descr TEXT,
            photo_path VARCHAR(255) DEFAULT '',
            submitter VARCHAR(100) DEFAULT '',
            submitter_phone VARCHAR(30) DEFAULT '',
            reviewer VARCHAR(100) DEFAULT '',
            review_note TEXT,
            reward VARCHAR(20) DEFAULT '',
            punish VARCHAR(20) DEFAULT '',
            status VARCHAR(20) DEFAULT 'pending',
            created_at VARCHAR(32),
            review_time VARCHAR(32),
            approvers TEXT
        ) DEFAULT CHARSET=utf8mb4;
        """)
    else:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS highlights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ship_id INTEGER NOT NULL,
            kind TEXT DEFAULT 'highlight',
            title TEXT DEFAULT '',
            descr TEXT,
            photo_path TEXT DEFAULT '',
            submitter TEXT DEFAULT '',
            submitter_phone TEXT DEFAULT '',
            reviewer TEXT DEFAULT '',
            review_note TEXT,
            reward TEXT DEFAULT '',
            punish TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            review_time TEXT,
            approvers TEXT
        );
        """)

    # ========== V1.6 A2：审批留痕表（谁在什么时候批/驳，留档可查） ==========
    if mode == "mysql":
        cur.execute("""
        CREATE TABLE IF NOT EXISTS approve_logs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            rid INT NOT NULL,
            actor_name VARCHAR(100) DEFAULT '',
            actor_phone VARCHAR(30) DEFAULT '',
            action VARCHAR(20) DEFAULT '',
            note TEXT,
            handle_way VARCHAR(20) DEFAULT '',
            fine_amount VARCHAR(20) DEFAULT '',
            created_at VARCHAR(32)
        ) DEFAULT CHARSET=utf8mb4;
        """)
    else:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS approve_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rid INTEGER NOT NULL,
            actor_name TEXT DEFAULT '',
            actor_phone TEXT DEFAULT '',
            action TEXT DEFAULT '',
            note TEXT,
            handle_way TEXT DEFAULT '',
            fine_amount TEXT DEFAULT '',
            created_at TEXT
        );
        """)
    print("[数据库] V1.6 结构检查完成（btype / 记类型 / 审批 / 亮点 / 审批留痕）")

    # ========== V1.16.28 A：在线会话表 ==========
    # 为什么必须有：Flask 默认会话是**签名 Cookie**，服务端不存任何东西，
    # 所以「谁在线 / 是不是同一个人开了两个窗口」光看 cookie 是看不出来的。
    # 落一张表，才谈得上「检测到已在线」「超管强制退出」。
    #   sess_tok  —— 会话令牌（每次认到人时随机生成，写进 cookie）
    #   kicked    —— 被顶替/被管理员踢 ⇒ 1，这一侧的旧会话下次请求就被请出去
    #   last_seen —— 最近活跃时间，用于「多久没动了算离线」
    if mode == "mysql":
        cur.execute("""
        CREATE TABLE IF NOT EXISTS online_sessions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            phone VARCHAR(30) NOT NULL,
            name VARCHAR(100) DEFAULT '',
            sess_tok VARCHAR(64) DEFAULT '',
            ua VARCHAR(255) DEFAULT '',
            ip VARCHAR(64) DEFAULT '',
            biz VARCHAR(20) DEFAULT 'debug',
            kicked INT DEFAULT 0,
            kicked_by VARCHAR(100) DEFAULT '',
            created_at VARCHAR(32),
            last_seen VARCHAR(32)
        ) DEFAULT CHARSET=utf8mb4;
        """)
    else:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS online_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            name TEXT DEFAULT '',
            sess_tok TEXT DEFAULT '',
            ua TEXT DEFAULT '',
            ip TEXT DEFAULT '',
            biz TEXT DEFAULT 'debug',
            kicked INTEGER DEFAULT 0,
            kicked_by TEXT DEFAULT '',
            created_at TEXT,
            last_seen TEXT
        );
        """)
    # 索引：按手机号查「这个人在不在别处在线」是最高频的查询
    if mode == "mysql":
        try:
            cur.execute("CREATE INDEX idx_online_phone ON online_sessions (phone)")
        except Exception:
            pass
    else:
        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_online_phone ON online_sessions (phone)")
        except Exception:
            pass

    # ========== V1.16.28 B：换人申请单（代操作审批）============
    # 「不能随便换人」：真要替别人操作，得先提单说明理由，
    # 管理员批了才切；没批之前，身份一直是原来那个人。
    #   status: pending 待审批 / approved 已批准（等本人确认生效）/ rejected 已驳回
    #           / applied 已生效（本人点了"用新身份"）
    if mode == "mysql":
        cur.execute("""
        CREATE TABLE IF NOT EXISTS id_switch_reqs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            from_phone VARCHAR(30) NOT NULL,
            from_name VARCHAR(100) DEFAULT '',
            to_phone VARCHAR(30) NOT NULL,
            to_name VARCHAR(100) DEFAULT '',
            reason VARCHAR(1000) DEFAULT '',
            biz VARCHAR(20) DEFAULT 'debug',
            status VARCHAR(20) DEFAULT 'pending',
            approver VARCHAR(100) DEFAULT '',
            approver_note VARCHAR(500) DEFAULT '',
            created_at VARCHAR(32),
            handled_at VARCHAR(32),
            applied_at VARCHAR(32)
        ) DEFAULT CHARSET=utf8mb4;
        """)
    else:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS id_switch_reqs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_phone TEXT NOT NULL,
            from_name TEXT DEFAULT '',
            to_phone TEXT NOT NULL,
            to_name TEXT DEFAULT '',
            reason TEXT DEFAULT '',
            biz TEXT DEFAULT 'debug',
            status TEXT DEFAULT 'pending',
            approver TEXT DEFAULT '',
            approver_note TEXT DEFAULT '',
            created_at TEXT,
            handled_at TEXT,
            applied_at TEXT
        );
        """)
    # V1.16.34：删掉此处 conn.commit() —— _migrate 只有 cur 参数、没有 conn，
    # 原写法引用未定义变量（运行时 NameError 被 try/except 吞掉，Cython 静态检查则直接报
    # "undeclared name not builtin: conn" 导致编译失败）。建表改动由调用方 init_db() 的 commit 兜底。
    print("[数据库] V1.16.28 结构检查完成（在线会话 / 换人申请单）")


# ============ 查询 ============

def get_records() -> list:
    """全部收尾记录（按 id 升序）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM records ORDER BY id ASC")
        return _rows_to_dicts(cur.fetchall())
    finally:
        conn.close()


def get_records_by_ship(ship_id: int) -> list:
    """某张看板（产品船）下的收尾记录（按 id 升序）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT * FROM records WHERE ship_id = " + ph + " ORDER BY id ASC", (ship_id,))
        return _rows_to_dicts(cur.fetchall())
    finally:
        conn.close()


def get_record(rid: int) -> dict:
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM records WHERE id = %s" % ("%s" if mode == "mysql" else "?"), (rid,))
        rows = _rows_to_dicts(cur.fetchall())
        return rows[0] if rows else None
    finally:
        conn.close()


def get_system_list() -> list:
    """默认看板的系统清单（历史兼容入口）"""
    return get_ship_systems(1)


# ============ 多看板（产品船） ============

def get_ships(biz: str = None) -> list:
    """全部看板（含记录数、未闭环数统计），按 id 升序。
    V1.5.3：可传 biz 过滤——'debug' 只返回调试看板、'safety' 只返回安全隐患看板；
    不传则返回全部（老的调用点行为不变）。"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        if biz:
            ph = "%s" if mode == "mysql" else "?"
            cur.execute("SELECT * FROM ships WHERE IFNULL(biz, 'debug') = " + ph + " ORDER BY id ASC", (biz,))
        else:
            cur.execute("SELECT * FROM ships ORDER BY id ASC")
        ships = _rows_to_dicts(cur.fetchall())
        for s in ships:
            cur.execute(
                "SELECT COUNT(*) AS c FROM records WHERE ship_id = " + ("%s" if mode == "mysql" else "?"),
                (s["id"],))
            s["total"] = int(cur.fetchone()["c"])
            cur.execute(
                "SELECT COUNT(*) AS c FROM records WHERE ship_id = " + ("%s" if mode == "mysql" else "?")
                + " AND status NOT IN ('已闭环', '已完成')", (s["id"],))
            s["open"] = int(cur.fetchone()["c"])
        return ships
    finally:
        conn.close()


def get_ship(ship_id: int) -> dict:
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT * FROM ships WHERE id = " + ph, (ship_id,))
        rows = _rows_to_dicts(cur.fetchall())
        return rows[0] if rows else None
    finally:
        conn.close()


def create_ship(name: str, ship_no: str, webhook: str, created_by: str = "", biz: str = "debug",
                btype: str = "") -> int:
    """新建看板，返回 id（biz: debug=调试看板 / safety=安全隐患看板；
    btype: V1.6 看板类型——产品船 / 地面场地 / 其他，空表示按老口径当作产品船）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO ships (name, ship_no, webhook, is_default, created_at, created_by, biz, btype) "
            "VALUES (" + _ph(8, mode) + ")",
            (name, ship_no, webhook, 0, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), created_by, biz, btype))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def set_ship_btype(ship_id: int, btype: str) -> bool:
    """V1.6 A1：修改看板类型（产品船 / 地面场地 / 其他）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("UPDATE ships SET btype = " + ph + " WHERE id = " + ph, (btype, ship_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_ship_defaults(ship_id: int, def_debug_sup: str, def_ministers: str):
    """V1.16.10：保存看板级默认调试上级负责人 / 默认部长。"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute(
            "UPDATE ships SET def_debug_sup = " + ph + ", def_ministers = " + ph + " WHERE id = " + ph,
            (def_debug_sup or '', def_ministers or '', ship_id))
        conn.commit()
    finally:
        conn.close()


def get_ship_by_biz(biz: str) -> dict:
    """按业务档取第一条看板（安全隐患看板用），没有返回 None"""
    lst = get_ships(biz)
    return lst[0] if lst else None


def ensure_biz_ship(biz: str, name: str) -> int:
    """确保某个业务档有一块看板，返回 ship_id（不存在则自动建）"""
    ship = get_ship_by_biz(biz)
    if ship:
        return int(ship["id"])
    return create_ship(name, "", "", "", biz=biz)


def delete_ship(ship_id: int):
    """删除看板：级联删除其收尾记录与系统清单（默认看板同样可删，转移默认由调用方处理）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("DELETE FROM records WHERE ship_id = " + ph, (ship_id,))
        cur.execute("DELETE FROM ship_systems WHERE ship_id = " + ph, (ship_id,))
        cur.execute("DELETE FROM ships WHERE id = " + ph, (ship_id,))
        conn.commit()
    finally:
        conn.close()


def set_default_ship(ship_id: int):
    """将指定看板设为默认看板（其余看板取消默认）。删除默认看板后用于自动转移默认。"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("UPDATE ships SET is_default = 0")
        cur.execute("UPDATE ships SET is_default = 1 WHERE id = " + ph, (ship_id,))
        conn.commit()
    finally:
        conn.close()


def rename_ship(ship_id: int, name: str, ship_no: str = None):
    """重命名看板（V1.5：ship_no 传入字符串则一并更新船号，传空串=清空；None=不改船号）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        sets = ["name = " + ph]
        params = [name]
        if ship_no is not None:
            sets.append("ship_no = " + ph)
            params.append(ship_no)
        params.append(ship_id)
        cur.execute("UPDATE ships SET " + ", ".join(sets) + " WHERE id = " + ph, params)
        conn.commit()
    finally:
        conn.close()


def get_ship_systems(ship_id: int) -> list:
    """某看板的系统清单"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT name FROM ship_systems WHERE ship_id = " + ph + " ORDER BY id ASC", (ship_id,))
        return [r["name"] for r in _rows_to_dicts(cur.fetchall()) if r.get("name")]
    finally:
        conn.close()


def import_ship_systems(ship_id: int, names: list):
    """全量刷新某看板的系统清单（以新清单为准，去重、忽略空值）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("DELETE FROM ship_systems WHERE ship_id = " + ph, (ship_id,))
        seen = set()
        for name in names:
            name = (name or "").strip()
            if not name or name in seen:
                continue
            cur.execute("INSERT INTO ship_systems (ship_id, name) VALUES (" + ph + ", " + ph + ")",
                        (ship_id, name))
            seen.add(name)
        conn.commit()
    finally:
        conn.close()


def merge_ship_systems(ship_id: int, names: list) -> dict:
    """V1.5 增量并入系统清单：只追加清单中还没有的系统，绝不删除已有。
    返回 {'added': 新增条数, 'existed': 跳过条数（已在清单 / 本次重复输入）}"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT name FROM ship_systems WHERE ship_id = " + ph, (ship_id,))
        existing = {r["name"] for r in _rows_to_dicts(cur.fetchall()) if r.get("name")}
        added = existed = 0
        for name in names:
            name = (name or "").strip()
            if not name:
                continue
            if name in existing:
                existed += 1
                continue
            cur.execute("INSERT INTO ship_systems (ship_id, name) VALUES (" + ph + ", " + ph + ")",
                        (ship_id, name))
            existing.add(name)
            added += 1
        conn.commit()
        return {"added": added, "existed": existed}
    finally:
        conn.close()


def rename_ship_system(ship_id: int, old_name: str, new_name: str) -> dict:
    """V1.5 重命名某看板下的一个系统名，并联动更新该看板已发出的历史手尾记录
    （records.sys_name 一并改，避免历史记录与清单失联）。
    返回 {'renamed': bool, 'records_updated': int}；
    旧名不存在、新名为空/与旧名相同、目标名已存在 → renamed=False。"""
    old_name = (old_name or "").strip()
    new_name = (new_name or "").strip()
    if not old_name or not new_name or old_name == new_name:
        return {"renamed": False, "records_updated": 0}
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT COUNT(*) AS c FROM ship_systems WHERE ship_id = " + ph + " AND name = " + ph,
                    (ship_id, new_name))
        if int(cur.fetchone()["c"]) > 0:
            return {"renamed": False, "records_updated": 0}
        cur.execute("UPDATE ship_systems SET name = " + ph + " WHERE ship_id = " + ph + " AND name = " + ph,
                    (new_name, ship_id, old_name))
        renamed = cur.rowcount > 0
        cur.execute("UPDATE records SET sys_name = " + ph + " WHERE ship_id = " + ph + " AND sys_name = " + ph,
                    (new_name, ship_id, old_name))
        records_updated = int(cur.rowcount or 0)
        conn.commit()
        return {"renamed": bool(renamed), "records_updated": records_updated}
    finally:
        conn.close()


def get_staff_list() -> list:
    """人员台账，返回中文键名（与前端模板一致）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT company, dept1, dept2, area, name, phone FROM staff ORDER BY id ASC")
        result = []
        for r in _rows_to_dicts(cur.fetchall()):
            result.append({
                "公司": r.get("company", ""),
                "一级部门": r.get("dept1", ""),
                "二级部门": r.get("dept2", ""),
                "作业区": r.get("area", ""),
                "姓名": r.get("name", ""),
                "电话": r.get("phone", ""),
            })
        return result
    finally:
        conn.close()


# ============ 写入 ============

def add_record(sys_name: str, description: str, debug_users: str, debug_phones: str,
               debug_sup: str, install_user: str, area_leader: str, dept_leader: str,
               submit_time: str, photo_path: str, push_tag: str = "0", notify_level: int = 0,
               submitted_by: str = "", ship_id: int = 1, area_name: str = "", dept_name: str = "",
               extra_users: str = "", place_name: str = "") -> int:
    """新增一条收尾记录，返回自增 id（push_tag/notify_level 默认未推送状态）
    extra_users: V1.2 发起时追加抄送人（JSON 数组字符串，[{name, phone}, ...]）
    place_name: V1.17.1 场地位置（安全隐患看板「场地」分类下提交隐患时选的发生场地）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        sql = ("INSERT INTO records (sys_name, description, debug_users, debug_phones, debug_sup, "
               "install_user, area_leader, dept_leader, submit_time, photo_path, status, push_tag, notify_level, submitted_by, ship_id, area_name, dept_name, extra_users, place_name) "
               "VALUES (" + _ph(19, mode) + ")")
        cur.execute(sql, (sys_name, description, debug_users, debug_phones, debug_sup,
                          install_user, area_leader, dept_leader, submit_time, photo_path,
                          "未完成", push_tag, notify_level, submitted_by, ship_id, area_name, dept_name,
                          extra_users, place_name))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def get_area_defaults(ship_id: int) -> list:
    """读取某看板的责任单位默认人员配置（责任单位可自由增删改名，含部长）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT id, ship_id, area_name, sup_value, foreman_value, is_preset, ministers "
                    "FROM area_defaults WHERE ship_id = " + ph + " ORDER BY id ASC", (ship_id,))
        return _rows_to_dicts(cur.fetchall())
    finally:
        conn.close()


def set_area_defaults(ship_id: int, items: list):
    """保存某看板的责任单位默认人员配置（整体替换）。
    items: [{'area_name': str, 'sup_value': str, 'foreman_value': str, 'ministers': str}, ...]
    - sup_value: 默认责任人（可多个，逗号分隔「姓名 - 电话」）
    - foreman_value: 默认上级领导（责任单位领导，可多个）
    - ministers: 部长（部门长，独立于责任单位，可多个逗号分隔）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("DELETE FROM area_defaults WHERE ship_id = " + ph, (ship_id,))
        for it in items:
            area_name = (it.get('area_name') or '').strip()
            if not area_name:
                continue
            cur.execute(
                "INSERT INTO area_defaults (ship_id, area_name, sup_value, foreman_value, is_preset, ministers) "
                "VALUES (" + _ph(6, mode) + ")",
                (ship_id, area_name, it.get('sup_value') or '', it.get('foreman_value') or '', 1,
                 it.get('ministers') or ''))
        conn.commit()
    finally:
        conn.close()


def append_extra_responsible(rid: int, items: list) -> bool:
    """追加责任人（V1.3 需求2）：向 records.extra_responsible 追加 [{name, phone}]（按手机号去重），
    返回是否新增了至少一人。"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT extra_responsible FROM records WHERE id = " + ("%s" if mode == "mysql" else "?"), (rid,))
        row = cur.fetchone()
        old = []
        raw = (row[0] if row else None) or ""
        if raw:
            try:
                arr = json.loads(raw)
                if isinstance(arr, list):
                    old = [it for it in arr if isinstance(it, dict) and it.get('phone')]
            except Exception:
                pass
        existing = {str(it.get('phone', '')) for it in old}
        added = False
        for it in items or []:
            phone = str(it.get('phone') or '').strip()
            name = (it.get('name') or '').strip()
            if not phone or phone in existing:
                continue
            old.append({'name': name or '责任人', 'phone': phone})
            existing.add(phone)
            added = True
        if added:
            cur.execute(
                "UPDATE records SET extra_responsible = " + ("%s" if mode == "mysql" else "?") + " WHERE id = " + ("%s" if mode == "mysql" else "?"),
                (json.dumps(old, ensure_ascii=False), rid))
            conn.commit()
        return added
    finally:
        conn.close()


def update_record_persons(rid: int, **fields) -> bool:
    """V1.6 需求1：改派——批量更新记录上的责任字段。

    只允许改责任相关的列，避免误伤其它字段。
    用法：update_record_persons(12, install_user='xx - 13800138000', dept_leader='')
    返回是否更新成功。"""
    allowed = ('install_user', 'area_leader', 'dept_leader', 'debug_sup', 'debug_users')
    pairs = [(k, v) for k, v in fields.items() if k in allowed]
    if not pairs:
        return False
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        sets = ", ".join(k + " = " + ph for k, _v in pairs)
        cur.execute("UPDATE records SET " + sets + " WHERE id = " + ph,
                    tuple(v for _k, v in pairs) + (rid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_record_area(rid: int, area_name: str):
    """回填记录的显式作业区名称（幂等）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("UPDATE records SET area_name = " + ph + " WHERE id = " + ph, (area_name, rid))
        conn.commit()
    finally:
        conn.close()


def feedback(rid: int, fb_desc: str, fb_photo: str, fb_time: str):
    """更新反馈（描述/照片做追加合并），并置推送标记为 feedback_pending"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT fb_desc, fb_photo FROM records WHERE id = " + ("%s" if mode == "mysql" else "?"), (rid,))
        row = cur.fetchone()
        old_desc = (row["fb_desc"] or "") if row else ""
        old_photo = (row["fb_photo"] or "") if row else ""

        new_desc = f"{old_desc} | {fb_desc}" if old_desc else fb_desc
        new_photo = (old_photo + "," + fb_photo) if old_photo and fb_photo else (fb_photo or old_photo)

        cur.execute(
            "UPDATE records SET fb_desc = %s, fb_photo = %s, fb_time = %s, push_tag = 'feedback_pending' WHERE id = %s"
            % tuple("%s" if mode == "mysql" else "?" for _ in range(4)),
            (new_desc, new_photo, fb_time, rid))
        conn.commit()
    finally:
        conn.close()


def close(rid: int, close_time: str):
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE records SET status = '已闭环', close_time = %s, push_tag = 'closed' WHERE id = %s"
            % tuple("%s" if mode == "mysql" else "?" for _ in range(2)),
            (close_time, rid))
        conn.commit()
    finally:
        conn.close()


def dispatch(rid: int, dispatch_user: str, dispatch_phone: str, dispatch_by: str,
             dispatch_note: str, dispatch_time: str):
    """安装责任人派工：把收尾任务派给安装个人。
    records 表保存最新一次派工（覆盖），dispatch_log 表追加一条派工日志，
    形成完整派工链（张三→李四→王五→……），详情页可完整追溯。"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE records SET dispatch_user = %s, dispatch_phone = %s, dispatch_by = %s, "
            "dispatch_note = %s, dispatch_time = %s WHERE id = %s"
            % tuple("%s" if mode == "mysql" else "?" for _ in range(6)),
            (dispatch_user, dispatch_phone, dispatch_by, dispatch_note, dispatch_time, rid))
        cur.execute(
            "INSERT INTO dispatch_log (rid, from_user, to_user, to_phone, note, created_at) "
            "VALUES (" + _ph(6, mode) + ")",
            (rid, dispatch_by, dispatch_user, dispatch_phone, dispatch_note, dispatch_time))
        conn.commit()
    finally:
        conn.close()


def get_dispatch_logs(rid: int) -> list:
    """读取某条收尾记录的完整派工链（按时间正序）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT id, rid, from_user, to_user, to_phone, note, created_at "
                    "FROM dispatch_log WHERE rid = " + ph + " ORDER BY id ASC", (rid,))
        return _rows_to_dicts(cur.fetchall())
    finally:
        conn.close()


def get_dispatch_logs_map(rids: list) -> dict:
    """批量读取多条记录的派工链（rid -> [log,...] 按时间正序），
    供「用户中心」一次拉取所有相关记录的派工链，避免逐条查询。"""
    rids = [int(x) for x in (rids or []) if x]
    if not rids:
        return {}
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        marks = ", ".join([ph] * len(rids))
        cur.execute("SELECT id, rid, from_user, to_user, to_phone, note, created_at "
                    "FROM dispatch_log WHERE rid IN (" + marks + ") ORDER BY id ASC", rids)
        rows = _rows_to_dicts(cur.fetchall())
        out = {}
        for r in rows:
            out.setdefault(r['rid'], []).append(r)
        return out
    finally:
        conn.close()


def add_urge_log(rid: int, actor_phone: str, actor_name: str,
                 target_phones: list, target_names: list, created_at: str):
    """V1.4 用户中心：催办留痕（谁在什么时候催办了哪些人）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO urge_log (rid, actor_phone, actor_name, target_phones, target_names, created_at) "
            "VALUES (" + _ph(6, mode) + ")",
            (int(rid), actor_phone or '', actor_name or '',
             ",".join([str(p) for p in (target_phones or [])]),
             ",".join([str(n) for n in (target_names or [])]),
             created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()


def get_urge_logs(rid: int) -> list:
    """读取某条收尾记录的催办历史（按时间倒序）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT id, rid, actor_phone, actor_name, target_phones, target_names, created_at "
                    "FROM urge_log WHERE rid = " + ph + " ORDER BY id DESC", (rid,))
        return _rows_to_dicts(cur.fetchall())
    finally:
        conn.close()


def delete_record(rid: int):
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM records WHERE id = " + ("%s" if mode == "mysql" else "?"), (rid,))
        conn.commit()
    finally:
        conn.close()


# ============ 巡检程序用 ============

def get_active_records() -> list:
    """返回所有未闭环记录（供巡检程序使用）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM records WHERE status NOT IN ('已闭环', '已完成') ORDER BY id ASC")
        return _rows_to_dicts(cur.fetchall())
    finally:
        conn.close()


def set_notify_level(rid: int, level: int):
    """持久化提醒等级，防止进程重启后重复推送"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE records SET notify_level = %s WHERE id = %s AND notify_level < %s"
            % tuple("%s" if mode == "mysql" else "?" for _ in range(3)),
            (level, rid, level))
        conn.commit()
    finally:
        conn.close()


def set_push_tag(rid: int, tag: str):
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE records SET push_tag = %s WHERE id = %s"
            % tuple("%s" if mode == "mysql" else "?" for _ in range(2)),
            (tag, rid))
        conn.commit()
    finally:
        conn.close()


def update_extra_users(rid: int, extra_users: str):
    """更新记录的追加抄送人（V1.2 需求2：JSON 数组字符串整体替换）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE records SET extra_users = %s WHERE id = %s"
            % tuple("%s" if mode == "mysql" else "?" for _ in range(2)),
            (extra_users or "", rid))
        conn.commit()
    finally:
        conn.close()


def append_notified_phones(rid: int, phones: list):
    """推送留痕：把本次实际推送的手机号追加进 notified_phones（逗号分隔、去重）。
    V1.2 需求3：每次推送都在记录上留痕，便于事后核查谁收到了通知。"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT notified_phones FROM records WHERE id = " + ph, (rid,))
        row = cur.fetchone()
        old = (row["notified_phones"] or "") if row else ""
        seen = [p for p in old.split(",") if p] if old else []
        for p in phones:
            p = (p or "").strip()
            if p and p not in seen:
                seen.append(p)
        new_val = ",".join(seen)
        cur.execute("UPDATE records SET notified_phones = %s WHERE id = %s"
                    % tuple("%s" if mode == "mysql" else "?" for _ in range(2)),
                    (new_val, rid))
        conn.commit()
    finally:
        conn.close()


def import_sys_list(names: list):
    """批量导入系统清单（去重，忽略空值）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        seen = set(get_system_list())
        for name in names:
            name = (name or "").strip()
            if not name or name in seen:
                continue
            cur.execute("INSERT INTO sys_list (name) VALUES (" + ("%s" if mode == "mysql" else "?") + ")", (name,))
            seen.add(name)
        conn.commit()
    finally:
        conn.close()


def import_staff(rows: list):
    """批量导入人员台账：rows 为 dict，键：公司/一级部门/二级部门/作业区/姓名/电话"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM staff")
        ph = _ph(6, mode)
        for r in rows:
            cur.execute(
                "INSERT INTO staff (company, dept1, dept2, area, name, phone) VALUES (" + ph + ")",
                (r.get("公司", ""), r.get("一级部门", ""), r.get("二级部门", ""),
                 r.get("作业区", ""), r.get("姓名", ""), r.get("电话", "")))
        conn.commit()
    finally:
        conn.close()


def count_records() -> int:
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM records")
        row = cur.fetchone()
        return int(row["c"]) if row else 0
    finally:
        conn.close()


# ============ 增量导入 / 推送状态管理 ============

def get_record_by_key(sys_name: str, description: str, submit_time: str) -> dict:
    """按唯一键（系统名+描述+发起时间）查找记录，用于 Excel 增量导入判重"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute(
            "SELECT * FROM records WHERE sys_name = %s AND description = %s AND submit_time = %s"
            % (ph, ph, ph),
            (sys_name, description, submit_time))
        rows = _rows_to_dicts(cur.fetchall())
        return rows[0] if rows else None
    finally:
        conn.close()


def update_record_fields(rid: int, fields: dict):
    """更新记录的指定业务字段。
    自动排除受保护字段（id / notify_level / push_tag），确保导入旧数据时不会覆盖推送状态。"""
    protected = {"id", "notify_level", "push_tag"}
    updates = {k: v for k, v in fields.items() if k not in protected}
    if not updates:
        return
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        set_sql = ", ".join([f"{k} = {ph}" for k in updates])
        cur.execute("UPDATE records SET " + set_sql + " WHERE id = " + ph,
                    list(updates.values()) + [rid])
        conn.commit()
    finally:
        conn.close()


def reset_push_state() -> int:
    """重置所有未闭环记录的推送状态（notify_level=0, push_tag='0'）。
    用于演示时让之前已推送过的记录能重新触发推送。返回受影响行数。"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE records SET notify_level = 0, push_tag = '0' WHERE status NOT IN ('已闭环', '已完成')")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ============ 用户管理 ============

def find_staff_by_phone(phone: str) -> dict:
    """从人员台账中按手机号查人，返回中文键名 dict 或 None"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT * FROM staff WHERE phone = " + ph, (phone,))
        rows = _rows_to_dicts(cur.fetchall())
        if not rows:
            return None
        r = rows[0]
        return {
            "公司": r.get("company", ""),
            "一级部门": r.get("dept1", ""),
            "二级部门": r.get("dept2", ""),
            "作业区": r.get("area", ""),
            "姓名": r.get("name", ""),
            "电话": r.get("phone", ""),
        }
    finally:
        conn.close()


def create_user(phone: str, password_hash: str, name: str, role: str = "debug_user",
                status: str = "pending", created_at: str = "", approved_by: str = "",
                emp_no: str = "", biz: str = "debug") -> bool:
    """创建用户，phone 重复则返回 False。emp_no: V1.3 工号（可空）
    biz: V1.5.3 业务档——debug=调试看板用户 / safety=安全隐患看板用户（两套用户不互通）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = _ph(9, mode)
        cur.execute(
            "INSERT INTO users (phone, password, name, emp_no, role, status, created_at, approved_by, biz) "
            "VALUES (" + ph + ")",
            (phone, password_hash, name, emp_no or '', role, status, created_at, approved_by,
             biz or 'debug'))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


def get_user_by_phone(phone: str) -> dict:
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT * FROM users WHERE phone = " + ph, (phone,))
        rows = _rows_to_dicts(cur.fetchall())
        return rows[0] if rows else None
    finally:
        conn.close()


def get_all_users(biz: str = None) -> list:
    """全部账号；传 biz 时只返回该业务档的账号（两个看板的用户互不相通）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        if biz:
            ph = "%s" if mode == "mysql" else "?"
            cur.execute("SELECT * FROM users WHERE IFNULL(biz, 'debug') = " + ph + " ORDER BY id ASC",
                        (biz,))
        else:
            cur.execute("SELECT * FROM users ORDER BY id ASC")
        return _rows_to_dicts(cur.fetchall())
    finally:
        conn.close()


def search_users(keyword: str) -> list:
    """V1.3 需求2：从 users 表按 姓名/手机号/工号 三合一模糊检索已有账号。
    返回 users 行列表（按 name 排序）；keyword 为空返回全部。"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        kw = (keyword or "").strip()
        ph = "%s" if mode == "mysql" else "?"
        if kw:
            like = f"%{kw}%"
            cur.execute(
                "SELECT * FROM users WHERE name LIKE " + ph + " OR phone LIKE " + ph
                + " OR emp_no LIKE " + ph
                + " ORDER BY name ASC", (like, like, like))
        else:
            cur.execute("SELECT * FROM users ORDER BY name ASC")
        return _rows_to_dicts(cur.fetchall())
    finally:
        conn.close()


def update_user_emp_no(phone: str, emp_no: str):
    """V1.3 需求1：超管为已有账号补/改工号（用于三合一检索）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("UPDATE users SET emp_no = " + ph + " WHERE phone = " + ph,
                    (emp_no or '', phone))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_user_status(phone: str, status: str, approved_by: str = ""):
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute(
            "UPDATE users SET status = " + ph + ", approved_by = " + ph + " WHERE phone = " + ph,
            (status, approved_by, phone))
        conn.commit()
    finally:
        conn.close()


def update_user_role(phone: str, role: str):
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("UPDATE users SET role = " + ph + " WHERE phone = " + ph, (role, phone))
        conn.commit()
    finally:
        conn.close()


def update_user_password(phone: str, password_hash: str):
    """用户自助修改密码：更新密码哈希"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("UPDATE users SET password = " + ph + " WHERE phone = " + ph,
                    (password_hash, phone))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_user(phone: str):
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("DELETE FROM users WHERE phone = " + ph, (phone,))
        conn.commit()
    finally:
        conn.close()


# ============ 登录日志（防内部攻击留痕） ============

def add_login_log(username: str, ip: str, success: bool, detail: str = "", biz: str = "debug"):
    """记录一次登录尝试（成功/失败都记），失败留痕用于追查爆破行为。
    V1.5：插入后自动清理，只保留最近 300 条（超出删除最旧的）。
    V1.5.3：按业务档分别留痕——两块看板的登录日志各自独立，各自保留最近 300 条。"""
    mode = resolve_mode()
    biz = biz if biz in ("debug", "safety") else "debug"
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph6 = _ph(6, mode)
        ph1 = "%s" if mode == "mysql" else "?"
        cur.execute(
            "INSERT INTO login_logs (username, ip, success, detail, created_at, biz) "
            "VALUES (" + ph6 + ")",
            (username or "", ip or "", 1 if success else 0, detail or "",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), biz))
        # V1.5 只留最近 300 条：先取第 300 条的 id，再删更旧的（规避 SQLite/MySQL
        # 对"同表子查询删除"的限制差异，用两步实现）。
        # V1.5.3：只对本业务档的日志做裁剪，不会把另一块看板的日志删掉。
        cur.execute("SELECT id FROM login_logs WHERE IFNULL(biz, 'debug') = " + ph1 +
                    " ORDER BY id DESC LIMIT 300", (biz,))
        ids = [r["id"] for r in _rows_to_dicts(cur.fetchall())]
        if len(ids) >= 300:
            keep_min = ids[-1]
            cur.execute("DELETE FROM login_logs WHERE id < " + ph1 +
                        " AND IFNULL(biz, 'debug') = " + ph1, (keep_min, biz))
        conn.commit()
    except Exception:
        pass  # 日志失败不影响登录主流程
    finally:
        conn.close()


def get_login_logs(limit: int = 300, biz: str = None) -> list:
    """取最近 limit 条登录日志（新在前）。
    V1.5.3：传 biz 时只看该业务档的日志（两块看板的登录日志各自独立）。"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        if biz:
            ph = "%s" if mode == "mysql" else "?"
            cur.execute("SELECT * FROM login_logs WHERE IFNULL(biz, 'debug') = " + ph +
                        " ORDER BY id DESC LIMIT %s" % int(limit), (biz,))
        else:
            cur.execute("SELECT * FROM login_logs ORDER BY id DESC LIMIT %s" % int(limit))
        return _rows_to_dicts(cur.fetchall())
    finally:
        conn.close()


# ================= V1.6 新增数据层 =================

_SIX_CATS_DEFAULT = ['有限空间', '密闭舱室', '动火作业',
                     '涂装油漆', '高空作业', '触电安全',
                     '机械伤害', '起重伤害', '厂内交通']


def count_records_with_system(ship_id: int, name: str) -> int:
    """统计某看板下有多少条记录用了这个系统 / 隐患类别（用于 A4 删除前校验）"""
    name = (name or "").strip()
    if not name:
        return 0
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT COUNT(*) AS c FROM records WHERE ship_id = " + ph + " AND sys_name = " + ph,
                    (ship_id, name))
        row = cur.fetchone()
        if row is None:
            return 0
        try:
            return int(row["c"])
        except (TypeError, IndexError):
            return int(row[0])
    finally:
        conn.close()


def delete_ship_system(ship_id: int, name: str, force: bool = False) -> dict:
    """删除某看板下的一个系统 / 隐患类别。
    默认：被历史记录引用过 -> 拒绝删除（{'deleted': False, 'used': N}）。
    V1.16.5：`force=True` 时**即便被引用也删**（用户要求名单能自由删改）。
              只删清单项，records.sys_name 原样保留，不会动历史数据。
    返回 {'deleted': bool, 'used': int}
    """
    name = (name or "").strip()
    if not name:
        return {"deleted": False, "used": 0}
    # V1.16.5：先确认它在清单里，再谈引用 —— 否则"不在清单"会被误报成"被 N 条记录使用"
    if name not in (get_ship_systems(ship_id) or []):
        return {"deleted": False, "used": 0}
    used = count_records_with_system(ship_id, name)
    if used > 0 and not force:
        return {"deleted": False, "used": used}
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("DELETE FROM ship_systems WHERE ship_id = " + ph + " AND name = " + ph,
                    (ship_id, name))
        deleted = cur.rowcount > 0
        conn.commit()
        return {"deleted": deleted, "used": 0}
    finally:
        conn.close()


# ---------- A2：审批 ----------

def set_record_approval(rid: int, status: str, approvers=None, actor_name='',
                        actor_phone='', note='', handle_way='', fine_amount=''):
    """写一条记录的审批状态与审批人快照（approvers 为 list 时序列化成 JSON）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        sets = ["approval_status = " + ph, "approved_by = " + ph,
                "approve_time = " + ph, "approve_note = " + ph,
                "handle_way = " + ph, "fine_amount = " + ph]
        vals = [status, actor_name, now_str(), note, handle_way, fine_amount]
        if approvers is not None:
            sets.append("approvers = " + ph)
            vals.append(json.dumps(approvers, ensure_ascii=False) if isinstance(approvers, (list, dict))
                        else str(approvers))
        vals.append(rid)
        cur.execute("UPDATE records SET " + ", ".join(sets) + " WHERE id = " + ph, tuple(vals))
        cur.commit() if hasattr(cur, 'commit') else None
        conn.commit()
        return True
    finally:
        conn.close()


def add_approve_log(rid: int, actor_name='', actor_phone='', action='', note='',
                    handle_way='', fine_amount=''):
    """审批留痕：谁在什么时候批了 / 驳了这条记录"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("INSERT INTO approve_logs (rid, actor_name, actor_phone, action, note, "
                    "handle_way, fine_amount, created_at) VALUES (" +
                    ", ".join([ph] * 8) + ")",
                    (rid, actor_name, actor_phone, action, note, handle_way, fine_amount, now_str()))
        conn.commit()
        return True
    finally:
        conn.close()


def get_approve_logs(rid: int) -> list:
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT * FROM approve_logs WHERE rid = " + ph + " ORDER BY id ASC", (rid,))
        return _rows_to_dicts(cur.fetchall())
    finally:
        conn.close()


# ---------- A5：亮点展示 ----------

def get_highlights(ship_id: int, status: str = '', limit: int = 200) -> list:
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        sql = "SELECT * FROM highlights WHERE ship_id = " + ph
        args = [ship_id]
        if status:
            sql += " AND status = " + ph
            args.append(status)
        sql += " ORDER BY id DESC LIMIT " + str(int(limit))
        cur.execute(sql, tuple(args))
        return _rows_to_dicts(cur.fetchall())
    finally:
        conn.close()


def get_highlights_multi(ship_ids, status: str = '', limit: int = 500) -> list:
    """按多个看板 id 查亮点（V1.17.0：「我的」页跨看板汇总「待我审核的亮点」用）。"""
    ship_ids = [int(x) for x in (ship_ids or [])]
    if not ship_ids:
        return []
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        q = ", ".join([ph] * len(ship_ids))
        sql = "SELECT * FROM highlights WHERE ship_id IN (" + q + ")"
        args = list(ship_ids)
        if status:
            sql += " AND status = " + ph
            args.append(status)
        sql += " ORDER BY id DESC LIMIT " + str(int(limit))
        cur.execute(sql, tuple(args))
        return _rows_to_dicts(cur.fetchall())
    finally:
        conn.close()


def add_highlight(ship_id: int, kind: str, title: str, descr: str, photo_path: str,
                  submitter: str, submitter_phone: str, approvers: str = '') -> int:
    """新增一条亮点/问题。approvers 为审批人清单 JSON（提交人自己指定）。"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("INSERT INTO highlights (ship_id, kind, title, descr, photo_path, submitter, "
                    "submitter_phone, status, created_at, approvers) VALUES ("
                    + ", ".join([ph] * 10) + ")",
                    (ship_id, kind, title, descr, photo_path, submitter, submitter_phone,
                     'pending', now_str(), approvers or ''))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def get_highlight(hid: int) -> dict:
    """按 id 取一条亮点（审核前校验权限用）"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT * FROM highlights WHERE id = " + ph, (hid,))
        rows = _rows_to_dicts(cur.fetchall())
        return rows[0] if rows else None
    finally:
        conn.close()


def review_highlight(hid: int, ok: bool, reviewer: str, note: str = '',
                     reward: str = '', punish: str = '') -> bool:
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("UPDATE highlights SET status = " + ph + ", reviewer = " + ph +
                    ", review_note = " + ph + ", reward = " + ph + ", punish = " + ph +
                    ", review_time = " + ph + " WHERE id = " + ph,
                    ('approved' if ok else 'rejected', reviewer, note, reward, punish,
                     now_str(), hid))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_highlight(hid: int) -> bool:
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        ph = "%s" if mode == "mysql" else "?"
        cur.execute("DELETE FROM highlights WHERE id = " + ph, (hid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ================= V1.16.28 在线会话 / 换人审批 =================

# 多久没动算「离线」。前端页面会自动刷新，所以 5 分钟足够；
# 手机息屏、放口袋不动都可能超过 3 分钟，所以别设更短，否则老是把人误判成离线。
ONLINE_IDLE_MIN = 8


def _v1628_norm_rows(cur):
    return _rows_to_dicts(cur.fetchall())


def online_login(phone: str, name: str, sess_tok: str, ua: str = '',
                 ip: str = '', biz: str = 'debug',
                 take_over: bool = False) -> dict:
    """记一次「认到人」：同一个人（phone）只保留**最新**一条在线记录。

    用户选的口径是「提示后允许顶替」+「手机/电脑先不做严格锁」，
    所以这里的语义是：
      · 同一个令牌重复进来 —— 同一台设备刷新页面，续期即可（不动 kicked）
      · 同一个手机号换了令牌 —— **默认不顶**：把别处那条原样返回在
        返回值的 'takeover' 键里，让 app 层提示用户去点「顶替并继续」；
        只有调用方传 take_over=True 时才真的把别处那条删掉。
      · 不同手机号 —— 各记各的，互不影响（本来就是多人）

    🔴 早先这里是无条件顶掉别处那条，结果新登录的一端自己查不到"别处已开"，
    顶替变成静默发生（用户被顶了却不知情）。已修。
    """
    phone = (phone or '').strip()
    sess_tok = (sess_tok or '').strip()
    if not phone or not sess_tok:
        return {}
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        p1 = "%s" if mode == "mysql" else "?"
        ts = now_str()

        # 🔴 V1.16.28 修：别处已在线时**不要立刻顶掉**。
        #   先把别处那条查出来看看还活着没（kicked=0 且最近有活动），
        #   活着就原样留着、由调用方决定要不要顶 ——
        #   否则新登录这一端根本看不到"别处已开"，顶替就变成静默的了。
        other = {}
        cur.execute("SELECT * FROM online_sessions "
                    "WHERE phone = " + p1 + " AND IFNULL(kicked, 0) = 0 "
                    "AND IFNULL(sess_tok, '') <> " + p1 + " "
                    "ORDER BY last_seen DESC", (phone, sess_tok))
        _ors = _v1628_norm_rows(cur)
        if _ors and (_ors[0].get('last_seen') or '') >= _v1628_deadline(ONLINE_IDLE_MIN):
            other = _ors[0]

        if other and take_over:
            # 用户明确点了"顶替" ⇒ 这才真的把别处那条清掉
            cur.execute("DELETE FROM online_sessions WHERE phone = " + p1 +
                        " AND IFNULL(sess_tok, '') <> " + p1, (phone, sess_tok))
            other = {}

        cur.execute("SELECT id FROM online_sessions WHERE phone = " + p1 +
                    " AND IFNULL(sess_tok, '') = " + p1, (phone, sess_tok))
        row = cur.fetchone()
        if row:
            rid = row["id"] if isinstance(row, dict) else row[0]
            cur.execute("UPDATE online_sessions SET name = " + p1 + ", ua = " + p1 +
                        ", ip = " + p1 + ", biz = " + p1 + ", kicked = 0, kicked_by = '', "
                        "last_seen = " + p1 + " WHERE id = " + p1,
                        (name or '', ua or '', ip or '', biz or 'debug', ts, rid))
        else:
            cur.execute("INSERT INTO online_sessions "
                        "(phone, name, sess_tok, ua, ip, biz, kicked, kicked_by, "
                        " created_at, last_seen) "
                        "VALUES (" + _ph(10, mode) + ")",
                        (phone, name or '', sess_tok, ua or '', ip or '',
                         biz or 'debug', 0, '', ts, ts))
        conn.commit()
        return {'phone': phone, 'name': name or '', 'sess_tok': sess_tok,
                'ip': ip or '', 'last_seen': ts,
                # 🔴 有值 = 这个账号在别处还开着，本次**没有**顶掉它。
                # app 层拿它来提示用户、或渲染"顶替并继续"按钮。
                'takeover': other}
    finally:
        conn.close()


def online_state(sess_tok: str) -> dict:
    """🔴 V1.16.29 新增：只读地查一个令牌的在线状态，**不续期、不改任何数据**。

    为什么必须有它（配合 app.py 的 `_online_ensure_registered`）：
      `online_touch()` 返回 False 有**两种**含义 ——
        ① 记录不存在（从没登记过 / 被 60 分钟清理掉了）
        ② 记录在，但 `kicked=1`（真被踢了）
      而 `before_request` 里必须分清这两者：① 要补登记后放行，② 才清身份。
      只靠 `online_touch()` 的布尔值做不到，所以这里把三种状态分开返回：
        {'state': 'active'|'kicked'|'unknown', 'row': {...} 或 None}
    """
    sess_tok = (sess_tok or '').strip()
    if not sess_tok:
        return {'state': 'unknown', 'row': None}
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        p1 = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT * FROM online_sessions "
                    "WHERE IFNULL(sess_tok, '') = " + p1, (sess_tok,))
        rows = _v1628_norm_rows(cur)
        if not rows:
            return {'state': 'unknown', 'row': None}
        r = rows[0]
        if r.get('kicked'):
            return {'state': 'kicked', 'row': r}
        return {'state': 'active', 'row': r}
    finally:
        conn.close()


def online_touch(sess_tok: str) -> bool:
    """续期：刷新一下「最近活跃」。返回 False = 这个令牌已被标记踢出/不存在。"""
    sess_tok = (sess_tok or '').strip()
    if not sess_tok:
        return False
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        p1 = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT id, kicked FROM online_sessions "
                    "WHERE IFNULL(sess_tok, '') = " + p1, (sess_tok,))
        row = cur.fetchone()
        if not row:
            return False
        if (row["kicked"] if isinstance(row, dict) else row[1]):
            return False
        cur.execute("UPDATE online_sessions SET last_seen = " + p1 +
                    " WHERE IFNULL(sess_tok, '') = " + p1, (now_str(), sess_tok))
        conn.commit()
        return True
    finally:
        conn.close()


def get_online_list(biz: str = '', only_active: bool = True) -> list:
    """当前在线的人（按最近活跃倒序）。"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        p1 = "%s" if mode == "mysql" else "?"
        sql = "SELECT * FROM online_sessions WHERE IFNULL(kicked, 0) = 0"
        args = []
        if biz:
            sql += " AND IFNULL(biz, 'debug') = " + p1
            args.append(biz)
        if only_active:
            sql += " AND last_seen >= " + p1
            args.append(_v1628_deadline(ONLINE_IDLE_MIN))
        sql += " ORDER BY last_seen DESC"
        cur.execute(sql, tuple(args))
        return _v1628_norm_rows(cur)
    finally:
        conn.close()


def _v1628_deadline(minutes: int) -> str:
    import datetime as _dt
    return (_dt.datetime.now() - _dt.timedelta(minutes=max(1, int(minutes)))) \
        .strftime('%Y-%m-%d %H:%M:%S')


def kick_online(phone: str, by_name: str = '', only_others: str = '') -> int:
    """强制退出：把这个人的在线会话标记为已踢。返回影响条数。

    only_others 传自己的令牌时，只踢**别的**那一端（=「顶替」按钮）；
    不传就全踢（= 超管的「强制退出」按钮）。
    """
    phone = (phone or '').strip()
    if not phone:
        return 0
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        p1 = "%s" if mode == "mysql" else "?"
        sql = ("UPDATE online_sessions SET kicked = 1, kicked_by = " + p1 +
               " WHERE phone = " + p1 + " AND IFNULL(kicked, 0) = 0")
        args = [by_name or '管理员', phone]
        if (only_others or '').strip():
            sql += " AND IFNULL(sess_tok, '') <> " + p1
            args.append(only_others.strip())
        cur.execute(sql, tuple(args))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def is_online_elsewhere(phone: str, sess_tok: str) -> dict:
    """当前这个人是不是**在别处**已经开着。开着就返回那条，登录时好提示他。"""
    phone = (phone or '').strip()
    sess_tok = (sess_tok or '').strip()
    if not phone:
        return {}
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        p1 = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT * FROM online_sessions "
                    "WHERE phone = " + p1 + " AND IFNULL(kicked, 0) = 0 "
                    "AND IFNULL(sess_tok, '') <> " + p1 +
                    " ORDER BY last_seen DESC", (phone, sess_tok))
        rows = _v1628_norm_rows(cur)
        if not rows:
            return {}
        r = rows[0]
        if (r.get('last_seen') or '') < _v1628_deadline(ONLINE_IDLE_MIN):
            return {}
        return r
    finally:
        conn.close()


def cleanup_online(keep_minutes: int = 60) -> int:
    """清掉很久没动的在线记录（cron / 每次登录时顺手调一次，避免表无限长）。"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        p1 = "%s" if mode == "mysql" else "?"
        cur.execute("DELETE FROM online_sessions WHERE last_seen < " + p1,
                    (_v1628_deadline(keep_minutes),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ---------- 换人申请单 ----------

def add_id_switch_req(from_phone: str, from_name: str, to_phone: str, to_name: str,
                      reason: str, biz: str = 'debug') -> int:
    """提一条换人申请（代操作）。返回单号；失败返回 0。"""
    from_phone = (from_phone or '').strip()
    to_phone = (to_phone or '').strip()
    if not from_phone or not to_phone:
        return 0
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO id_switch_reqs "
                    "(from_phone, from_name, to_phone, to_name, reason, biz, "
                    " status, created_at) VALUES (" + _ph(8, mode) + ")",
                    (from_phone, from_name or '', to_phone, to_name or '',
                     reason or '', biz or 'debug', 'pending', now_str()))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def get_id_switch_reqs(status: str = '', biz: str = '', limit: int = 100) -> list:
    """换人申请单列表。status 传 '' = 全部（最新在前）。"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        p1 = "%s" if mode == "mysql" else "?"
        sql = "SELECT * FROM id_switch_reqs WHERE 1=1"
        args = []
        if status:
            sql += " AND IFNULL(status, 'pending') = " + p1
            args.append(status)
        if biz:
            sql += " AND IFNULL(biz, 'debug') = " + p1
            args.append(biz)
        sql += " ORDER BY id DESC LIMIT %s" % int(limit)
        cur.execute(sql, tuple(args))
        return _v1628_norm_rows(cur)
    finally:
        conn.close()


def get_id_switch_req(rid: int):
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        p1 = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT * FROM id_switch_reqs WHERE id = " + p1, (int(rid),))
        rows = _v1628_norm_rows(cur)
        return rows[0] if rows else None
    finally:
        conn.close()


def handle_id_switch_req(rid: int, ok: bool, approver: str, note: str = '') -> bool:
    """批 / 驳。批完状态是 approved —— **还不生效**，
    等申请人自己点一下「用新身份」才切（这才叫「审批」而不是「管理员直接改你身份」）。"""
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        p1 = "%s" if mode == "mysql" else "?"
        cur.execute("UPDATE id_switch_reqs SET status = " + p1 + ", approver = " + p1 +
                    ", approver_note = " + p1 + ", handled_at = " + p1 +
                    " WHERE id = " + p1,
                    ('approved' if ok else 'rejected', approver or '', note or '',
                     now_str(), int(rid)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def apply_id_switch_req(rid: int, expect_from: str = '') -> dict:
    """申请人点「用新身份」：把这条单子标记成已生效，返回要切到的人。

    只认 approved；pending/rejected/applied 都返回空 —— 「没批不许换」是硬约束。

    🔴 V1.16.28-17：expect_from 是**申请人自己**的手机号，由调用方从 session 取，
    **绝不能信前端传的值**。原来不校验归属，等于：拿到别人的单号就能切成那个人
    （页面上单号是明文可见的，也可以手敲 URL）—— 是个实打实的冒用后门。
    """
    req = get_id_switch_req(rid)
    if not req or (req.get('status') or '') != 'approved':
        return {}
    if (expect_from or '').strip() and \
            (req.get('from_phone') or '').strip() != expect_from.strip():
        return {}
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        p1 = "%s" if mode == "mysql" else "?"
        cur.execute("UPDATE id_switch_reqs SET status = " + p1 + ", applied_at = " + p1 +
                    " WHERE id = " + p1, ('applied', now_str(), int(rid)))
        conn.commit()
        return req
    finally:
        conn.close()


def my_id_switch_reqs(phone: str, limit: int = 50) -> list:
    """某人自己提过的换人申请（新的在前）。"""
    phone = (phone or '').strip()
    if not phone:
        return []
    mode = resolve_mode()
    conn = get_conn()
    try:
        cur = conn.cursor()
        p1 = "%s" if mode == "mysql" else "?"
        cur.execute("SELECT * FROM id_switch_reqs WHERE from_phone = " + p1 +
                    " ORDER BY id DESC LIMIT %s" % int(limit), (phone,))
        return _v1628_norm_rows(cur)
    finally:
        conn.close()


def now_str() -> str:
    import datetime as _dt
    return _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
