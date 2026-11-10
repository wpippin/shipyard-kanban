# -*- coding: utf-8 -*-
"""
config.py - 配置读取模块
优先读取 config.json（用户可改），未配置的项使用内置默认值。
"""
import os
import sys
import json
import copy

if getattr(sys, 'frozen', False):
    # 打包为 exe 时：
    #   BASE_DIR = exe 所在目录（可读写，放数据库/上传照片/配置/日志等运行数据）
    #   RES_DIR  = PyInstaller 解压的只读资源目录（放代码加密产物、templates 等）
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, '_MEIPASS', BASE_DIR)
else:
    # 源码运行：数据与资源都在项目目录
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = BASE_DIR


DEFAULTS = {
    # 数据库配置：type = auto | mysql | sqlite
    #   auto   : 优先连接 MySQL，连不上自动降级为内置 SQLite（零安装，适合演示/单机）
    #   mysql  : 强制使用 MySQL（正式上线用），连不上会直接报错提示
    #   sqlite : 强制使用 SQLite（完全不需要装数据库）
    "db": {
        "type": "auto",
        "host": "127.0.0.1",
        "port": 3306,
        "user": "root",
        "password": "",
        "database": "shipyard"
    },
    # 管理员账号（提交手尾 / 闭环 / 删除 三个操作统一用此账号登录）
    # 上线前务必修改用户名和密码！密码建议包含大小写字母+数字+特殊符号
    "admin": {
        "username": "admin",
        "password": "Sh1pYard@2026#Admin",
        "remember_days": 7   # 勾选"记住我"后的登录有效期（天）
    },
    # session 签名密钥：留空则启动时自动生成并写回 config.json（重启后记住登录状态不失效）
    "secret_key": "",
    # 企微群机器人 Webhook（通知推送的「回退通道」：船海智云未匹配/异常时使用）
    "webhook_url": "",
    # 船海智云消息推送（通知推送的「主通道」，替代企微 webhook）
    # 对接《船海智云对外服务接口V2.3》，正式参数请联系船海智云管理员获取
    "chz": {
        "enabled": False,        # true=启用船海智云推送（未匹配用户时自动回退 webhook_url）
        "base_url": "https://qyzd.cssccloud.net",
        "companyId": "3",        # 企业 id（正式数据找管理员获取）
        "account": "",           # 对接账号
        "password": "",          # 密码：可填明文（自动 MD5）或已加密的 32 位 MD5
        "uuid": "",              # 校验码（登录接口必填，找管理员获取）
        "appId": "",             # 应用 id（即同步 id）
        "msgType": 2,            # 1平台系统消息 2用户消息通知可跳转 3不可跳转 4待办
        "sourceTargetType": 0,   # 0目标互联网 1目标内网
        "user_cache_file": "chz_users.json"   # 组织架构缓存文件（手机号→用户id映射）
    },
    # 用户访问的看板地址（推送给员工时展示，上线后改成服务器公网/内网地址）
    "site_url": "http://127.0.0.1:5000",
    "host": "0.0.0.0",
    "port": 5000,
    # 超时提醒梯度阈值（小时，按工作日小时计；V1.3 需求4：超管可配置）
    # l1 = 第一级超时提醒（原 24h），l2 = 第二级超时提醒（原 48h）
    # 修改后推送提醒、看板超时展示、分析报表统计三处同步生效
    "thresholds": {
        "l1": 24,
        "l2": 48
    },
    # V1.5.3：按产品船单独设置超时提醒阈值（不配的船自动沿用上面的全局值）
    # 格式：{"2": {"l1": 12, "l2": 24}, "5": {"l1": 8, "l2": 16}}
    # 键是产品船 id（看板 id），可在「超时提醒设置」页面直接改，不用手写 JSON
    "ship_thresholds": {},
    # V1.5.3：安全业务（安全隐患看板）自己的全局超时阈值——
    # 与调试业务（船舶完整性看板）完全分开，各配各的，互不影响
    "safety_thresholds": {
        "l1": 24,
        "l2": 48
    }
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并字典，override 覆盖 base"""
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config() -> dict:
    cfg_path = os.path.join(BASE_DIR, "config.json")
    user_cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
        except Exception as e:
            print(f"[警告] 读取 config.json 失败（{e}），使用默认配置")
    return _deep_merge(DEFAULTS, user_cfg)


CONFIG = load_config()


def get_thresholds(ship_id=None, group: str = "debug") -> dict:
    """V1.3 需求4：读取超时提醒梯度阈值（每次重新读文件，保证 monitor 不重启也生效）。
    V1.5.3：支持"按产品船单独配置"——config.json 里 ship_thresholds 可给某条产品船
    单独设置 l1/l2，没有单独配置的船自动沿用本业务档的全局值。
    group：业务档——'debug'（船舶完整性看板，读 thresholds）
                       'safety'（安全隐患看板，读 safety_thresholds，与调试完全分开）
    返回 {'l1': int, 'l2': int, 'scope': 'ship'|'global'}，非法值回退默认，
    l2 <= l1 时自动拉开。"""
    try:
        with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
    except Exception:
        user_cfg = {}

    def _pick(src, key, default):
        try:
            v = int(src.get(key))
            return v if v >= 1 else default
        except Exception:
            return default

    t = (user_cfg.get("safety_thresholds") if group == "safety" else user_cfg.get("thresholds")) or {}
    l1 = _pick(t, "l1", 24)
    l2 = _pick(t, "l2", 48)

    scope = "global"
    if ship_id is not None:
        per_ship = user_cfg.get("ship_thresholds") or {}
        s = per_ship.get(str(ship_id)) or per_ship.get(ship_id) or {}
        if isinstance(s, dict) and (s.get("l1") or s.get("l2")):
            # 只填了其中一项时，另一项沿用全局
            if s.get("l1"):
                l1 = _pick(s, "l1", l1)
            if s.get("l2"):
                l2 = _pick(s, "l2", l2)
            scope = "ship"

    if l2 <= l1:
        l2 = l1 + 1
    return {"l1": l1, "l2": l2, "scope": scope}
