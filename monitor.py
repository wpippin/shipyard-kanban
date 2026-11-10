# -*- coding: utf-8 -*-
"""
monitor.py - 收尾异常超时自动巡检推送程序

功能：
1. 巡检未闭环记录，按 24h / 48h 梯度推送通知（按「工作日」计时，周六日与法定节假日不计）
   推送通道：仅「船海智云」消息推送（按手机号匹配组织架构用户，支持点击跳转详情页）；
            企微群机器人兜底已按需求彻底移除（V1.4.1），匹配不到的号码静默跳过
2. 推送状态持久化到数据库（notify_level / push_tag），进程重启不会重复推送
3. 内置后台线程：每 60 秒把数据库最新数据写回船舶完整性看板.xlsx（保持后台 Excel 实时更新）
4. 推送链接自动使用本机局域网 IP（config.json 的 site_url 填 "auto"），手机可点

★ 工作日计时说明：
    超时时间只累计工作日小时。周六周日、国家法定节假日不计时；
    调休补班的周六/周日算工作日。节假日数据在 holidays.json 中维护（每年11月国务院发布后更新）。

用法：
    python monitor.py              # 正常启动巡检
    python monitor.py --dry-run    # 试运行：只打印模拟推送，不真正发送消息（演示安全）
    python monitor.py --reset      # 先重置所有未闭环记录的推送状态，再立即巡检（演示重新推送用）
    python monitor.py --reset --dry-run   # 重置 + 试运行
"""
import datetime
import gc
import json
import os
import re
import socket
import sys
import threading
import time

import db
import holidays
import sync_excel
import chz_push
from config import CONFIG, get_thresholds

# 轮询间隔（秒）
POLL_INTERVAL = 10
# Excel 写回间隔（秒）
EXCEL_SYNC_INTERVAL = 60

# 命令行参数
DRY_RUN = "--dry-run" in sys.argv
RESET = "--reset" in sys.argv


# ================= 工具函数 =================

def get_lan_ip() -> str:
    """自动获取本机局域网 IP（用于手机访问看板）。
    V1.3.2：优先 UDP connect 探测；失败（隔离网段无外网）时改用主机名解析，
    再失败才返回空串——避免隔离服务器误回退 127.0.0.1 导致推送链接手机打不开。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return ""


def get_site_url() -> str:
    """看板访问地址：site_url 为 auto/空/127.0.0.1 时自动用本机局域网 IP"""
    site = CONFIG.get("site_url", "http://127.0.0.1:5000")
    port = int(CONFIG.get("port", 5000))
    if site in ("auto", "") or site.startswith("http://127.0.0.1"):
        ip = get_lan_ip() or "127.0.0.1"
        return f"http://{ip}:{port}"
    return site


SITE_URL = get_site_url()


def detail_url(rid, grp=None):
    """记录详情页地址。V1.5.3：安全业务（安全隐患看板）的记录带 ?biz=safety——
    点进去一定落在安全隐患看板的语境；即便记录之后被删，提示页也知道该回哪个看板。
    调试业务的链接与以前完全一致（不带参数）。"""
    url = f"{SITE_URL}/detail/{int(rid)}"
    return url + '?biz=safety' if grp == 'safety' else url


def extract_phones(raw) -> list:
    """从文本或混合字符串中正则提取所有 11 位手机号"""
    if not raw:
        return []
    phones = re.findall(r'1[3-9]\d{9}', str(raw))
    return list(set(phones))


def parse_extra_users(raw) -> list:
    """解析 records.extra_users（JSON 数组字符串），返回手机号列表。
    兼容：JSON [{name, phone}, ...] / 纯文本逗号分隔 / 异常时兜底正则提取。"""
    if not raw:
        return []
    s = str(raw).strip()
    phones = []
    try:
        arr = json.loads(s)
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, dict):
                    p = str(it.get("phone") or "").strip()
                else:
                    p = str(it)
                if re.match(r'^1[3-9]\d{9}$', p):
                    phones.append(p)
    except Exception:
        pass
    # JSON 解析失败或字段缺失 → 正则兜底
    if not phones:
        phones = extract_phones(s)
    return list(set(phones))


# 安全业务（安全隐患看板）的推送文案换词——与 app.py 的 SAFETY_MSG_WORDS 同口径。
# 隐患超时的通知会推给安全员/安全上级，不能出现"收尾未闭环"这类调试口径的词。
_SAFETY_MSG_WORDS = [
    ('异常手尾照片', '隐患照片'),
    ('总手尾', '总数'),
    ('系统名称', '隐患类别'),
    ('手尾', '隐患'),
    ('收尾', '隐患'),
    ('未闭环', '未整改'),
    ('已闭环', '已整改'),
    ('闭环', '整改'),
    ('调试上级责任人', '安全上级'),
    ('调试责任人', '安全负责人'),
]


def _bt(text, grp):
    """按业务档把推送文案换成安全口径（grp != 'safety' 时原样返回）。"""
    if grp != 'safety' or not text:
        return text
    for _a, _b in _SAFETY_MSG_WORDS:
        text = text.replace(_a, _b)
    return text


def _push_and_record(rid: int, msg: str, phones: list, mobile_click_url: str = "", grp: str = None):
    """推送并留痕（V1.2 需求3）：发送后把本次实际目标号码写入 notified_phones。
    与 _set_notify_level 同理：dry-run 只打印不落库，防止「假推送真留痕」。
    V1.4.1：企微群兜底已移除，本函数不再接收 webhook_url。
    V1.5.3：grp='safety' 时把文案换成安全口径（隐患/未整改）。"""
    unique = list(set(phones))
    send_msg(_bt(msg, grp), unique, mobile_click_url=mobile_click_url)
    if not DRY_RUN:
        db.append_notified_phones(rid, unique)


def parse_dt(val):
    """时间解析函数"""
    if isinstance(val, datetime.datetime):
        return val
    if not val:
        return None
    s = str(val).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
                "%m-%d %H:%M:%S",    "%m-%d %H:%M"):
        try:
            dt = datetime.datetime.strptime(s, fmt)
            if "%Y" not in fmt:
                dt = dt.replace(year=datetime.datetime.now().year)
            return dt
        except ValueError:
            continue
    return None


def send_msg(content: str, mobiles: list, mobile_click_url: str = ""):
    """发送推送消息（V1.4.1 起仅船海智云个人通道，企微群兜底已彻底移除）。

    只走「船海智云」按手机号匹配组织架构用户推送（支持点击跳转详情页）；
    匹配不到的号码与主通道异常均不再回退企微群 —— 静默跳过并打印日志。
    """
    unique_phones = list(set(mobiles))[:20] if mobiles else []
    if DRY_RUN:
        print(f"  [试运行] 模拟推送 → 艾特: {unique_phones}")
        return

    if not chz_push.is_enabled():
        print("  ⚠️ 船海智云未启用，本批推送跳过（企微群兜底已移除）")
        return
    try:
        title = (content or "").strip().split("\n")[0][:60]
        sent, unmatched = chz_push.send_msg(
            title, content, unique_phones, mobile_click_url=mobile_click_url)
        print(f"  ✅ 船海智云推送成功 → {len(sent)} 人: {sent}")
        if unmatched:
            print(f"  ⚠️ 未匹配到船海智云用户 {len(unmatched)} 个，已静默跳过"
                  f"（企微群兜底已移除）: {unmatched}")
        return
    except Exception as e:
        print(f"  ❌ 船海智云推送失败: {e}（无企微群兜底，本批不发送）")


def _set_notify_level(rid: int, level: int):
    """试运行（--dry-run）时只打印不落库，防止「假推送真标记」导致真实推送被跳过"""
    if DRY_RUN:
        print(f"  [试运行] 跳过写库：记录 #{rid} notify_level={level}")
        return
    db.set_notify_level(rid, level)


def _set_push_tag(rid: int, tag: str):
    """试运行（--dry-run）时只打印不落库，防止「假推送真标记」导致真实推送被跳过"""
    if DRY_RUN:
        print(f"  [试运行] 跳过写库：记录 #{rid} push_tag={tag}")
        return
    db.set_push_tag(rid, tag)


def excel_sync_loop():
    """后台线程：每 60 秒把数据库最新数据写回船舶完整性看板.xlsx。
    V1.3.2：每轮写回后强制 gc.collect()——openpyxl 反复加载/保存大工作簿会产生
    循环引用与临时图片对象，仅靠自动回收会推高 RSS（长期运行内存只涨不降）。"""
    while True:
        try:
            sync_excel.sync_to_excel()
        except Exception as e:
            print(f"[同步] 后台线程异常: {e}")
        finally:
            gc.collect()
        time.sleep(EXCEL_SYNC_INTERVAL)


# ================= 巡检主逻辑 =================

_BIZ_GROUP_CACHE = {}


def _ship_group(ship_id):
    """看板属于哪个业务档（决定用哪套超时阈值）：debug / safety。带缓存。"""
    try:
        sid = int(ship_id)
    except (TypeError, ValueError):
        return "debug"
    if sid in _BIZ_GROUP_CACHE:
        return _BIZ_GROUP_CACHE[sid]
    try:
        ship = db.get_ship(sid) or {}
    except Exception:
        ship = {}
    grp = "safety" if (ship.get("biz") or "debug") == "safety" else "debug"
    if ship:
        _BIZ_GROUP_CACHE[sid] = grp
    return grp

def _set_deadline_push(rid, val):
    """V1.16：标记「过截止时间的超时提醒」已发过（只提醒一遍）"""
    try:
        db.update_record_fields(rid, {"deadline_push": str(val)})
    except Exception as e:
        print(f"  ⚠️ 写 deadline_push 失败 #{rid}: {e}")


def monitor_job():
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # V1.3 需求4：每轮巡检实时读取超时阈值（config.json 可被超管修改，不重启也生效）
    # V1.5.3：全局值只用于日志展示；每条手尾按自己所属产品船的阈值判定（见循环内）
    _th = get_thresholds()      # 仅用于日志展示；每条记录按其业务档取（见循环内）
    L1, L2 = _th["l1"], _th["l2"]
    print(f"\n[{now_str}] 开始巡检（数据库: {db.resolve_mode()}，推送链接: {SITE_URL}，"
          f"超时阈值: {L1}h/{L2}h）")

    records = db.get_active_records()
    if not records:
        print("  当前无未闭环记录，本轮巡检完毕。")
        return

    now = datetime.datetime.now()
    checked = 0

    for r in records:
        rid = r["id"]
        system_name = r.get("sys_name") or ""
        submit_time_v = r.get("submit_time")
        desc = r.get("description") or ""
        install_user_full = r.get("install_user") or r.get("debug_sup") or ""   # 责任单位负责人（优先相关责任人，缺失时回退作业区/上级）
        status = r.get("status") or ""
        fb_desc = r.get("fb_desc") or ""
        push_tag = str(r.get("push_tag") or "0")
        level = int(r.get("notify_level") or 0)

        # ★ V1.6 A2：违章处罚在审批通过前「不生效」，不参与超时提醒。
        #   审批中 / 已驳回 的记录只挂在发起人那里等审批，不打扰责任人；
        #   审批通过（已通过）后才开始计入超时时钟。
        #   隐患整改 / 5S 为即时生效（rec_type 不是「违章处罚」），不受影响。
        if str(r.get("rec_type") or "") == "违章处罚" and str(r.get("approval_status") or "") != "已通过":
            print(f"  #{rid} 违章处罚（{r.get('approval_status') or '审批中'}）未生效，跳过提醒")
            continue

        # 手机号提取（兼容长字符串与多责任人）
        debug_phones = extract_phones(r.get("debug_users")) + extract_phones(r.get("debug_phones"))
        install_phones = extract_phones(r.get("install_user"))
        area_phones = extract_phones(r.get("area_leader"))
        dept_phones = extract_phones(r.get("dept_leader"))
        # ★ 调试上级责任人：所有级别推送（新发起/反馈/24h/48h）一律抄送
        sup_phones = extract_phones(r.get("debug_sup"))
        # ★ V1.2 需求2：发起时追加的抄送人，同样参与所有级别推送
        extra_phones = parse_extra_users(r.get("extra_users"))
        # ★ V1.3 需求2：手尾发出后追加的责任人（从 users 表选人），等同调试上级，参与所有级别推送
        resp_phones = parse_extra_users(r.get("extra_responsible"))
        # ★ V1.3 需求3：作业长/部门长「被通知过才通知」——之前没被推送过就不打扰
        _notified = set(extract_phones(r.get("notified_phones")))
        area_cond = [p for p in area_phones if p in _notified]
        dept_cond = [p for p in dept_phones if p in _notified]

        submit_dt = parse_dt(submit_time_v)
        if not submit_dt:
            continue

        submit_str = str(submit_time_v).strip()
        # ★ 按工作日计时：周六日与法定节假日不计入，周末补班日计入
        hours = holidays.work_hours_between(submit_dt, now)
        checked += 1
        # V1.5.3：按产品船取超时阈值（该船单独配了就用它的，没配回退本业务档全局）
        # 业务档：安全隐患看板走 safety_thresholds，与调试看板完全分开
        _grp = _ship_group(r.get("ship_id"))
        _durl = detail_url(rid, _grp)
        _th_r = get_thresholds(r.get("ship_id"), group=_grp)
        L1, L2 = _th_r["l1"], _th_r["l2"]
        print(f"  #{rid} [{system_name}] 已过 {hours:.1f} 工作日h | 状态: {status} | 提醒等级: {level} | "
              f"推送标记: {push_tag} | 阈值: {L1}h/{L2}h" + ("（按船）" if _th_r.get("scope") == "ship" else ""))

        # ★ V1.16：安全隐患看板「不需要分级提醒」——不再按 24h/48h 逐级升级，
        #   改为：**过了手填的「截止时间」** 就提醒一遍所有抄送人（只提醒一次）。
        if _grp == "safety":
            _dl_raw = str(r.get("deadline") or "").strip()
            _dl_dt = parse_dt(_dl_raw) if _dl_raw else None
            if not _dl_dt:
                print(f"  #{rid} 未填截止时间，安全档不做超时提醒")
                continue
            if now >= _dl_dt and str(r.get("deadline_push") or "") != "sent":
                _to = list(dict.fromkeys(install_phones + extra_phones + resp_phones + debug_phones))
                if _to:
                    _msg = (
                        f"⏰【隐患已过整改截止时间】\n"
                        f"隐患类别：{system_name}\n"
                        f"提交时间：{submit_str}\n"
                        f"截止时间：{_dl_raw}\n"
                        f"描述：{desc}\n"
                        f"请责任单位尽快整改反馈；本通知已提醒全部抄送人。\n"
                        f"👉 点击或复制下方链接查看详情并上传整改照片：\n"
                        f"{_durl}"
                    )
                    _push_and_record(rid, _msg, _to, mobile_click_url=_durl, grp=_grp)
                _set_deadline_push(rid, "sent")
                print(f"  #{rid} 已过截止时间 {_dl_raw}，已提醒 {len(_to)} 人（仅一次）")
            else:
                print(f"  #{rid} 截止时间 {_dl_raw} 未到 或 已提醒过，跳过")
            continue

        # 【L0】新发起通知（仅限发起后 L1 内的新记录；历史导入且已超时的记录跳过 L0，直接走超时提醒）
        if level < 1 and push_tag in ("0", "", "None") and hours < L1:
            msg = (
                f"🆕【新收尾提出通知】\n"
                f"系统：{system_name}\n"
                f"描述：{desc}\n"
                f"请责任单位负责人【{install_user_full}】尽快查看照片并整改反馈！\n"
                f"👉 点击或复制下方链接查看该收尾详情并上传整改照片：\n"
                f"{_durl}"
            )
            _push_and_record(rid, msg,
                             debug_phones + install_phones + sup_phones + extra_phones + resp_phones,
                             mobile_click_url=_durl, grp=_grp)
            _set_notify_level(rid, 1)
            _set_push_tag(rid, "sent")   # ★ 关键修复：标记已推送，杜绝重复
            continue

        # 【L0-FB】整改反馈推送通知（只推一次，推送后标记 sent）
        if push_tag == "feedback_pending":
            msg = (
                f"✅【收尾整改反馈通知】\n"
                f"系统：{system_name}\n"
                f"反馈说明：{fb_desc or '无'}\n"
                f"请调试责任人跟踪确认闭环，责任单位负责人保持关注！\n"
                f"👉 点击或复制下方链接查看该收尾详情并确认闭环：\n"
                f"{_durl}"
            )
            _push_and_record(rid, msg,
                             debug_phones + install_phones + sup_phones + extra_phones + resp_phones,
                             mobile_click_url=_durl, grp=_grp)
            _set_push_tag(rid, "sent")   # ★ 关键修复：原代码漏了这行，导致每10秒重复推送
            continue

        # 【L2】超 L2 小时 → 部门领导介入（等级 2 → 3）
        if hours >= L2 and level < 3:
            msg = (
                f"🚨【极度超时：超{L2}小时未闭环】\n"
                f"系统名称：{system_name}\n"
                f"收尾发起：{submit_str}\n"
                f"描述：{desc}\n"
                f"已超时 {hours:.0f} 工作日小时（不含周末/法定节假日，≥{L2}h），请部门领导立即介入考核！\n"
                f"👉 点击或复制下方链接查看该收尾详情：\n"
                f"{_durl}"
            )
            _push_and_record(rid, msg,
                             debug_phones + install_phones + sup_phones + extra_phones + resp_phones
                             + area_cond + dept_cond,
                             mobile_click_url=_durl, grp=_grp)
            _set_notify_level(rid, 3)
            _set_push_tag(rid, "sent")

        # 【L1】超 L1 小时 → 责任单位领导跟进（等级 0/1 → 2）
        elif hours >= L1 and level < 2:
            msg = (
                f"⚠️【收尾超时：超{L1}小时未闭环】\n"
                f"系统名称：{system_name}\n"
                f"收尾发起：{submit_str}\n"
                f"描述：{desc}\n"
                f"已超时 {hours:.0f} 工作日小时（不含周末/法定节假日，≥{L1}h），请责任单位领导跟进处理。\n"
                f"👉 点击或复制下方链接查看该收尾详情：\n"
                f"{_durl}"
            )
            _push_and_record(rid, msg,
                             debug_phones + install_phones + sup_phones + extra_phones + resp_phones
                             + area_cond,
                             mobile_click_url=_durl, grp=_grp)
            _set_notify_level(rid, 2)
            _set_push_tag(rid, "sent")

    print(f"  共检查 {checked} 条未闭环记录，本轮巡检完毕。")


def run_forever():
    """常驻巡检入口：供打包后的单 exe（main.py）以线程方式调用。

    与 __main__ 逻辑一致：初始化数据库、启动 Excel 写回线程，然后循环巡检。
    V1.3.2：每 30 轮（约 5 分钟）主动 gc.collect() 一次，释放巡检累积的
    临时对象（记录字典/正则/推送响应），保证长期运行 RSS 稳定。"""
    db.init_db()
    threading.Thread(target=excel_sync_loop, daemon=True).start()
    _loop_count = 0
    while True:
        try:
            monitor_job()
        except Exception as e:
            print(f"❌ 主循环异常: {e}")
        _loop_count += 1
        if _loop_count % 30 == 0:
            gc.collect()
        time.sleep(POLL_INTERVAL)


# ================= 启动入口 =================

if __name__ == "__main__":
    print("=" * 55)
    print("  收尾异常超时自动巡检程序 启动")
    print(f"  数据库模式: {db.resolve_mode()}")
    print(f"  推送链接: {SITE_URL}")
    print(f"  计时口径: 工作日小时（周六日/法定节假日不计，调休补班计入）")
    if DRY_RUN:
        print("  ⚠️  试运行模式：只打印模拟推送，不会真正发送企微消息")
    if RESET:
        print("  ⚠️  已指定 --reset：将重置推送状态后立即巡检（用于演示重新推送）")
    print("=" * 55)

    db.init_db()

    if RESET:
        n = db.reset_push_state()
        print(f"  ♻️  已重置 {n} 条未闭环记录的推送状态")

    run_forever()
