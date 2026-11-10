# -*- coding: utf-8 -*-
"""
chz_push.py - 船海智云消息推送模块（替代企微群机器人 webhook）

对接《船海智云对外服务接口 V2.3》（前缀 https://qyzd.cssccloud.net/）：
  - 账号认证（获取 token）  : POST /api/app-site/index/login/action
  - 组织架构全量同步        : GET  /api/app-site/sync_manage/sync_org
  - 推送消息（按用户 id）    : POST /api/message-center/msg_manage/send_msg

推送流程：
  1. 配置：config.json 的 "chz" 段（base_url / companyId / account / password / uuid / appId）
  2. 首次运行：拉取组织架构，建立「手机号 → 船海智云用户id」映射，缓存到 chz_users.json
     （手机号来自组织架构的 phone 字段；程序按 11 位手机号匹配人员）
  3. 推送：手机号 → 用户 id → 调 send_msg 接口；支持 mobileClickUrl 跳转系统详情页

安全/兼容说明：
  - token 按返回的 tokenTimeout 缓存，过期前自动复用，过期自动重新登录
  - 未匹配到船海智云用户 / 接口异常时，由调用方（monitor.py）回退企微 webhook，不丢消息
  - password 字段可填「明文密码」（自动 MD5）或「已加密的 32 位 MD5」，两者均兼容

命令行：
  python chz_push.py --dry-run            试运行：只打印将要推送的内容，不真正发送
  python chz_push.py --refresh-users      强制重新拉取组织架构并更新缓存
  python chz_push.py --test 13812345678   给指定手机号发一条测试消息（验证对接）
"""
import datetime
import hashlib
import json
import os
import sys
import time

import requests

from config import CONFIG, BASE_DIR

# ---------------- 配置 ----------------
CHZ = CONFIG.get("chz") or {}
ENABLED = bool(
    CHZ.get("enabled")
    and (CHZ.get("base_url") or "").strip()
    and (CHZ.get("account") or "").strip()
    and (CHZ.get("appId") or "").strip()
)
BASE_URL = (CHZ.get("base_url") or "https://qyzd.cssccloud.net").strip().rstrip("/")
COMPANY_ID = str(CHZ.get("companyId") or "3").strip()
ACCOUNT = str(CHZ.get("account") or "").strip()
PASSWORD_RAW = str(CHZ.get("password") or "").strip()
UUID = str(CHZ.get("uuid") or "").strip()
APP_ID = str(CHZ.get("appId") or "").strip()
MSG_TYPE = int(CHZ.get("msgType") or 2)
SOURCE_TARGET_TYPE = int(CHZ.get("sourceTargetType") or 0)
USER_CACHE_FILE = os.path.join(BASE_DIR, CHZ.get("user_cache_file") or "chz_users.json")

# token 内存缓存：{value, expire_at}
_TOKEN = {"value": "", "expire_at": 0}
# 组织架构内存缓存：phone -> {id, code, name}
_USERS = None


# ---------------- 工具函数 ----------------

def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _norm_phone(p) -> str:
    """手机号归一化：只保留数字，兼容 +86/空格/横线，截取 11 位；非手机号返回空串"""
    if not p:
        return ""
    digits = "".join(ch for ch in str(p) if ch.isdigit())
    if len(digits) >= 13 and digits.startswith("86"):
        digits = digits[2:]
    if len(digits) >= 11 and digits.startswith("1"):
        return digits[-11:]
    return ""


def _password_param() -> str:
    """password 兼容：已填 32 位 MD5 则直接用，否则对明文做 MD5"""
    p = PASSWORD_RAW
    if len(p) == 32 and all(c in "0123456789abcdefABCDEF" for c in p):
        return p.lower()
    return _md5(p)


def _req(method: str, path: str, timeout: int = 15,
         params=None, json_body=None, token: str = None):
    url = BASE_URL + path
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    resp = requests.request(method, url, params=params, json=json_body,
                            headers=headers, timeout=timeout)
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"船海智云接口返回非 JSON（HTTP {resp.status_code}）: {resp.text[:300]}")
    if data.get("code") not in (0,):
        raise RuntimeError(f"船海智云接口错误: {data}")
    return data.get("data")


def is_enabled() -> bool:
    """chz 配置完整且 enabled=true 时启用船海智云推送"""
    return ENABLED


# ---------------- Token 获取（登录） ----------------

def get_token(force: bool = False) -> str:
    """获取/复用 token；过期或强制时重新登录"""
    now = time.time()
    if not force and _TOKEN["value"] and _TOKEN["expire_at"] > now + 60:
        return _TOKEN["value"]

    payload = {
        "companyId": COMPANY_ID,
        "account": ACCOUNT,
        "password": _password_param(),
        "uuid": UUID,
    }
    data = _req("POST", "/api/app-site/index/login/action", json_body=payload, timeout=10)
    d = data or {}
    token = str(d.get("tokenValue") or "")
    if not token:
        raise RuntimeError("船海智云登录成功但未返回 tokenValue")
    timeout_s = int(d.get("tokenTimeout") or 7200)
    _TOKEN["value"] = token
    _TOKEN["expire_at"] = now + timeout_s
    print(f"  [船海智云] 登录成功，token 有效 {timeout_s} 秒")
    return token


# ---------------- 组织架构（手机号 → 用户id 映射） ----------------

def fetch_users() -> dict:
    """拉取全量组织架构，返回 {phone: {id, code, name}}"""
    token = get_token()
    params = {
        "syncId": APP_ID,
        "isAll": "true",
        "isSyncCodingUser": "false",
        "companyId": COMPANY_ID,
    }
    data = _req("GET", "/api/app-site/sync_manage/sync_org",
                params=params, token=token)
    d = data or {}
    mapping = {}
    for u in d.get("syncUserList") or []:
        phone = _norm_phone(u.get("phone"))
        if phone:
            mapping[phone] = {
                "id": str(u.get("id") or ""),
                "code": str(u.get("code") or ""),
                "name": str(u.get("name") or ""),
            }
    print(f"  [船海智云] 组织架构同步完成：共 {len(mapping)} 个手机号可匹配")
    return mapping


def ensure_users(force: bool = False) -> dict:
    """加载用户映射：优先读本地缓存，无缓存/强制/企业应用不符时拉取组织架构"""
    global _USERS
    if _USERS is not None and not force:
        return _USERS
    if not force and os.path.exists(USER_CACHE_FILE):
        try:
            with open(USER_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
            # 缓存必须绑定 companyId/appId，防止换正式环境后推送到旧环境人员
            if (str(cached.get("companyId") or "") == COMPANY_ID
                    and str(cached.get("appId") or "") == APP_ID):
                _USERS = cached.get("users") or {}
                print(f"  [船海智云] 使用本地组织架构缓存（{len(_USERS)} 人），"
                      f"需要更新请运行 --refresh-users")
                return _USERS
            print("  [船海智云] 缓存与当前企业/应用不匹配，重新拉取组织架构...")
        except Exception:
            pass
    _USERS = fetch_users()
    _save_users_cache()
    return _USERS


def _save_users_cache():
    """把组织架构映射连同企业/应用标识写入缓存文件"""
    try:
        with open(USER_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "companyId": COMPANY_ID,
                "appId": APP_ID,
                "saved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "users": _USERS or {},
            }, f, ensure_ascii=False, indent=1)
        print(f"  [船海智云] 组织架构已缓存到 {os.path.basename(USER_CACHE_FILE)}")
    except Exception as e:
        print(f"  [船海智云] 组织架构缓存写入失败: {e}")


# ---------------- 消息推送 ----------------

def current_user_info(sso_token: str) -> dict:
    """单点登录：用船海智云跳转时附带的 access_token 换取用户信息（文档 2.21）。

    接口：GET /api/app-site/index/current_user_info
    返回 data 里含 id / account / name / code(工号) / phone / divisionName /
    divisionFullName / headImg / companyName 等；失败返回 {}（不抛异常，
    因为单点登录失败不能让用户进不去系统）。
    """
    tok = (sso_token or "").strip()
    if not tok:
        return {}
    try:
        data = _req("GET", "/api/app-site/index/current_user_info",
                    token=tok, timeout=10)
        return data or {}
    except Exception as e:
        print(f"  [船海智云] 单点登录校验失败: {e}")
        return {}


def send_msg(title: str, content: str, mobiles: list,
             mobile_click_url: str = "", pcweb_click_url: str = "") -> tuple:
    """
    按手机号推送消息给对应船海智云用户。

    返回 (sent_phones, unmatched_phones)：
      sent_phones     推送成功（已匹配并发送）的手机号列表
      unmatched_phones 在组织架构中未匹配到的手机号（调用方可回退 webhook）
    接口异常时抛出 RuntimeError，由调用方决定回退策略。
    """
    phones = [_norm_phone(p) for p in (mobiles or [])]
    phones = list(dict.fromkeys(p for p in phones if p))
    if not phones:
        return [], []

    users = ensure_users()
    matched = [(p, users[p]) for p in phones if users.get(p)]
    unmatched = [p for p in phones if p not in users]
    if not matched:
        print(f"  [船海智云] 手机号均未匹配到组织架构用户，跳过: {unmatched}")
        return [], unmatched

    token = get_token()
    extra = {
        "templateType": "0",
        "mobileClickUrl": mobile_click_url or "",
        "pcwebClickUrl": pcweb_click_url or "",
        "callbackUrl": "",
    }
    payload = {
        "id": "",
        "title": str(title or ""),
        "content": str(content or ""),
        "appId": APP_ID,
        "receiveUserVOS": [
            {"receiveUserId": u["id"], "state": "", "extra": ""}
            for _, u in matched
        ],
        "companyId": COMPANY_ID,
        "msgType": MSG_TYPE,
        "extra": json.dumps(extra, ensure_ascii=False),
        "sourceTargetType": SOURCE_TARGET_TYPE,
    }
    data = _req("POST", "/api/message-center/msg_manage/send_msg",
                json_body=payload, token=token)
    sent_phones = [p for p, _ in matched]
    print(f"  [船海智云] 推送成功：{len(sent_phones)} 人 → {sent_phones}"
          + (f"，未匹配 {len(unmatched)} 人 → {unmatched}" if unmatched else "")
          + (f"，消息 id: {data}" if data else ""))
    return sent_phones, unmatched


# ---------------- 命令行入口 ----------------

def main():
    print("=" * 55)
    print("  船海智云消息推送模块 自检/测试")
    print(f"  接口前缀: {BASE_URL}")
    print(f"  启用状态: {'✅ 已启用' if ENABLED else '❌ 未启用（config.json 的 chz 配置不完整）'}")
    print("=" * 55)

    dry_run = "--dry-run" in sys.argv
    test_phone = None
    if "--test" in sys.argv:
        i = sys.argv.index("--test")
        if i + 1 < len(sys.argv):
            test_phone = sys.argv[i + 1]

    if not ENABLED:
        print("配置不完整，无法继续。请先在 config.json 的 chz 段填写：")
        print("  enabled / base_url / account / password / uuid / appId / companyId")
        sys.exit(1)

    if dry_run:
        print("【试运行】仅打印将要执行的动作，不调用任何接口：")
        print("  1. 登录获取 token（POST /api/app-site/index/login/action）")
        print("  2. 拉取组织架构（GET /api/app-site/sync_manage/sync_org）")
        print("  3. 推送消息（POST /api/message-center/msg_manage/send_msg）")
        if test_phone:
            print(f"  测试消息接收人: {test_phone}")
        print("试运行结束。正式执行请去掉 --dry-run。")
        return

    # 刷新组织架构
    if "--refresh-users" in sys.argv:
        print("强制重新拉取组织架构...")
        ensure_users(force=True)

    # 发测试消息
    if test_phone:
        print(f"发送测试消息到 {test_phone} ...")
        try:
            sent, unmatched = send_msg(
                "【测试消息】船海智云推送对接验证",
                "如果您收到此消息，说明系统已成功对接船海智云消息推送。",
                [test_phone],
            )
            if sent:
                print(f"✅ 测试消息发送成功")
            else:
                print(f"⚠️ 该手机号未匹配到组织架构用户，请确认手机号或刷新组织架构")
        except Exception as e:
            print(f"❌ 测试消息发送失败: {e}")
            sys.exit(1)
        return

    # 仅检查映射
    users = ensure_users()
    print(f"当前可匹配手机号: {len(users)} 人")
    sample = list(users.items())[:5]
    for phone, u in sample:
        print(f"  {phone} → {u['name']} (id={u['id']}, 工号={u['code']})")
    if not users:
        print("提示：如组织架构为空，请确认 appId/companyId 是否正确，或联系船海智云管理员授权。")


if __name__ == "__main__":
    main()
