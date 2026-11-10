# -*- coding: utf-8 -*-
"""船舶完整性管理看板 Web 服务"""
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, send_file, session, jsonify, abort
from datetime import datetime, timedelta
import io
import os
import json
import html as _html_mod
import re
import secrets
import hmac
import hashlib
import logging
import socket
import tempfile
import threading
import time
import urllib.parse

import db
import holidays
from config import CONFIG, BASE_DIR, RES_DIR, get_thresholds
import safety_review      # V1.12 安全隐患看板「审核权」名单（按部门自动生成）
import admin_grant        # V1.14 超管名单（按部门/按个人授权超管，改完即时生效）
import debug_sup_grant    # V1.16.2 调试业务「调试上级责任人」名单（不写业务库、即时生效）
import debug_admin_grant   # V1.16.16 调试业务「超管」名单（与安全档 admin_grant 各自独立）
import debug_user_grant    # V1.16.17 调试业务「调试责任人」名单（个人指定 > 部门授权的优先级用）
import safety_user_grant   # V1.39 安全业务「普通用户」名单（按部门/人员批量开通，与其它名单独立）
import board_categories    # V1.46 看板类别配置（超管可增删改，不再写死在代码里）
import ship_overview      # V1.48 综合数据总览（只读聚合层，零改表）
import chz_org            # V1.13 船海智云组织架构（部门树 + 人员）：按部门选人 / 定审核权
from sixs_data import SIX_S, sixs_items, sixs_count

# 打包为 exe 时模板从内置只读资源目录加载，源码运行时即项目目录
app = Flask(__name__,
            template_folder=os.path.join(RES_DIR, 'templates'),
            static_folder=os.path.join(RES_DIR, 'static'))

app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ============ 密码哈希工具 ============
def hash_password(password: str) -> str:
    """sha256 + 随机盐，返回 salt+hash 拼接字符串"""
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode('utf-8')).hexdigest()
    return salt + h

def verify_password(password: str, stored: str) -> bool:
    """验证密码：stored 前 32 位是 salt，后面是 hash"""
    if not stored or len(stored) < 64:
        return False
    salt = stored[:32]
    h = stored[32:]
    return hmac.compare_digest(
        hashlib.sha256((salt + password).encode('utf-8')).hexdigest(), h)

# ============ 角色与权限 ============
# V1.5.3：系统只保留一个超级管理员 = 王平平（2026-09-21 用户明确要求）
# 启动时会自动把其它 super_admin 账号降为业务负责人，保证"唯一超管"
SUPER_ADMIN_PHONE = '17786363880'
SUPER_ADMIN_NAME = '王平平'

# V1.17.1：固定场地清单 —— 安全隐患看板「场地」分类下，提交隐患时选择发生场地
# V1.37（用户要求「所有的码头场地分开」）：1-3 / 4-5 / 7-8 这类合并项全部拆成单个码头
_PLACE_NAMES = [
    '1号码头', '2号码头', '3号码头', '4号码头', '5号码头',
    '6号码头', '7号码头', '8号码头',
    '大气试验场地', '5号船台', '4号船台', '东坞墙', '500米码头', '其他',
]
# 历史记录里已存过的合并场地名：不再出现在下拉，但允许旧记录通过校验 / 编辑
_PLACE_NAMES_LEGACY = ['1-3号码头', '4-5号码头', '7-8号码头']

# V1.37：责任单位唯一权威名单 —— 前端下拉、配置弹窗、后端校验全部读这一份，
# 杜绝超管在「责任单位名称」里自由输入人名 / 外号造成的口径混乱。
# V1.43：按「总装二部」组织架构补齐全部 12 个二级部门（作业区 / 科室）。
#   前 12 项＝总装二部下属单位，顺序与组织架构一致；
#   后 6 项＝历史沿用单位，保留以免打断历史记录与既有配置。
_AREA_UNITS = [
    '综合管理室', '机装作业区', '电装作业区', '安全设备室',
    '技术质量室', '调试作业区', '支持作业区', '动装作业区',
    '船装作业区', '特船作业区', '生产管理室', '上建作业区',
    '设计', '物资', '工艺', '搭载', '涂装', '管理室',
]


# V1.46：类别不再写死在代码里，统一读 board_categories 配置。
#   一个类别 = 名字(key，直接存 ships.btype) + 行为(behavior)。
#   behavior 只有 ship / place / device 三种，决定要不要船号、要不要场地位置、
#   归到哪个列表组；下游所有判断只看 behavior ⇒ 超管加新类别 = 加一条配置，
#   零改代码、零重编译。BOARD_TYPE_DEVICE 保留为兼容别名。
BOARD_TYPE_DEVICE = board_categories.default_key('device')
_BOARD_TYPES_ALL = board_categories.all_keys()   # 启动快照；实际校验一律走 is_valid()


def _board_cat(ship):
    """看板 → behavior：'ship' / 'place' / 'device'（V1.46 配置驱动）。
    历史兼容：btype 空但填了船号仍算产品船；配置里已不存在的历史 btype 归 place。"""
    bt = ((ship or {}).get('btype') or '').strip()
    no = ((ship or {}).get('ship_no') or '').strip()
    if bt:
        return board_categories.behavior_of(bt)
    return 'ship' if no else 'place'


def _is_device_board(ship) -> bool:
    """看板是否「设备」类（V1.42）。"""
    return _board_cat(ship) == 'device'


def _is_place_board(ship) -> bool:
    """看板是否「场地」类（与 boards.html 的分类口径一致）：
    产品船 / 设备 → False；地面场地、其他、无船号 → True。"""
    return _board_cat(ship) == 'place'


def _board_list_home(biz, from_page='', ship=None):
    """V1.46b：看板增 / 删 / 改之后回哪个列表页。
    设备看板已收进安全看板统一列表（与产品船 / 场地并列），不再有独立页 ⇒
    设备类一律回 /safety 并自动选中对应类别 tab。"""
    if biz != 'safety':
        return '/boards'
    if (from_page or '').strip() == 'devices' or _is_device_board(ship or {}):
        return url_for('safety_board', cat=board_categories.default_key('device'))
    return '/safety'


ROLE_LEVEL = {'debug_user': 1, 'debug_sup': 2, 'super_admin': 3}
ROLE_LABELS = {
    'super_admin': '超级管理员',
    'debug_sup': '调试上级责任人',
    'debug_user': '调试责任人',
}

def enforce_single_super_admin():
    """保证【调试业务】只有一个超管（王平平）：启动时把其它 debug 超管降为业务负责人。
    V1.5.3：超管按业务档独立——安全业务（安全隐患看板）可以有自己的超管，本函数不碰它。
    幂等——没有多余超管时什么都不做。"""
    try:
        changed = []
        for u in db.get_all_users():
            # V1.14：名单里明确授权的超管不动（否则重启就把页面上加的超管降级了）
            # V1.16.16：新增的「调试业务超管名单」同样算授权，一起保护
            try:
                if admin_grant.is_member(u.get('phone') or '', u.get('name') or '') or \
                   debug_admin_grant.is_member(u.get('phone') or '', u.get('name') or ''):
                    continue
            except Exception:
                pass
            if (u.get('role') == 'super_admin'
                    and (u.get('biz') or 'debug') == 'debug'
                    and (u.get('phone') or '') != SUPER_ADMIN_PHONE):
                db.update_user_role(u['phone'], 'debug_sup')
                changed.append(f"{u.get('name') or ''}({u['phone']})")
        if changed:
            logger.warning(f"超管唯一化（调试业务）：已将 {len(changed)} 个账号降为业务负责人 -> {', '.join(changed)}")
        _safety_admins = [f"{u.get('name') or ''}({u['phone']})" for u in db.get_all_users()
                          if u.get('role') == 'super_admin' and (u.get('biz') or 'debug') == 'safety']
        logger.info(f"超级管理员账号[调试业务]：{SUPER_ADMIN_NAME}（{SUPER_ADMIN_PHONE}）")
        logger.info("超级管理员账号[安全业务]：" + ('、'.join(_safety_admins) if _safety_admins else '（暂无）'))
    except Exception as e:
        logger.warning(f"超管唯一化检查失败（不影响运行）: {e}")


# ============ V1.12 业务档管理权 / 安全隐患看板审核权 ============
_SHIP_BIZ_CACHE = {}


def _ship_biz_of(ship_id) -> str:
    """看板属于哪个业务档（debug / safety）。带缓存，避免循环里反复查库。"""
    try:
        sid = int(ship_id or 0)
    except (TypeError, ValueError):
        return 'debug'
    if sid in _SHIP_BIZ_CACHE:
        return _SHIP_BIZ_CACHE[sid]
    try:
        biz = (db.get_ship(sid) or {}).get('biz') or 'debug'
    except Exception:
        biz = 'debug'
    _SHIP_BIZ_CACHE[sid] = biz
    return biz


def _request_biz() -> str:
    """当前这个请求属于哪个业务档（debug / safety），取不到就返回 ''。

    V1.16.16：超管名单按业务档分开之后，登录态里的角色也必须按「这次打开的页面属于哪个档」来算 ——
    否则在完整性看板加的超管，打开安全隐患看板照样是超管，两套权限就串了。"""
    try:
        b = (request.args.get('biz') or '').strip()
    except Exception:
        b = ''
    if b in ('debug', 'safety'):
        return b
    try:
        p = (request.path or '')
    except Exception:
        return ''
    if p == '/safety' or p.startswith('/safety/'):
        return 'safety'
    if p.startswith('/boards') or p.startswith('/board/') or p == '/':
        return 'debug'
    # 带看板 id 的页面（/statistics/<id>、/syslists?ship_id=… 等）
    sid = ''
    try:
        sid = (request.args.get('ship_id') or '').strip()
    except Exception:
        sid = ''
    if not sid:
        try:
            sid = (request.view_args or {}).get('ship_id') or ''
        except Exception:
            sid = ''
    if sid:
        return _ship_biz_of(sid)
    return ''


def _biz_admin(biz) -> bool:
    """是不是某个业务档的超管：
    · 固定超管 王平平 —— 两个系统都算（用户 V1.16.16 明确要求）；
    · 或者在该业务档自己的「超管名单」里（名单是按档分开的两份配置）。

    V1.16.22：门禁从「登录态」改成「认到人」。免登录下没认到人（直接敲 URL 进来）
    原来会被 _is_logged_in() 恒 True 放行、再回退成超管 —— 等于谁都能管。

    🔴 V1.16.27：改成要求**可信身份**（船海智云 SSO 认证过）。
    自选身份只是「自己声明是谁」，拿它当管理权的门槛 = 任何人都能选个名字当超管。
    业务动作（提隐患 / 闭环）仍走 _has_identity()，不受影响。"""
    if not _identity_trusted():
        return False
    _b = biz or 'debug'
    me = _current_identity_phone()
    # 🔴 V1.16.31：两个"固定超管"**两个业务档都算**（用户要求：
    #   「王平平应该是所有看板的默认管理员」）。
    #   原来只比 SUPER_ADMIN_PHONE，config.json 里的 admin 兜底超管要靠
    #   下面 `_current_role()=='super_admin'` 那条兼容分支兜着 ——
    #   而那条分支会`db.get_user_by_phone('admin')` 拿到 None，
    #   再按 `(_u.get('biz') or 'debug') == _b` 判 ⇒ **只有 debug 档过、safety 档挂**。
    if me and me in (SUPER_ADMIN_PHONE, ADMIN_USER):
        return True
    try:
        _g = admin_grant if _b == 'safety' else debug_admin_grant
        if _g.is_member(me, (session.get('my_name') or '').strip()):
            return True
    except Exception:
        pass
    # 兼容历史数据：库里 role 本来就是超管、且账号本身属于这个档
    if _current_role() == 'super_admin':
        try:
            _u = db.get_user_by_phone(me) or {}
            if not _u:
                # 🔴 V1.16.31：库里查不到这个人（config 兜底超管就是这种情况）
                #   却因为 `or 'debug'` 被当成 debug 档 ⇒ 安全隐患看板永远进不去。
                #   查不到账号时**只要角色确实是超管就放行**，别猜档。
                return True
            return (_u.get('biz') or 'debug') == _b
        except Exception:
            return False
    return False


def _can_manage_biz(biz) -> bool:
    """某业务档的「管理权」（建看板 / 改默认责任人 / 隐患类别 / 看板改名删除）：
    · 调试档：超管 + 调试上级责任人（保持原口径）
    · 安全档：**只有超管**（V1.12 用户明确：安全上级、安全员『全部不需要』）
    V1.16.16：超管按业务档判 —— 完整性看板的超管 ≠ 安全隐患看板的超管。
    V1.16.22：同样要求先认到人。
    🔴 V1.16.27：要求可信身份（SSO 认证过），自选身份不再能拿到管理权。
    """
    if not _identity_trusted():
        return False
    _b = biz or 'debug'
    if _biz_admin(_b):
        return True
    if _b == 'safety':
        return False
    return _current_role() == 'debug_sup'


def _pick_debug_role(phone='', name=''):
    """V1.16.17：调试档两级名单的生效角色。

    名单有两份（都已经支持「按部门」和「按个人」）：
      · debug_sup_grant   —— 调试上级责任人
      · debug_user_grant  —— 调试责任人
    优先级：**个人指定 > 部门授权**；两份都是个人指定时，**「调试责任人」优先**（低权限优先，
    两份都写时不会争权）。返回 'debug_sup' / 'debug_user' / ''（不在任何名单里）。
    这样「部门整批授成上级」之后，还能把个别人单独退回到「调试责任人」。
    """
    phone = (phone or '').strip()
    name = (name or '').strip()
    if not phone and not name:
        return ''
    eu = es = du = ds = False
    try:
        eu = debug_user_grant.is_explicit(phone, name)
        es = debug_sup_grant.is_explicit(phone, name)
        du = debug_user_grant.is_dept_member(phone, name)
        ds = debug_sup_grant.is_dept_member(phone, name)
    except Exception:
        return ''
    if eu and not es:
        return 'debug_user'
    if es and not eu:
        return 'debug_sup'
    if eu and es:
        return 'debug_user'
    if du:
        return 'debug_user'
    if ds:
        return 'debug_sup'
    return ''


GRANT_ROLE_LABELS = {
    'admin': '超管',
    'sup': '调试上级责任人',
    'user': '调试责任人',
    'review': '审核人',
}


def _grant_list_of(biz, role):
    """V1.16.17：按「业务档 + 角色」取对应的名单模块；返回 None = 这个档没有这个角色。

    · 两个看板都有「超管」；调试档另有「调试上级责任人 / 调试责任人」；
     安全隐患看板另有「审核人」。四个角色全部只有超管能改。
    """
    biz = 'safety' if biz == 'safety' else 'debug'
    role = (role or 'admin').strip() or 'admin'
    if role == 'admin':
        return admin_grant if biz == 'safety' else debug_admin_grant
    if biz == 'safety':
        # V1.39：安全档补「普通用户」名单（按部门 / 人员批量开通），与审核人名单互不影响
        if role == 'review':
            return safety_review
        if role == 'user':
            return safety_user_grant
        return None
    if role == 'sup':
        return debug_sup_grant
    if role == 'user':
        return debug_user_grant
    return None


def _board_name_exists(biz, name, exclude_id=None) -> bool:
    """V1.16.13：同一业务档内看板名称不允许重复（忽略首尾空格与大小写）。
    exclude_id 用于「改看板信息」时把自己排除在外。"""
    key = (name or '').strip().lower()
    if not key:
        return False
    try:
        ships = db.get_ships(biz)
    except Exception as e:
        logger.error(f"查重看板名失败（跳过校验）：{e}")
        return False
    for s in ships or []:
        try:
            if exclude_id is not None and int(s.get('id') or 0) == int(exclude_id):
                continue
        except Exception:
            pass
        if (s.get('name') or '').strip().lower() == key:
            return True
    return False


def _dup_board_name_page(name) -> str:
    """同名看板的友好拦截页（引用名字做了 HTML 转义，避免注入）。"""
    safe = _html_mod.escape(name or '')
    return ("<script>alert('已存在同名看板："
            + safe.replace('\\', '').replace("'", '’')
            + "\\n请换个名称后再提交。');history.back();</script>")


def _current_identity_phone() -> str:
    """当前操作人手机号：优先「单点登录 / 自选身份」认到的人（my_phone），
    其次登录态。免登录下 user_phone 是空的，用它能正确认出人。"""
    return (session.get('my_phone') or '').strip() or _current_phone()


def _has_identity() -> bool:
    """V1.16.22：当前会话「认到人」了吗（SSO 或自选身份都算）。

    免登录 = 不做账号密码校验，但**不等于没有身份**。修掉「没认到人也算超管」：
    所有**管理权**（建看板 / 改名删板 / 改默认人 / 用户管理 / 阈值 / 名单授权）
    一律要求先认到人；看板浏览、发起、闭环这些业务动作仍按免登录放行，
    避免把同事正常提隐患的路径一起堵死。

    🔴 V1.16.27：这只是「弱身份」。自选身份 = 自己声明是谁，**没有密码、没有二次验证**，
    任何人都能声明成别人。真正可信的是船海智云单点登录（SSO）——
    ——身份可信度判定请用 `_identity_trusted()`，别再用这里。"""
    try:
        if not NO_LOGIN:
            return bool(session.get('logged_in'))
        return bool((session.get('my_phone') or '').strip())
    except Exception:
        return False


def _identity_trusted() -> bool:
    """V1.16.27：当前身份是「**平台认证过的**」吗（只有走船海智云单点登录才算）。

    为什么必须和 `_has_identity()` 分开：
      · `_has_identity()` —— 只要会话里有个人号就算（自选身份也过），
        用于**业务动作**（提隐患、闭环），保持同事手机上一键能用，不被登录卡住。
      · `_identity_trusted()` —— 必须是 SSO 用 access_token 换回来的，
        用于**管理权**（用户管理 / 名单授权 / 阈值 / 建删看板）和「我是谁」以外的敏感操作。

    也就是说：开了强制模式（config.json 的 sso_require）之后，
    **光在页面上选个名字骗不过管理权这一关**——这才是「冒用账号」被真正堵住的地方。
    判据是 session 里的 id_src：`_sso_from_cscloud` 写 'sso'、`/login` 写 'pwd'、
    `/my/whoami` 写 'pick'、`/my?as=` 写 'as'。

    🔴 V1.16.30（用户要求）：**还没开强制模式时，自选身份也当可信**。
    为什么必须改：船海智云的跳转地址到现在还没配好，本地/内网所有人都是
    「自己声明是谁」进来的（id_src='pick'）。V1.16.27 一刀切只要 'sso'，
    结果是 **用户管理、换人审批、名单授权这些管理页一个都进不去**
    （点进去就是「权限不足！」然后弹框跳走），本地根本没法干活。
    口径：`sso_require=false` ⇒ 'pick' 也算可信；一旦打开强制模式，
    这条自动失效、只认 'sso'，冒用口子同步关闭 —— **上线前必须记得打开**。

    🔴 V1.16.31：补 `'pwd'`（账号密码登录通过）。
    用户报障「用户管理里王平平进不去」的根因：`/login` 原来**只写 user_phone**，
    不写 my_phone / id_src，于是 `_identity_trusted()` 第一行就 False。
    密码验证通过是强凭据，不该比「自己声明名字」还弱。
    ⚠️ `'pwd'` **不受 `sso_require` 影响**：强制模式下账号密码仍是合法正门，
    这是「强制单点登录」与「禁止密码登录」两码事，别一起关掉。
    """
    try:
        if not (session.get('my_phone') or '').strip():
            return False
        _src = (session.get('id_src') or '').strip()
        if _src == 'sso':
            return True
        if _src == 'pwd':
            # 🔴 V1.16.31：手机号 + 密码验证通过 = 强凭据，可信度与 SSO 同级。
            #   不认它的话，**走账号密码登录的管理员（含王平平自己）进不去用户管理**，
            #   只能去点「自选身份」—— 用户报障「王平平进不去」就是这个。
            return True
        if _src == 'pick' and not SSO_REQUIRE:
            return True
        # 🔴 故意**不**放行 'as'：那是 `/my?as=` 换来的身份，零凭据、
        #   地址栏里敲一下就能冒充别人，永远不算可信（V1.16.30）。
        return False
    except Exception:
        return False


def _is_safety_reviewer(phone=None, name=None) -> bool:
    """有没有审核权（违章处罚审批 / 亮点审核）：超管始终有；
    其余人看是否属于「总装二部/生产管理室」「总装二部/安全设备室」
    （名单在 safety_review.py，由船海智云组织架构接口按部门生成）。

    🔴 V1.16.27：超管身份要求**可信**（船海智云认证过）。
    原来只看 _current_role()=='super_admin'，免登录 + 自选身份声明成超管号
    就拿到了审核权 ⇒ 任何人都能审批处罚 / 过亮点。"""
    try:
        if _current_role() == 'super_admin' and _identity_trusted():
            return True
    except Exception:
        pass
    # 身份优先取「单点登录 / 自选身份」认到的那个人（my_phone/my_name），
    # 而不是登录态（免登录下 user_phone 是空的），否则审核权永远判不出来。
    if phone is None or not str(phone).strip():
        _p = _current_identity_phone()
    else:
        _p = str(phone).strip()
    if name is None:
        _n = (session.get('my_name') or '').strip()
        if not _n:
            try:
                _n = _current_actor_name()
            except Exception:
                _n = ''
    else:
        _n = name
    return safety_review.is_reviewer(_p or '', _n or '')


def _safety_approver_list() -> list:
    """亮点「审批人」选人名单（V1.17.0：与违章处罚一致，只列安全超管）。
    返回 [{name, phone, tag=部门简称}]，供 hl.html 的 APPR 控件用。"""
    out = []
    seen = set()

    def add(name, phone, dept=''):
        name = (name or '').strip()
        phone = (phone or '').strip()
        if not name or not phone or not re.match(r'^1[3-9]\d{9}$', phone):
            return
        if phone in seen:
            return
        seen.add(phone)
        out.append({'name': name, 'phone': phone,
                    'tag': '/'.join([x for x in (dept or '').split('/')[1:] if x])})

    # 固定超管（王平平）始终可选为审批人
    add(SUPER_ADMIN_NAME, SUPER_ADMIN_PHONE, '安全超管')
    # 安全超管名单（admin_grant：按部门/个人授权）
    try:
        for p in admin_grant.people():
            add(p.get('name'), p.get('phone'), p.get('dept'))
    except Exception:
        pass
    return out


def _reject_non_reviewer(approvers_json) -> str:
    """安全隐患看板的审批人必须是有审核权的人；返回第一个不合格者的姓名（'' = 全合格）"""
    try:
        arr = json.loads(approvers_json) if approvers_json else []
    except Exception:
        arr = []
    if not isinstance(arr, list):
        return ''
    for it in arr:
        if not isinstance(it, dict):
            continue
        if not safety_review.is_reviewer((it.get('phone') or '').strip(),
                                        (it.get('name') or '').strip()):
            return (it.get('name') or it.get('phone') or '该人员')
    return ''


def _safety_approver_items() -> list:
    """违章处罚「审批人」选人名单（V1.17.0）：只列安全超管 —— 固定超管 + 安全超管名单(admin_grant)。

    以前用 safety_review.picker_items()（审核人名单），而那份名单被配成「整个总装二部
    deep=true」= 4153 人，选审批人时等于人人能选。用户要求：审批人只能是审批人（安全超管）。
    返回结构与 safety_review.picker_items() 一致：{label, sublabel, value, name, phone}。"""
    out = []
    seen = set()

    def add(name, phone, dept=''):
        name = (name or '').strip()
        phone = (phone or '').strip()
        if not name or not phone or not re.match(r'^1[3-9]\d{9}$', phone):
            return
        if phone in seen:
            return
        seen.add(phone)
        out.append({
            'label': name,
            'sublabel': ((dept or '') + ' / ' + phone).strip(' /'),
            'value': (dept + ' - ' + name + ' - ' + phone) if dept else (name + ' - ' + phone),
            'name': name,
            'phone': phone,
        })

    # 固定超管（王平平）始终可被指定为审批人
    add(SUPER_ADMIN_NAME, SUPER_ADMIN_PHONE, '安全超管')
    # 安全超管名单（admin_grant：按部门/个人授权）
    try:
        for p in admin_grant.people():
            add(p.get('name'), p.get('phone'), p.get('dept'))
    except Exception:
        pass
    return out


def _reject_non_approver(approvers_json) -> str:
    """违章处罚的审批人必须是安全超管（固定超管 + admin_grant 名单）；返回第一个不合格者姓名（''=全合格）。

    V1.17.0：以前走 _reject_non_reviewer（审核人=safety_review 总装二部全员），谁都能当审批人。
    现在收窄到安全超管。"""
    try:
        arr = json.loads(approvers_json) if approvers_json else []
    except Exception:
        arr = []
    if not isinstance(arr, list):
        return ''
    for it in arr:
        if not isinstance(it, dict):
            continue
        ph = (it.get('phone') or '').strip()
        nm = (it.get('name') or '').strip()
        if ph == SUPER_ADMIN_PHONE:
            continue
        if not admin_grant.is_member(ph, nm):
            return (nm or ph or '该人员')
    return ''


def _can_manage_users_for_phone(phone, biz=None):
    """谁能管理账号操作（审核/改角色/删号/重置密码）：
    · 超管：任何账号
    · 安全上级（debug_sup）：🔴 **不再有**（V1.16.36 起只有超管）
    两个看板的用户互不相通，防止跨业务改到对方的人。

    🔴 V1.16.36（用户报障「把超管和普通管理分开，科室下面的人只有普通权限，
    超管才能有审批权」，并明确「名单授权也收归超管」）：
    原来下面有一段 debug_sup 兜底 —— 只要 session 角色是调试上级，
    就能审核账号 / 改角色 / 删号 / 重置密码 / 批换人申请。
    这一段是**审批权**，按新口径整条删除，现在账号管理只有超管能做。
    （被删掉的原逻辑留档：
        if role != 'debug_sup' or not _identity_trusted(): return False
        if biz == 'safety': return True
        u = db.get_user_by_phone(phone)
        return bool(u) and (u.get('biz') or 'debug') == 'safety'）
    """
    role = _current_role()
    # V1.16.16：超管身份按业务档判（王平平两个档都算）
    if _biz_admin(biz or 'debug'):
        return True
    _ = role
    return False


# ============ 管理员账号与会话配置 ============
# config.json 的 admin 节是「超级管理员」兜底账号（上线前务必修改！）
# 注册用户的账号/密码/权限存在数据库 users 表中。
# ============ V1.6 B1 取消登录开关 ============
# no_login = true  → 打开网页直接用，不用登录、不做身份识别；
#                    因为没有身份，管理入口（建看板/改默认人/用户管理/阈值…）对所有人开放。
#                    等船海智云「网页授权 + 可信域名」配好后，改回 false 并按 userid 认人即可。
# no_login_role  → 免登录时所有人按这个角色走（默认 super_admin = 全部放开）
NO_LOGIN = bool(CONFIG.get('no_login', False))
NO_LOGIN_ROLE = (CONFIG.get('no_login_role') or 'super_admin').strip()
NO_LOGIN_NAME = (CONFIG.get('no_login_name') or '').strip()

ADMIN_USER = CONFIG['admin']['username']
ADMIN_PASS = CONFIG['admin']['password']
REMEMBER_DAYS = int(CONFIG['admin'].get('remember_days', 7))

# secret_key：config.json 未配置时自动生成并写回，保证重启后已登录状态不失效
SECRET_KEY = CONFIG.get('secret_key') or ''
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    try:
        _cfg_path = os.path.join(BASE_DIR, 'config.json')
        with open(_cfg_path, 'r', encoding='utf-8') as _f:
            _cfg = json.load(_f)
        _cfg['secret_key'] = SECRET_KEY
        with open(_cfg_path, 'w', encoding='utf-8') as _f:
            json.dump(_cfg, _f, ensure_ascii=False, indent=2)
    except Exception:
        pass
app.secret_key = SECRET_KEY
# 勾选"记住我"后的登录有效期
app.permanent_session_lifetime = timedelta(days=REMEMBER_DAYS)

# ============ 登录防爆破（内网防"搞幺蛾子"） ============
# 同一「账号+IP」连续失败 LOGIN_MAX_FAILS 次，锁定 LOGIN_LOCK_MINUTES 分钟
# 同一 IP 对不同账号连续失败超过阈值（防批量撞库），同样锁定
LOGIN_MAX_FAILS = 5          # 连续失败 N 次锁定
LOGIN_LOCK_MINUTES = 15      # 锁定时长（分钟）
LOGIN_IP_MAX_FAILS = 15      # 同一 IP 总失败阈值（防遍历账号撞库）
_login_fails = {}            # key -> {'fails': int, 'lock_until': timestamp}
_login_lock = threading.Lock()


def _client_ip() -> str:
    """取真实客户端 IP（兼容反代场景）"""
    xf = request.headers.get('X-Forwarded-For', '')
    if xf:
        return xf.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def _login_locked(username: str, ip: str):
    """返回锁定剩余秒数（0 表示未锁定）"""
    now = time.time()
    with _login_lock:
        for key in (f"u:{username}|{ip}", f"ip:{ip}"):
            rec = _login_fails.get(key)
            if rec and rec.get('lock_until', 0) > now:
                return int(rec['lock_until'] - now)
        return 0


def _login_record_fail(username: str, ip: str):
    """记录一次登录失败，达到阈值则锁定"""
    now = time.time()
    with _login_lock:
        ukey = f"u:{username}|{ip}"
        rec = _login_fails.setdefault(ukey, {'fails': 0, 'lock_until': 0})
        rec['fails'] += 1
        if rec['fails'] >= LOGIN_MAX_FAILS:
            rec['lock_until'] = now + LOGIN_LOCK_MINUTES * 60
        ikey = f"ip:{ip}"
        irec = _login_fails.setdefault(ikey, {'fails': 0, 'lock_until': 0})
        irec['fails'] += 1
        if irec['fails'] >= LOGIN_IP_MAX_FAILS:
            irec['lock_until'] = now + LOGIN_LOCK_MINUTES * 60


def _login_clear_fail(username: str, ip: str):
    """登录成功后清除该账号+IP 的失败计数"""
    with _login_lock:
        _login_fails.pop(f"u:{username}|{ip}", None)


def _login_remaining(username: str, ip: str) -> int:
    """V1.5：该账号+IP 距锁定的剩余可试次数（>=1）；已锁定返回 0；无记录返回初始阈值"""
    now = time.time()
    with _login_lock:
        rec = _login_fails.get(f"u:{username}|{ip}")
        if rec:
            if rec.get('lock_until', 0) > now:
                return 0
            return max(0, LOGIN_MAX_FAILS - rec.get('fails', 0))
    return LOGIN_MAX_FAILS


# ============ V1.4.5 写操作防重复（60 秒幂等窗口，配合前端按钮禁用双保险） ============
# 同一「操作人 + 操作指纹」在 60 秒内重复提交 → 视为重复点击，直接拦截。
# 解决手机端"点了没反应再点一次"导致的重复派工/重复手尾/重复推送。
_IDEM_WINDOW = 60
_idem_cache = {}
_idem_lock = threading.Lock()


def _actor_key() -> str:
    """操作人标识：认到的人用手机号，否则用客户端 IP
    V1.16.22：原来用 _current_phone()，免登录下恒空 ⇒ 所有人共用同一个 IP 当操作人，
    60 秒防重复会误伤同一条记录的正常二次提交。"""
    ph = _current_identity_phone()
    return ph or _client_ip()


def _idem_fingerprint(*parts) -> str:
    raw = "|".join(str(x or "").strip() for x in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _idem_allow(key: str) -> bool:
    """同一 key 窗口期内首次调用返回 True 并计时，重复调用返回 False"""
    now = time.time()
    with _idem_lock:
        prev = _idem_cache.get(key)
        if prev is not None and now - prev < _IDEM_WINDOW:
            return False
        _idem_cache[key] = now
        if len(_idem_cache) > 1024:  # 顺带清理过期 key，防内存膨胀
            expired = [k for k, v in _idem_cache.items() if now - v >= _IDEM_WINDOW]
            for k in expired:
                _idem_cache.pop(k, None)
        return True


def _dup_blocked_html(redirect_url: str):
    """表单类重复提交的统一拦截提示（alert 后跳转到目标页，避免 history.back 死循环）"""
    return ("<script>alert('请勿重复提交：相同操作在 60 秒内已自动拦截。"
            "如刚才已提交成功，请查看刷新后的最新页面。');"
            "window.location.href='" + str(redirect_url or '/boards') + "';</script>")


def _dup_blocked_json():
    """AJAX 类重复提交的统一拦截返回"""
    return jsonify(ok=False, dup=True,
                   msg='请勿重复提交：相同操作在 60 秒内已自动拦截，系统不会重复派工/推送。如已成功请刷新页面查看。'), 409

# 操作日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(os.path.join(BASE_DIR, 'app.log'), encoding='utf-8')]
)
logger = logging.getLogger(__name__)

# 启动时确保数据库可用
db.init_db()

# V1.5.3：启动即保证"唯一超管"（王平平）——把其它 super_admin 降为业务负责人
try:
    enforce_single_super_admin()
except Exception as _e:      # 任何异常都不影响启动
    print(f"[警告] 超管唯一化检查跳过: {_e}")


@app.template_filter('extract_name')
def extract_name(val):
    if not val:
        return ""
    items = str(val).split(',')
    res = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split('-')]
        if len(parts) >= 6:
            res.append(parts[4])
        elif len(parts) >= 2:
            res.append(parts[-2])
        else:
            res.append(item)
    return ", ".join(res)


@app.template_filter('extract_area')
def extract_area(val):
    if not val:
        return ""
    items = str(val).split(',')
    res = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split(' - ')]
        # 台账格式：公司 - 一级 - 二级 - 作业区 - 姓名 - 电话
        if len(parts) >= 6:
            res.append(parts[3])
        elif len(parts) >= 2:
            # 船海智云格式：姓名 - 电话 → 尝试从人员台账查作业区
            phone = parts[-1].strip()
            staff = db.find_staff_by_phone(phone)
            if staff and staff.get('作业区'):
                res.append(staff['作业区'])
            else:
                # 查不到作业区，至少只显示姓名（不显示电话）
                res.append(parts[0])
        else:
            res.append(item)

    # 列表去重但保持原有顺序
    seen = set()
    return ", ".join([x for x in res if not (x in seen or seen.add(x))])


def _smart_extract_dept(val, level=2):
    """智能提取部门名称。level=2 取二级部门，level=1 取一级部门。
    与 extract_area 类似，2 段格式（船海智云）时从人员台账查部门。"""
    if not val:
        return ""
    items = str(val).split(',')
    res = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split(' - ')]
        if len(parts) >= 6:
            idx = 2 if level == 2 else 1
            dept = parts[idx] if len(parts) > idx else parts[1]
            if dept:
                res.append(dept)
        elif len(parts) >= 2:
            phone = parts[-1].strip()
            staff = db.find_staff_by_phone(phone)
            if staff:
                if level == 2 and staff.get('二级部门'):
                    res.append(staff['二级部门'])
                elif staff.get('一级部门'):
                    res.append(staff['一级部门'])
                else:
                    res.append('未知部门')
            else:
                res.append('未知部门')
        else:
            res.append('未知部门')
    seen = set()
    return ", ".join([x for x in res if not (x in seen or seen.add(x))])


def _normalize_staff_value(val):
    """归一化人员值：如果是 2 段格式（姓名 - 电话），尝试从人员台账补全为 6 段格式。
    台账已有则返回完整 6 段；查不到则原样返回（保持 2 段）。"""
    if not val:
        return val
    items = str(val).split(',')
    res = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split(' - ')]
        if len(parts) >= 6:
            res.append(item)  # 已是完整格式
        elif len(parts) >= 2:
            phone = parts[-1].strip()
            staff = db.find_staff_by_phone(phone)
            if staff:
                full = " - ".join([x for x in [
                    staff.get('公司', ''), staff.get('一级部门', ''),
                    staff.get('二级部门', ''), staff.get('作业区', ''),
                    staff.get('姓名', ''), staff.get('电话', '')] if x])
                res.append(full if full else item)
            else:
                res.append(item)  # 台账查不到，保持原样
        else:
            res.append(item)
    return ",".join(res)


def get_site_url() -> str:
    """看板访问地址：site_url 为 auto/空/127.0.0.1 时自动用本机局域网 IP（与 monitor.py 口径一致）。
    V1.3.2：UDP 探测失败（隔离网段无外网）时改用主机名解析，避免隔离服务器误回退 127.0.0.1。"""
    site = CONFIG.get("site_url", "http://127.0.0.1:5000")
    port = int(CONFIG.get("port", 5000))
    if site in ("auto", "") or site.startswith("http://127.0.0.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            ip = ""
        if not ip or ip.startswith("127."):
            try:
                ip = socket.gethostbyname(socket.gethostname())
            except Exception:
                ip = ""
        if not ip or ip.startswith("127."):
            ip = "127.0.0.1"
        return f"http://{ip}:{port}"
    return site


def detail_url(rid, ship_id=None) -> str:
    """记录详情页地址（推送正文 + 手机端点击链接都用它）。
    V1.5.3：安全业务（安全隐患看板）的记录带上 ?biz=safety——
    这样点进去一定落在安全隐患看板的语境里；即便这条记录后来被删了，
    提示页也知道该把你送回哪个看板（否则只能回船舶完整性看板）。
    调试业务的链接与以前完全一致（不带参数）。"""
    url = f"{get_site_url()}/detail/{int(rid)}"
    if ship_id is not None and ship_biz(ship_id) == 'safety':
        url += '?biz=safety'
    return url


def _extract_phones(*values) -> list:
    """从若干人员字符串（6 段台账 / 2 段姓名-电话，逗号分隔多人）中提取全部 11 位手机号（去重保序）"""
    phones = []
    for v in values:
        phones.extend(re.findall(r'1[3-9]\d{9}', str(v or '')))
    seen = set()
    return [p for p in phones if not (p in seen or seen.add(p))]


def _parse_extra_users(raw) -> str:
    """校验并规范化前端提交的追加抄送人 JSON（[{name, phone}, ...]）。
    合法：保留 name/phone 字段并序列化返回；非法/空：返回 ''。
    V1.2 需求2：抄送人参与所有推送级别，详情页可见。"""
    if not raw:
        return ''
    raw = str(raw).strip()
    try:
        arr = json.loads(raw)
        if not isinstance(arr, list):
            return ''
        cleaned = []
        for it in arr:
            if isinstance(it, dict):
                name = str(it.get('name') or '').strip()
                phone = str(it.get('phone') or '').strip()
                if phone and re.match(r'^1[3-9]\d{9}$', phone):
                    cleaned.append({'name': name, 'phone': phone})
        return json.dumps(cleaned, ensure_ascii=False)
    except Exception:
        return ''


def send_push(content: str, mobiles: list, webhook_url: str = "", mobile_click_url: str = "",
              rid: int = None):
    """统一推送入口（app 侧派工/反馈等即时推送用）：
    V1.4.1 起仅走「船海智云」按手机号推送（支持点击跳转），企微群兜底已彻底移除；
    匹配不到的号码、主通道异常均静默跳过，不再发企微群。
    webhook_url 参数仅作兼容保留（调用方仍传 ship.webhook），已不再使用。
    rid: V1.2 需求3 推送留痕——传入记录 id 时，把本次实际推送号码写入 notified_phones。"""
    import chz_push
    phones = list({p.strip() for p in (mobiles or []) if p and p.strip()})[:20]
    if not phones:
        return
    # V1.11：本地演示/联调安全阀 —— config.json 里 push_dry_run=true 时只记日志不真发，
    #        免得在本地造测试数据时把消息打到真人手机上。默认 false（与原来完全一致）。
    if CONFIG.get('push_dry_run'):
        logger.info('[推送-试运行] 不真发: %s -> %s' % ((content or '').split('\n')[0][:40], phones))
        return
    if not chz_push.is_enabled():
        return
    try:
        title = (content or "").strip().split("\n")[0][:60]
        chz_push.send_msg(title, content, phones, mobile_click_url=mobile_click_url)
    except Exception as e:
        logger.warning(f"船海智云推送失败: {e}（无企微群兜底，本批不发送）")
    finally:
        # 推送留痕：无论发送成功与否，都把本次目标号码记到记录上（留痕失败不影响主流程）
        if rid:
            try:
                db.append_notified_phones(rid, phones)
            except Exception as e:
                logger.warning(f"推送留痕失败(不影响发送): {e}")


# ============ V1.4 用户中心：责任链解析助手 ============

def _persons_from_staff_str(val, tag=''):
    """解析台账人员串（6段/2段，可多人逗号分隔）→ [{name, phone, tag}, ...]。
    名称取手机号前一段；无 11 位手机号的条目忽略（无法匹配本人则无意义）。"""
    out = []
    if not val:
        return out
    for item in str(val).split(','):
        item = item.strip()
        if not item:
            continue
        m = re.search(r'1[3-9]\d{9}', item)
        if not m:
            continue
        phone = m.group(0)
        name = ''
        parts = [p.strip() for p in item.split('-') if p.strip()]
        for i, p in enumerate(parts):
            if re.fullmatch(r'1[3-9]\d{9}', p):
                if i > 0:
                    name = parts[i - 1]
                break
        if not name:
            name = item.replace(phone, '').strip(' -') or phone
        out.append({'name': name, 'phone': phone, 'tag': tag})
    return out


def _person_list_from_json(raw, tag=''):
    """解析记录中的 JSON 责任人/抄送人字段 [{name, phone}, ...] → [{name, phone, tag}, ...]"""
    out = []
    try:
        arr = json.loads(raw or '[]')
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, dict):
                    phone = str(it.get('phone') or '').strip()
                    if re.fullmatch(r'1[3-9]\d{9}', phone):
                        out.append({'name': (it.get('name') or '').strip() or '责任人',
                                    'phone': phone, 'tag': tag})
    except Exception:
        pass
    return out


def _chain_people(rec, logs=None):
    """一条手尾的『责任链人员』（按角色顺序 + 手机号去重保序）：
    调试上级 → 调试责任人 → 责任主管 → 作业区/部门主管 → 派工链被派工人(含最新) → 追加责任人。
    催办时向这些人同时推送；用户中心"我是责任人"也按这些手机号自动匹配。"""
    if not rec:
        return []
    people = []
    seen = set()

    def _add(p):
        phone = p.get('phone') or ''
        if not phone or phone in seen:
            return
        seen.add(phone)
        people.append({'name': p.get('name') or phone, 'phone': phone, 'tag': p.get('tag') or ''})

    for p in _persons_from_staff_str(rec.get('debug_sup'), '调试上级'):
        _add(p)
    for src in (rec.get('debug_users'), rec.get('debug_phones')):
        for p in _persons_from_staff_str(src, '调试责任人'):
            _add(p)
    for p in _persons_from_staff_str(rec.get('install_user'), '责任主管'):
        _add(p)
    for p in _persons_from_staff_str(rec.get('area_leader'), '作业区主管'):
        _add(p)
    for p in _persons_from_staff_str(rec.get('dept_leader'), '部门长'):
        _add(p)
    for log in (logs or []):
        for p in _persons_from_staff_str(log.get('to_user'), '被派工人'):
            _add(p)
    # 兜底：老数据没写 dispatch_log 时的直接派工字段
    for p in _persons_from_staff_str(rec.get('dispatch_user'), '被派工人'):
        _add(p)
    for p in _person_list_from_json(rec.get('extra_responsible'), '追加责任人'):
        _add(p)
    return people


def _chain_summary(rec, logs=None):
    """责任链展示文案（列表页用）：调试上级/调试/安装 + 派工链 A→B→C + 追加责任人"""
    if not rec:
        return ''
    segs = []
    sup = extract_name(rec.get('debug_sup') or '')
    if sup:
        segs.append("调试上级:" + sup)
    dbg = extract_name(rec.get('debug_users') or '')
    if dbg:
        segs.append("调试:" + dbg)
    ins = extract_name(rec.get('install_user') or '')
    if ins:
        segs.append("安装:" + ins)
    nodes = []
    logs = logs or []
    if logs:
        f = (extract_name(logs[0].get('from_user') or '') or '').strip()
        if f:
            nodes.append(f)
        for log in logs:
            t = (extract_name(log.get('to_user') or '') or '').strip()
            if t:
                nodes.append(t)
    if nodes:
        segs.append("派工:" + "→".join(nodes))
    er_names = [p['name'] for p in _person_list_from_json(rec.get('extra_responsible'), '')]
    if er_names:
        segs.append("追加:" + "、".join(er_names))
    return " ｜ ".join(segs)


def _fmt_dispatch(logs):
    """派工记录 → 多行文本（每行一次派工：时间 甲→乙（备注））。导出 Excel 用。"""
    out = []
    for lg in (logs or []):
        f = (extract_name(lg.get('from_user') or '') or '').strip()
        t = (extract_name(lg.get('to_user') or '') or '').strip()
        note = (lg.get('note') or '').strip()
        ts = (lg.get('created_at') or '').strip()
        if not (f or t):
            continue
        line = (ts + ' ' if ts else '') + (f + '→' + t if f else t)
        if note:
            line += '（' + note + '）'
        out.append(line)
    return '\n'.join(out)


def _fmt_urge(logs):
    """催办记录 → 多行文本（按时间正序：时间 催办人 催办了 谁谁谁）。导出 Excel 用。"""
    out = []
    for lg in reversed(list(logs or [])):      # db 取出来是倒序，这里翻正
        ts = (lg.get('created_at') or '').strip()
        who = (lg.get('actor_name') or '').strip()
        targets = (lg.get('target_names') or '').strip()
        if not (who or targets):
            continue
        out.append((ts + ' ' if ts else '') + (who or '') + ' 催办：' + targets)
    return '\n'.join(out)


def _work_hours_since(submit_time_val):
    """计算发起至今的工作日小时数（口径同 monitor.py：跳过周末/法定节假日，调休补班计入）。
    无法解析时间时返回 None。"""
    now = datetime.now()
    sub_time = now
    if isinstance(submit_time_val, datetime):
        sub_time = submit_time_val
    elif submit_time_val:
        try:
            sub_time = datetime.strptime(str(submit_time_val).strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                sub_time = datetime.strptime(str(submit_time_val).strip(), "%d/%m/%Y %H:%M")
            except ValueError:
                return None
    return holidays.work_hours_between(sub_time, now)


def _parse_dt_any(val):
    """宽松解析时间字符串（整改截止时间存的是 %Y-%m-%d %H:%M:%S）。解析不了返回 None。"""
    v = str(val or '').strip().replace('T', ' ')
    if not v:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(v, fmt)
        except Exception:
            continue
    return None


def compute_display_status(status: str, submit_time_val, ship_id=None, deadline=None) -> str:
    """根据状态和发起时间计算展示状态（阈值取超管可配置的 l1/l2，V1.3 需求4）。
    V1.5.3：支持按产品船单独配置——传入 ship_id 时优先用该船自己的阈值。
    计时口径为工作日小时：自动跳过周六日与法定节假日，调休补班日照常计时
    （口径与 monitor.py 巡检提醒完全一致，见 holidays.py / holidays.json）。
    注意：文案随阈值动态变化，统计口径一律用 _work_hours_since 的数字，勿依赖字符串。"""
    _safety = (ship_biz(ship_id) == 'safety')
    if status in ("已闭环", "已完成"):
        # V1.5.3：安全业务用「已整改」（与"未整改"配成一对）
        return '已整改' if _safety else "已闭环"
    # V1.16.5（用户口径）：安全档状态**只有两种**——超时 / 未超时（+ 已整改），
    #   不再按 24/48 小时分级。
    #   超时 = 现在 > 发起时手填的「整改截止时间」；没填截止时间的算未超时
    #   （与 monitor 的提醒口径一致：没填截止时间就不提醒）。
    if _safety:
        _dl = _parse_dt_any(deadline)
        return '超时' if (_dl and datetime.now() > _dl) else '未超时'
    th = get_thresholds_for(ship_id)
    l1, l2 = th["l1"], th["l2"]
    diff_hours = _work_hours_since(submit_time_val)
    if diff_hours is None:
        return "未闭环"
    _word = '未整改' if ship_biz(ship_id) == 'safety' else '未闭环'
    if diff_hours < l1:
        return f"{_word}（小于{l1}小时）"
    elif diff_hours <= l2:
        return f"{_word}（{l1}-{l2}小时）"
    else:
        return f"{_word}（大于{l2}小时）"


# 面向用户的**纯文本**文案（JSON msg / alert / 推送到手机的推送正文）按业务档换词。
# 这些字符串不经过模板，模板映射管不到它们，必须单独处理——否则安全业务的人会收到
# "收尾超时未闭环"这种调试口径的通知。
SAFETY_MSG_WORDS = [
    # ⚠️ 顺序敏感：长词必须排在短词之前（"系统名称"先于"系统名"，
    #   "调试上级责任人"先于"调试上级"），否则会被短词先吃掉留下病句。
    ('异常手尾照片', '隐患照片'),
    # 紧接上一条：整句替换，避免"手尾的系统名称"被拆成"隐患的隐患类别"
    ('手尾的系统名称', '记录的隐患类别'),
    ('总手尾', '总数'),
    ('系统名称', '隐患类别'),
    ('系统清单', '隐患类别'),
    ('系统名', '隐患类别'),
    ('系统：', '隐患类别：'),
    ('手尾', '隐患'),
    ('收尾', '隐患'),
    ('未闭环', '未整改'),
    ('已闭环', '已整改'),
    ('闭环', '整改'),
    ('调试业务', '安全业务'),
    ('调试:', '安全负责人:'),
    ('调试：', '安全负责人：'),
    ('调试上级责任人', '安全上级'),
    ('调试上级', '安全上级'),
    ('调试责任人', '安全负责人'),
    ('调试负责人', '安全负责人'),
    ('超级管理员', '超管'),
]


def biz_text(text, biz='debug'):
    """把提示/推送文案里的调试口径词换成安全口径（仅 biz='safety' 生效）。"""
    if biz != 'safety' or not text:
        return text
    for a, b in SAFETY_MSG_WORDS:
        text = text.replace(a, b)
    return text


def rec_text(text, ship_id):
    """按记录所属看板的业务档换词（最常用：拿记录手上的 ship_id 直接调）。"""
    return biz_text(text, ship_biz(ship_id))


# ============ V1.5.3 业务档（biz）：一套模板服务多个业务看板 ============
# 'debug'  → 船舶完整性看板（调试业务，原程序，/boards、/board/<id>）
# 'safety' → 安全隐患看板（安全业务，/safety）：结构与调试看板一致，
#            但去掉"选择系统"这一块，术语按安全口径（手尾→隐患、未闭环→未整改…）
#
# 实现方式：safety 渲染时对【模板源码】做术语映射，而不是复制一套模板——
# 好处是以后改界面只改一份模板，两块看板同时生效；映射只作用于模板里的
# 界面文字，不会碰到数据库里的任何内容（记录描述等原样显示）。
SAFETY_SHIP_NAME = '安全隐患看板'
# 安全隐患看板的"隐患类别"（首次使用自动灌入，安全上级/超管可自行增改）
# V1.6 A4：安全隐患看板默认只灌这几类（用户指定），其余类别由安全员自行增删
SAFETY_CATEGORIES = [
    '有限空间', '密闭舱室', '动火作业', '涂装油漆', '高空作业',
    '触电安全', '机械伤害', '起重伤害', '厂内交通',
]
# 安全业务的角色口径：只有 超管 / 安全上级 / 安全员 三类
SAFETY_ROLE_LABELS = {
    'super_admin': '超管',
    'debug_sup': '安全上级',
    'debug_user': '安全员',
}


def ensure_safety_categories(ship_id):
    """安全隐患看板首次使用时，把默认隐患类别灌进去（已有类别则不动）"""
    try:
        if db.get_ship_systems(ship_id):
            return
        db.import_ship_systems(ship_id, SAFETY_CATEGORIES)
        logger.info(f"安全隐患看板 #{ship_id} 已灌入 {len(SAFETY_CATEGORIES)} 个默认隐患类别（V1.6 精简版）")
    except Exception as e:
        logger.warning(f"灌入默认隐患类别失败: {e}")


def get_thresholds_for(ship_id=None, group=None):
    """按业务档取超时阈值：安全看板走 safety_thresholds，调试看板走 thresholds。
    两块看板的超时提醒配置从此完全独立，互不影响。"""
    if group is None:
        group = ship_biz(ship_id) if ship_id else 'debug'
    return get_thresholds(ship_id, group=group)
SAFETY_LABEL_MAP = [
    # ⚠️ 顺序极其重要：**先列头/筛选这类"要显示成安全负责人"的固定写法，
    #   再放"调试责任人→安全员"这种通用角色替换**，否则列头会被角色规则先吃掉。
    # 1) 照片列（必须先整体换，否则"异常手尾照片"会变成"异常隐患照片"）
    ('异常手尾照片', '隐患照片'),
    # 2) 列头 / 筛选 / 单元格标签：显示为"安全负责人 / 安全上级"
    ('>调试责任人<', '>安全负责人<'),
    ('>调试上级责任人<', '>安全上级<'),
    ('data-label="调试责任人"', 'data-label="安全负责人"'),
    ('data-label="调试上级责任人"', 'data-label="安全上级"'),
    ('调试责任人筛选', '安全负责人筛选'),
    ('调试上级筛选', '安全上级筛选'),
    ('调试负责人筛选', '安全负责人筛选'),
    # 3) 业务名词
    ('总手尾', '总数'),
    ('手尾', '隐患'),
    # 「闭环」全族换「整改」：⚠️ 必须先换"未闭环"（否则会被下一条切成"未整改"以外的怪词），
    # 再换"已闭环"，最后兜底把剩下的"闭环"（闭环时间/确认闭环/闭环率…）统一换掉。
    ('未闭环', '未整改'),
    ('已闭环', '已整改'),
    ('闭环', '整改'),
    # 4) 角色口径：安全业务只有 超管 / 安全上级 / 安全员
    ('调试上级责任人', '安全上级'),
    ('调试责任人', '安全员'),
    ('调试负责人', '安全负责人'),
    ('调试上级', '安全上级'),
    ('超级管理员', '超管'),
    # 5) 看板名与"选择系统 → 选择隐患类别"
    # V1.16.5：管理卡片里新加的入口 —— 必须先于其它"系统清单"规则，否则换不出来
    ('系统清单维护', '隐患类别维护'),
    ('船舶完整性管理看板', '安全隐患看板'),
    ('船舶完整性看板', '安全隐患看板'),
    ('我的 - 船舶完整性管理</title>', '我的隐患 - 安全隐患看板</title>'),
    ('船舶完整性管理', '安全管理'),
    ('系统名称筛选', '隐患类别筛选'),
    ('系统名称', '隐患类别'),
    # 「系统」这一族**不能一条通用规则搞定**：同一个词在不同句子里的安全叫法不同，
    # 按上下文逐条精确替换，避免出现"与隐患类别维护将被一并删除"这类病句。⚠️ 顺序从上到下生效。
    ('超级管理员可新建产品船看板（命名 + 船号 + 系统清单 Excel）',
     '超管与安全上级可新建安全隐患看板（填船号即可，名称自动生成）'),
    ('与系统清单将被一并删除', '与隐患类别将被一并删除'),
    ('、系统清单、', '、隐患类别、'),
    (' 系统清单</a>', ' 隐患类别维护</a>'),
    ('>系统清单</h4>', '>隐患类别维护</h4>'),
    ('系统清单 - ', '隐患类别 - '),
    ('系统清单 Excel', '隐患类别维护 Excel'),
    ('全部系统清单', '全部隐患类别'),
    ('还没有系统清单', '还没有隐患类别'),
    ('选择系统', '选择隐患类别'),
    ('新系统名', '新隐患类别'),
    ('系统名', '隐患类别'),
    ('收尾', '隐患'),
    # 6) 页面内跳转：安全业务只在 /safety 里转，不跳回调试看板
    ('href="/boards"', 'href="/safety"'),
    ("href = '/boards'", "href = '/safety'"),
    ('href="/board/{{ rec.ship_id or 1 }}"', 'href="/safety"'),
    # 7) 隐患类别维护页的措辞
    (' 个系统。', ' 种隐患。'),
    ('追加新增系统', '追加新增隐患类别'),
    ('每行一个系统。', '每行一个隐患类别。'),
    ('手动新增单个系统', '手动新增单个隐患类别'),
    ('安全上级负责人', '安全上级'),
    # 8) 角色显示走安全口径；页面业务档标记（登录/注册要带上它，两套用户不互通）
    ("fetch('/api/session')", "fetch('/api/session?biz=safety')"),
    ('data-biz="debug"', 'data-biz="safety"'),
    ('name="biz" value="debug"', 'name="biz" value="safety"'),
    ('href="/my"', 'href="/my?biz=safety"'),
    ('href="/syslists"', 'href="/syslists?biz=safety"'),
    ('href="/admin/thresholds"', 'href="/safety/thresholds"'),
    ('href="/admin/login_logs"', 'href="/admin/login_logs?biz=safety"'),
    ('href="/admin/users"', 'href="/admin/users?biz=safety"'),
    ('<a href="/" class="btn btn-outline-secondary btn-sm">返回看板</a>',
     '<a href="/safety" class="btn btn-outline-secondary btn-sm">返回看板</a>'),
    ('href="/board/{{ s.id }}"', 'href="/safety/board/{{ s.id }}"'),
]
_BIZ_TEMPLATE_CACHE = {}


def render_biz(template_name, biz='debug', **ctx):
    """按业务档渲染模板。safety 时把模板源码里的界面文字换成安全口径。"""
    ctx['IS_SAFETY'] = (biz == 'safety')
    ctx['BIZ'] = biz
    # V1.16：有审核权的人也能配看板默认责任人（用户口径）
    try:
        ctx['can_review'] = _is_safety_reviewer() if biz == 'safety' else False
    except Exception:
        ctx['can_review'] = False
    # safety 档走的是 jinja_env.from_string + tpl.render()，**绕过了 Flask 的
    # context_processor**，所以 no_login 之类的全局变量必须在这里显式补上，
    # 否则「免登录」相关的判断在安全档下会静默变成未定义（按钮整块不显示）。
    ctx.setdefault('no_login', NO_LOGIN)
    # V1.37：责任单位名单同源（safety 档绕过 context_processor，必须在这里补）
    ctx.setdefault('area_units', _AREA_UNITS)
    # V1.46：类别配置同源注入（safety 档绕过 context_processor，两处缺一不可）
    ctx.setdefault('BOARD_CATS', board_categories.all_cats())
    ctx.setdefault('CAT_BEHAVIORS', board_categories.BEHAVIOR_LABELS)
    ctx.setdefault('CAT_GROUP_LABELS', board_categories.group_labels())
    ctx.setdefault('INIT_CAT', request.args.get('cat') or 'all')
    # 模板里用 {{ biz }} 判断业务档（如用户管理页的业务徽章）。
    # 用 setdefault：调用方若已显式传 biz 则以调用方为准，也不会再报重复传参。
    ctx.setdefault('biz', biz)
    if biz != 'safety':
        return render_template(template_name, **ctx)
    tpl = _BIZ_TEMPLATE_CACHE.get(template_name)
    if tpl is None:
        path = os.path.join(app.template_folder, template_name)
        with open(path, 'r', encoding='utf-8') as f:
            src = f.read()
        for a, b in SAFETY_LABEL_MAP:
            src = src.replace(a, b)
        tpl = app.jinja_env.from_string(src)
        _BIZ_TEMPLATE_CACHE[template_name] = tpl
    return tpl.render(**ctx)


_SHIP_BIZ_CACHE = {}


def ship_biz(ship_id):
    """看某块看板属于哪个业务档；查不到按 debug 处理（老数据全部是调试看板）。
    结果缓存（看板的业务档建成后不会变），避免列表渲染时逐条查库。"""
    try:
        sid = int(ship_id)
    except (TypeError, ValueError):
        return 'debug'
    if sid in _SHIP_BIZ_CACHE:
        return _SHIP_BIZ_CACHE[sid]
    try:
        ship = db.get_ship(sid)
    except Exception:
        ship = None
    biz = ((ship or {}).get('biz') or 'debug')
    if ship:
        _SHIP_BIZ_CACHE[sid] = biz
    return biz


# ============ V1.5.3 业务门户（首页） ============
# 业务板块（V1.16.25 共 4 个：安全业务 / 调试业务 / 管理业务 / 工艺业务）。
# 以后要增删入口，只改下面这个列表即可（url 填站内路径）。
# icon 可选值（见 templates/portal.html 里的 ICONS）：board / mine / list / clock /
# users / shield / gear / chart / doc / lock / plus
# disabled=True 表示"待接入"占位（灰色、点不动）。
# 注意：门户上只放"业务入口"。原有程序里的功能页（我的手尾 / 系统清单 / 超时提醒设置 /
# 用户管理 / 登录日志 / 统计报表）全部收进「船舶完整性管理看板」内部，不在这里单独暴露；
# 登录状态、用户信息也只在进入看板后才显示。
PORTAL_TITLE = '总装船舶综合业务平台'
PORTAL_SUBTITLE = ''
PORTAL_FOOTER = ''
# V1.16.18（用户要求）：安全业务板块排在调试业务**上面**
PORTAL_SECTIONS = [
    # 🔴 V1.16.30（用户要求）：「在线管理 / 换人审批」**并进「用户管理」**，做成标签页。
    #   门户里不再单独挂这两个入口（用户反馈点进去"打不开"），改成挂一个「用户管理」，
    #   标签页用 ?tab= 指定 —— 各业务档各看各的，跟之前按档分开是一致的。
    {
        'title': '安全业务',
        'desc': '',
        'items': [
            {'label': '安全隐患看板', 'url': '/safety', 'icon': 'shield'},
            {'label': '数据总览', 'url': '/overview?biz=safety', 'icon': 'chart'},
            # 🔴 V1.46b（用户要求）：设备不再单独占一个门户入口。
            #   它是安全看板 /safety 里的一个类别，与「产品船」「场地」并列，
            #   类别由超管在「类别维护」里配置，老链接 /safety/devices 自动跳回列表并选中它。
            {'label': '产品船 6S 评分', 'url': '/sixs?biz=safety', 'icon': 'doc'},
            {'label': '亮点展示', 'url': '/hl?biz=safety', 'icon': 'star'},
            {'label': '用户管理', 'url': '/admin/users?biz=safety', 'icon': 'users'},
        ],
    },
    {
        'title': '调试业务',
        'desc': '',
        'items': [
            {'label': '船舶完整性管理看板', 'url': '/boards', 'icon': 'board'},
            {'label': '总体视图', 'url': '/overview?biz=debug', 'icon': 'chart'},
            {'label': '用户管理', 'url': '/admin/users?biz=debug', 'icon': 'users'},
        ],
    },
    # V1.16.25（用户要求）：调试业务**下面**加一个「管理业务」板块。
    #   位置：调试业务之后、工艺业务之前。
    #   items 为空数组时 portal.html 只渲染板块标题、不渲染任何图标入口
    #   （模板里 `{% for it in sec['items'] %}` 空转），所以留空不会有空格子或占位按钮。
    # V1.16.29：「在线管理 / 换人审批」已移回各业务档，这里重新留空给管理室用。
    {
        'title': '管理业务',
        'desc': '',
        'items': [],
    },
    {
        'title': '工艺业务',
        'desc': '',
        'items': [],
    },
]


@app.route('/')
def index():
    """V1.5.3：根路径改为业务门户——先看到各业务板块，再点进具体系统。
    原程序（船舶完整性管理看板）挂在「调试业务」板块下，路径仍是 /boards。"""
    return portal()


@app.route('/portal')
def portal():
    """业务门户首页（各业务板块 + 图标入口）。
    注意：门户刻意不显示登录人信息——登录状态与"姓名 · 角色"只在进入
    「船舶完整性管理看板」之后才出现（用户 2026-09-21 明确要求）。"""
    return render_template('portal.html',
                           sections=PORTAL_SECTIONS,
                           portal_title=PORTAL_TITLE,
                           portal_subtitle=PORTAL_SUBTITLE,
                           portal_footer=PORTAL_FOOTER)


@app.route('/overview')
def overview():
    """V1.48 综合数据总览（只读，零改表、零改接口）。

    安全档：全部看板（产品船 / 场地 / 设备，V1.63 起不再只圈产品船），
            6S 只对产品船评分且只给诚实守信指标（评分制，不造「问题数/已整改/待处理」）。
    调试档：全部看板一起汇总，不按类别归类（用户 2026-11-09 口径）。

    刻意用 render_template 而不是 render_biz：术语由 ship_overview.labels()
    按业务档给出，不经过 SAFETY_LABEL_MAP 整页字符串替换，
    避免「隐患的隐患类别」这类病句，也不依赖两处全局变量注入。
    """
    biz = (request.args.get('biz') or '').strip()
    if biz not in ('safety', 'debug'):
        return redirect('/overview?biz=safety')
    data = ship_overview.build(biz)
    return render_template('overview.html', D=data, L=data['L'], biz=biz,
                           IS_SAFETY=(biz == 'safety'),
                           user_name=session.get('user_name', ''))


@app.route('/overview/export.xlsx')
def overview_export_xlsx():
    """V1.48 阶段 2：总览导出真 .xlsx（汇总 / 明细 / 6S 三个 Sheet）。

    纯读：复用 ship_overview.build()，不写库、不改表、不加字段。
    明细按前端当前筛选条件过滤，口径与页面 applyDetail() 一致；
    导出取全量明细（不受页面 500 条上限约束），描述不截断。
    """
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    biz = (request.args.get('biz') or '').strip()
    if biz not in ('safety', 'debug'):
        return redirect('/overview?biz=safety')

    D = ship_overview.build(biz, detail_limit=None)
    L = D['L']
    ship_f = (request.args.get('ship') or '').strip()
    cat_f = (request.args.get('cat') or '').strip()
    state_f = (request.args.get('state') or '').strip()
    kw = (request.args.get('kw') or '').strip().lower()
    pdim = (request.args.get('pivot_dim') or '').strip()
    pval = (request.args.get('pivot_val') or '').strip()

    def _match(d):
        if state_f and d.get('state') != state_f:
            return False
        if pdim in ('cat', 'state', 'unit', 'month') and pval:
            if (d.get(pdim) or '未指定') != pval:
                return False
        if ship_f and d.get('ship') != ship_f:
            return False
        if cat_f and d.get('cat') != cat_f:
            return False
        if kw and kw not in ((d.get('desc_full') or '') + ' ' +
                             (d.get('no') or '') + ' ' +
                             (d.get('cat') or '')).lower():
            return False
        return True

    matched = [d for d in D['details'] if _match(d)]

    wb = Workbook()
    head_font = Font(bold=True, color='FFFFFF')
    head_fill = PatternFill('solid', fgColor='1D4ED8')

    def _style(ws, widths):
        for c in ws[1]:
            c.font = head_font
            c.fill = head_fill
            c.alignment = Alignment(horizontal='center', vertical='center')
        ws.freeze_panes = 'A2'
        ws.row_dimensions[1].height = 22
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        if ws.max_row > 1:
            ws.auto_filter.ref = 'A1:%s%d' % (get_column_letter(len(widths)), ws.max_row)

    # Sheet 1：跨船汇总 + 合计
    ws = wb.active
    ws.title = '汇总'
    head = [L['scope_short'], L['item'] + '总数', L['done'], L['open'], L['rate']]
    if D['sixs']:
        head.append('6S 得分')
    ws.append(head)
    for r in D['rows']:
        row = [r['name'], r['total'], r['closed'], r['open'], r['rate'] / 100.0]
        if D['sixs']:
            row.append(r.get('sixs'))
        ws.append(row)
    n_total = sum(r['total'] for r in D['rows'])
    n_closed = sum(r['closed'] for r in D['rows'])
    rate = round(n_closed * 100.0 / n_total, 1) if n_total else 0
    total_row = ['合计', n_total, n_closed, n_total - n_closed, rate / 100.0]
    if D['sixs']:
        total_row.append('')
    ws.append(total_row)
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for row in ws.iter_rows(min_row=2, min_col=5, max_col=5):
        for c in row:
            c.number_format = '0.0%'
    _style(ws, [26, 12, 12, 12, 12] + ([10] if D['sixs'] else []))

    # Sheet 2：明细（全量 + 当前筛选）
    wsd = wb.create_sheet('明细')
    wsd.append([L['scope_short'], L['cat'], L['unit'], '状态', '日期', '描述'])
    for d in matched:
        wsd.append([d['ship'], d['cat'], d['unit'], d['state'], d['date'],
                    d.get('desc_full') or d['desc']])
    _style(wsd, [26, 18, 18, 12, 14, 60])

    # Sheet 3：6S 诚实守信指标（仅安全档）
    if D['sixs']:
        sx = D['sixs']
        wss = wb.create_sheet('6S 评分')
        wss.append(['指标', '数值'])
        wss.append(['参评船数', sx['scored_ships']])
        wss.append(['平均分', sx['avg_score']])
        wss.append(['最低分', sx['min_score']])
        wss.append(['扣分条目数', sx['deduct_items']])
        wss.append([])
        wss.append(['高频扣分项', '次数', '大项', '标准内容'])
        for it in sx['top_items']:
            wss.append([it['code'], it['count'], it['group'], it['text']])
        wss.append([])
        wss.append(['口径', '6S 为一船一份评分，不含「问题总数 / 已整改 / 待处理」'])
        wss.freeze_panes = 'A2'
        wss.column_dimensions['A'].width = 20
        wss.column_dimensions['B'].width = 12
        wss.column_dimensions['C'].width = 18
        wss.column_dimensions['D'].width = 52
        for c in wss[1]:
            c.font = head_font
            c.fill = head_fill

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    fname = '%s_综合数据总览_%s.xlsx' % (L['biz_name'], datetime.now().strftime('%Y%m%d_%H%M'))
    logger.info('导出总览 Excel - biz=%s, 明细 %d/%d 条' %
                (biz, len(matched), len(D['details'])))
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.context_processor
def _inject_no_login():
    """V1.6 B1：所有模板都能拿到 no_login 标志，用来隐藏登录/注册/退出这些入口
       V1.16.8：顺带把「记住我」的天数发给模板（原来写死 7 天，改配置不生效）"""
    return dict(no_login=NO_LOGIN, REMEMBER_DAYS=REMEMBER_DAYS, area_units=_AREA_UNITS,
                BOARD_CATS=board_categories.all_cats(),
                CAT_BEHAVIORS=board_categories.BEHAVIOR_LABELS,
                CAT_GROUP_LABELS=board_categories.group_labels(),
                INIT_CAT=(request.args.get('cat') or 'all'))


def _ships_with_counts(biz):
    """V1.6：给看板列表补「未闭环 / 已闭环」计数（界面按 demo 的卡片样式显示）"""
    ships = db.get_ships(biz)
    for s in ships:
        try:
            recs = db.get_records_by_ship(int(s['id']))
        except Exception:
            recs = []
        op = cl = 0
        for r in recs:
            if (r.get('status') or '') in ('已闭环', '已完成'):
                cl += 1
            else:
                op += 1
        s['open'] = op
        s['closed'] = cl
        s['total'] = op + cl
        # V1.46：给模板备好类别 key（筛选用）与 behavior（显示船号用）
        s['_catkey'] = board_categories.key_of(s)
        s['_behavior'] = _board_cat(s)
    # V1.17.1：场地看板永远排产品船前面；V1.42：设备看板排最后（各按 id 升序）
    _cat_order = {'place': 0, 'ship': 1, 'device': 2}
    ships.sort(key=lambda s: (_cat_order.get(_board_cat(s), 1), int(s['id'])))
    return ships


@app.route('/boards')
def boards():
    """看板列表页：所有产品船船舶完整性看板"""
    return render_template('boards.html', ships=_ships_with_counts('debug'),
                           staff_list=db.get_staff_list(),
                           user_role=session.get('user_role', ''),
                           user_name=session.get('user_name', ''),
                           user_phone=session.get('user_phone', ''),
                           can_delete=_can_manage_biz('debug'))


# ============ A6 产品船 6S 评分（独立于看板，固定 84 条标准） ============
SIXS_BASE = 100


# ========== V1.51：产品船 6S 按「周一 ~ 周日」每周打分 ==========
def _sixs_week_start(value=None):
    """把日期/时间字符串归一到所在周的周一(YYYY-MM-DD)；空值＝本周周一。
    非法输入一律回退本周，不让页面 500。"""
    try:
        d = datetime.strptime(str(value).strip()[:10], '%Y-%m-%d') if value else datetime.now()
    except Exception:
        d = datetime.now()
    return (d - timedelta(days=d.weekday())).strftime('%Y-%m-%d')


def _sixs_week_of(row):
    """这条评分属于哪一周：新记录用 week_start；历史记录从 ctime 派生（零回填）。"""
    ws = ((row or {}).get('week_start') or '').strip()
    if ws:
        try:
            return datetime.strptime(ws[:10], '%Y-%m-%d').strftime('%Y-%m-%d')
        except Exception:
            pass
    ct = ((row or {}).get('ctime') or '').strip()
    return _sixs_week_start(ct) if ct else _sixs_week_start()


def _sixs_week_label(ws):
    """'11月2日 ~ 11月8日'；跨年时左侧补年份。"""
    try:
        a = datetime.strptime(ws, '%Y-%m-%d')
        b = a + timedelta(days=6)
        left = ('%d年%d月%d日' % (a.year, a.month, a.day)) if a.year != b.year \
            else ('%d月%d日' % (a.month, a.day))
        return '%s ~ %d月%d日' % (left, b.month, b.day)
    except Exception:
        return str(ws or '')


def _sixs_week_options(rows, span=10):
    """下拉：有评分记录的周 + 以本周为起点的前 span 周，去重倒序。"""
    ws = set()
    for r in rows:
        w = _sixs_week_of(r)
        if w:
            ws.add(w)
    this = _sixs_week_start()
    for i in range(span):
        ws.add((datetime.strptime(this, '%Y-%m-%d') - timedelta(days=7 * i)).strftime('%Y-%m-%d'))
    opts = []
    for w in sorted(ws, reverse=True):
        opts.append({'value': w,
                     'label': _sixs_week_label(w) + ('（本周）' if w == this else ''),
                     'count': sum(1 for r in rows if _sixs_week_of(r) == w)})
    return opts


def _sixs_back_qs(biz, week, dup=None):
    """列表页返回链接的查询串（biz / week / dup 三个参数）。"""
    parts = []
    if biz == 'safety':
        parts.append('biz=safety')
    if week:
        parts.append('week=' + week)
    if dup:
        parts.append('dup=' + urllib.parse.quote(str(dup)))
    return ('?' + '&'.join(parts)) if parts else ''


def _sixs_ph():
    """SQL 参数占位符：MySQL 用 %s，SQLite 用 ?"""
    return "%s" if db.resolve_mode() == "mysql" else "?"


@app.route('/sixs')
def sixs_index():
    """6S 评分首页：按「周一 ~ 周日」查看某一周的产品船评分。
    V1.51：一船一周一份；week=该周周一(YYYY-MM-DD)，week=all 看全部，空＝本周。"""
    conn = db.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM sixs ORDER BY id DESC")
        all_rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        all_rows = []
    finally:
        conn.close()
    biz = (request.args.get('biz') or 'debug').strip()
    if biz not in ('debug', 'safety'):
        biz = 'debug'
    week_arg = (request.args.get('week') or '').strip()
    show_all = (week_arg == 'all')
    week_start = _sixs_week_start(None if show_all else week_arg)
    _w = datetime.strptime(week_start, '%Y-%m-%d')
    prev_week = (_w - timedelta(days=7)).strftime('%Y-%m-%d')
    next_week = (_w + timedelta(days=7)).strftime('%Y-%m-%d')
    this_week = _sixs_week_start()
    for r in all_rows:
        r['week_start'] = _sixs_week_of(r)
        r['week_label'] = _sixs_week_label(r['week_start'])
    rows = all_rows if show_all else [r for r in all_rows if r['week_start'] == week_start]
    nos = []
    for s in db.get_ships(biz):
        n = (s.get('ship_no') or '').strip()
        if n and n not in nos:
            nos.append(n)
    # V1.16.7：每条记录算好「我能不能删」（本人 or 固定超管）；dup= 表示刚被「已存在」拦回来
    for r in rows:
        try:
            r['can_del'] = _sixs_can_del(r)
        except Exception:
            r['can_del'] = False
    scores = []
    for r in rows:
        try:
            scores.append(float(r.get('score') or 0))
        except (TypeError, ValueError):
            pass
    week_stats = {'ships': len(rows),
                  'avg': round(sum(scores) / len(scores), 1) if scores else 0,
                  'min': int(min(scores)) if scores else 0,
                  'has': bool(scores)}
    dup = (request.args.get('dup') or '').strip()
    return render_template('sixs.html', rows=rows, ship_nos=nos, biz=biz,
                           can_score=_can_score_sixs(), dup=dup,
                           n_items=sixs_count(), base=SIXS_BASE,
                           week_start=week_start, week_label=_sixs_week_label(week_start),
                           prev_week=prev_week, next_week=next_week,
                           this_week=this_week, show_all=show_all,
                           week_options=_sixs_week_options(all_rows),
                           week_stats=week_stats,
                           user_role=session.get('user_role', ''),
                           user_name=session.get('user_name', ''))


@app.route('/sixs/new')
def sixs_new():
    """打分页：按十大项目列出全部检查项，点一条即记「不符合扣 1 分」
    V1.51：同一条船【同一周】只保留一份评分；可指定 week 补录前几周。"""
    if not _can_score_sixs():
        return _permission_denied()
    ship_no = (request.args.get('ship_no') or '').strip()
    biz = (request.args.get('biz') or 'debug').strip()
    if biz not in ('debug', 'safety'):
        biz = 'debug'
    week_start = _sixs_week_start(request.args.get('week'))
    if not ship_no:
        return redirect('/sixs' + _sixs_back_qs(biz, week_start))
    # V1.51：同一船同一周已有评分 → 回列表提示；不同周互不影响的
    if _sixs_row_of_ship(ship_no, week_start):
        return redirect('/sixs' + _sixs_back_qs(biz, week_start, dup=ship_no))
    week_choices = []
    for i in range(10):
        w = (datetime.strptime(_sixs_week_start(), '%Y-%m-%d') - timedelta(days=7 * i)).strftime('%Y-%m-%d')
        week_choices.append({'value': w,
                             'label': _sixs_week_label(w) + ('（本周）' if i == 0 else '')})
    return render_template('sixs_new.html', ship_no=ship_no, groups=SIX_S, biz=biz,
                           week_start=week_start, week_label=_sixs_week_label(week_start),
                           week_choices=week_choices,
                           n_items=sixs_count(), base=SIXS_BASE,
                           user_role=session.get('user_role', ''),
                           user_name=session.get('user_name', ''))


@app.route('/sixs/save', methods=['POST'])
def sixs_save():
    """保存评分：picks = {编号: 备注}，得分 = 100 - 扣分条数
    V1.51：一船一周一份（同船同周拦重复，不同周各存一份）；week_start 存所属周周一。
    V1.16.7：picks 可为空（满分也能存）；记录打分人手机号（只有本人/超管能删）。"""
    if not _can_score_sixs():
        return jsonify({'ok': 0, 'msg': '只有审核人可以打分'}), 403
    data = request.get_json(force=True, silent=True) or {}
    ship_no = (data.get('ship_no') or '').strip()
    picks = data.get('picks') or {}
    scorer = (data.get('scorer') or '').strip()
    week_start = _sixs_week_start(data.get('week_start'))
    if not ship_no:
        return jsonify({'ok': 0, 'msg': '缺船号'})
    _old = _sixs_row_of_ship(ship_no, week_start)
    if _old:
        return jsonify({'ok': 0, 'msg': '{}船在 {} 这一周已有评分（{} 分），要改请先删掉那一条'.format(
            ship_no, _sixs_week_label(week_start), _old.get('score') or 0)})
    deduct = len(picks)
    score = max(0, SIXS_BASE - deduct)
    creator = (_current_identity_phone() or '').strip()
    ph = _sixs_ph()
    conn = db.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO sixs(ship_no,scorer,picks,deduct,score,photos,ctime,creator,week_start) "
                    "VALUES(" + ",".join([ph] * 9) + ")",
                    (ship_no, scorer, json.dumps(picks, ensure_ascii=False),
                     deduct, score, '',
                     datetime.now().strftime('%Y-%m-%d %H:%M'), creator, week_start))
        mid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': 1, 'id': mid, 'week': week_start})


@app.route('/sixs/<int:mid>')
def sixs_view(mid):
    """评分明细：只列出被扣分的那几条 + 备注"""
    conn = db.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM sixs WHERE id=" + _sixs_ph(), (mid,))
        row = cur.fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    if not row:
        return redirect('/sixs')
    row = dict(row)
    try:
        picks = json.loads(row.get('picks') or '{}')
    except Exception:
        picks = {}
    biz = (request.args.get('biz') or 'debug').strip()
    if biz not in ('debug', 'safety'):
        biz = 'debug'
    week_start = _sixs_week_of(row)
    return render_template('sixs_view.html', r=row, picks=picks, biz=biz,
                           can_score=_can_score_sixs(),
                           week_start=week_start, week_label=_sixs_week_label(week_start),
                           back_qs=_sixs_back_qs(biz, week_start),
                           items=sixs_items(), n_items=sixs_count(), base=SIXS_BASE,
                           user_role=session.get('user_role', ''),
                           user_name=session.get('user_name', ''))


@app.route('/sixs/<int:mid>/del', methods=['POST'])
def sixs_delete(mid):
    """V1.16.7：删评分只限【打分人本人】+【固定超管】（以前是超管+所有审核人都能删）"""
    if not _can_score_sixs():
        return _permission_denied()
    conn = db.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM sixs WHERE id=" + _sixs_ph(), (mid,))
        _r = cur.fetchone()
        row = dict(_r) if _r else None
    except Exception:
        row = None
    finally:
        conn.close()
    if row is None:
        return redirect('/sixs')
    if not _sixs_can_del(row):
        return _permission_denied()
    week_start = _sixs_week_of(row)
    biz = (request.args.get('biz') or 'debug').strip()
    if biz not in ('debug', 'safety'):
        biz = 'debug'
    conn = db.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM sixs WHERE id=" + _sixs_ph(), (mid,))
        conn.commit()
    finally:
        conn.close()
    return redirect('/sixs' + _sixs_back_qs(biz, week_start))


@app.route('/board/<int:ship_id>')
def board(ship_id):
    """单张看板主页（调试业务）：按产品船过滤记录与系统清单"""
    return _render_board(ship_id, 'debug')


@app.route('/safety')
def safety_board():
    """V1.46：安全业务【统一列表页】——所有类别一起显示，靠 ?cat= 与页面筛选切换。
    超管在「类别维护」里新增的类别会自动出现在这里，无需改代码。
    /safety/devices 保留为老链接兼容，直接重定向回本页并选中设备类别。"""
    return render_biz('boards.html', 'safety',
                      ships=_ships_with_counts('safety'),
                      staff_list=db.get_staff_list(),
                      user_role=session.get('user_role', ''),
                      user_name=session.get('user_name', ''),
                      user_phone=session.get('user_phone', ''),
                      DEVICE_PAGE=False,
                      can_delete=_can_manage_biz('safety'))


@app.route('/safety/devices')
def safety_devices():
    """V1.46b（用户要求）：设备板块不再单独占门户入口，也不再有独立页。
    它只是安全看板统一列表里的一个类别，与产品船 / 场地并列。
    老链接 / 收藏夹保留兼容 ⇒ 直接跳回统一列表并自动选中设备类别 tab。"""
    return redirect(url_for('safety_board', cat=board_categories.default_key('device')))


@app.route('/admin/board_cats')
def admin_board_cats():
    """V1.46：超管维护安全看板类别（新增 / 改名 / 改行为 / 停用 / 排序）。"""
    if not _can_manage_biz('safety'):
        return _permission_denied()
    used = board_categories.used_keys_from_ships(db.get_ships('safety'))
    return render_template('board_cats.html',
                           cats=board_categories.all_cats(),
                           behaviors=board_categories.BEHAVIOR_LABELS,
                           used_keys=used,
                           max_cats=board_categories.MAX_CATS,
                           user_name=session.get('user_name', ''),
                           biz='safety')


@app.route('/admin/board_cats/save', methods=['POST'])
def admin_board_cats_save():
    """V1.46：保存类别配置。校验在 board_categories 模块里，后端不信任前端。"""
    if not _can_manage_biz('safety'):
        return jsonify(ok=False, msg='只有超级管理员能维护看板类别'), 403
    data = request.get_json(silent=True) or {}
    used = board_categories.used_keys_from_ships(db.get_ships('safety'))
    ok, msg = board_categories.save(data, used)
    if ok:
        logger.info('[V1.46] 看板类别已更新：%s，操作人 %s', msg, _current_phone())
    return jsonify(ok=ok, msg=msg), (200 if ok else 400)


@app.route('/safety/board/<int:ship_id>')
def safety_kanban(ship_id):
    """单条产品船的安全隐患看板（如「2554船安全隐患看板」）。
    结构与调试看板一致，但没有"选择系统"，术语按安全口径（隐患类别/隐患/未整改）。"""
    ship = db.get_ship(ship_id)
    if not ship or (ship.get('biz') or 'debug') != 'safety':
        return "看板不存在或已被删除", 404
    ensure_safety_categories(ship_id)      # 没有类别时自动灌入 9 个默认隐患类别（V1.6 A4 精简版）
    return _render_board(ship_id, 'safety')


def _render_board(ship_id, biz):
    ship = db.get_ship(ship_id)
    if not ship:
        return "看板不存在或已被删除", 404
    records = []
    for r in db.get_records_by_ship(ship_id):
        records.append({
            'id': r['id'],
            'sys_name': r.get('sys_name') or '',
            'desc': r.get('description') or '',
            'debug_user': r.get('debug_users') or '',
            'debug_sup': r.get('debug_sup') or '',
            'install_user': r.get('install_user') or '',
            'area_leader': r.get('area_leader') or '',
            'dept_leader': r.get('dept_leader') or '',
            # 显式选择的作业区/部门优先（快速选作业区功能），旧数据回退台账反查
            'area_name': (r.get('area_name') or '').strip() or extract_area(r.get('area_leader') or ''),
            'dept_name': (r.get('dept_name') or '').strip() or _smart_extract_dept(r.get('dept_leader') or '', level=2),
            # V1.17.1：场地位置（场地看板提交隐患时选的发生场地）
            'place_name': (r.get('place_name') or '').strip(),
            'submit_time': r.get('submit_time') or '',
            'photo_path': r.get('photo_path') or '',
            'status': r.get('status') or '未完成',
            'display_status': compute_display_status(r.get('status') or '未完成', r.get('submit_time'), r.get('ship_id'), r.get('deadline')),
            'fb_desc': r.get('fb_desc') or '',
            'fb_photo': r.get('fb_photo') or '',
            'fb_time': r.get('fb_time') or '',
            'close_time': r.get('close_time') or '',
            'submitted_by': r.get('submitted_by') or '',
            'dispatch_user': r.get('dispatch_user') or '',
            'dispatch_phone': r.get('dispatch_phone') or '',
            'dispatch_by': r.get('dispatch_by') or '',
            'dispatch_time': r.get('dispatch_time') or '',
            'dispatch_note': r.get('dispatch_note') or '',
            # V1.6 A2（界面 Demo s3）：记录列表新增三列要用到的字段
            'rec_type': (r.get('rec_type') or '').strip() or '隐患整改',
            'approval_status': (r.get('approval_status') or '').strip(),
            'handle_way': (r.get('handle_way') or '').strip(),
            'fine_amount': (r.get('fine_amount') or '').strip(),
        })
    # 当前登录人「姓名 - 电话」（发起手尾时预填为默认调试责任人，界面可改）
    # V1.16.22：用「认到的身份」，免登录下 _current_phone() 恒空 ⇒ 预填不出来
    debug_user_label = ""
    if _is_logged_in():
        _me_ph = _current_identity_phone()
        staff = db.find_staff_by_phone(_me_ph)
        if staff:
            debug_user_label = f"{staff.get('姓名', '')} - {staff.get('电话', '')}"
        else:
            debug_user_label = f"{session.get('user_name', '')} - {_me_ph}"
    th = get_thresholds_for(ship_id)
    if biz == 'safety':
        # 安全业务把"系统"换成"隐患类别"：清单必须传给模板，否则发起隐患时没得选
        ensure_safety_categories(ship_id)
    # 切换看板只在调试业务给（安全业务回 /safety 列表即可）
    return render_biz('index.html', biz,
                      records=records, ship=ship,
                      # V1.17.0：安全隐患看板的「选审批人」只列安全超管（固定超管 + admin_grant 名单）
                      reviewers=(_safety_approver_items() if biz == 'safety' else []),
                      # V1.17.1：场地位置 —— 固定场地清单 + 本看板是否「场地」类
                      place_names=_PLACE_NAMES,
                      is_place_board=_is_place_board(ship),
                      ships=(db.get_ships('debug') if biz == 'debug' else []),
                      staff_list=db.get_staff_list(),
                      system_list=db.get_ship_systems(ship_id),
                      area_defaults=db.get_area_defaults(ship_id),
                      l1=th['l1'], l2=th['l2'],
                      debug_user_label=debug_user_label,
                      user_role=session.get('user_role', ''),
                      user_name=session.get('user_name', ''),
                      user_phone=session.get('user_phone', ''))


# ============ 用户登录 / 注册 / 权限校验 ============

def _is_logged_in() -> bool:
    """V1.6 B1：开了免登录开关后，所有权限门禁一律放行（不再区分登没登录）"""
    if NO_LOGIN:
        return True
    return bool(session.get('logged_in'))


@app.after_request
def _no_cache_html(resp):
    """V1.6：HTML 页面不做缓存。

    页面上的按钮是服务端按角色渲染的，手机内置浏览器一旦缓存了旧页面，
    超管改完角色后当事人看到的还是旧按钮 —— 表现为「改了和没改一样」。"""
    try:
        if (resp.content_type or '').startswith('text/html'):
            resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            resp.headers['Pragma'] = 'no-cache'
    except Exception:
        pass
    return resp


@app.after_request
def _stick_my_identity(resp):
    """V1.16.8：身份一旦认出来就落到 cookie（不只放 session）。

    原来只有 /my/whoami（选「我是谁」）才写 my_who cookie，
    而 /my?as=手机号（分享链接 / SSO 自动识别）只写 session —— 浏览器一关 session 就没了，
    下次进来又得重新认人，看起来就是「进去了不能自动登录」。
    这里统一兜住：只要会话里有身份，就顺手写回 cookie（180 天）。"""
    try:
        if 'my_phone' in session:
            ph = (session.get('my_phone') or '').strip()
            if ph:
                resp.set_cookie(MY_COOKIE, ph, max_age=60 * 60 * 24 * 180,
                                httponly=True, samesite='Lax')
            else:
                resp.set_cookie(MY_COOKIE, '', max_age=0)
    except Exception:
        pass
    return resp


# ============ V1.8 船海智云单点登录（免登授权）============
# 文档《船海智云对外服务接口 V2.3》2.20 / 2.21：
#   船海智云点应用跳转时，把 token 以 GET 方式附在业务系统 URL 的 access_token 参数上；
#   业务系统拿它调 /api/app-site/index/current_user_info 换取用户信息（含 phone / name）。
# ⇒ 从船海智云点进来就自动认出是谁，用户完全无感（不用登录、也不用自选身份）。
SSO_PARAM = 'access_token'
# 认到人之后，是否按这个人的真实角色收紧管理动作（"只有调试上级能建看板"）。
# 默认关：先只做"认出你是谁"，不改变任何人的操作权限；确认无误后在 config.json 打开。
SSO_ENFORCE = bool((CONFIG.get('sso_enforce_admin')))
# 🔴 V1.16.27：强制单点登录。关（默认）= 认不到人还能"自选身份"进来干活；
#   开 = **必须**从船海智云点进来，认不到人就是访客，连自选身份都不给。
#   和 sso_enforce_admin 是两回事：那个管"认到人之后按不按真实角色给权限"，
#   这个管"没认到人时让不让人自己声明身份"。
SSO_REQUIRE = bool((CONFIG.get('sso_require')))

# 🔴 V1.16.28（用户要求）：「一进去就是登录好的、我的里是真实身份、不能随便换人」。
#   这三个开关就是为此加的，全部默认关 —— 逐项打开、逐项验证，别一把全开。
#
# ID_LOCK —— **锁死身份**。开了之后：
#   · /my?pick=1（自己声明是谁）整页关掉，不再给"选个人就能上"的机会；
#   · /my?as=（用别人的视角打开）整条路关掉；
#   · /my/whoami 只接受**管理员批过的换人单**，别人一律拒。
#   ⇒ 结果：进来的每一个人，身份都由船海智云说了算。
ID_LOCK = bool((CONFIG.get('id_lock')))
#
# ONLINE_GUARD —— **在线管控**。开了之后：
#   · 每次认到人都落一条 online_sessions 记录（谁、在哪台设备、哪个 IP、什么时候动的）；
#   · 同一账号在别处已经开着 ⇒ 提示"已在线"，可选择顶替（按用户口径：不硬锁）；
#   · 超管能看在线列表、能一键强制退出（把人从当前会话里请出去，下一次请求就生效）。
ONLINE_GUARD = bool((CONFIG.get('online_guard')))

# 会话令牌 cookie 的名字（和 MY_COOKIE 一样是 180 天）
SESS_COOKIE = 'shipyard_sess_tok'


def _sess_token() -> str:
    """本次会话的令牌：session 里没有就现生成一个。

    为什么需要它：Flask 默认会话是**签名 Cookie**，服务端不留痕。
    所以"这个账号是不是已经在别处开着"光看 cookie 判断不出来 ——
    令牌的作用就是给这一端（浏览器/手机）一个稳定标识，好落进 online_sessions 表。
    """
    try:
        t = (session.get('sess_tok') or '').strip()
        if not t:
            t = secrets.token_hex(16)
            session['sess_tok'] = t
        return t
    except Exception:
        return ''


def _client_ua() -> str:
    return (request.headers.get('User-Agent') or '')[:250]


def _online_super_exempt(phone='') -> bool:
    """🔴 V1.16.36（用户报障「当前在线这一点不太好，我需要超管一上线就可以用」）：
    **超管（含名单里的超管）永远豁免在线管控** —— 既不会被顶替提示拦，
    也不会被别人顶掉后踢出，同时他自己仍然看得到在线列表、能踢别人。

    为什么必须单独开这个口子：
      · 用户是唯一超管，天天要用；一旦开了 online_guard 又不小心在另一台设备
        选了「顶替并继续」，或者被别人（调试上级）点「强制退出」，
        唯一超管自己被请出系统 ⇒ **谁也进不去管理页，等于全厂锁死**。
      · 定位 `SUPER_ADMIN_PHONE` 之外还看超管名单（admin_grant / debug_admin_grant），
        因为 V1.16.16 起超管名单可以不止一个人。

    ⚠️ `phone` 传空才回退到「当前登录的人」；**传了具体手机号就一定判那个人**。
    原因：这个函数有两类用法 —— 「我该不该被管控」（不传）和
    「这个人能不能被踢」（传靶子号）。混了就会出现「超管踢普通人被拒」这种事。
    """
    if phone and str(phone).strip():
        ph = str(phone).strip()
    else:
        ph = _current_identity_phone()
    if not ph:
        return False
    if ph == SUPER_ADMIN_PHONE:
        return True
    # 🔴 V1.16.36（实测发现）：用 config.json 里的 admin 账号密码登录时，
    #   session['my_phone'] 存的是字符串 **'admin'**（app.py login() 第 2703 行），
    #   不是手机号 ⇒ 只比手机号会漏掉这个入口，超管照样被"别处已开"红提示打扰。
    if ph == ADMIN_USER:
        return True
    try:
        biz = _request_biz() or 'debug'
    except Exception:
        biz = 'debug'
    try:
        g = admin_grant if biz == 'safety' else debug_admin_grant
        if g.is_member(ph, ''):
            return True
    except Exception:
        pass
    # 🔴 V1.16.36 兜底（第三版才写对）：**查库里这个手机号是不是超管**。
    #   前面那些 ID 比较都是"猜有哪些超管入口"，这里才是正路 ——
    #   超管可能由固定超管 / config 兜底账号 / 超管名单（按部门或按人）三种方式产生，
    #   漏掉一条就意味着某个超管被锁在门外，这是全厂级事故，不能靠枚举。
    #
    #   ⚠️ **绝不能在这里用 `_biz_admin(biz)`**：那个函数问的是
    #   「**当前登录的人**是不是超管」，与传进来的 `ph`（被判定的那个人）无关。
    #   实测踩过：超管踢一个**普通人**时，这里返回 True ⇒ 弹出「不能强制退出」，
    #   **在线管控对非超管彻底失效**（谁也踢不掉任何人）。必须按 ph 查库。
    try:
        u = db.get_user_by_phone(ph) or {}
    except Exception:
        u = {}
    if (u.get('role') or '').strip() == 'super_admin':
        return True
    return False


def _register_online(phone='', name=''):
    """认到人之后登记在线。**必须**在真认到人之后调，别在访客阶段调
    （否则未认证访客会把某个号的在线位占住，真人进来反而提示"已在线"）。

    🔴 V1.16.36：超管豁免 —— 超管一上线就直接可用，不进顶替/被踢流程
    （见 `_online_super_exempt`）。豁免的账号**照样登记在线列表**，
    只是不参与互踢。"""
    if not ONLINE_GUARD:
        return {}
    ph = (phone or '').strip()
    if not ph:
        return {}
    _exempt = _online_super_exempt(ph)
    try:
        r = db.online_login(ph, (name or '').strip(), _sess_token(),
                            ua=_client_ua(), ip=_client_ip(),
                            biz=(_request_biz() or 'debug'))
    except Exception as e:
        logger.warning('在线会话登记失败: %s' % e)
        return {}
    # 🔴 别处还开着时 db 不再自动顶替，而是把那条回传。
    #   记到 session 里，页面据此显示"你顶不掉 TA，先确认"，
    #   用户在 /online 点「顶替并继续」才真顶（口径：提示后允许顶替，不静默）。
    #   🔴 V1.16.36：超管不参与这个提示 —— 别人别处开着也当没这回事，直接可用。
    try:
        if _exempt:
            session.pop('takeover', None)
        else:
            tk = (r or {}).get('takeover') or {}
            if tk:
                session['takeover'] = {'ua': _ua_short(tk.get('ua')),
                                       'ip': tk.get('ip') or '',
                                       'at': (tk.get('created_at') or '')[-8:]}
                logger.info('在线检测：%s 已在别处开着（%s），本次未顶替，等用户确认',
                            ph, _ua_short(tk.get('ua')))
            else:
                session.pop('takeover', None)
    except Exception:
        pass
    return r


def _online_others(phone=''):
    """当前这个人是不是**在别处**已经开着（不含本次会话自己）。

    两个来源取其一：
      · session['takeover'] —— 刚才登录那一刻 db 就发现了（更准，带设备描述）；
      · 实时查库 —— 登录后别的人才上的线，或顶替之后再看。
    """
    if not ONLINE_GUARD:
        return {}
    ph = (phone or _current_identity_phone() or '').strip()
    if not ph:
        return {}
    try:
        r = db.is_online_elsewhere(ph, _sess_token())
    except Exception:
        r = {}
    if r:
        return r
    try:
        t = (session.get('takeover') or {})
        return t if t else {}
    except Exception:
        return {}


def _online_ensure_registered():
    """🔴 V1.16.29 修：确保本端在 `online_sessions` 里有记录，**没有就补一条**。

    为什么必须有这一步（这是真 bug，不是优化）：
      `_online_others()` 会调 `_sess_token()` 生成令牌，但**不落库**。
      于是出现这条死路 ——
        ① 用户在 `online_guard=false` 期间就登好了（session 里有 my_phone）；
        ② 管理员把 `online_guard` 改成 true；
        ③ 该用户下一次请求：`_online_kicked()` 拿 `sess_tok` 去查表 → **查不到**
           （因为这条会话从没登记过）→ 判定"被踢" → **把身份清空并跳 /online/kicked**。
      结果就是"一开在线管控，全公司正在用的人点一下就被请出去"。

    口径：**只有表里明确记着 `kicked=1` 才算被踢**；
    查不到记录 = "还没登记过"，补登记后正常放行。
    """
    if not ONLINE_GUARD:
        return
    tok = (session.get('sess_tok') or '').strip()
    if not tok:
        # 连令牌都没有（老会话从没进过 /online 这条路）→ 先造一个
        tok = _sess_token()
        if not tok:
            return
    try:
        st = (db.online_state(tok) or {}).get('state') or 'unknown'
    except Exception:
        return
    if st == 'active':
        return                          # 有记录且没被踢，正常放行
    if st == 'kicked':
        return                          # 真被踢，交给 `_online_kicked()` 处理
    # st == 'unknown' —— 从没登记过 / 已被清理，补一条（不顶替任何别处会话）
    try:
        _ph = _current_identity_phone()
        if not _ph:
            return
        db.online_login(_ph, (session.get('my_name') or '').strip(), tok,
                        ua=_client_ua(), ip=_client_ip(),
                        biz=(_request_biz() or 'debug'), take_over=False)
        logger.info('在线补登记：%s（原本没有会话记录，按正常在线处理，不当成被踢）',
                    _name_of_phone(_ph) or _ph)
    except Exception as e:
        logger.warning('在线补登记失败: %s' % e)


def _online_kicked() -> bool:
    """本次会话是不是**已经被踢了**（被别人顶替，或被超管强制退出）。

    在 before_request 里查一次；返回 True 就把身份清掉并送回登录入口。

    🔴 V1.16.29：口径是「**表里明确记着这一条被踢** 才算被踢」。
       原来写成 `not db.online_touch(tok)`（查不到 = 被踢），会把
       「从没登记过的老会话」和「刚被踢」混为一谈 ——
       表现就是开了 `online_guard` 之后，所有老会话点一下就被请出去。
       现在改成先 `_online_ensure_registered()` 补登记，再判 `kicked=1`。
    """
    if not ONLINE_GUARD:
        return False
    _online_ensure_registered()
    tok = (session.get('sess_tok') or '').strip()
    if not tok:
        return False
    try:
        return not db.online_touch(tok)
    except Exception:
        return False


def _sso_phone(raw):
    """从接口返回的 phone 里取出 11 位手机号（兼容 +86 / 空格 / 横线）"""
    digits = ''.join(ch for ch in str(raw or '') if ch.isdigit())
    if len(digits) >= 13 and digits.startswith('86'):
        digits = digits[2:]
    if len(digits) == 11 and digits[0] == '1':
        return digits
    return ''


def _sso_phone_by_code(info):
    """单点登录返回里没有手机号时的兜底：拿 工号(code) / 账号(account) 去账号表反查手机号。

    实测船海智云返回里 phone 可能为 null（如对接账号），所以不能只认 phone。
    """
    vals = [(info.get('code') or '').strip(), (info.get('account') or '').strip(),
            (info.get('buildCode') or '').strip(), (info.get('guidkey') or '').strip()]
    vals = [v for v in vals if v]
    if not vals:
        return ''
    try:
        conn = db.get_conn()
    except Exception:
        return ''
    try:
        cur = conn.cursor()
        ph = '%s' if db.resolve_mode() == 'mysql' else '?'
        for v in vals:
            cur.execute('SELECT phone FROM users WHERE emp_no = ' + ph +
                        ' OR phone = ' + ph, (v, v))
            for r in (cur.fetchall() or []):
                raw = r.get('phone') if isinstance(r, dict) else (r[0] if r else '')
                p = _sso_phone(raw)
                if p:
                    return p
    except Exception:
        return ''
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return ''



# ---------- 单点登录自检日志（手机端自助排查用，不用手动看 token） ----------
SSO_LOG_FILE = os.path.join(BASE_DIR, 'sso_log.jsonl')
_SSO_DIAG = {}


def _mask_phone(p):
    p = str(p or '')
    return (p[:3] + '****' + p[-4:]) if len(p) == 11 else (p or '')


def _sso_log_event(ev):
    """记一次单点登录事件（只留最近 60 条）。
    手机上直接打开 /sso/log 就能看到：平台有没有把 token 传过来、接口怎么说。"""
    try:
        ev['t'] = time.strftime('%Y-%m-%d %H:%M:%S')
        lines = []
        if os.path.exists(SSO_LOG_FILE):
            try:
                lines = open(SSO_LOG_FILE, encoding='utf-8').read().splitlines()
            except Exception:
                lines = []
        lines.append(json.dumps(ev, ensure_ascii=False))
        lines = [x for x in lines if x.strip()][-60:]
        with open(SSO_LOG_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
    except Exception as e:
        logger.warning(f'单点登录日志写入失败: {e}')


def _sso_identify(token):
    """用单点登录 token 换身份。返回 (phone, name, info)；任何异常都返回空（静默降级）。

    同时把「接口到底怎么答的」记到 _SSO_DIAG，供 /sso/log 排查用
    （直接调 _req 而不是 current_user_info，就是为了拿到明确的错误信息）。"""
    global _SSO_DIAG
    _SSO_DIAG = {}
    if not (token or '').strip():
        return '', '', {}
    try:
        import chz_push
        info = chz_push._req('GET', '/api/app-site/index/current_user_info',
                             token=token.strip(), timeout=10) or {}
    except Exception as e:
        _SSO_DIAG = {'err': str(e)[:240]}
        logger.warning(f"单点登录校验失败（已降级为自选身份）: {e}")
        return '', '', {}
    if not info:
        _SSO_DIAG = {'err': '接口没返回用户信息'}
        return '', '', {}
    _SSO_DIAG = {'info': info}
    phone = _sso_phone(info.get('phone')) or _sso_phone_by_code(info)
    if not phone:
        _SSO_DIAG['err'] = ('接口返回了「%s」，但其中 phone 为空，且工号/账号在账号表里也没查到人'
                            % (info.get('name') or '?'))
    return (phone, (info.get('name') or '').strip(), info)


@app.before_request
def _sso_from_cscloud():
    """从船海智云跳进来时自动认人（放在角色同步之前执行，让身份先确定）。

    - 认到了：写进会话，并把 URL 上的 access_token 摘掉后重定向（token 不留地址栏/日志）
    - 没认到：同样摘掉 token 跳转，用户继续用「自选身份」这条路 —— 绝不挡人
    """
    if request.method != 'GET' or request.endpoint == 'static':
        return None
    tok = (request.args.get(SSO_PARAM) or '').strip()
    if not tok:
        return None

    phone, name, info = _sso_identify(tok)
    try:
        _sso_log_event({
            'path': request.path,
            'args': '&'.join(k for k in request.args.keys() if k != SSO_PARAM)[:60],
            'token_len': len(tok),
            'token_tail': tok[-6:],
            'ok': bool(phone),
            'name': name or '',
            'phone': _mask_phone(phone),
            'diag': _SSO_DIAG.get('err') or '',
            'who': (info.get('account') or '') if info else '',
            'ua': (request.headers.get('User-Agent') or '')[:60],
        })
    except Exception:
        pass
    if phone:
        try:
            session['my_phone'] = phone
            session['my_name'] = name or _name_of_phone(phone)
            session['sso_at'] = int(time.time())
            session['sso_name'] = info.get('divisionFullName') or info.get('divisionName') or ''
            # V1.16.22：单点登录认到人 = 已登录。原来只写 my_phone 不写 logged_in，
            # 下游若按 logged_in 判身份，会把真正走 SSO 的人当成没登录挡在门外。
            session['logged_in'] = True
            session['user_phone'] = phone
            # 🔴 V1.16.27：这个会话的身份是「平台认证过的」。
            #   整份代码里只有这里写 'sso'，所以伪造不出可信身份 ——
            #   /my/whoami（自选身份）写的是 'pick'，不满足 _identity_trusted()。
            session['id_src'] = 'sso'
            # 🔴 V1.16.28：真认到人了 ⇒ 这一端登记为在线。
            #   放在写完 id_src 之后 —— 身份还没确定就登记会把访客算成在线。
            _register_online(phone, session.get('my_name') or name)
            logger.info(f"船海智云单点登录成功: {name}({phone})")
        except Exception as e:
            logger.warning(f"单点登录写入会话失败: {e}")
    else:
        logger.info("船海智云单点登录未识别到用户（已降级为自选身份）")

    # 去掉 access_token 再跳转（成功与否都摘掉，避免 token 泄露）
    try:
        import urllib.parse as _up
        args = request.args.to_dict(flat=True)
        args.pop(SSO_PARAM, None)
        target = request.path
        if args:
            target += '?' + _up.urlencode(args)
        # 🔴 V1.16.36（用户报障「从船海智云点应用图标进来，直接跳到船舶完整性看板
        #   而不是总界面」）：船海智云后台把这个应用的「跳转地址」配成了 /boards，
        #   所以 SSO 认证完按 request.path 原样跳 ⇒ 直接落在看板列表。
        #   正解是平台侧把地址改成 `/`；这里加一层保险：落点是看板/安全列表时
        #   且**没带任何业务参数**（说明是"进应用"而不是"进某个具体页面"），
        #   就改道总界面 `/`，让人先进门户自己挑板块。
        #   带参数（如 ?ship_id=…、?tab=idreq）的一律原样跳，不改道。
        if phone and not args and request.path in ('/boards', '/safety'):
            logger.info("单点登录落点是看板列表，按V1.16.36 改道总界面 /")
            return redirect('/')
        return redirect(target)
    except Exception:
        return None



@app.route('/sso/entry')
def sso_entry():
    """V1.16.41：单点登录入口引导页 —— **只保留两句话**（用户口径）。

    1) 请从船海智云进入本系统。
    2) 管理员账号登录（折叠表单 · 应急通道）。

    V1.16.40 之前的诊断说明、操作步骤、自检记录入口、自选身份入口全部去掉：
    对外只说明「该从哪进来」，一个多余的字、按钮和信息都不留。
    ⚠️ 「管理员账号登录」必须保留 —— 首页/看板都被门禁挡住后，
       index.html / boards.html 里的登录弹窗就再也看不到了，
       这个折叠表单是管理员唯一的应急入口（登录成功 id_src='pwd' ⇒ 首页放行）。
    """
    # V1.17.2（用户报障「点进去又要从船海智云进入本系统」）：船海智云后台把应用
    # 跳转地址配成了 /sso/entry，导致平台点进来认到人后 redirect 回 /sso/entry，
    # 而本页不判断登录态 ⇒ 已登录的人仍被卡在引导页。兜底：已可信身份（SSO/密码）
    # 点到本页时直接进首页，未登录的人才看引导页。不依赖平台改配置。
    if _identity_trusted():
        return redirect('/')
    out = []
    out.append('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">')
    out.append('<meta name="viewport" content="width=device-width, initial-scale=1.0">')
    out.append('<title>请从船海智云进入本系统</title><style>')
    out.append('body{margin:0;padding:0;background:#eef1f6;color:#1c2331;')
    out.append('font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}')
    out.append('.w{min-height:100vh;display:flex;align-items:center;justify-content:center;')
    out.append('padding:24px 18px;box-sizing:border-box}')
    out.append('.c{width:100%;max-width:400px;background:#fff;border:1px solid #e3e7ee;')
    out.append('border-radius:14px;padding:26px 22px;box-sizing:border-box}')
    out.append('.h{font-size:19px;font-weight:700;line-height:1.65;margin:0;text-align:center}')
    out.append('.lg{margin-top:20px;padding-top:16px;border-top:1px solid #eef1f6}')
    out.append('.lg summary{cursor:pointer;font-size:14px;color:#1d4ed8;text-align:center}')
    out.append('.lg input{width:100%;box-sizing:border-box;margin-top:10px;padding:10px 12px;')
    out.append('border:1px solid #d3d8e3;border-radius:9px;font-size:14px;background:#fff;color:#1c2331}')
    out.append('.lg button{width:100%;margin-top:12px;padding:11px 0;border:0;border-radius:10px;')
    out.append('background:#1d4ed8;color:#fff;font-size:14px;font-weight:600;cursor:pointer}')
    out.append('.msg{font-size:12.5px;color:#dc2626;margin-top:8px;min-height:16px;text-align:center}')
    out.append('</style></head><body><div class="w"><div class="c">')
    out.append('<div class="h">请从船海智云进入本系统</div>')
    out.append('<details class="lg"><summary>管理员账号登录</summary>')
    out.append('<input id="lgU" autocomplete="username" placeholder="账号">')
    out.append('<input id="lgP" type="password" autocomplete="current-password" placeholder="密码">')
    out.append('<button type="button" onclick="adminLogin()">登录</button>')
    out.append('<div class="msg" id="lgMsg"></div></details>')
    out.append('<script>function adminLogin(){'
               'var u=document.getElementById("lgU").value.trim();'
               'var p=document.getElementById("lgP").value;'
               'var m=document.getElementById("lgMsg");'
               'if(!u||!p){m.textContent="请先填写账号和密码";return;}'
               'm.style.color="#6b7488";m.textContent="登录中…";'
               'fetch("/login",{method:"POST",headers:{"Content-Type":"application/json"},'
               'body:JSON.stringify({username:u,password:p,biz:"debug"})})'
               '.then(function(x){return x.json().then(function(j){return{s:x.status,j:j};});})'
               '.then(function(o){if(o.j&&o.j.ok){m.style.color="#16a34a";'
               'm.textContent="登录成功，正在进入…";location.href="/";}'
               'else{m.style.color="#dc2626";m.textContent=(o.j&&o.j.msg)||("登录失败（"+o.s+"）");}})'
               '.catch(function(){m.style.color="#dc2626";m.textContent="网络异常，请稍后重试";});}'
               '</script>')
    out.append('</div></div></body></html>')
    return ''.join(out)


@app.route('/sso/log')
def sso_log():
    """单点登录自检：手机上直接打开这个页面，就能看到最近几次跳转的结果。
    不用去地址栏里找 token、也不用截图 —— 给谁排查都方便。"""
    rows = []
    try:
        if os.path.exists(SSO_LOG_FILE):
            import json as _json
            for ln in open(SSO_LOG_FILE, encoding='utf-8').read().splitlines()[-40:]:
                if ln.strip():
                    try:
                        rows.append(_json.loads(ln))
                    except Exception:
                        pass
    except Exception:
        rows = []
    rows.reverse()          # 最新的在最上面
    out = []
    out.append('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">')
    out.append('<meta name="viewport" content="width=device-width, initial-scale=1.0">')
    out.append('<title>单点登录自检</title><style>')
    out.append('body{margin:0;padding:14px;background:#eef1f6;color:#1c2331;')
    out.append('font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}')
    out.append('.h{font-size:17px;font-weight:700;margin:0 0 4px}')
    out.append('.sub{font-size:12px;color:#6b7488;line-height:1.6;margin-bottom:12px}')
    out.append('.c{background:#fff;border:1px solid #e3e7ee;border-radius:12px;padding:10px 12px;margin-bottom:9px}')
    out.append('.ok{border-left:3px solid #16a34a}.bad{border-left:3px solid #dc2626}')
    out.append('.t{font-size:12px;color:#6b7488;margin-bottom:5px}')
    out.append('.k{font-size:13px;line-height:1.7}')
    out.append('.k b{font-weight:600}')
    out.append('.err{font-size:12px;color:#dc2626;background:#fdecec;border-radius:8px;padding:6px 8px;margin-top:6px;word-break:break-all}')
    out.append('.none{background:#fff;border:1px solid #e3e7ee;border-radius:12px;padding:16px;font-size:13px;color:#6b7488;line-height:1.8}')
    out.append('.btn{display:inline-block;margin-top:12px;font-size:13px;padding:8px 14px;border-radius:9px;')
    out.append('background:#1d4ed8;color:#fff;text-decoration:none}')
    out.append('.bar{display:flex;gap:8px;margin-top:12px}')
    out.append('.bar a{flex:1;text-align:center;font-size:13px;padding:9px 0;border-radius:9px;background:#fff;')
    out.append('border:1px solid #e3e7ee;color:#1c2331;text-decoration:none}')
    out.append('</style></head><body>')
    out.append('<div class="h">船海智云单点登录 · 自检</div>')
    out.append('<div class="sub">这里记录最近几次「带 access_token 跳进来」的结果。'
               '没有记录 = 平台压根没把 token 传过来（是平台侧地址没配）。</div>')
    if not rows:
        out.append('<div class="none">还没有收到过带 <b>access_token</b> 的访问。<br>'
                   '说明船海智云那边还没把本系统的地址配成应用跳转地址，'
                   '或者你这次是从浏览器直接打开、不是从船海智云点进来的。</div>')
    for r in rows:
        ok = bool(r.get('ok'))
        out.append('<div class="c %s">' % ('ok' if ok else 'bad'))
        out.append('<div class="t">%s　·　%s</div>' % (r.get('t') or '', r.get('path') or ''))
        if ok:
            out.append('<div class="k">识别成功：<b>%s</b>　%s</div>'
                       % (r.get('name') or '(无名)', r.get('phone') or ''))
            if r.get('who'):
                out.append('<div class="k" style="color:#6b7488">平台账号：%s</div>' % r['who'])
        else:
            out.append('<div class="k">没能认出是谁</div>')
        out.append('<div class="k" style="color:#6b7488">收到 token 长度 %s，尾号 …%s</div>'
                   % (r.get('token_len') or 0, r.get('token_tail') or ''))
        if r.get('diag'):
            out.append('<div class="err">%s</div>' % str(r['diag'])[:400])
        if r.get('ua'):
            out.append('<div class="k" style="color:#94a3b8;font-size:11px">%s</div>' % r['ua'][:60])
        out.append('</div>')
    out.append('<div class="bar"><a href="/sso/log">刷新</a>'
               '<a href="/sso/log/clear">清空记录</a></div>')
    out.append('</body></html>')
    return ''.join(out)


@app.route('/sso/log/clear')
def sso_log_clear():
    """清空自检记录（排查完点一下就好）"""
    try:
        if os.path.exists(SSO_LOG_FILE):
            os.remove(SSO_LOG_FILE)
    except Exception:
        pass
    return redirect('/sso/log')


@app.route('/sso/test')
def sso_test():
    """诊断用：粘一个 access_token 看接口到底回什么（排查对接问题）。
    用法：/sso/test?token=xxxxx"""
    tok = (request.args.get('token') or '').strip()
    if not tok:
        return jsonify(ok=0, msg='用法：/sso/test?token=xxx（从船海智云跳转后的地址栏里拿 access_token）')
    try:
        import chz_push
        enabled = chz_push.is_enabled()
    except Exception as e:
        return jsonify(ok=0, msg=f'chz_push 模块不可用: {e}')
    phone, name, info = _sso_identify(tok)
    return jsonify(ok=1 if phone else 0, enabled=enabled,
                   phone=phone, name=name, raw=info)


@app.before_request
def _sso_gate():
    """V1.16.27：**强制单点登录**的守门人。

    sso_require=true 时，只有被船海智云认证过（_identity_trusted）的会话能碰业务页面；
    没认证的访客一律送去 /sso/entry 看清"该怎么进来"，而不是自己声明一个身份。

    刻意放行的（否则系统会因为几个非业务页而把人都堵在外面）：
      · 静态资源、/sso/* 自身（入口/自检/测试）、/api/session（顶栏要读它显示"未识别"）
      · /my 与 /my/whoami（要能去看自选入口、也要能"忘掉身份"重新走 SSO）
      · 登录/登出等账号口令路径（应急通道：平台出问题时还能用账号密码进来）
    """
    if not SSO_REQUIRE:
        return None
    # 🔴 V1.16.43（用户报障：同事「这个还能访问」+「不用登录就能提交」）：
    #   这里原来有两行豁口 ——
    #     ① `if request.method != 'GET': return None` ⇒ **所有 POST 一律放行**
    #        ⇒ 未认证访客虽然打不开页面，仍能直接 POST /add_batch 提记录、/delete 删记录；
    #     ② `if p.startswith('/api/'): return None` ⇒ **所有接口一律放行**
    #        ⇒ 知道路径就能拉数据（/api/online/list 之类）。
    #   两者叠加＝"门只挡了门面，侧面全开着"。现在只放行 OPTIONS（跨域预检）
    #   与静态资源，其余一切走下面的白名单。
    #   ⚠️ 已认证的人不受影响（上面 `_identity_trusted()` 已放行）；
    #      未认证的人现在连 POST 也会被送到 /sso/entry。
    if request.method == 'OPTIONS':
        return None
    if request.endpoint == 'static':
        return None
    if _identity_trusted():
        return None
    p = request.path or '/'
    # 🔴 V1.16.40（用户口径「连首页一起挡」）：这里原来放行 `/` 与 `/index`，
    #   理由是"让访客看到有哪些业务，而不是白屏"。但公网端口对外开了，
    #   结果任何人直接输网址就能看到门户板块列表 —— 用户要求**连首页都不给看**。
    #   ⇒ 整段删除，未认证访客打开 `/` 也落到 `/sso/entry` 引导页。
    #   ⚠️ 配套改动（必须同时存在，否则应急通道会失效）：
    #   登录框只存在于 index.html / boards.html 的 `#loginModal` 里，
    #   首页一挡就**再也找不到登入口** ⇒ 已在 `/sso/entry` 引导页补了
    #   「管理员账号登录」折叠表单（`_sso_entry` 的 adminLogin 段）。
    # V1.17.3（用户报障「推送后再点，跳转不到详情页」）：详情页 `/detail/<rid>` 是
    #   推送通知（mobile_click_url）的落点 —— 推送链接**不带 access_token**、点开时
    #   手机浏览器/船海智云 webview 里通常也没有本系统的 session cookie，等于一个
    #   「匿名入口」。若把它也拦去 /sso/entry，收到的每一条推送都会点不开详情。
    #   故放行 /detail/ 页（只读展示）。详情页里的写操作（派工/闭环/删除/反馈/审批）
    #   走各自 POST 路由，那些路由有独立鉴权，不受此放行影响；未认证访客看到的
    #   手机号已在 detail 路由里做了掩码，不会借此枚举全厂手机号。
    allow = ('/sso/', '/api/session', '/my', '/login', '/logout',
             '/static/', '/favicon.ico', '/detail/')
    if p.startswith(allow):
        return None
    return redirect('/sso/entry')


@app.before_request
def _online_gate():
    """V1.16.28：**被顶替 / 被超管强制退出**的人，请他重新进。

    为什么必须有这一步：online_sessions 里 kicked=1 只是"记了一笔"，
    真正把人请出去得有人读它 —— 就是这里。每次请求查一次令牌还在不在。

    刻意放行的（否则会出现"提示已退出"页面自己套自己）：
      · 静态资源、/sso/*（重新单点登录的入口）、/api/session、
      · /online/*（踢人页与重新进入的确认页）、/api/kicked（前端读提示）
    只拦 GET 页面：POST 走 API，让它照常返回，由前端提示后刷新。
    """
    if not ONLINE_GUARD:
        return None
    if request.method != 'GET' or request.endpoint == 'static':
        return None
    if not (session.get('my_phone') or '').strip():
        return None                       # 还没认到人，没什么可踢的
    # 🔴 V1.16.36（用户报障「我需要超管一上线就可以用」）：超管豁免在线管控。
    #   被顶替/被踢的人请他重新进 —— 但**超管不适用**：
    #   唯一超管一旦被别处顶掉、或被人点「强制退出」，就会被请出系统，
    #   结果是全厂再没人能进管理页（超管自己也被锁在外面）。这条必须堵。
    if _online_super_exempt(session.get('my_phone') or ''):
        return None
    if not _online_kicked():
        return None
    p = request.path or '/'
    allow = ('/sso/', '/api/session', '/online/', '/api/kicked',
             '/static/', '/favicon.ico')
    if p.startswith(allow):
        return None
    # 把"你是谁 + 被谁踢的"留住，好让 /online/kicked 说清楚；身份本身要清掉，
    # 否则不清的话他手上还留着超管权限（页面虽然被拦，/api/* 是不拦的！）
    _by = ''
    try:
        _by = (session.get('kicked_by') or '').strip()
    except Exception:
        _by = ''
    try:
        session['kicked_phone'] = session.get('my_phone') or ''
        session['kicked_name'] = session.get('my_name') or ''
        session['kicked_by'] = _by
        session.pop('my_phone', None)
        session.pop('my_name', None)
        session.pop('user_phone', None)
        session.pop('id_src', None)
        session.pop('logged_in', None)
        session.pop('user_role', None)
        session.pop('sess_tok', None)     # 换新令牌，下次进来算"新的一端"
    except Exception:
        pass
    logger.warning('会话被强制退出：%s（原因：%s）',
                   session.get('kicked_name') or '', _by or '未知')
    if p.startswith('/api/'):
        return None
    return redirect('/online/kicked')


@app.before_request
def _sync_role_from_db():
    """V1.5.7：每次请求都用数据库里的最新角色刷新 session。

    此前角色只在登录那一刻写进 session，管理员改了某人的权限后，
    对方必须退出重新登录才生效 —— 用户反馈"改了权限一直不更新"。"""
    if request.endpoint == 'static':
        return
    if NO_LOGIN:
        # V1.8：单点登录（或自选身份）已经认到人 + 开关打开 ⇒ 按这个人的真实角色渲染按钮。
        # 这就是"只有调试上级能建看板"的落地方式：不再人人都是超管。
        me = (session.get('my_phone') or '').strip()
        if SSO_ENFORCE and me:
            try:
                u = db.get_user_by_phone(me) or {}
            except Exception:
                u = {}
            # V1.12：认到人、但系统里没有他的账号 → 一律按「普通用户」算。
            # 原来这里回退成 NO_LOGIN_ROLE（=超管），等于任何同事点一下链接就拿到
            # 全部权限（能建板/删板/管用户/改阈值）。唯一超管（王平平）除外。
            role = (u.get('role') or '').strip()
            if role not in ('super_admin', 'debug_sup', 'debug_user'):
                # 🔴 V1.16.31（用户报障「用户管理里王平平进不去」的**真正根因**）：
                #   这一行原来只认 SUPER_ADMIN_PHONE（手机号 17786363880），
                #   于是**用 config.json 里的 admin 账号密码登录的人**，
                #   在这第一次请求就被打回 'debug_user' —— 登录时明明写的 super_admin。
                #   表现：密码完全正确、角色显示超级管理员，但点任何管理页都是「权限不足」，
                #   而且**刷新一次就永久降级**，日志里还留着「用户登录(super_admin)」，极难查。
                #   修法：config 兜底超管与王平平同等对待，两个都保 super_admin。
                if me == SUPER_ADMIN_PHONE or me == ADMIN_USER:
                    role = 'super_admin'
                else:
                    role = 'debug_user'
            # V1.14：超管名单（按部门/按个人授权）命中 ⇒ 就是超管。
            # 这是"超管可以添加其他超管"的落地方式 —— 不写业务库、不建账号，改完即时生效，
            # 也就不会跟 enforce_single_super_admin() 的"唯一超管"规则打架。
            # 业务档优先取「这次请求属于哪一档」（页面决定权限），取不到才退回账号自己的档。
            try:
                _pb = _request_biz() or (u.get('biz') or 'debug')
            except Exception:
                _pb = 'debug'
            if role != 'super_admin':
                try:
                    # V1.16.16：超管名单按业务档分开 —— 安全档 admin_grant / 调试档 debug_admin_grant；
                    #           王平平（固定超管）在两个系统里都是超管。
                    _g = admin_grant if _pb == 'safety' else debug_admin_grant
                    if me == SUPER_ADMIN_PHONE or _g.is_member(me, (u.get('name') or '')):
                        role = 'super_admin'
                except Exception:
                    pass

            # V1.16.17：调试档两级名单（调试上级责任人 / 调试责任人）
            #   ① 只在调试档生效 —— 原来没判档，调试上级名单里的人打开安全隐患看板也变成「上级」；
            #   ② 优先级：个人指定 > 部门授权；同级时「调试责任人」优先（低权限优先）。
            if _pb == 'debug' and me != SUPER_ADMIN_PHONE:
                try:
                    _r2 = _pick_debug_role(me, (u.get('name') or ''))
                except Exception:
                    _r2 = ''
                if _r2 == 'debug_user':
                    role = 'debug_user'      # 名单里明确是「调试责任人」→ 也覆盖账号原有的上级角色
                elif _r2 == 'debug_sup' and role == 'debug_user':
                    role = 'debug_sup'
            if session.get('user_role') != role:
                session['user_role'] = role
            nm = (u.get('name') or session.get('my_name') or '').strip()
            if nm and session.get('user_name') != nm:
                session['user_name'] = nm
            return
        # 免登录且没认到人：页面上的管理入口（新建看板 / 编辑 / 删除…）是按 user_role
        # 渲染的，这里统一把「免登录角色」写进会话，否则那些按钮整块不渲染（表现为"按钮不见了"）。
        if session.get('user_role') != NO_LOGIN_ROLE:
            session['user_role'] = NO_LOGIN_ROLE
        if NO_LOGIN_NAME and session.get('user_name') != NO_LOGIN_NAME:
            session['user_name'] = NO_LOGIN_NAME
        return
    if not session.get('logged_in'):
        return
    phone = session.get('user_phone') or ''
    if not phone or phone == ADMIN_USER:
        return                      # 技术兜底超管，不受数据库约束
    try:
        u = db.get_user_by_phone(phone)
    except Exception:
        return
    if not u:
        session.clear()             # 账号已被删除 → 立即登出，避免残留权限继续操作
        return
    role = (u.get('role') or '').strip()
    if role and role != session.get('user_role'):
        logger.info(f"角色实时刷新 - {phone}: {session.get('user_role')} -> {role}")
        session['user_role'] = role
    name = (u.get('name') or '').strip()
    if name and name != session.get('user_name'):
        session['user_name'] = name


def _current_role() -> str:
    if NO_LOGIN:
        # V1.8：认到人（单点登录/自选身份）且开关打开时，按真实角色
        if SSO_ENFORCE and (session.get('my_phone') or '').strip():
            r = (session.get('user_role') or '').strip()
            if r:
                return r
        # V1.16.22：没认到人时，**最多给到 NO_LOGIN_ROLE**（config 配成 debug_user）。
        # 原来这里回退 super_admin，配合 _is_logged_in() 恒 True，等于「谁打开链接谁是超管」。
        return NO_LOGIN_ROLE
    return session.get('user_role', '')


def _current_phone() -> str:
    return session.get('user_phone', '')


def _role_level() -> int:
    return ROLE_LEVEL.get(_current_role(), 0)


def _role_of_phone(phone: str, biz: str = 'debug') -> int:
    """V1.16.26：**某个别人**在某业务档会算成什么角色，返回其权限等级。

    和 `_current_role()` 的区别：那个读 session（只对"当前这个人"有意义），
    这个是纯函数式地问"换成 phone 这个手机号登录/自选，他在 biz 档会是什么角色"——
    `/my?as=` 判定"目标权限是否高于当前"时要用。

    刻意**不写 session**，纯查询：否则一调用就把当前会话身份冲掉了。
    等级口径与 `_sync_role_from_db()` 的免登录分支保持一致（超管名单 + 调试档两级名单）。
    """
    ph = (phone or '').strip()
    if not ph:
        return 0
    try:
        if ph == SUPER_ADMIN_PHONE:
            return ROLE_LEVEL.get('super_admin', 0)
        role = ''
        try:
            u = db.get_user_by_phone(ph) or {}
            role = (u.get('role') or '').strip()
            if not (u.get('biz') or 'debug') == (biz or 'debug'):
                role = ''          # 账号属于另一块看板 ⇒ 本档不认他的角色
        except Exception:
            u = {}
        if role not in ('super_admin', 'debug_sup', 'debug_user'):
            role = 'debug_user'
        # 超管名单（按档分开）：命中就是超管
        if role != 'super_admin':
            try:
                _g = admin_grant if biz == 'safety' else debug_admin_grant
                if _g.is_member(ph, (u.get('name') or '')):
                    role = 'super_admin'
            except Exception:
                pass
        # 调试档两级名单（只在调试档生效）
        if biz == 'debug' and role != 'super_admin':
            try:
                _r2 = _pick_debug_role(ph, (u.get('name') or ''))
                if _r2 == 'debug_user':
                    role = 'debug_user'
                elif _r2 == 'debug_sup' and role == 'debug_user':
                    role = 'debug_sup'
            except Exception:
                pass
        return ROLE_LEVEL.get(role, 0)
    except Exception:
        return 0


def _can_manage(rid: int) -> bool:
    """闭环与删除权限：管理员可操作所有手尾；调试责任人仅可操作自己提交的手尾

    V1.16.22：比对自己身份一律用 _current_identity_phone()（单点登录/自选身份认到的
    my_phone）。原来用 _current_phone()（=user_phone），免登录下恒为空 —— 结果是
    「普通责任人连自己提的记录都闭环不了」（空串比 submitted_by 也比不过）。

    🔴 V1.16.27 两处加固：
    ① 超管/调试上级这条捷径要求**可信身份**。否则自选身份声明成上级，
       就能闭环/删除**任何**记录（含别人的、别的档的）。
    ② 跨档缺口：原来只看角色、不看记录属于哪一档 ⇒ 调试档上级能闭环安全档的记录
       （实测 can=True）。现按记录所属业务档分别判管理权。"""
    role = _current_role()
    rec = db.get_record(rid)
    if role in ('super_admin', 'debug_sup') and _identity_trusted():
        if role == 'super_admin':
            return True
        # 调试上级只管调试档；安全档的管理权只给安全档超管（与 _can_manage_biz 同口径）
        if rec is None:
            return False
        if _ship_biz_of(rec.get('ship_id')) == 'safety':
            return _biz_admin('safety')
        return True
    if role == 'debug_user':
        rec = db.get_record(rid)
        me = _current_identity_phone()
        if rec and me and rec.get('submitted_by') and rec.get('submitted_by') == me:
            return True
    return False


def _can_add_responsible(rid: int) -> bool:
    """V1.3 需求2：追加责任人的入口权限（四类人）：
    1) 超级管理员  2) 调试上级责任人  3) 发起该收尾的调试责任人  4) 当前调试责任人
    注意：被添加的账号本身权限不受入口影响——所选账号按其自身角色在系统内生效。"""
    if not _is_logged_in():
        return False
    role = _current_role()
    # 🔴 V1.16.27：超管/调试上级这两条必须**可信**（船海智云认证过）。
    #   否则自选身份声明成名单里的上级，就能给任意记录追加责任人。
    if role in ('super_admin', 'debug_sup') and _identity_trusted():
        return True
    rec = db.get_record(rid)
    if not rec:
        return False
    me = _current_identity_phone()      # V1.16.22：免登录下 user_phone 恒空，用认到的身份
    if me and rec.get('submitted_by') and rec.get('submitted_by') == me:
        return True
    # 当前调试责任人：记录 debug_users（6 段台账格式）或 debug_phones 里含当前登录手机号
    if me and me in (str(rec.get('debug_users') or '') + ',' + str(rec.get('debug_phones') or '')):
        return True
    return False


def _login_required_script():
    """未登录时的兜底响应（正常场景前端已拦截，这里防止绕过直接 POST）"""
    return "<script>alert('请先登录！'); window.location.href='/';</script>"


def _permission_denied():
    """未登录 / 权限不足：提示后跳回本业务入口。
    V1.5.3：① 跳转目标按业务档——从 /safety 系列进来的回 /safety，其余回 /（主页）；
             ② 额外输出一段静态兜底（alert 被 WebView/预览面板拦截、或 JS 被禁用时，
                 不至于只看到一片空白）。
    🔴 V1.16.30：分两种情况，别再一律「弹框 + 扔回看板」了 ——
      · **认到人了、只是不是超管** ⇒ 老样子弹框（提示一下就够）；
      · **压根没认到人**（访客，或还没在「我的」里选身份）⇒ 不弹框，
        给一个能点的「去选择我的身份」按钮。
      原因：改完 V1.16.30 之后，本地/内网所有人都是自选身份进来的，
      而这些管理页（用户管理 / 换人审批 / 名单授权）以前一律弹框把人弹走，
      表现就是「打不开」，用户根本不知道自己下一步该干什么。"""
    _to = '/safety' if request.path.startswith('/safety') else '/'
    # 没认到人 ⇒ 走「引导去选身份」那一版
    try:
        _known = bool((session.get('my_phone') or '').strip())
    except Exception:
        _known = False
    if not _known:
        _b = 'safety' if request.path.startswith('/safety') else (
            'debug' if request.path.startswith('/boards') else 'debug')
        return (
            "<div style=\"font-family:system-ui,-apple-system,'Microsoft YaHei',sans-serif;"
            "background:#eef3f8;color:#1f2a37;margin:0;padding:48px 20px;text-align:center;\">"
            "<div style=\"max-width:420px;margin:0 auto;background:#fff;border-radius:16px;"
            "padding:26px 22px;box-shadow:0 8px 30px rgba(10,35,70,.12);\">"
            "<div style=\"font-size:17px;font-weight:700;margin-bottom:10px;\">"
            "还没认到你是谁</div>"
            "<div style=\"font-size:14px;color:#64748b;line-height:1.8;margin-bottom:18px;\">"
            "这个页面要管理权（改名单、审批、在线管理），得先知道你是谁。<br>"
            "先在「我的」里选一次你的身份，再回来就行。</div>"
            "<div style=\"display:flex;gap:10px;justify-content:center;flex-wrap:wrap;\">"
            "<a href=\"/my?pick=1&biz=" + _b + "\" style=\"display:inline-block;background:#14406b;color:#fff;"
            "text-decoration:none;padding:10px 20px;border-radius:9px;font-weight:600;\">"
            "去选择我的身份</a>"
            "<a href=\"" + _to + "\" style=\"display:inline-block;background:#fff;color:#14406b;"
            "border:1px solid #c9d6e6;text-decoration:none;padding:10px 20px;border-radius:9px;"
            "font-weight:600;\">先回主页</a></div></div></div>")
    return (
        "<script>alert('权限不足！'); window.location.href='" + _to + "';</script>"
        "<div style=\"font-family:system-ui,-apple-system,'Microsoft YaHei',sans-serif;"
        "background:#eef3f8;color:#1f2a37;margin:0;padding:48px 20px;text-align:center;\">"
        "<div style=\"max-width:380px;margin:0 auto;background:#fff;border-radius:16px;"
        "padding:26px 22px;box-shadow:0 8px 30px rgba(10,35,70,.12);\">"
        "<div style=\"font-size:17px;font-weight:700;margin-bottom:10px;\">需要更高权限</div>"
        "<div style=\"font-size:14px;color:#64748b;line-height:1.8;margin-bottom:18px;\">"
        "你已经认到人了，但这个页面只有超级管理员能进。<br>"
        "需要的话请找该业务档的超管给你加权限。</div>"
        "<a href=\"" + _to + "\" style=\"display:inline-block;background:#14406b;color:#fff;"
        "text-decoration:none;padding:10px 20px;border-radius:9px;font-weight:600;\">"
        "返回主页</a></div></div>")


@app.route('/api/session')
def api_session():
    """返回当前登录状态与权限信息。
    V1.5.3：带 ?biz=safety 时按安全业务口径返回角色名（超管 / 安全上级 / 安全员）。"""
    _labels = SAFETY_ROLE_LABELS if (request.args.get('biz') == 'safety') else ROLE_LABELS
    _role = _current_role()
    # V1.16.26：原来只读 user_phone（=登录账号手机号），**免登录下恒为空串**——
    #   于是前端顶栏拿不到"我是谁"、测试脚本也没法拿自己的号去试 /my?as=。
    #   免登录模式身份在 my_phone（单点登录/自选身份写的就是它），所以优先用它。
    #   同理姓名：my_name 是本次认到的真名，user_name 可能还是旧的/空的。
    _phone = _current_identity_phone()
    _name = (session.get('my_name') or '').strip() or session.get('user_name', '') \
        or (NO_LOGIN_NAME if NO_LOGIN else '')
    return jsonify(
        logged_in=_is_logged_in(),
        no_login=NO_LOGIN,
        username=_name,
        role=_role,
        role_label=_labels.get(_role, ''),
        phone=_phone,
        # V1.16.26：顶栏要能明确告诉用户"你现在是谁"。
        # 免登录下认到人 = 身份已确定；没认到人才是真正的访客。
        identity_ok=_has_identity(),
        # 🔴 V1.16.27：区分「平台认证过」和「自己声明的」。
        #   trusted=False 时前端要明确提示"你现在只能处理自己的事，改不了管理项"，
        #   否则同事会以为系统坏了 / 或者以为自己权限丢了。
        identity_trusted=_identity_trusted(),
        identity_src=(session.get('id_src') or '').strip(),
        sso_require=SSO_REQUIRE,
        # 🔴 V1.16.28：顶栏要能显示"身份已锁定 / 正在管控在线"。
        #   前端据此把「切换身份」换成「我要代操作 → 提交换人申请」，
        #   否则用户点了才知道自己被锁，容易当成系统故障。
        id_lock=ID_LOCK,
        online_guard=ONLINE_GUARD,
    )


@app.route('/api/chz_users')
def api_chz_users():
    """船海智云通讯录（供发起手尾任务时检索选中人员）：
    优先读本地缓存 chz_users.json，无缓存时实时拉取组织架构。"""
    try:
        import chz_push
        users = chz_push.ensure_users()
        items = [{"name": (u.get("name") or "").strip(),
                  "code": (u.get("code") or "").strip(),
                  "phone": p}
                 for p, u in users.items() if p and (u.get("name") or u.get("code"))]
        items.sort(key=lambda x: (x["name"], x["phone"]))
        return jsonify(ok=True, items=items, total=len(items))
    except Exception as e:
        logger.warning(f"船海智云通讯录读取失败: {e}")
        return jsonify(ok=False, msg=str(e), items=[], total=0), 500


@app.route('/api/can_manage/<int:rid>')
def api_can_manage(rid):
    """检查当前用户是否有权限闭环/删除指定记录"""
    if not _is_logged_in():
        return jsonify(can=False, reason='not_logged_in')
    return jsonify(can=_can_manage(rid), role=_current_role())


@app.route('/login', methods=['POST'])
def login():
    """登录：支持 config.json 超级管理员 或 注册用户（手机号）"""
    data = request.get_json(silent=True) or request.form
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    ip = _client_ip()
    # V1.5.3：业务档要提前取——登录日志按业务档分别留痕（两块看板日志各自独立）
    _login_biz = (data.get('biz') or 'debug')
    if _login_biz not in ('debug', 'safety'):
        _login_biz = 'debug'

    # 0) 防爆破：连续失败达到阈值则临时锁定
    remain = _login_locked(username, ip)
    if remain > 0:
        db.add_login_log(username, ip, False, f'锁定期间尝试登录（剩 {remain}s）', _login_biz)
        mins = remain // 60 + 1
        return jsonify(ok=False, msg=f'登录失败次数过多，账号已临时锁定，请在约 {mins} 分钟后再试'), 423

    # 1) config.json 超级管理员兜底
    if username == ADMIN_USER and hmac.compare_digest(password, ADMIN_PASS):
        session.clear()
        session['logged_in'] = True
        session['user_phone'] = ADMIN_USER
        session['user_name'] = '超级管理员'
        session['user_role'] = 'super_admin'
        # 🔴 V1.16.31（用户报障「用户管理王平平进不去」的第一条修法）：
        #   原来这里只写 user_phone，**不写 my_phone / id_src** ⇒
        #   `_identity_trusted()` 判 `session['my_phone']` 为空直接 False
        #   ⇒ 拿 config 密码正确登录的管理员，**进不去任何管理页**。
        #   密码验证通过本身就是强凭据，凭它认下的身份必须可用。
        session['my_phone'] = ADMIN_USER
        session['my_name'] = '超级管理员'
        session['id_src'] = 'pwd'      # 凭密码认证的可信身份
        if data.get('remember'):
            session.permanent = True
        _login_clear_fail(username, ip)
        db.add_login_log(username, ip, True, '超级管理员登录', _login_biz)
        _register_online(ADMIN_USER, '超级管理员')     # V1.16.28：登录也在线
        logger.info(f"超级管理员登录[{_login_biz}] - {username} ({ip})")
        return jsonify(ok=True, username=biz_text('超级管理员', _login_biz), role='super_admin',
                       role_label=biz_text('超级管理员', _login_biz))

    # 2) 注册用户（手机号登录）
    # V1.5.3：两块看板的用户互不相通——在本业务档注册的账号，只能从本业务档登录。
    # 超管也按业务档隔离（安全业务有自己的超管）；只有王平平这个技术兜底账号可跨业务。
    _biz_word = '安全隐患看板' if _login_biz == 'safety' else '船舶完整性看板'
    user = db.get_user_by_phone(username)
    _is_bootstrap = (username == SUPER_ADMIN_PHONE)
    if user and (user.get('biz') or 'debug') != _login_biz and not _is_bootstrap:
        _other = '安全隐患看板' if (user.get('biz') or 'debug') == 'safety' else '船舶完整性看板'
        db.add_login_log(username, ip, False, f'跨业务登录被拒（账号属于{_other}）', _login_biz)
        return jsonify(ok=False, msg=f'该账号是【{_other}】的用户，请到那个看板登录'), 403
    if not user:
        _login_record_fail(username, ip)
        db.add_login_log(username, ip, False, '账号不存在', _login_biz)
        left = _login_remaining(username, ip)
        if left <= 0:
            return jsonify(ok=False, msg=f'账号或密码错误，已连续失败 {LOGIN_MAX_FAILS} 次，账号临时锁定约 {LOGIN_LOCK_MINUTES} 分钟'), 423
        return jsonify(ok=False, msg=f'账号或密码错误，还可尝试 {left} 次（连续失败 {LOGIN_MAX_FAILS} 次将锁定 {LOGIN_LOCK_MINUTES} 分钟）'), 401
    if user.get('status') != 'approved':
        status_msg = {'pending': '账号待管理员审核，请耐心等待',
                      'rejected': '注册申请已被拒绝，请联系管理员'}
        db.add_login_log(username, ip, False, f"状态异常:{user.get('status')}", _login_biz)
        return jsonify(ok=False, msg=status_msg.get(user['status'], '账号状态异常')), 403
    if not verify_password(password, user.get('password', '')):
        _login_record_fail(username, ip)
        db.add_login_log(username, ip, False, '密码错误', _login_biz)
        left = _login_remaining(username, ip)
        if left <= 0:
            return jsonify(ok=False, msg=f'账号或密码错误，已连续失败 {LOGIN_MAX_FAILS} 次，账号临时锁定约 {LOGIN_LOCK_MINUTES} 分钟'), 423
        return jsonify(ok=False, msg=f'账号或密码错误，还可尝试 {left} 次（连续失败 {LOGIN_MAX_FAILS} 次将锁定 {LOGIN_LOCK_MINUTES} 分钟）'), 401

    session.clear()
    session['logged_in'] = True
    session['user_phone'] = username
    session['user_name'] = user.get('name', '') or username
    session['user_role'] = user.get('role', 'debug_user')
    # 🔴 V1.16.31：同上，**注册用户登录也必须写身份三件套**。
    #   原来只写 user_phone/my_name 不写 ⇒ 手机号+密码正确登录的用户
    #   一律 `_identity_trusted()=False` ⇒ 用户管理、名单授权、阈值全进不去。
    #   这正是用户报「王平平进不去用户管理」的根因（王平平走的就是这条路）。
    session['my_phone'] = username
    session['my_name'] = user.get('name', '') or username
    session['id_src'] = 'pwd'          # 凭密码认证的可信身份
    if data.get('remember'):
        session.permanent = True
    _login_clear_fail(username, ip)
    db.add_login_log(username, ip, True, f"用户登录({session['user_role']})", _login_biz)
    logger.info(f"用户登录[{_login_biz}] - {username} ({session['user_role']}) ({ip})")
    _labels = SAFETY_ROLE_LABELS if _login_biz == 'safety' else ROLE_LABELS
    return jsonify(ok=True, username=session['user_name'], role=session['user_role'],
                   role_label=_labels.get(session['user_role'], ''))


@app.route('/register', methods=['POST'])
def register():
    """V1.46：自助注册已关闭 —— 企业组织架构授权是唯一的人员开通通道。"""
    return jsonify(ok=False, msg=(
        '自助注册已关闭：请联系管理员在「用户管理」里从企业组织架构授权。')), 403

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    logger.info("用户退出登录")
    return jsonify(ok=True)


@app.route('/change_password', methods=['POST'])
def change_password():
    """修改密码：普通用户改数据库；超级管理员改 config.json（内存 + 文件同步更新）"""
    if not _is_logged_in():
        return jsonify(ok=False, msg='请先登录'), 401

    data = request.get_json(silent=True) or request.form
    old_pwd = data.get('old_password') or ''
    new_pwd = data.get('new_password') or ''

    # 新密码强度：≥8 位且必须同时含字母和数字（与注册一致）
    if len(new_pwd) < 8 or not (any(c.isalpha() for c in new_pwd) and any(c.isdigit() for c in new_pwd)):
        return jsonify(ok=False, msg='新密码至少 8 位，且必须同时包含字母和数字'), 400
    if old_pwd == new_pwd:
        return jsonify(ok=False, msg='新密码不能与原密码相同'), 400

    phone = _current_phone()

    # 超级管理员：密码存在 config.json，同步更新内存与文件
    if session.get('user_role') == 'super_admin':
        global ADMIN_PASS
        if not hmac.compare_digest(old_pwd, ADMIN_PASS):
            logger.warning(f"超管修改密码失败 - 原密码错误（IP: {request.remote_addr}）")
            return jsonify(ok=False, msg='原密码错误'), 401
        try:
            cfg_path = os.path.join(BASE_DIR, 'config.json')
            disk_cfg = {}
            if os.path.exists(cfg_path):
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    disk_cfg = json.load(f)
            disk_cfg.setdefault('admin', {})['password'] = new_pwd
            with open(cfg_path, 'w', encoding='utf-8') as f:
                json.dump(disk_cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            return jsonify(ok=False, msg=f'config.json 写入失败：{e}'), 500
        ADMIN_PASS = new_pwd
        CONFIG['admin']['password'] = new_pwd
        logger.info("超级管理员已修改密码（config.json 已同步更新）")
        return jsonify(ok=True, msg='密码修改成功，下次登录请使用新密码')

    # 普通注册用户：校验原密码后更新数据库
    user = db.get_user_by_phone(phone)
    if not user:
        return jsonify(ok=False, msg='账号不存在'), 404
    if not verify_password(old_pwd, user.get('password', '')):
        logger.warning(f"用户修改密码失败 - 原密码错误（{phone}）")
        return jsonify(ok=False, msg='原密码错误'), 401
    db.update_user_password(phone, hash_password(new_pwd))
    logger.info(f"用户修改密码成功 - {phone}")
    return jsonify(ok=True, msg='密码修改成功，下次登录请使用新密码')


@app.route('/add_batch', methods=['POST'])
def add_batch():
    if not _is_logged_in():
        return _login_required_script()

    ship_id = int(request.form.get('ship_id') or 1)
    ship = db.get_ship(ship_id)
    if not ship:
        # 🔴 V1.16.36：异常兜底回总界面（原来跳 /boards，用户报障 bug 1）
        return "<script>alert('看板不存在，请刷新后重试'); window.location.href='/';</script>"

    sys_name = request.form.get('sys_name')
    # V1.18.4：5S 不是隐患，不设隐患类别 —— 类别固定记为「5S」（前端已隐藏类别字段）
    _rec_type_raw = (request.form.get('rec_type') or '').strip() or '隐患整改'
    # V1.5.3：安全业务（安全隐患看板）的「隐患类别」只能从本看板的清单里选，
    # 不允许像调试业务那样现场自造一个新系统名（用户明确要求"只能从本清单中选择"）。
    if (ship or {}).get('biz') == 'safety':
        if _rec_type_raw == '5S':
            sys_name = '5S'
        else:
            _cats = db.get_ship_systems(ship_id)
            _sel = (sys_name or '').strip()
            if not _sel:
                return rec_text(
                    "<script>alert('请选择隐患类别（发起隐患时必须从本清单中选择）');"
                    " history.back();</script>", ship_id)
            if _sel not in _cats:
                return rec_text(
                    "<script>alert('隐患类别「%s」不在本看板清单中，请从下拉列表中选择；"
                    "如需新增类别，请先到「隐患类别维护」里添加。'); history.back();</script>"
                    % _sel, ship_id)
    # 调试责任人：界面默认预填当前登录人，也允许手动修改/增加其他责任人；
    # 2 段格式（船海智云）→ 归一化为完整 6 段台账格式（含作业区/部门信息）
    debug_users_form = ",".join([p.strip() for p in request.form.getlist('debug_users') if p.strip()])
    if debug_users_form:
        debug_users = _normalize_staff_value(debug_users_form)
    else:
        # 兜底：表单未带调试责任人时仍固定为当前登录人
        # V1.16.22：用「认到的身份」，免登录下 _current_phone() 恒空 ⇒ 兜底成空字符串
        _me_ph2 = _current_identity_phone()
        staff = db.find_staff_by_phone(_me_ph2)
        if staff:
            debug_users = " - ".join([x for x in [
                staff.get('公司', ''), staff.get('一级部门', ''), staff.get('二级部门', ''),
                staff.get('作业区', ''), staff.get('姓名', ''), staff.get('电话', '')] if x])
        else:
            debug_users = f"{session.get('user_name', '')} - {_me_ph2}"
    debug_sup = ",".join([p.strip() for p in request.form.getlist('debug_sup_users') if p.strip()])
    area_leaders = ",".join([p.strip() for p in request.form.getlist('area_phones') if p.strip()])
    dept_leaders = ",".join([p.strip() for p in request.form.getlist('dept_phones') if p.strip()])
    # 归一化人员值：2段格式（船海智云）→ 查台账补全为6段（含作业区/部门信息）
    debug_sup = _normalize_staff_value(debug_sup)
    area_leaders = _normalize_staff_value(area_leaders)
    dept_leaders = _normalize_staff_value(dept_leaders)
    # V1.11：安全隐患看板不再设「部门长」——责任单位领导改为「同时抄送」
    _is_safety_board = ((ship or {}).get('biz') or 'debug') == 'safety'
    if _is_safety_board:
        dept_leaders = ''      # 表单里已按业务档移除该块，这里兜底清空
    else:
        # V1.4.4 需求3：部门长（三级）为必填，后端同样拦截（防止跳过前端直接 POST）
        if not _extract_phones(dept_leaders):
            return ("<script>alert('部门长为必填项：请在「三级 · 部门长选择」中至少选一位部门长后再提交');"
                    " history.back();</script>")
        # V1.18.1：责任单位领导（主任/作业长）为必填（仅调试看板；安全隐患看板该字段改抄送，不强制）
        if not _extract_phones(area_leaders):
            return ("<script>alert('责任单位领导（主任/作业长）为必填项：请在「责任单位领导」中至少选一位后再提交');"
                    " history.back();</script>")
    # V1.2 需求2：追加抄送人（前端选人组件提交 JSON [{name, phone}, ...]）
    extra_users = _parse_extra_users(request.form.get('extra_users') or '')
    # V1.11：安全隐患看板的「责任单位领导」不参与分级，改成发起时同时抄送
    #   —— 并入抄送人列表（抄送人本身参与各级推送），职责上仍记在 area_leader 字段
    if _is_safety_board and area_leaders:
        try:
            _cc = json.loads(extra_users) if extra_users else []
        except Exception:
            _cc = []
        if not isinstance(_cc, list):
            _cc = []
        _has_cc = {str(x.get('phone') or '') for x in _cc if isinstance(x, dict)}
        _lead_name = extract_name(area_leaders) or ''
        for _p in _extract_phones(area_leaders):
            if _p not in _has_cc:
                _cc.append({'name': _lead_name or _p, 'phone': _p})
        extra_users = json.dumps(_cc, ensure_ascii=False)
    extra_phones = _extract_phones(extra_users)   # 抄送人手机号，参与发起即时推送

    # V1.6 A2：记录类型（隐患整改 / 违章处罚 / 5S）与审批人清单
    rec_type = (request.form.get('rec_type') or '').strip() or '隐患整改'
    if rec_type not in REC_TYPES:
        rec_type = '隐患整改'
    approvers = _parse_extra_users(request.form.get('approvers') or '')
    # V1.18：违章处罚可由提交人预填罚款金额（审核人审批时可修改，不填则沿用发起时的金额）
    fine_in = (request.form.get('fine_amount_in') or '').strip()
    if rec_type == '违章处罚' and not approvers:
        return ("<script>alert('违章处罚必须指定至少一位审批人（可从通讯录中选择）');"
                " history.back();</script>")
    # V1.17.0：安全隐患看板的审批人只能是安全超管（固定超管 + admin_grant 名单）
    if _is_safety_board and approvers:
        _bad_appr = _reject_non_approver(approvers)
        if _bad_appr:
            return ("<script>alert('审批人「%s」不是安全超管。安全隐患看板的审批人只能是"
                    "安全超管，请重新选择。'); history.back();</script>"
                    % _bad_appr)

    # V1.16：安全隐患看板可以手填「隐患提交时间」（默认现在），并可填「整改截止时间」
    def _v16_dt(val):
        v = (val or '').strip().replace('T', ' ')
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
            try:
                return datetime.strptime(v, fmt)
            except Exception:
                continue
        return None

    submit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _in_sub = _v16_dt(request.form.get('submit_time_in'))
    if _in_sub:
        submit_time = _in_sub.strftime("%Y-%m-%d %H:%M:%S")
    _in_dl = _v16_dt(request.form.get('deadline_in'))
    _deadline_val = _in_dl.strftime("%Y-%m-%d %H:%M:%S") if _in_dl else ''

    # V1.16.22：必须用「认到的身份」而不是登录态。免登录下 _current_phone()（=user_phone）
    # 恒为空 ⇒ 新记录 submitted_by 全丢 ⇒ ①发起人无从追溯 ②普通责任人闭环自己提的记录时
    # _can_manage 比不到人。库里 22 条 submitted_by 为空就是这个原因。
    submitter_phone = _current_identity_phone()
    site_url = get_site_url()   # 发起即时推送的详情页链接前缀

    # 快速选作业区：显式记录所选作业区/部门名称（不再依赖台账反查）
    area_quick = (request.form.get('area_quick') or '').strip()
    custom_area = (request.form.get('custom_area_name') or '').strip()
    custom_dept = (request.form.get('custom_dept_name') or '').strip()
    if area_quick == '__other__':
        record_area_name = custom_area
    else:
        record_area_name = area_quick
    record_dept_name = custom_dept if area_quick == '__other__' else ''

    # V1.17.1：场地位置 —— 安全隐患看板的「场地」类看板提交隐患时，必选发生场地
    place_name = (request.form.get('place_name') or '').strip()
    if _is_safety_board and _is_place_board(ship):
        if not place_name:
            return ("<script>alert('场地位置为必填项：请选择隐患发生的具体场地（码头 / 船台 / 场地）');"
                    " history.back();</script>")
        if place_name not in _PLACE_NAMES and place_name not in _PLACE_NAMES_LEGACY:
            return ("<script>alert('场地位置「%s」不在固定场地清单中，请从下拉列表中选择');"
                    " history.back();</script>" % place_name)

    indices = {k.split('_')[1] for k in request.form.keys() if k.startswith('desc_')}

    # V1.4.5 防重复：同一提交人 60s 内相同表单内容重复 POST → 拦截（防手机端重复点击多建手尾）
    fp_text = sorted(f"{k}={v}" for k, v in request.form.items())
    fp = _idem_fingerprint(_actor_key(), 'add_batch', "\n".join(fp_text))
    if not _idem_allow(fp):
        return _dup_blocked_html(url_for('safety_kanban', ship_id=ship_id) if _is_safety_board
                                 else url_for('board', ship_id=ship_id))

    # 先整体校验所有手尾项（照片必填），全部通过后再统一写入，
    # 避免中途拦截造成"前面的项已入库、后面的项缺失"的不一致
    for idx in sorted(indices, key=int):
        photos = [p for p in request.files.getlist(f'photos_{idx}[]') if p and p.filename]
        if not photos:
            return rec_text(
                "<script>alert('第 %s 个手尾项未上传手尾照片，手尾照片为必填项，"
                "请返回补传后重新提交'); history.back();</script>" % (int(idx) + 1),
                ship_id)

    count = 0
    push_tasks = []   # V1.23：收集异步推送任务 (消息, 手机号, webhook, 详情链接, rid)
    for idx in sorted(indices, key=int):
        desc = request.form.get(f'desc_{idx}')
        install_user = _normalize_staff_value(request.form.get(f'install_user_{idx}') or '')
        # 每行手尾项的作业区（前端该行"责任作业区"下拉）：
        # 船/机/电/设计/物资/搭载/涂装 → 直接作为本条记录的作业区；未选 → 从所选人员/作业区领导的6段格式提取
        item_area = (request.form.get(f'item_area_{idx}') or '').strip()
        # V1.37：责任单位只允许固定单位名单；非法值（人名 / 外号 / 绕过页面直接 POST）
        # 不写进 area_name，改走下面原有的兜底提取，但不阻断提交。
        if item_area and item_area not in _AREA_UNITS:
            item_area = ''
        if item_area:
            rec_area_name = item_area
        else:
            rec_area_name = (extract_area(install_user or '') or
                             extract_area(area_leaders or '') or record_area_name)
        photos = [p for p in request.files.getlist(f'photos_{idx}[]') if p and p.filename]
        photo_paths = []
        for p in photos:
            fn = f"req_{datetime.now().strftime('%Y%m%d%H%M%S')}_{idx}_{os.path.basename(p.filename)}"
            p.save(os.path.join(app.config['UPLOAD_FOLDER'], fn))
            photo_paths.append(fn)

        rid = db.add_record(
            sys_name=sys_name,
            description=desc,
            debug_users=debug_users,
            debug_phones="",          # 电话不单列，包含在6段台账格式中
            debug_sup=debug_sup,
            install_user=install_user,
            area_leader=area_leaders,
            dept_leader=dept_leaders,
            submit_time=submit_time,
            photo_path=",".join(photo_paths),
            submitted_by=submitter_phone,
            ship_id=ship_id,
            area_name=rec_area_name,
            dept_name=record_dept_name,
            extra_users=extra_users,
            place_name=place_name,
        )
        count += 1
        # V1.6 A2：写记录类型；违章处罚进入「审批中」并记录审批人快照（任一人通过即生效）
        _appr_fields = {'rec_type': rec_type}
        if rec_type == '违章处罚':
            _appr_fields['approval_status'] = '审批中'
            # V1.18：提交人预填的罚款金额随发起入库，审批人可改
            if fine_in:
                _appr_fields['fine_amount'] = fine_in
            # approvers 已是 JSON 文本（_parse_extra_users 的返回值），直接存，
            # 否则会二次编码，_approvers_of 解析出来是 str 而不是 list，审批人永远匹配不上
            _appr_fields['approvers'] = approvers
        if _deadline_val:
            _appr_fields['deadline'] = _deadline_val
            _appr_fields['deadline_push'] = ''
        db.update_record_fields(rid, _appr_fields)

        # ★ 发起即推送（新收尾提出通知）：推给调试责任人 + 安装责任人 + 全部调试上级责任人。
        # 先置推送标记（notify_level=1 / push_tag=sent）再发送，
        # 与 monitor 巡检轮询完全隔离，杜绝同一记录的重复 L0 推送；
        # 后续 24h/48h 超时梯度提醒不受影响（notify_level=1 < 2 仍会正常升级）。
        # V1.11：安全隐患看板「没有分级提醒」——发起即推一次（并抄送责任单位领导），
        #        不再按 24h/48h 逐级升级；等级直接置顶，monitor 的 L1/L2 分支因此不会触发。
        #        （整改反馈通知仍照常推送，见 monitor 的 feedback_pending 分支）
        # V1.23：推送改为异步——notify_level/push_tag 必须同步落库（防 monitor 巡检重复推），
        #        网络发送收集到 push_tasks 后由后台线程统一发，提交秒回不再等船海智云接口。
        db.set_notify_level(rid, 3 if _is_safety_board else 1)
        db.set_push_tag(rid, "sent")
        _durl = detail_url(rid, ship_id)
        _biz = ship.get('biz') or 'debug'
        install_name = extract_name(install_user) or '责任单位负责人'
        # V1.11：安全隐患看板不设部门长、责任单位领导改为「同时抄送」，
        #        推送正文里点明一句，收件人一眼知道这事谁还看得到。
        # V1.14：安全档发起页不再有「责任单位领导」这一栏，抄送提示随之去掉
        _cc_line = ''
        l0_msg = (
            f"🆕【新收尾提出通知】\n"
            f"系统：{sys_name}\n"
            f"描述：{desc}\n"
            f"请责任单位负责人【{install_name}】尽快查看照片并整改反馈！\n"
            f"{_cc_line}"
            f"👉 点击或复制下方链接查看该收尾详情并上传整改照片：\n"
            f"{_durl}"
        )
        l0_phones = (_extract_phones(debug_users) + _extract_phones(install_user)
                     + _extract_phones(debug_sup) + extra_phones)
        push_tasks.append((biz_text(l0_msg, _biz), l0_phones,
                           ship.get("webhook") or "", _durl, rid))
        # V1.17.0：违章处罚发起时给审批人推一条「待你审批」通知（带详情链接）。
        # V1.70：按用户要求改成「发起审批后提醒一遍所有人」——
        #   责任人 / 安全员 / 安全上级 / 发起人 / 指定审批人 / 追加抄送人，去重后一次推送；
        #   审批人也在这条名单里，正文仍点明「待你审批」这个待办。
        if rec_type == '违章处罚':
            _all_at_submit = _approval_all_phones(
                debug_users, install_user, debug_sup, submitter_phone,
                approvers, extra_users)
            if _all_at_submit:
                _appr_msg = (
                    f"📋【违章处罚待审批 · 全员提醒】\n"
                    f"隐患类别：{sys_name}\n"
                    f"描述：{desc}\n"
                    f"请指定审批人尽快处理（待你审批），任一人通过即生效；其他相关人员请知悉。\n"
                    f"👉 点击或复制下方链接查看隐患详情并审批：\n"
                    f"{_durl}"
                )
                push_tasks.append((biz_text(_appr_msg, _biz), _all_at_submit,
                                   ship.get("webhook") or "", _durl, rid))

    logger.info(f"新增收尾任务 - 看板: {ship.get('name')}(#{ship_id}), 系统: {sys_name}, 项数: {count}, 提交人: {submitter_phone}")
    # V1.23：后台线程异步发推送，提交立刻返回，不再等船海智云接口（批量提交也不会超时）
    if push_tasks:
        def _fire_push():
            for _c, _ph, _wh, _u, _rid in push_tasks:
                try:
                    send_push(_c, _ph, _wh, mobile_click_url=_u, rid=_rid)
                except Exception as e:
                    logger.warning(f"发起即时推送失败(不影响保存): {e}")
        threading.Thread(target=_fire_push, daemon=True).start()
    # V1.23：按业务档跳回正确的看板（安全档回安全隐患看板，不再误跳船舶完整性看板）
    return redirect(url_for('safety_kanban', ship_id=ship_id) if _is_safety_board
                    else url_for('board', ship_id=ship_id))


@app.route('/feedback', methods=['POST'])
def feedback():
    """反馈无需登录，任何人收到推送链接均可反馈"""
    idx = int(request.form.get('row_idx'))
    new_desc = request.form.get('fb_desc')

    if not (new_desc or '').strip():
        return ("<script>alert('请填写反馈备注'); history.back();</script>")

    rec = db.get_record(idx)
    _board_url = url_for('board', ship_id=rec.get('ship_id') or 1) if rec else url_for('boards')
    # V1.4.5 防重复：同一人 60s 内对同一手尾的相同反馈重复提交 → 拦截
    fp = _idem_fingerprint(_actor_key(), 'feedback', idx, (new_desc or '').strip())
    if not _idem_allow(fp):
        return _dup_blocked_html(_board_url)

    photos = [p for p in request.files.getlist('fb_photos[]') if p and p.filename]
    if not photos:
        return ("<script>alert('反馈照片为必填项，请至少上传一张整改后的现场照片');"
                " history.back();</script>")

    photo_paths = []
    for p in photos:
        fn = f"fb_{datetime.now().strftime('%Y%m%d%H%M%S')}_{os.path.basename(p.filename)}"
        p.save(os.path.join(app.config['UPLOAD_FOLDER'], fn))
        photo_paths.append(fn)

    db.feedback(idx, new_desc, ",".join(photo_paths), datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info(f"反馈更新 - 记录ID: {idx}")
    return redirect(_board_url)


@app.route('/dispatch', methods=['POST'])
def dispatch_task():
    """安装责任人派工：把收尾任务派给安装个人，并立即推送通知给该安装个人（可点击链接反馈）。
    与反馈一致，不强制登录（收到推送链接即可派工）。"""
    rid = int(request.form.get('row_idx') or 0)
    dispatch_user = (request.form.get('dispatch_user') or '').strip()   # 完整 6 段台账格式
    dispatch_note = (request.form.get('dispatch_note') or '').strip()   # 派工备注（可选）

    rec = db.get_record(rid)
    if not rec:
        # 🔴 V1.16.36：异常兜底回总界面（原来跳 /boards，用户报障 bug 1）
        return "<script>alert('记录不存在'); window.location.href='/';</script>"
    if rec.get('status') in ('已闭环', '已完成'):
        return rec_text("<script>alert('该手尾已闭环，无需派工'); history.back();</script>",
                        rec.get('ship_id'))
    if not dispatch_user:
        return "<script>alert('请选择要派工的安装个人'); history.back();</script>"

    # 提取派工对象的手机号（11 位）
    phones = _extract_phones(dispatch_user)
    dispatch_phone = phones[0] if phones else ""

    # V1.4.5 防重复：同一人 60s 内对同一手尾派给同一人重复提交 → 拦截（防重复派工/重复推送）
    fp = _idem_fingerprint(_actor_key(), 'dispatch', rid, dispatch_phone)
    if not _idem_allow(fp):
        return _dup_blocked_html(url_for('board', ship_id=rec.get('ship_id') or 1))

    # 派工人：优先当前登录人，未登录时从表单带过来（隐藏域已注入安装责任人姓名）
    dispatch_by = (request.form.get('dispatch_by') or '').strip() or session.get('user_name', '') or '责任单位负责人'
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    db.dispatch(rid, dispatch_user, dispatch_phone, dispatch_by, dispatch_note, now_str)
    logger.info(f"派工 - 记录ID: {rid}, 派给: {dispatch_user}, 派工人: {dispatch_by}")

    # 立即推送通知给安装个人（带详情页链接，可点击反馈），并抄送全部调试上级责任人
    ship = db.get_ship(int(rec.get('ship_id') or 1))
    site_url = get_site_url()
    _durl = detail_url(rid, rec.get('ship_id'))
    content = (
        f"🔧【收尾派工通知】\n"
        f"系统：{rec.get('sys_name') or ''}\n"
        f"描述：{(rec.get('description') or '')[:80]}\n"
        f"派工人：{dispatch_by}\n"
        + (f"派工备注：{dispatch_note}\n" if dispatch_note else "")
        + f"请您（安装个人）尽快查看详情并整改反馈！\n"
        f"👉 点击或复制下方链接查看该收尾详情并上传整改照片：\n"
        f"{_durl}"
    )
    try:
        # 收件人：安装个人 + 全部调试上级责任人（所有推送均抄送调试上级）
        # + V1.2 抄送人（发起时追加的抄送人同样收到派工通知）
        sup_phones = _extract_phones(rec.get('debug_sup'))
        extra_phones = _extract_phones(rec.get('extra_users'))
        send_push(rec_text(content, rec.get('ship_id')),
                  [dispatch_phone] + sup_phones + extra_phones,
                  (ship or {}).get("webhook") or "",
                  mobile_click_url=_durl, rid=rid)
    except Exception as e:
        logger.warning(f"派工推送失败(不影响派工保存): {e}")

    return redirect(url_for('board', ship_id=rec.get('ship_id') or 1))


@app.route('/close', methods=['POST'])
def close():
    if not _is_logged_in():
        return _login_required_script()
    idx = int(request.form.get('row_idx'))
    if not _can_manage(idx):
        return _permission_denied()
    rec = db.get_record(idx)
    _board_url = url_for('board', ship_id=rec.get('ship_id') or 1) if rec else url_for('boards')
    # V1.4.5 防重复：同一人 60s 内对同一手尾重复闭环 → 拦截
    fp = _idem_fingerprint(_actor_key(), 'close', idx)
    if not _idem_allow(fp):
        return _dup_blocked_html(_board_url)
    db.close(idx, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info(f"任务闭环 - 记录ID: {idx}, 操作人: {_current_phone()}")
    return redirect(_board_url)


@app.route('/delete', methods=['POST'])
def delete_task():
    if not _is_logged_in():
        return _login_required_script()
    idx = int(request.form.get('row_idx'))
    rec = db.get_record(idx)
    if not _can_manage(idx):
        return _permission_denied()
    _board_url = url_for('board', ship_id=rec.get('ship_id') or 1) if rec else url_for('boards')
    # V1.4.5 防重复：同一人 60s 内对同一手尾重复删除 → 拦截
    fp = _idem_fingerprint(_actor_key(), 'delete', idx)
    if not _idem_allow(fp):
        return _dup_blocked_html(_board_url)
    db.delete_record(idx)
    logger.info(f"删除记录 - 记录ID: {idx}, 操作人: {_current_phone()}")
    return redirect(_board_url)


# ============ 用户管理（仅超级管理员） ============

# ============ V1.13 审核权设置（超管自己选：整个部门/科室 或 某个人） ============

def _org_top_id():
    """组织架构的根（公司）部门 id —— 部门浏览器从这里开始"""
    try:
        import chz_org as org
        st = org.load()
        for d in st['by_id'].values():
            if d['n'] == '中船黄埔文冲船舶有限公司':
                return d['i']
        rs = org.roots()
        return rs[0]['id'] if rs else ''
    except Exception:
        return ''


def _can_edit_review() -> bool:
    """能不能改审核权配置：安全档的管理权 = 只有超管"""
    return _can_manage_biz('safety')


def _can_score_sixs() -> bool:
    """V1.16：能不能打分（产品船 6S 评分）—— 超管 + 有审核权的人；其他人只能看历史。

    🔴 V1.16.36（用户口径：「普通权限……可以写产品船6S表」）：
    原来只要是超管或安全档审核人能打分，科室下面的人打开 6S 表只能看不能填。
    现在**认到人就能打分** —— 6S 评分是业务动作不是审批动作，
    不该拿管理权去卡；真正的写操作仍受 `_is_logged_in()` 与提交接口自身校验保护。
    """
    if _can_manage_biz('safety'):
        return True
    try:
        if _is_safety_reviewer():
            return True
    except Exception:
        pass
    # V1.16.36：认到人（SSO / 密码登录 / 自选身份）即可打分
    return bool(_has_identity())


def _sixs_row_of_ship(ship_no: str, week_start: str = None):
    """同一条船在【某一周】是不是已经有评分（返回那一行 dict，没有返回 None）
    V1.51：从「一船一份」放开为「一船一周一份」。历史记录没有 week_start，
    用 ctime 派生所在周，所以旧数据不用回填也能正确归类。"""
    try:
        conn = db.get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM sixs WHERE ship_no=" + _sixs_ph(), (ship_no,))
            for r in cur.fetchall():
                row = dict(r)
                if not week_start or _sixs_week_of(row) == week_start:
                    return row
            return None
        finally:
            conn.close()
    except Exception:
        return None


def _sixs_can_del(row) -> bool:
    """V1.16.7：一条 6S 评分记录谁能删 —— 只有【打分人本人】和【固定超管】"""
    if not _can_score_sixs():
        return False
    creator = ((row or {}).get('creator') or '').strip()
    me = (_current_identity_phone() or '').strip()
    if not creator and not me:
        # 免登录且没认出身份（本地直接开浏览器就是这种）→ 沿用旧行为，别把人卡住
        return True
    if me and me == SUPER_ADMIN_PHONE:
        return True
    return bool(me) and me == creator


@app.route('/admin/reviewers')
def admin_reviewers():
    """V1.15：审核权设置已并入「用户管理」页，老链接直接跳过去（保留书签可用）"""
    if not _is_logged_in() or not _can_edit_review():
        return _permission_denied()
    return redirect('/admin/users?biz=safety')


@app.route('/admin/reviewers_old')
def admin_reviewers_old():
    """审核权设置页（旧版，已废弃不用）"""
    if not _is_logged_in() or not _can_edit_review():
        return _permission_denied()
    s = safety_review.summary()
    try:
        ad = admin_grant.summary()
    except Exception:
        ad = {'count': 0, 'depts': [], 'people': []}
    org_saved = ''
    try:
        import chz_org as org
        org_saved = org.load().get('saved_at') or ''
    except Exception:
        pass
    return render_biz('admin_reviewers.html', 'safety',
                      count=s['count'], dept_rows=s['depts'], people_rows=s['people'],
                      admin_count=ad['count'], admin_dept_rows=ad['depts'],
                      admin_people_rows=ad['people'],
                      super_name=SUPER_ADMIN_NAME, super_phone=SUPER_ADMIN_PHONE,
                      org_saved_at=org_saved, saved_at=s.get('saved_at') or '',
                      user_role=session.get('user_role', ''),
                      user_name=session.get('user_name', ''))


@app.route('/api/org/depts')
def api_org_depts():
    """某部门的直接下级部门（带人数）。parent 为空 = 从公司根开始"""
    if not _is_logged_in():
        return jsonify(ok=False, msg='权限不足'), 403
    import chz_org as org
    if not org.ready():
        return jsonify(ok=True, items=[], crumbs=[], msg='组织架构缓存为空，请先点「重新拉取」')
    pid = (request.args.get('parent') or '').strip() or _org_top_id()
    return jsonify(ok=True, items=org.children(pid), crumbs=org.path_to(pid))


@app.route('/api/org/people')
def api_org_people():
    """某部门（默认含下级）的人员"""
    if not _is_logged_in():
        return jsonify(ok=False, msg='权限不足'), 403
    import chz_org as org
    if not org.ready():
        return jsonify(ok=True, items=[])
    did = (request.args.get('dept') or '').strip()
    kw = (request.args.get('kw') or '').strip()
    try:
        limit = int(request.args.get('limit') or 300)
    except (TypeError, ValueError):
        limit = 300
    return jsonify(ok=True, items=org.people(did, kw, limit=min(limit, 500)))


@app.route('/api/org/dept')
def api_org_dept():
    """单个部门信息（把它设为「审核部门」时用）"""
    if not _is_logged_in():
        return jsonify(ok=False, msg='权限不足'), 403
    import chz_org as org
    did = (request.args.get('id') or '').strip()
    d = org.dept(did)
    if not d:
        return jsonify(ok=False, msg='部门不存在')
    return jsonify(ok=True, item={'id': did, 'name': d['n'], 'short': org.short_full(d['f']),
                                  'full': org.short_full(d['f']),
                                  'total': org.count_people(did),
                                  'direct': org.load()['direct'].get(did, 0)})


@app.route('/api/org/search')
def api_org_search():
    """按关键字同时搜部门和人员（选人时用）"""
    if not _is_logged_in():
        return jsonify(ok=False, msg='权限不足'), 403
    import chz_org as org
    kw = (request.args.get('kw') or '').strip()
    if not kw or not org.ready():
        return jsonify(ok=True, depts=[], people=[])
    return jsonify(ok=True, depts=org.search_depts(kw)[:20], people=org.people('', kw, limit=40))


@app.route('/api/org/refresh', methods=['POST'])
def api_org_refresh():
    """重新拉取船海智云组织架构（超管；约十几秒）"""
    if not _is_logged_in() or not (_can_manage_biz('safety') or _can_manage_biz('debug')):
        return jsonify(ok=False, msg='权限不足'), 403
    try:
        import chz_org as org
        st = org.refresh(verbose=False)
        return jsonify(ok=True, depts=len(st['by_id']), users=st['n_users'])
    except Exception as e:
        logger.warning(f'刷新组织架构失败: {e}')
        return jsonify(ok=False, msg=str(e)[:200])


@app.route('/api/review/save', methods=['POST'])
def api_review_save():
    """保存审核权配置（按部门 + 按个人），保存后立即生效"""
    data = request.get_json(silent=True) or {}
    # V1.16.2：调试页保存的是「调试上级责任人」名单 —— 权限按页面的业务档判：
    #   安全档 = 只有超管（原口径不变）；调试档 = 超管 + 调试上级责任人
    _bz = (data.get('biz') or 'safety')
    if _bz not in ('debug', 'safety'):
        _bz = 'safety'
    # V1.16.17：四份名单（超管 / 调试上级责任人 / 调试责任人 / 审核人）**全部只有超管能改** ——
    #   「都由王平平添加」。原来这里放的是 _can_manage_biz（调试上级也能进），一并收紧。
    if not _is_logged_in() or not _biz_admin(_bz):
        return jsonify(ok=False, msg='只有超管可以改鉴权名单'), 403
    depts = []
    for x in (data.get('depts') or []):
        if not isinstance(x, dict):
            continue
        path = (x.get('path') or '').strip()
        did = (x.get('id') or '').strip()
        if not path and not did:
            continue
        depts.append({'path': path, 'id': did, 'deep': bool(x.get('deep', True))})
    people = []
    for x in (data.get('people') or []):
        if not isinstance(x, dict):
            continue
        ph = ''.join(ch for ch in str(x.get('phone') or '') if ch.isdigit())
        nm = (x.get('name') or '').strip()
        if not ph and not nm:
            continue
        people.append({'name': nm, 'phone': ph, 'dept': (x.get('dept') or '').strip()})
    # V1.14：同一页上还能配「超管名单」（按部门 / 按个人）
    def _clean(seq):
        out = []
        for x in (seq or []):
            if not isinstance(x, dict):
                continue
            path = (x.get('path') or '').strip()
            did = (x.get('id') or '').strip()
            ph = ''.join(ch for ch in str(x.get('phone') or '') if ch.isdigit())
            nm = (x.get('name') or '').strip()
            if ph or nm:
                out.append({'name': nm, 'phone': ph, 'dept': (x.get('dept') or '').strip()})
            elif path or did:
                out.append({'path': path, 'id': did, 'deep': bool(x.get('deep', True))})
        return out

    ad_depts = [x for x in _clean(data.get('admin_depts')) if x.get('path') or x.get('id')]
    ad_people = [x for x in _clean(data.get('admin_people')) if x.get('phone') or x.get('name')]
    # V1.16.17：固定超管（王平平）只存在于「内置」名单里，不写进配置文件 —— 页面上他的条目带
    #   fixed 标记一起提交，这里统一剔除，免得每次保存都往 *_grant_config.json 里塞一条冗余记录。
    ad_people = [x for x in ad_people
                 if (x.get('phone') or '').strip() != SUPER_ADMIN_PHONE]
    # V1.16.15：单个超管改到「用户管理」最下方表格里逐人管（/admin/grant_add|grant_remove）。
    #   所以超管卡片现在只提交 admin_depts → 这里**没带的键保持原值，不覆盖**，
    #   否则「部门授权」一保存就会把表格里设的个人超管整批清空。
    if 'admin_depts' not in data:
        ad_depts = None
    if 'admin_people' not in data:
        ad_people = None

    # ⚠️ 只有请求里**真的带了**这个键才覆盖，避免「只保存超管」时把审核权名单清空
    #    （前端一次提交两份名单，正常都带；这样兜住异常/半截请求）
    sg_depts = [x for x in _clean(data.get('sup_depts')) if x.get('path') or x.get('id')]
    sg_people = [x for x in _clean(data.get('sup_people')) if x.get('phone') or x.get('name')]
    # V1.16.17：调试档第三份名单 —— 调试责任人（个人指定 > 部门授权）
    us_depts = [x for x in _clean(data.get('user_depts')) if x.get('path') or x.get('id')]
    us_people = [x for x in _clean(data.get('user_people')) if x.get('phone') or x.get('name')]

    # ⚠️ 按业务档只收自己那份键 —— 调试档的人即使自己拼请求，也改不到安全档名单
    try:
        if _bz == 'safety':
            if 'depts' in data or 'people' in data:
                safety_review.save_config(depts=depts, people=people)
            if 'admin_depts' in data or 'admin_people' in data:
                admin_grant.save_config(depts=ad_depts, people=ad_people)
            # V1.39：前端 gxSave 一直有提交这两个键，原来 safety 分支整段丢掉，
            #        导致「普通用户」按部门 / 人员加了不生效。
            if 'user_depts' in data or 'user_people' in data:
                safety_user_grant.save_config(depts=us_depts, people=us_people)
        else:
            if 'sup_depts' in data or 'sup_people' in data:
                debug_sup_grant.save_config(depts=sg_depts, people=sg_people)
            # V1.16.17：调试档「调试责任人」名单
            if 'user_depts' in data or 'user_people' in data:
                debug_user_grant.save_config(depts=us_depts, people=us_people)
            # V1.16.16：调试档也有自己的「超管」名单（与安全档各自独立，互不影响）
            if 'admin_depts' in data or 'admin_people' in data:
                debug_admin_grant.save_config(depts=ad_depts, people=ad_people)
    except Exception as e:
        logger.warning(f'鉴权配置保存失败: {e}')
    try:
        _n = safety_review.count()
    except Exception:
        _n = 0
    try:
        _a = admin_grant.count() if _bz == 'safety' else debug_admin_grant.count()
        # V1.16.17：固定超管（王平平）不在配置文件里，但页面上算进「超管 N 人」，
        #   这里保持同一口径，免得保存后提示「超管 0 人」看着像没生效。
        _eff = (admin_grant if _bz == 'safety' else debug_admin_grant).people()
        if not any((x.get('phone') or '').strip() == SUPER_ADMIN_PHONE for x in _eff):
            _a += 1
    except Exception:
        _a = 0
    try:
        _s = debug_sup_grant.count()
    except Exception:
        _s = 0
    try:
        # V1.39：安全档的「普通用户」人数取自己那份名单，别再报调试档的数字
        _us = (debug_user_grant if _bz != 'safety' else safety_user_grant).count()
    except Exception:
        _us = 0
    logger.info(f"鉴权配置已更新（{_bz}）- 审核权：部门 {len(depts)} 项 / 个人 {len(people)} 人 / 生效 {_n} 人；"
                f"超管名单：部门 {'未改' if ad_depts is None else len(ad_depts)} / "
                f"个人 {'未改' if ad_people is None else len(ad_people)} / 生效 {_a} 人；"
                f"调试上级责任人：部门 {len(sg_depts)} 项 / 个人 {len(sg_people)} 人 / {_s} 人")
    return jsonify(ok=True, count=_n, admin_count=_a, sup_count=_s, user_count=_us)


@app.route('/admin/users')
def admin_users():
    """用户管理（V1.5.3 按业务档）：调试业务只有超管能进；安全业务允许超管与安全上级进。
    两个看板的用户列表互不相通，默认按当前业务档过滤。

    🔴 V1.16.30（用户要求）：「在线管理」「换人审批」并进这一页，做成三个标签页：
      · ?tab=users  （默认）用户名单与权限配置 —— 原来这一页的全部内容
      · ?tab=online 在线管理（看谁在用、强制退出）—— 原 /online?biz=
      · ?tab=idreq  换人审批（批 / 驳代操作申请）—— 原 /admin/idreq?biz=
    原来那两个页面**保留不动**（老链接、收藏夹还能用），只是不再从门户单独挂出去。
    """
    biz = request.args.get('biz') or 'debug'
    if biz not in ('debug', 'safety'):
        biz = 'debug'
    tab = (request.args.get('tab') or 'users').strip()
    if tab not in ('users', 'online', 'idreq'):
        tab = 'users'
    if not _is_logged_in():
        return _permission_denied()
    role = _current_role()
    # V1.16.17：两个看板的「用户管理」都只有超管能进 —— 四个角色（超管 / 调试上级责任人 /
    #   调试责任人 / 审核人）全部由超管（王平平）添加，与服务器上原来那一套鉴权一致。
    if not _biz_admin(biz):
        return _permission_denied()
    users = db.get_all_users(biz)
    labels = SAFETY_ROLE_LABELS if biz == 'safety' else ROLE_LABELS
    # V1.14.2：超管由「超管名单」决定，页面要能一并标出来（否则名单里的超管显示成普通用户）
    # V1.16.15：名单分三种来源（个人授权 / 部门授权 / 库里本来就是超管），表格里逐人能取消
    # V1.16.16：超管名单按业务档分开（安全档 admin_grant / 调试档 debug_admin_grant）
    _adm = admin_grant if biz == 'safety' else debug_admin_grant
    grant_people_all, grant_explicit, grant_dept_people = [], [], []
    try:
        grant_people_all = _adm.people()          # 生效超管 = 部门展开 + 个人 - 排除
        grant_explicit = _adm.explicit_people()   # 单独指定的
        grant_dept_people = _adm.dept_people()    # 部门 / 科室带出来的
    except Exception:
        pass
    grant_phones = [p.get('phone') for p in grant_people_all if p.get('phone')]
    _explicit_phones = {((p.get('phone') or '').strip()) for p in grant_explicit}
    _dept_phones = {((p.get('phone') or '').strip()) for p in grant_dept_people}
    # V1.15：「用户管理」与「鉴权设置」合并成一页 —— 同一页上还要渲染超管名单与审核权名单
    try:
        _ad = _adm.summary()
    except Exception:
        _ad = {'count': 0, 'depts': [], 'people': []}
    try:
        _rv = safety_review.summary()
    except Exception:
        _rv = {'count': 0, 'depts': [], 'people': []}
    # V1.16.2：调试业务的名单是「调试上级责任人」—— 不再用超管 / 审核人那两块
    try:
        _dg = debug_sup_grant.summary()
    except Exception:
        _dg = {'count': 0, 'depts': [], 'people': []}
    _is_dbg = (biz != 'safety')
    sup_count = _dg.get('count', 0) if _is_dbg else 0
    sup_dept_rows = _dg.get('depts') or [] if _is_dbg else []
    sup_people_rows = _dg.get('people') or [] if _is_dbg else []
    sup_phones = [p.get('phone') for p in sup_people_rows if p.get('phone')]
    # V1.16.17：调试档再加一份「调试责任人」名单（个人指定 > 部门授权；可把部门级上级单独退回）
    # V1.39：安全档也有「普通用户」名单（独立模块 / 独立配置文件），与调试档互不串档
    try:
        _du = (debug_user_grant if _is_dbg else safety_user_grant).summary()
    except Exception:
        _du = {'count': 0, 'depts': [], 'people': []}
    user_count = _du.get('count', 0)
    user_dept_rows = _du.get('depts') or []
    user_people_rows = _du.get('people') or []
    user_phones = [p.get('phone') for p in user_people_rows if p.get('phone')]
    _sup_exp_phones = {((x.get('phone') or '').strip()) for x in (_dg.get('people') or [])}
    _user_exp_phones = {((x.get('phone') or '').strip()) for x in (_du.get('people') or [])}
    # V1.16.17：固定超管王平平要出现在两个看板的「超管」名单里（卡片可见、不可删）
    _adm_people = [x for x in (_ad.get('people') or [])
                   if ((x.get('phone') or '').strip() != SUPER_ADMIN_PHONE)]
    _adm_people.insert(0, {'name': SUPER_ADMIN_NAME, 'phone': SUPER_ADMIN_PHONE,
                           'dept': '', 'fixed': True})
    _ad['people'] = _adm_people
    if not any((x.get('phone') or '').strip() == SUPER_ADMIN_PHONE
               for x in grant_people_all):
        _ad['count'] = (_ad.get('count') or 0) + 1
    if _is_dbg:
        # V1.16.16：调试档现在也有「超管」名单（_ad / grant_* 保留），只清掉「审核人」那块
        _rv = {'count': 0, 'depts': [], 'people': []}
    # V1.16.15：给每行标好「超管来源」，表格里才能给出对得上的操作按钮
    rows = []
    for u in users:
        _u = dict(u)
        _ph = (_u.get('phone') or '').strip()
        if _ph and _ph == SUPER_ADMIN_PHONE:
            _u['grant_src'] = 'fixed'                       # 固定超管（王平平），不给取消按钮
        elif _u.get('role') == 'super_admin' or _ph in grant_phones:
            if _ph in _explicit_phones:
                _u['grant_src'] = 'person'                  # 个人授权（可直接取消）
            elif _ph in _dept_phones:
                _u['grant_src'] = 'dept'                    # 部门授权（取消=单独剔除这个人）
            else:
                _u['grant_src'] = 'role'                    # 库里 role 本来就是超管
        else:
            _u['grant_src'] = ''
        # V1.16.17：不是超管的人，把他在「调试档两级名单」里的生效角色与来源标出来；
        #   安全档则标出有没有审核权（表格里据此给「设为 / 取消 审核人」按钮）。
        _u['role_view'] = ''
        _u['role_src'] = ''
        if not _u['grant_src'] and biz != 'safety':
            try:
                # 注意：变量名不要用 _rv —— 它是上面的「审核人名单汇总」，会被覆盖掉
                _myrole = _pick_debug_role(_ph, _u.get('name') or '')
            except Exception:
                _myrole = ''
            if _myrole:
                _u['role_view'] = _myrole
                _u['role_src'] = ('person' if (_ph and (_ph in _sup_exp_phones or _ph in _user_exp_phones))
                                  else 'dept')
            else:
                # V1.16.17b：两份名单都没写 → 沿用账号本身的角色（与上面「角色实时刷新」同一口径）。
                #   不回落的话，账号 role=debug_sup 的人会被页面显示成「调试责任人」，看着像被降权了。
                _db_role = (_u.get('role') or '').strip()
                if _db_role in ('debug_sup', 'debug_user'):
                    _u['role_view'] = _db_role
                    _u['role_src'] = 'role'
        _u['is_reviewer'] = False
        if biz == 'safety':
            try:
                _u['is_reviewer'] = bool(_ph and safety_review.is_reviewer(_ph, _u.get('name') or ''))
            except Exception:
                _u['is_reviewer'] = False
        rows.append(_u)
    # 只有名单、没有账号的超管也必须出现在表格里（否则「加了看不见、更删不掉」）
    _known = {(_u.get('phone') or '').strip() for _u in rows}
    grant_extra = []
    # V1.16.16：王平平是「两个系统的固定超管」——安全档他没有账号也要出现在表里
    if SUPER_ADMIN_PHONE not in _known:
        grant_extra.append({'name': SUPER_ADMIN_NAME, 'phone': SUPER_ADMIN_PHONE,
                            'dept': '', 'from_dept': False, 'fixed': True, 'kind': 'admin'})
    for _p in grant_people_all:
        _ph2 = (_p.get('phone') or '').strip()
        if _ph2 and _ph2 in _known:
            continue
        _q = dict(_p)
        _q['from_dept'] = bool(_ph2 and _ph2 in _dept_phones and _ph2 not in _explicit_phones)
        _q['fixed'] = False
        _q['kind'] = 'admin'
        grant_extra.append(_q)
    # V1.16.17：调试档两级名单里「还没建账号」的人同样列出来（否则加了看不见、更删不掉）
    # V1.39：两档都要列「名单里有、但没建账号」的人；调试档另多一份「调试上级责任人」
    _known2 = set(_known)
    for _x in grant_extra:
        _known2.add((_x.get('phone') or '').strip())
    _extra_kinds = (('sup', sup_people_rows), ('user', user_people_rows)) if _is_dbg \
        else (('user', user_people_rows),)
    for _kind, _rows2 in _extra_kinds:
        for _p in _rows2:
            _ph3 = (_p.get('phone') or '').strip()
            if _ph3 and _ph3 in _known2:
                continue
            _q2 = dict(_p)
            _q2['from_dept'] = False
            _q2['fixed'] = False
            _q2['kind'] = _kind
            grant_extra.append(_q2)
            _known2.add(_ph3)
    org_saved = ''
    try:
        import chz_org as org
        org_saved = org.load().get('saved_at') or ''
    except Exception:
        pass
    # ---------- V1.16.30：在线管理 / 换人审批 两个标签的数据 ----------
    # 口径复用「抽出来的共用函数 / 共用查询」，与原独立页面完全一致：
    #   · 在线：`_online_view(biz)` —— 与 /online 同一个函数
    #   · 审批：`db.get_id_switch_reqs(biz=biz)` —— 按档取，与 /admin/idreq 同一个查询
    on_rows, on_can, on_others = _online_view(biz)
    idreq_rows = []
    try:
        idreq_rows = db.get_id_switch_reqs(biz=biz)
    except Exception as e:
        logger.warning('审批列表读取失败: %s' % e)
    return render_biz('admin_users.html', biz, users=rows, role_labels=labels,
                      tab=tab, online_rows=on_rows, online_can=on_can,
                      online_others=on_others, online_guard=ONLINE_GUARD,
                      online_idle_min=db.ONLINE_IDLE_MIN,
                      idreq_rows=idreq_rows,
                      grant_phones=grant_phones, super_phone=SUPER_ADMIN_PHONE,
                      super_name=SUPER_ADMIN_NAME,
                      grant_extra=grant_extra,
                      admin_count=_ad['count'], admin_dept_rows=_ad['depts'],
                      admin_people_rows=_ad['people'],
                      review_count=_rv['count'], dept_rows=_rv['depts'],
                      people_rows=_rv['people'], org_saved_at=org_saved,
                      sup_count=sup_count, sup_dept_rows=sup_dept_rows,
                      sup_people_rows=sup_people_rows, sup_phones=sup_phones,
                      user_count=user_count, user_dept_rows=user_dept_rows,
                      user_people_rows=user_people_rows, user_phones=user_phones,
                      user_name=session.get('user_name', ''),
                      can_admin=_biz_admin(biz),
                      user_role=role)


@app.route('/admin/login_logs')
def admin_login_logs():
    """登录日志：用于追查暴力破解/异常登录。
    V1.5：支持按 手机号/姓名 关键字筛选；每条记录附真实姓名（users 表反查）。
    V1.5.3：按业务档独立——两块看板各看各的登录日志，不再混在一起、也没有切换。
    权限：超管可看；安全业务下安全上级也可看（与用户管理同口径）。"""
    biz = request.args.get('biz') or 'debug'
    if biz not in ('debug', 'safety'):
        biz = 'debug'
    if not _is_logged_in():
        return _permission_denied()
    role = _current_role()
    # V1.12：安全档只有超管；调试档超管+调试上级
    if not _can_manage_biz(biz):
        return _permission_denied()
    logs = db.get_login_logs(300, biz)
    # 手机号 → 姓名映射（admin 兜底账号特殊处理）
    phone2name = {}
    try:
        for u in db.get_all_users():
            if u.get('phone'):
                phone2name[u['phone']] = u.get('name') or u['phone']
    except Exception:
        pass
    for l in logs:
        uname = (l.get('username') or '').strip()
        l['name'] = ('超级管理员' if uname == ADMIN_USER
                     else phone2name.get(uname, ''))
    kw = (request.args.get('q') or '').strip()
    if kw:
        kwl = kw.lower()
        logs = [l for l in logs
                if kwl in (l.get('username') or '').lower()
                or kwl in (l.get('name') or '').lower()]
    return render_biz('login_logs.html', biz, logs=logs, kw=kw,
                      qs=('?biz=safety' if biz == 'safety' else ''),
                      user_name=session.get('user_name', ''),
                      user_role=role)


@app.route('/admin/approve', methods=['POST'])
def admin_approve():
    if not _is_logged_in():
        return jsonify(ok=False, msg='权限不足'), 403
    data = request.get_json(silent=True) or request.form
    phone = (data.get('phone') or '').strip()
    # V1.5.3：超管可管任何账号；安全上级只能管安全业务（safety）的账号
    if not _can_manage_users_for_phone(phone, data.get('biz')):
        return jsonify(ok=False, msg='权限不足'), 403
    action = (data.get('action') or '').strip()
    if action == 'approve':
        db.update_user_status(phone, 'approved', _current_phone())
        logger.info(f"审核通过 - {phone}, 审核人: {_current_phone()}")
        return jsonify(ok=True, msg='已通过')
    elif action == 'reject':
        db.update_user_status(phone, 'rejected', _current_phone())
        logger.info(f"审核拒绝 - {phone}, 审核人: {_current_phone()}")
        return jsonify(ok=True, msg='已拒绝')
    return jsonify(ok=False, msg='未知操作'), 400


# ============ V1.16.15：超管名单逐人管理（用户管理页最下方表格里直接操作，即时生效） ============

def _grant_person_of(data):
    """从请求里取出手机号/姓名（手机号只留数字）"""
    phone = ''.join(ch for ch in str(data.get('phone') or '') if ch.isdigit())
    name = (data.get('name') or '').strip()
    return phone, name


@app.route('/admin/grant_add', methods=['POST'])
def admin_grant_add():
    """名单逐人授权 —— 不建账号、不用再点保存、即时生效。

    V1.16.17：支持 role = admin(超管) / sup(调试上级责任人) / user(调试责任人) / review(审核人)，
              角色必须属于当前业务档；不传 role 时默认 admin（兼容旧调用）。"""
    data = request.get_json(silent=True) or request.form
    _bz = data.get('biz') or 'debug'
    _bz = 'safety' if _bz == 'safety' else 'debug'
    # 按业务档判权 —— 名单里的超管（账号可能还是普通用户）也要能管理人，
    # 否则「超管加超管」会被误拒；同时保证安全档 / 调试档互不越权。
    if not _is_logged_in() or not _biz_admin(_bz):
        return jsonify(ok=False, msg='只有超管可以改鉴权名单'), 403
    _role = (data.get('role') or 'admin').strip() or 'admin'
    _g = _grant_list_of(_bz, _role)
    if _g is None:
        return jsonify(ok=False, msg='这个角色不属于当前看板'), 400
    _who = GRANT_ROLE_LABELS.get(_role, '名单')
    if _bz == 'safety' and _role == 'user':
        _who = '普通用户'          # V1.39：安全档 user 名单的界面叫法
    phone, name = _grant_person_of(data)
    if not phone and not name:
        return jsonify(ok=False, msg='缺少手机号或姓名'), 400
    if _role == 'admin' and phone and phone == SUPER_ADMIN_PHONE:
        return jsonify(ok=True, msg=f'{SUPER_ADMIN_NAME} 是系统固定超管，无需重复授权')
    try:
        _g.add_person(phone=phone, name=name, dept=(data.get('dept') or '').strip())
    except Exception as e:
        logger.warning(f'设为{_who}失败: {e}')
        return jsonify(ok=False, msg=f'保存失败：{str(e)[:120]}'), 500
    logger.info(f"{_who}名单（{_bz}）- 已添加: {name}({phone}), 操作人: {_current_phone()}")
    return jsonify(ok=True, msg=f'已设为{_who}（即时生效）：{name or phone}')


@app.route('/admin/grant_remove', methods=['POST'])
def admin_grant_remove():
    """取消某人的某个名单授权（V1.16.17 支持 role，同 /admin/grant_add）：
    · 来自「个人授权」→ 直接从名单删掉；
    · 来自「部门 / 科室授权」→ 只把这个人在名单里剔除，部门其余人不受影响；
    · 库里 role 本来就是对应角色的（非固定超管）→ 顺手降回普通用户。"""
    data = request.get_json(silent=True) or request.form
    _bz = data.get('biz') or 'debug'
    _bz = 'safety' if _bz == 'safety' else 'debug'
    if not _is_logged_in() or not _biz_admin(_bz):
        return jsonify(ok=False, msg='只有超管可以改鉴权名单'), 403
    _role = (data.get('role') or 'admin').strip() or 'admin'
    _g = _grant_list_of(_bz, _role)
    if _g is None:
        return jsonify(ok=False, msg='这个角色不属于当前看板'), 400
    _who = GRANT_ROLE_LABELS.get(_role, '名单')
    if _bz == 'safety' and _role == 'user':
        _who = '普通用户'          # V1.39：安全档 user 名单的界面叫法
    phone, name = _grant_person_of(data)
    if not phone and not name:
        return jsonify(ok=False, msg='缺少手机号或姓名'), 400
    if _role == 'admin' and phone and phone == SUPER_ADMIN_PHONE:
        return jsonify(ok=False, msg=f'{SUPER_ADMIN_NAME} 是系统固定超管，不能取消'), 400
    try:
        src = 'person' if _g.is_explicit(phone, name) else (
            'dept' if _g.is_dept_member(phone, name) else '')
        _g.remove_person(phone, name)
        if phone and _role in ('admin', 'sup'):
            _u = db.get_user_by_phone(phone)
            if _u and (_u.get('role') == ('super_admin' if _role == 'admin' else 'debug_sup')):
                db.update_user_role(phone, 'debug_user')
    except Exception as e:
        logger.warning(f'取消{_who}失败: {e}')
        return jsonify(ok=False, msg=f'保存失败：{str(e)[:120]}'), 500
    logger.info(f"{_who}名单（{_bz}）- 已移除: {name}({phone}) [{src or 'role'}], 操作人: {_current_phone()}")
    msg = f'已取消{_who}：{name or phone}'
    if src == 'dept':
        msg += '（他在部门授权范围内，已单独剔出；该部门其他人不受影响）'
    return jsonify(ok=True, msg=msg)


@app.route('/admin/set_role', methods=['POST'])
def admin_set_role():
    if not _is_logged_in():
        return jsonify(ok=False, msg='权限不足'), 403
    data = request.get_json(silent=True) or request.form
    phone = (data.get('phone') or '').strip()
    # V1.5.3：超管可管任何账号；安全上级只能管安全业务（safety）的账号
    if not _can_manage_users_for_phone(phone, data.get('biz')):
        return jsonify(ok=False, msg='权限不足'), 403
    role = (data.get('role') or '').strip()
    if role not in ('super_admin', 'debug_sup', 'debug_user'):
        return jsonify(ok=False, msg='无效角色'), 400
    # V1.5.3：超管按业务档唯一——调试业务只认王平平；安全业务可以有自己的超管
    if role == 'super_admin' and phone != SUPER_ADMIN_PHONE:
        _u = db.get_user_by_phone(phone) or {}
        if (_u.get('biz') or 'debug') != 'safety':
            return jsonify(ok=False, msg='调试业务只保留一个超级管理员账号（王平平），不能把其他账号设为超管'), 403
    db.update_user_role(phone, role)
    logger.info(f"修改用户角色 - {phone} → {role}, 操作人: {_current_phone()}")
    return jsonify(ok=True, msg='角色已更新')


@app.route('/admin/delete_user', methods=['POST'])
def admin_delete_user():
    if not _is_logged_in():
        return jsonify(ok=False, msg='权限不足'), 403
    data = request.get_json(silent=True) or request.form
    phone = (data.get('phone') or '').strip()
    # V1.5.3：超管可管任何账号；安全上级只能管安全业务（safety）的账号
    if not _can_manage_users_for_phone(phone, data.get('biz')):
        return jsonify(ok=False, msg='权限不足'), 403
    if phone == ADMIN_USER:
        return jsonify(ok=False, msg=biz_text('不能删除超级管理员兜底账号', data.get('biz'))), 400
    # V1.16.17c：系统固定超管（王平平）不可删除 —— 页面已置灰，这里再兜一道，防绕过页面直接提交
    if phone and phone == SUPER_ADMIN_PHONE:
        return jsonify(ok=False, msg=biz_text('不能删除系统固定超管', data.get('biz'))), 400
    # V1.16.15：删账号时一并把超管授权摘掉 —— 否则名单里还留着手机号，人看着"删了还是超管"
    try:
        if phone:
            for _m in (admin_grant, debug_admin_grant):
                if _m.is_explicit(phone) or _m.is_dept_member(phone):
                    _m.remove_person(phone)
    except Exception as e:
        logger.warning(f'删除用户时清理超管名单失败（忽略）: {e}')
    db.delete_user(phone)
    logger.info(f"删除用户 - {phone}, 操作人: {_current_phone()}")
    return jsonify(ok=True, msg='用户已删除')


# 管理员重置/添加账号时的统一初始密码（用户登录后应尽快自行修改）
ADMIN_RESET_PASSWORD = '88888888'


@app.route('/admin/add_user', methods=['POST'])
def admin_add_user():
    """V1.46（用户要求）：手工建本地账号的口子已关闭。

    保留路由是为了老书签 / 直接 POST 不 404，但一律拒绝。
    开通权限统一走「用户管理 → 企业组织架构名单授权」：不建账号、不设初始密码，
    船海智云 SSO 认到人后按名单即时生效。/login 账号密码应急入口仍保留。
    """
    return jsonify(ok=False, msg=(
        '账号创建入口已关闭：本系统不再单独开账号。请在「用户管理」里用'
        '「＋ 添加部门/科室」或「＋ 添加人员」把同事从企业组织架构加进名单，'
        '保存后即时生效，无需密码、无需审核。')), 403

@app.route('/admin/reset_password', methods=['POST'])
def admin_reset_password():
    """超管一键重置任意用户密码为 88888888（哈希后写入数据库）"""
    if not _is_logged_in():
        return jsonify(ok=False, msg='权限不足'), 403
    data = request.get_json(silent=True) or request.form
    phone = (data.get('phone') or '').strip()
    # V1.5.3：超管可管任何账号；安全上级只能管安全业务（safety）的账号
    if not _can_manage_users_for_phone(phone, data.get('biz')):
        return jsonify(ok=False, msg='权限不足'), 403
    if not phone:
        return jsonify(ok=False, msg='缺少手机号'), 400
    if phone == ADMIN_USER:
        return jsonify(ok=False, msg=biz_text('超级管理员兜底账号的密码存于服务器 config.json，请在该服务器上修改', data.get('biz'))), 400
    user = db.get_user_by_phone(phone)
    if not user:
        return jsonify(ok=False, msg='该手机号不存在账号'), 404

    db.update_user_password(phone, hash_password(ADMIN_RESET_PASSWORD))
    logger.info(f"管理员重置密码 - {phone} 重置为默认密码, 操作人: {_current_phone()}")
    return jsonify(ok=True, msg=f'已重置 {user.get("name", phone)} 的密码为 {ADMIN_RESET_PASSWORD}')


@app.route('/admin/lookup_chz')
def admin_lookup_chz():
    """按手机号查船海智云通讯录姓名与工号，用于添加账号时自动带出（仅超管）

    🔴 V1.16.27：原门禁 `_is_logged_in() + _current_role()=='super_admin'`
      挡不住"自选身份声明成超管号"（免登录下前者恒 True，后者按声明的号算），
      等于谁都能翻全厂通讯录。改用 _biz_admin()。"""
    if not _biz_admin((request.args.get('biz') or 'debug').strip()
                      if (request.args.get('biz') or '').strip() in ('debug', 'safety')
                      else 'debug'):
        return jsonify(ok=False, msg='权限不足'), 403
    phone = (request.args.get('phone') or '').strip()
    if not re.fullmatch(r'1\d{10}', phone):
        return jsonify(ok=False, msg='手机号格式不正确'), 400
    try:
        import chz_push
        info = (chz_push.ensure_users().get(phone) or {})
    except Exception:
        info = {}
    return jsonify(ok=True, name=info.get('name', ''), code=info.get('code', ''))


# ============ 看板管理（仅超级管理员） ============

class SysExcelFormatError(Exception):
    """V1.45：导入模板格式不对。整批拒绝，异常文本直接显示给操作人。"""


_MAX_IMPORT_ROWS = 500
_MAX_IMPORT_FILE = 2 * 1024 * 1024
_NAME_BAD_CHARS = ('，', ',', '、', ';', '；', '\n', '\r', '\t')


def _col_letter(n):
    s = ''
    while n:
        n, m = divmod(n - 1, 26)
        s = chr(65 + m) + s
    return s


def _parse_sys_excel(f) -> list:
    """V1.45 严格解析系统清单 Excel：格式不对一律抛 SysExcelFormatError，整批拒绝、一条不导。

    模板硬性要求（与页面下载的导入模板一致）：
      第一张工作表 A1 == 「名称」；只有 A 列允许有数据；A2 起每行一个名称；
      无合并单元格、无中间空行、无重复、无一行多名、无公式、名称 <= 100 字。
    """
    from openpyxl import load_workbook
    fname = (getattr(f, 'filename', '') or '').strip()
    if fname and not fname.lower().endswith(('.xlsx', '.xlsm')):
        raise SysExcelFormatError(
            '【导入被拒绝】文件格式不对：只支持 .xlsx / .xlsm 工作簿，'
            '收到的是「%s」。请下载页面上的导入模板，'
            '填完另存为 .xlsx 再传（.xls / .csv 改后缀都不行）。' % fname)
    _stream = getattr(f, 'stream', f)
    try:
        _stream.seek(0, 2)
        _sz = _stream.tell()
        _stream.seek(0)
    except Exception:
        _sz = 0
    if _sz > _MAX_IMPORT_FILE:
        raise SysExcelFormatError(
            '【导入被拒绝】文件太大（%.1f MB），'
            '一次最多 2 MB，请精简后重传。' % (_sz / 1048576.0))
    try:
        wb = load_workbook(f, data_only=True)
    except Exception:
        raise SysExcelFormatError(
            '【导入被拒绝】Excel 打不开：这个文件不是真正的 .xlsx 工作簿'
            '（常见原因是把 .csv / .xls 直接改了后缀）。'
            '请用页面上的导入模板另存为 .xlsx 再传。')
    # 再读一遍不带 data_only 的版本，专门用来认出公式单元格
    # （data_only=True 时公式的值是 None，光看值会被当成「空行」，报错原因不准）
    ws_raw = None
    try:
        _stream.seek(0)
        wb_raw = load_workbook(f, data_only=False)
        ws_raw = wb_raw.active
    except Exception:
        pass
    try:
        _stream.seek(0)
    except Exception:
        pass
    ws = wb.active
    if ws is None:
        raise SysExcelFormatError('【导入被拒绝】Excel 里没有工作表，请用导入模板重做。')
    if ws.max_row > _MAX_IMPORT_ROWS + 1 or ws.max_column > 64:
        raise SysExcelFormatError(
            '【导入被拒绝】工作表范围过大（%d 行 / %d 列），'
            '模板只需要一列，一次最多 %d 行。'
            % (ws.max_row, ws.max_column, _MAX_IMPORT_ROWS))
    # 1) 表头：不再兼容「没有表头 / 表头写别的」，否则整列乱七八糟的东西都会被当名称导进去
    first = ws.cell(row=1, column=1).value
    first = '' if first is None else str(first).strip()
    if first != '名称':
        raise SysExcelFormatError(
            '【导入被拒绝】A1 表头不对：必须是「名称」，'
            '当前是「%s」。请把第一张表的 A1 改成「名称」，'
            '或下载导入模板重填。' % (first or '空白'))
    # 2) 合并单元格
    try:
        merged = [str(r) for r in ws.merged_cells.ranges]
    except Exception:
        merged = []
    if merged:
        raise SysExcelFormatError(
            '【导入被拒绝】工作表里有合并单元格（%s），'
            '模板不允许合并，请全部拆分后重传。' % merged[0])
    # 3) B 列及以后必须全空（模板的说明放在第二张表，不放在数据行）
    for c in range(2, ws.max_column + 1):
        for r in range(1, ws.max_row + 1):
            v = ws.cell(row=r, column=c).value
            if v is not None and str(v).strip():
                raise SysExcelFormatError(
                    '【导入被拒绝】第 %s%d 格有多余内容「%s」：'
                    '模板只允许 A 列填名称，B 列及以后必须留空。'
                    % (_col_letter(c), r, str(v).strip()[:20]))
    # 4) 数据行
    names, seen, blank_at = [], {}, 0
    for r in range(2, ws.max_row + 1):
        _c = ws.cell(row=r, column=1)
        v = _c.value
        if v is None or not str(v).strip():
            # data_only=True 时公式单元格的值是 None（没有缓存值），会被误当成空行；
            # data_type 仍是 'f'，这里单独点出来，别让人以为是「忘了填」
            _is_formula = (getattr(_c, 'data_type', '') == 'f' or
                           (ws_raw is not None and
                            getattr(ws_raw.cell(row=r, column=1), 'data_type', '') == 'f'))
            if _is_formula:
                raise SysExcelFormatError(
                    '【导入被拒绝】第 %d 行是公式，请填纯文字名称，不要用公式。' % r)
            blank_at = r
            continue
        if blank_at:
            raise SysExcelFormatError(
                '【导入被拒绝】第 %d 行是空的，但下面第 %d 行还有名称：'
                '名称不能中间断开，请删掉空行再传。' % (blank_at, r))
        s = str(v).strip()
        if len(s) > 100:
            raise SysExcelFormatError(
                '【导入被拒绝】第 %d 行名称太长（%d 字），'
                '单个名称最多 100 字。' % (r, len(s)))
        if s.startswith('='):
            raise SysExcelFormatError(
                '【导入被拒绝】第 %d 行是公式「%s」，'
                '请填纯文字名称，不要用公式。' % (r, s[:20]))
        for ch in _NAME_BAD_CHARS:
            if ch in s:
                raise SysExcelFormatError(
                    '【导入被拒绝】第 %d 行「%s」里带了「%s」：'
                    '一行只能写一个名称，多个名称请拆成多行。'
                    % (r, s[:20], ch if ch != '\n' else '换行'))
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            raise SysExcelFormatError(
                '【导入被拒绝】第 %d 行是纯数字「%s」，'
                '名称必须是文字。' % (r, s))
        if s in seen:
            raise SysExcelFormatError(
                '【导入被拒绝】第 %d 行「%s」和第 %d 行重复，'
                '同一份表里名称不能重复。' % (r, s[:20], seen[s]))
        seen[s] = r
        names.append(s)
        if len(names) > _MAX_IMPORT_ROWS:
            raise SysExcelFormatError(
                '【导入被拒绝】一次最多导入 %d 个名称，请分批。' % _MAX_IMPORT_ROWS)
    if not names:
        raise SysExcelFormatError(
            '【导入被拒绝】A2 起没有填任何名称：'
            '请从第 2 行开始每行写一个名称（模板里的 3 行示例要先删掉）。')
    return names


@app.route('/admin/boards', methods=['POST'])
def admin_create_board():
    """新建船舶完整性看板：名称 + 船号 + 系统清单 Excel（可选，不传则仅有名称）。
    企微群机器人 webhook 已下线为可选项，新建时不再采集；推送主通道为船海智云，
    未配置全局 webhook 时即跳过企微回退。"""
    _biz = (request.form.get('biz') or 'debug')
    if not _is_logged_in():
        return _permission_denied()
    # V1.6 B1：调试档＝超管+调试上级都能建板；V1.12：安全档只有超管
    if not _can_manage_biz(_biz):
        return _permission_denied()
    name = (request.form.get('name') or '').strip()
    # V1.6 A1：看板名称完全自由填写，不再由船号自动拼「XX船安全隐患看板」
    btype = (request.form.get('btype') or '').strip() or '产品船'
    if not board_categories.is_valid(btype):
        btype = board_categories.default_key('ship')
    _from_page = (request.form.get('from_page') or '').strip()
    ship_no = (request.form.get('ship_no') or '').strip()
    if board_categories.behavior_of(btype) != 'ship':
        ship_no = ''          # 非产品船不存船号，列表里显示「—」
    if not name:
        return "<script>alert('请填写看板名称'); history.back();</script>"
    # V1.16.13：同一业务档内看板名不能重复（前端已先拦一道，这里是硬校验）
    if _board_name_exists(_biz, name):
        return _dup_board_name_page(name)

    # 🔴 V1.45：先校验上传的系统清单，格式不对直接拒绝、一条不导、**不建看板**。
    #   以前是先 create_ship 再导清单 ⇒ 导错会留下一张空看板，
    #   而页面又删不掉，越积越多（用户 11-06 报障）。
    _pre_names = []
    sys_file = request.files.get('sys_file')
    if sys_file and sys_file.filename and _biz != 'safety':
        try:
            _pre_names = _parse_sys_excel(sys_file)
        except SysExcelFormatError as e:
            logger.warning(f"新建看板「{name}」被拒绝：系统清单格式错误 - {e}")
            return ("<script>alert(" + json.dumps(str(e), ensure_ascii=False)
                    + "); history.back();</script>")
        except Exception:
            logger.exception("新建看板：系统清单 Excel 解析异常")
            return "<script>alert('系统清单 Excel 打不开，请下载导入模板重填'); history.back();</script>"

    ship_id = db.create_ship(name, ship_no, '', _current_phone(), biz=_biz, btype=btype)
    # 🔴 V1.16.36（用户报障「建了 2556 看板、添加了其他作业区，最终只显示船装那 4 个」）：
    #   原来安全档这一支**提前 return**，`_save_area_defaults_from_form()` 压根没被调用 ——
    #   弹窗里填的作业区（area_name[]）全部被丢弃，落库 0 行。
    #   看板页再打开「责任单位默认人」时 `area_defaults` 为空
    #   ⇒ index.html 的 `renderAreaDefaultRows()` / `areaOptionsHtml()` 走兜底分支，
    #   永远只给「船装/机装/电装/动装」这 4 个。
    #   现在两档都先保存责任单位默认人员，再按档做各自的后续动作。
    _n_areas = _save_area_defaults_from_form(ship_id, request.form)
    if _biz == 'safety':
        # 安全业务不需要导入系统清单，直接灌入默认隐患类别（V1.6：精简版）
        ensure_safety_categories(ship_id)
        logger.info(f"新建安全隐患看板 {name}(#{ship_id})，已灌入默认隐患类别 {len(SAFETY_CATEGORIES)} 项"
                    + (f"，已保存 {_n_areas} 个责任单位默认人员" if _n_areas else ""))
        if _from_page == 'devices':
            return redirect(url_for('safety_devices'))
        return redirect(url_for('safety_kanban', ship_id=ship_id))
    # 作业区默认人员配置（可选）：船装/机装/电装 各配默认主管与作业长
    if _pre_names:
        db.import_ship_systems(ship_id, _pre_names)
        logger.info(f"新建看板 {name}(#{ship_id})，导入系统清单 {len(_pre_names)} 项")
    else:
        logger.info(f"新建看板 {name}(#{ship_id})，未上传系统清单")
    return redirect(url_for('board', ship_id=ship_id))


_PRESET_AREAS = [('chuan', '船装作业区'), ('ji', '机装作业区'), ('dian', '电装作业区'), ('dong', '动装作业区')]


def _save_area_defaults_from_form(ship_id: int, form) -> int:
    """从表单读取责任单位默认人员配置并保存，返回保存的单位数（V1.3 需求1）。

    责任单位可自由增删/改名（历史记录快照不受影响）。表单格式（按下标对齐）：
      area_name[]     责任单位名称，如 船装作业区
      sup_value[]     默认责任人（可多个，逗号分隔「姓名 - 电话」）
      foreman_value[] 默认上级领导（责任单位领导，可多个）
    兼容旧格式：表单未提交 area_name 时回退 def_sup_{key}/def_foreman_{key} 固定四作业区。
    V1.16.11：责任单位不再配「部长」，部门长统一走看板级 ship_def_ministers
              ⇒ 这里一律把 ministers 清空（老数据也会在保存时被清掉）。"""
    _keep_ministers = False
    # V1.37：责任单位一律走固定名单；按表单原始下标对齐取人员，非法单位直接丢弃
    _all_names = [v.strip() for v in form.getlist('area_name')]
    if any(_all_names):
        sups = form.getlist('sup_value')
        foremans = form.getlist('foreman_value')
        ministers = form.getlist('ministers')
        items = []
        for i, area_name in enumerate(_all_names):
            if not area_name or area_name not in _AREA_UNITS:
                continue
            # V1.43：配置弹窗会把名单里所有单位都列出来（含尚未配人的空行），
            # 完全空的行不落库 ⇒ 保持「未配置」状态，也不会覆盖已有配置。
            _sup_v = (sups[i] if i < len(sups) else '') or ''
            _foreman_v = (foremans[i] if i < len(foremans) else '') or ''
            _minister_v = ((ministers[i] if i < len(ministers) else '') or '') if _keep_ministers else ''
            if not (_sup_v.strip() or _foreman_v.strip() or _minister_v.strip()):
                continue
            items.append({
                'area_name': area_name,
                'sup_value': _sup_v,
                'foreman_value': _foreman_v,
                'ministers': _minister_v,
            })
    else:
        # 旧格式回退（防御性兼容）：固定船/机/电/动四作业区
        items = []
        for key, area_name in _PRESET_AREAS:
            sup = (form.get(f'def_sup_{key}') or '').strip()
            foreman = (form.get(f'def_foreman_{key}') or '').strip()
            if sup or foreman:
                items.append({'area_name': area_name, 'sup_value': sup, 'foreman_value': foreman})
    db.set_area_defaults(ship_id, items)
    # V1.16.10：同时保存看板级默认调试上级负责人 / 默认部长
    db.update_ship_defaults(
        ship_id,
        (form.get('ship_def_debug_sup') or '').strip(),
        (form.get('ship_def_ministers') or '').strip())
    return len(items)


def _backfill_area_names(ship_id: int) -> int:
    """回填旧记录的显式作业区/部门名称：
    1) 记录的作业长/安装责任人电话命中默认主管/作业长 → 用该默认作业区
    2) 台账反查能确定真实作业区（结果在台账作业区集合或默认作业区集合中）→ 用反查结果
    返回回填条数"""
    defaults = db.get_area_defaults(ship_id)
    default_phones = {}   # phone -> area_name
    for d in defaults:
        for val in (d.get('sup_value') or '', d.get('foreman_value') or ''):
            phone = val.split(' - ')[-1].strip() if ' - ' in val else ''
            if phone:
                default_phones[phone] = d['area_name']
    known_areas = {d['area_name'] for d in defaults}
    try:
        for s in db.get_staff_list():
            if s.get('作业区'):
                known_areas.add(s['作业区'])
    except Exception:
        pass

    updated = 0
    for r in db.get_records_by_ship(ship_id):
        if (r.get('area_name') or '').strip():
            continue
        area_name = ''
        # 1) 命中默认人电话
        persons = (r.get('area_leader') or '') + ',' + (r.get('install_user') or '')
        for phone, area in default_phones.items():
            if phone in persons:
                area_name = area
                break
        # 2) 台账反查
        if not area_name:
            guess = extract_area(r.get('area_leader') or '')
            if guess in known_areas:
                area_name = guess
        if area_name:
            db.update_record_area(r['id'], area_name)
            updated += 1
    if updated:
        logger.info(f"看板 #{ship_id} 保存默认人员后自动回填 {updated} 条旧记录的作业区")
    return updated


# ============ V1.6 需求1：默认责任人改动后，旧责任人名下的未闭环记录自动改派给新人并重新推送 ============

_CLOSED_STATUS = ('已闭环', '已完成')

# 默认人字段 → 记录字段 的对应关系（照「发起手尾」自动带出的那套，不要改顺序）
_DEFAULT_TO_FIELD = {
    'sup': 'install_user',        # 默认责任人        → 相关责任人（责任主管）
    'foreman': 'area_leader',     # 默认上级领导      → 作业区主管（责任单位领导）
    'ministers': 'dept_leader',   # 部长              → 部门长
}


def _defaults_map(ship_id: int) -> dict:
    """看板的默认人员配置按作业区索引：{作业区名: {sup, foreman, ministers}}"""
    m = {}
    for d in db.get_area_defaults(ship_id):
        name = (d.get('area_name') or '').strip()
        if not name:
            continue
        m[name] = {
            'sup': (d.get('sup_value') or '').strip(),
            'foreman': (d.get('foreman_value') or '').strip(),
            'ministers': (d.get('ministers') or '').strip(),
        }
    return m


def _record_target_area(r: dict, old_map: dict, new_map: dict) -> str:
    """这条记录归哪个作业区管：优先用记录上的作业区，没有再从旧默认人反查"""
    a = (r.get('area_name') or '').strip()
    if a and a in new_map:
        return a
    cur_phones = set(_extract_phones(r.get('install_user'), r.get('area_leader')))
    if not cur_phones:
        return ''
    for name, d in old_map.items():
        old_ps = set(_extract_phones(d.get('sup'), d.get('foreman')))
        if old_ps & cur_phones and name in new_map:
            return name
    return ''


def _reassign_records_on_defaults_change(ship_id: int, old_map: dict, new_map: dict) -> list:
    """【V1.6 需求1】看板的责任单位默认人改了以后：

    ① 找出仍挂在旧默认人名下的**未闭环**记录 → 责任字段改成新的默认人（自动改派）
    ② 立即给新责任人推送一次通知（不再等超时巡检，避免新人不知情）
    ③ 新责任人写进 notified_phones，后续超时升级才会带上他

    注意：记录如果当初是人工指定了另外一个责任人（不在旧默认值里），保持不动。
    返回被改派的记录 id 列表。"""
    moved = []
    if not new_map:
        return moved
    for r in db.get_records_by_ship(ship_id):
        rid = int(r.get('id') or 0)
        if not rid or (r.get('status') or '') in _CLOSED_STATUS:
            continue
        area = _record_target_area(r, old_map, new_map)
        if not area:
            continue
        new_d = new_map.get(area) or {}
        old_d = old_map.get(area) or {}
        upd = {}
        for key, field in _DEFAULT_TO_FIELD.items():
            newv = new_d.get(key) or ''
            oldv = old_d.get(key) or ''
            if newv == oldv:            # 这项没改
                continue
            cur = (r.get(field) or '').strip()
            old_ps = set(_extract_phones(oldv))
            new_ps = set(_extract_phones(newv))
            cur_ps = set(_extract_phones(cur))
            # 旧默认人有值时：只动仍然挂在旧人名下的记录（或这一栏本来是空的）
            if old_ps and cur_ps and not (cur_ps & old_ps):
                continue
            if cur_ps == new_ps:        # 已经是新人了
                continue
            upd[field] = newv
        if not upd:
            continue
        if db.update_record_persons(rid, **upd):
            moved.append(rid)
            _push_reassign_notice(r, upd, area)
    return moved


def _push_reassign_notice(r: dict, upd: dict, area: str) -> None:
    """改派后的即时通知：推给新责任人（并抄送该记录的调试责任人/调试上级）"""
    rid = int(r.get('id') or 0)
    ship_id = r.get('ship_id')
    new_vals = [(upd.get(f) or '') for f in _DEFAULT_TO_FIELD.values()]
    phones = list(dict.fromkeys(
        _extract_phones(*new_vals)
        + _extract_phones(r.get('debug_users'), r.get('debug_phones'), r.get('debug_sup'))
    ))
    if not phones:
        return
    _labels = {'install_user': '责任人',
               'area_leader': '责任单位领导',
               'dept_leader': '部门长'}
    who = '/'.join(_labels.get(f, f) for f in upd)
    _url = detail_url(rid, ship_id)
    body = (
        "\U0001F501【责任人变更通知】\n"
        f"{area}：{who}已调整，以下收尾现由您跟进：\n"
        f"系统：{r.get('sys_name') or ''}\n"
        f"描述：{r.get('description') or ''}\n"
        "\U0001F449 点击或复制下方链接查看并处理：\n"
        f"{_url}"
    )
    send_push(rec_text(body, ship_id), phones, mobile_click_url=_url, rid=rid)


@app.route('/admin/boards/area_defaults', methods=['POST'])
def admin_area_defaults():
    """保存看板的作业区默认人员配置（船装/机装/电装/动装主管+作业长），并自动回填旧记录作业区。
    V1.5：维护权限由「仅超管」放开到「超级管理员 + 调试上级负责人」"""
    if not _is_logged_in():
        return _permission_denied()
    ship_id = int(request.form.get('ship_id') or 0)
    ship = db.get_ship(ship_id)
    # V1.12：安全档只有超管能改看板默认责任人
    # V1.16：用户口径 —— 「有审批权（审核人）的也可以配置默认责任人」⇒ 安全档放开给审核人
    _bz = (ship or {}).get('biz') or 'debug'
    if not _can_manage_biz(_bz):
        if not (_bz == 'safety' and _is_safety_reviewer()):
            return _permission_denied()
    if not ship:
        # 🔴 V1.16.36：异常兜底回总界面（原来跳 /boards，用户报障 bug 1）
        return "<script>alert('看板不存在'); window.location.href='/';</script>"
    old_map = _defaults_map(ship_id)                      # 改动前的默认人（用来反查哪些记录挂着旧人）
    n = _save_area_defaults_from_form(ship_id, request.form)
    new_map = _defaults_map(ship_id)
    # V1.6 需求1：默认人改了 → 旧责任人名下的未闭环记录自动改派给新人，并重新推送一次
    moved = _reassign_records_on_defaults_change(ship_id, old_map, new_map)
    backfilled = _backfill_area_names(ship_id)
    logger.info(f"看板 {ship.get('name')}(#{ship_id}) 更新作业区默认人员（{n} 个作业区），"
                f"回填旧记录 {backfilled} 条, 自动改派 {len(moved)} 条, 操作人: {_current_phone()}")
    _biz = (ship or {}).get('biz') or 'debug'
    _home = url_for('safety_kanban', ship_id=ship_id) if _biz == 'safety' else url_for('board', ship_id=ship_id)
    if moved:
        return ("<script>alert('默认人员已更新：已将 " + str(len(moved)) +
                " 条未闭环记录改派给新的责任人，并已重新推送通知');"
                "window.location.href='" + _home + "';</script>")
    return redirect(_home)


@app.route('/admin/boards/delete', methods=['POST'])
def admin_delete_board():
    """删除看板（默认看板同样可删；删除默认看板后自动把剩余第一个看板设为默认，
    若为唯一看板则删除后系统无看板，重启服务会自动重建默认看板）。
    V1.45：支持 format=json，供页面内确认层用 fetch 调用，失败给明确中文原因、
    不静默跳走；不传 format 时保持原来的表单提交 + redirect 行为。"""
    _json = (request.form.get('format') or '') == 'json' or request.is_json

    def _deny(msg, status=403):
        if _json:
            return jsonify(ok=False, msg=msg), status
        return ("<script>alert(" + json.dumps(msg, ensure_ascii=False) + ");"
                "window.location.href='/boards';</script>")

    ship_id = int(request.form.get('ship_id') or 0)
    ship = db.get_ship(ship_id)
    # V1.5.3：业务档按被删看板判定——删完回本业务的看板列表，默认看板转移也只在同业务内挑
    _biz = (ship or {}).get('biz') or 'debug'
    _home = _board_list_home(_biz, request.form.get('from_page'), ship)
    if not _is_logged_in():
        return _deny('删除看板需要先登录，请先用管理员账号登录。')
    # V1.12：安全档只有超管能删（原安全上级可删，已收紧）
    if not _can_manage_biz(_biz):
        return _deny('权限不足：删除看板需要超级管理员 / 调试上级身份。'
                     '请先在「我的」里认领身份，或用管理员账号登录。')
    if not ship:
        return _deny('看板不存在或已被删除', 404)
    remaining = [s for s in db.get_ships(_biz) if s['id'] != ship_id]
    db.delete_ship(ship_id)
    if ship.get('is_default') and remaining:
        db.set_default_ship(remaining[0]['id'])
        logger.info(f"删除默认看板后自动转移：{remaining[0]['name']}(#{remaining[0]['id']}) 设为默认看板")
    logger.info(f"删除看板[{_biz}] - {ship.get('name')}(#{ship_id}), 操作人: {_current_phone()}")
    if _json:
        return jsonify(ok=True, msg='看板「%s」已删除' % ship.get('name'), home=_home)
    return redirect(_home)


@app.route('/admin/boards/rename', methods=['POST'])
def admin_rename_board():
    """重命名看板 / 修改船号（V1.5：看板名称与船号可一并修改；船号可留空清空）。
    V1.5.3：业务档按被改看板自动判定——改完回本业务的看板列表
    （原来一律跳 /boards，导致在安全隐患看板改名后被甩到船舶完整性看板）。
    权限：调试看板仅超管；安全看板超管与安全上级都可改（与新建看板口径一致）。"""
    ship_id = int(request.form.get('ship_id') or 0)
    ship = db.get_ship(ship_id)
    _biz = (ship or {}).get('biz') or 'debug'
    _home = _board_list_home(_biz, request.form.get('from_page'), ship)
    if not _is_logged_in():
        return _permission_denied()
    # V1.6 B1：调试档超管+调试上级可改名；V1.12：安全档只有超管
    if not _can_manage_biz(_biz):
        return _permission_denied()
    name = (request.form.get('name') or '').strip()
    ship_no = (request.form.get('ship_no') or '').strip()
    if not ship:
        return ("<script>alert('看板不存在'); window.location.href='" + _home + "';</script>")
    if not name:
        return "<script>alert('请输入新的看板名称'); history.back();</script>"
    # V1.16.13：改成别人的名字也不行（同业务档内唯一，排除自己）
    if _board_name_exists(_biz, name, exclude_id=ship_id):
        return _dup_board_name_page(name)
    btype = (request.form.get('btype') or '').strip()
    if btype and not board_categories.is_valid(btype):
        btype = ''
    if btype and board_categories.behavior_of(btype) != 'ship':
        ship_no = ''          # ★ V1.6 A1：非产品船不存船号
    db.rename_ship(ship_id, name, ship_no)
    if btype:
        try:
            db.set_ship_btype(ship_id, btype)
        except Exception:
            logger.warning('修改看板类型失败 ship#%s -> %s', ship_id, btype)
    logger.info(f"重命名看板[{_biz}] - {ship.get('name')}(#{ship_id}) → {name}"
                f"，船号 {ship.get('ship_no') or '—'} → {ship_no or '—'}"
                f"，类型 {ship.get('btype') or '—'} → {btype or '不变'}, 操作人: {_current_phone()}")
    return redirect(_home)


# ============ V1.5 系统清单管理（全员可看；超管+调试上级可导入/新增/改名） ============

# 🔴 V1.16.36（用户报障「把超管和普通管理分开，科室下面的人只有普通权限，超管才能有审批权」）：
#   原来这个元组是 ('super_admin', 'debug_sup')，而它同时被三处「审批权」判定引用
#   （违章处罚审批 _can_approve_rec、亮点审核 _hl_can_review 的兜底），
#   ⇒ 调试上级责任人（科室下面的人）默认就有审批权。
#   现在按用户口径：**审批权只归超管**。普通管理（debug_sup / debug_user）只剩
#   「发起 / 删自己的 / 闭环自己的 / 写 6S 表 / 提交亮点」这些业务动作。
#   显式被指定为审批人的那条路不受影响（那是业务流程里发起人自己点的审批人）。
_SYS_MANAGE_ROLES = ('super_admin',)


def _sid_from_req() -> int:
    """从当前请求里尽力取出 ship_id（form / query / JSON body 都试一遍）。

    用途：/api/syslists/* 这些接口的提示文案需要按看板业务档换词，
    但前端并未随请求传 biz，只能由 ship_id 反查看板属于哪个业务档。"""
    v = request.form.get('ship_id') or request.args.get('ship_id')
    if not v:
        j = request.get_json(silent=True)
        if isinstance(j, dict):
            v = j.get('ship_id')
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _can_manage_syslists() -> bool:
    """系统清单 / 隐患类别可维护角色（V1.12 按业务档）：
    调试档＝超管+调试上级；安全档＝**只有超管**。"""
    if not _is_logged_in():
        return False
    sid = _sid_from_req()
    return _can_manage_biz(_ship_biz_of(sid) if sid else 'debug')


@app.route('/syslists')
def sys_lists_page():
    """系统清单版面：登录用户均可查看各看板的系统清单；
    超管/调试上级可导入 Excel（增量比对只加不删）、手动新增、重命名（联动历史手尾）"""
    if not _is_logged_in():
        return _permission_denied()
    # V1.5.3：按业务档显示——安全业务看的是"隐患类别维护"，且只列安全看板
    _req_id = int(request.args.get('ship_id') or 0)
    _biz = ship_biz(_req_id) if _req_id else (request.args.get('biz') or 'debug')
    if _biz not in ('debug', 'safety'):
        _biz = 'debug'
    ships = db.get_ships(_biz)
    if not ships:
        return render_biz('sys_lists.html', _biz,
                          ships=[], ship_id=0, sys_names=[],
                          can_edit=False, kw='', msg='', err='',
                          user_role=session.get('user_role', ''),
                          user_name=session.get('user_name', ''))
    ship_id = _req_id
    ids = [s['id'] for s in ships]
    if ship_id not in ids:
        ship_id = next((s['id'] for s in ships if s.get('is_default')), ships[0]['id'])
    # V1.6 Demo s5：每个类别「在用 N 条」（记录里引用过这个类别名的条数）
    sys_names = db.get_ship_systems(ship_id)
    sys_counts = {}
    try:
        for _r in db.get_records_by_ship(ship_id):
            _k = (_r.get('sys_name') or '').strip()
            if _k:
                sys_counts[_k] = sys_counts.get(_k, 0) + 1
    except Exception:
        pass
    return render_biz('sys_lists.html', _biz,
                      ships=ships, ship_id=ship_id,
                      sys_counts=sys_counts,
                      sys_names=sys_names,
                      can_edit=_can_manage_syslists(), kw='', msg='', err='',
                      user_role=session.get('user_role', ''),
                      user_name=session.get('user_name', ''))


@app.route('/api/syslists/import', methods=['POST'])
def api_syslists_import():
    """导入系统清单 Excel：与原清单比对，只追加新增，绝不删除已有系统"""
    _biz = ship_biz(_sid_from_req())
    if not _can_manage_syslists():
        return jsonify(ok=False, msg=biz_text('权限不足：仅超级管理员/调试上级可导入', _biz)), 403
    ship_id = int(request.form.get('ship_id') or 0)
    if not db.get_ship(ship_id):
        return jsonify(ok=False, msg='看板不存在'), 404
    f = request.files.get('sys_file')
    if not f or not f.filename:
        return jsonify(ok=False, msg='请选择要导入的 Excel 文件'), 400
    try:
        names = _parse_sys_excel(f)
    except SysExcelFormatError as e:
        logger.warning(f"导入被拒绝 - ship#{ship_id}: {e}")
        return jsonify(ok=False, rejected=True, msg=str(e)), 400
    except Exception:
        logger.exception("系统清单 Excel 解析失败")
        return jsonify(ok=False, rejected=True,
                       msg='【导入被拒绝】Excel 打不开或已损坏，'
                           '请下载页面上的导入模板重填（只支持 .xlsx）。'), 400
    res = db.merge_ship_systems(ship_id, names)
    logger.info(f"导入系统清单 - ship#{ship_id} 新增 {res['added']} 项/跳过 {res['existed']} 项, "
                f"操作人: {_current_phone()}")
    _unit = '隐患类别' if _biz == 'safety' else '系统'
    msg = f"导入完成：新增 {res['added']} 个{_unit}"
    if res['existed']:
        msg += f"，跳过已在清单中的 {res['existed']} 个（原清单未删除）"
    return jsonify(ok=True, msg=msg, added=res['added'], existed=res['existed'])


@app.route('/api/syslists/add', methods=['POST'])
def api_syslists_add():
    """手动新增单个系统名（已在清单则提示，不重复添加）"""
    _biz = ship_biz(_sid_from_req())
    if not _can_manage_syslists():
        return jsonify(ok=False, msg=biz_text('权限不足：仅超级管理员/调试上级可新增', _biz)), 403
    ship_id = int(request.form.get('ship_id') or 0)
    name = (request.form.get('name') or '').strip()
    if not name:
        return jsonify(ok=False, msg=biz_text('请输入系统名称', _biz)), 400
    if not db.get_ship(ship_id):
        return jsonify(ok=False, msg='看板不存在'), 404
    res = db.merge_ship_systems(ship_id, [name])
    if res['added'] <= 0:
        return jsonify(ok=False, msg=f'「{name}」已在清单中，无需重复添加'), 409
    logger.info(f"新增系统清单项 - ship#{ship_id} +「{name}」, 操作人: {_current_phone()}")
    return jsonify(ok=True, msg=f'已新增「{name}」')


@app.route('/api/syslists/rename', methods=['POST'])
def api_syslists_rename():
    """重命名系统：联动更新该看板下已发出的历史手尾记录的系统名称"""
    _biz = ship_biz(_sid_from_req())
    if not _can_manage_syslists():
        return jsonify(ok=False, msg=biz_text('权限不足：仅超级管理员/调试上级可改名', _biz)), 403
    data = request.get_json(silent=True) or request.form
    ship_id = int(data.get('ship_id') or 0)
    old_name = (data.get('old_name') or '').strip()
    new_name = (data.get('new_name') or '').strip()
    if not old_name or not new_name:
        return jsonify(ok=False, msg=biz_text('请填写完整的原系统名与新系统名', _biz)), 400
    if not db.get_ship(ship_id):
        return jsonify(ok=False, msg='看板不存在'), 404
    res = db.rename_ship_system(ship_id, old_name, new_name)
    if not res['renamed']:
        return jsonify(ok=False, msg=biz_text('改名失败：原系统名不在清单中，或新名称已存在', _biz)), 409
    msg = f'已改名：「{old_name}」→「{new_name}」'
    if res['records_updated']:
        msg += f'，并同步更新 {res["records_updated"]} 条已发出手尾的系统名称'
    else:
        msg += '（当前没有以该系统名发出的手尾记录）'
    msg = biz_text(msg, _biz)
    logger.info(f"重命名系统 - ship#{ship_id} 「{old_name}」→「{new_name}」"
                f", 联动 {res['records_updated']} 条记录, 操作人: {_current_phone()}")
    return jsonify(ok=True, msg=msg, records_updated=res['records_updated'])


@app.route('/api/syslists/delete', methods=['POST'])
def api_syslists_delete():
    """V1.6 A4：删除一个系统 / 隐患类别。
    已被历史记录引用过的类别禁止删除（先改记录或改名），避免历史记录与清单失联。"""
    _biz = ship_biz(_sid_from_req())
    if not _can_manage_syslists():
        return jsonify(ok=False, msg=biz_text('权限不足：仅超级管理员/调试上级可删除', _biz)), 403
    data = request.get_json(silent=True) or request.form
    ship_id = int(data.get('ship_id') or 0)
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify(ok=False, msg=biz_text('请指定要删除的系统名称', _biz)), 400
    if not db.get_ship(ship_id):
        return jsonify(ok=False, msg='看板不存在'), 404
    # V1.16.5（用户要求：名单要能删改）：被历史记录引用过也**允许删**，
    #   但第一次会先回 409 把用量告诉前端，前端确认后再带 force=1 重发。
    #   删的只是清单项，历史记录里的类别名原样保留，不会丢数据。
    _force = str(data.get('force') or '').strip() in ('1', 'true', 'True', 'yes', 'on')
    res = db.delete_ship_system(ship_id, name, force=_force)
    if not res['deleted']:
        if res['used']:
            return jsonify(
                ok=False, used=res['used'],
                msg=biz_text('「%s」已被 %d 条记录使用。' % (name, res['used']), _biz)), 409
        return jsonify(ok=False, msg=biz_text('「%s」不在本看板清单中' % name, _biz)), 404
    logger.info(f"删除系统清单项 - ship#{ship_id} -「{name}」(force={_force}, 引用 {res['used']} 条), "
                f"操作人: {_current_phone()}")
    _m = '已删除「%s」' % name
    if res['used']:
        _m += '；这 %d 条历史记录的类别名保留不变' % res['used']
    return jsonify(ok=True, msg=biz_text(_m, _biz), used=res['used'])


# ============ 单条记录详情页（精准推送链接用） ============

@app.route('/detail/<int:rid>')
def detail(rid):
    rec = db.get_record(rid)
    if not rec:
        # 记录已被删除（常见于旧推送链接）。原来这里直接拿 rec=None 去渲染详情模板，
        # 会抛异常变成 500 错误页；现在给一个自洽的提示页，并按 ?biz= 回对应看板。
        _b = request.args.get('biz')
        _b = _b if _b in ('debug', 'safety') else 'debug'
        _home = '/safety' if _b == 'safety' else '/boards'
        _board = '安全隐患看板' if _b == 'safety' else '船舶完整性看板'
        _rec_word = '隐患' if _b == 'safety' else '手尾'
        return ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">"
                "<title>记录不存在 - " + _board + "</title></head>"
                "<body style=\"font-family:system-ui,-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;"
                "background:#eef3f8;color:#1f2a37;padding:48px 20px;text-align:center;margin:0;\">"
                "<div style=\"max-width:420px;margin:0 auto;background:#fff;border-radius:16px;"
                "padding:28px 22px;box-shadow:0 8px 30px rgba(10,35,70,.12);\">"
                "<div style=\"font-size:18px;font-weight:700;margin-bottom:10px;\">记录不存在或已被删除</div>"
                "<div style=\"font-size:14px;color:#64748b;line-height:1.8;margin-bottom:20px;\">"
                "这条" + _rec_word + "可能已经被删除，<br>或者链接已经失效。</div>"
                "<a href=\"" + _home + "\" style=\"display:inline-block;background:#14406b;color:#fff;"
                "text-decoration:none;padding:10px 20px;border-radius:9px;font-weight:600;\">"
                "返回" + _board + "</a></div></body></html>"), 404
    # 附带展示状态
    rec['display_status'] = compute_display_status(rec.get('status') or '未完成', rec.get('submit_time'), rec.get('ship_id'), rec.get('deadline'))
    rec['debug_user_name'] = extract_name(rec.get('debug_users') or '')
    rec['debug_sup_name'] = extract_name(rec.get('debug_sup') or '')
    rec['install_user_name'] = extract_name(rec.get('install_user') or '')
    # V1.4.4 详情页三级标签改造：责任单位领导按人员姓名展示，作业区单独成行
    rec['area_leader_name'] = extract_name(rec.get('area_leader') or '')
    rec['area_leader_areas'] = extract_area(rec.get('area_leader') or '')
    rec['dept_leader_name'] = extract_name(rec.get('dept_leader') or '')
    rec['dispatch_user_name'] = extract_name(rec.get('dispatch_user') or '')
    # V1.2 需求2：解析追加抄送人 JSON → [{name, phone}]，供详情页展示
    extra_users_list = []
    try:
        eu = json.loads(rec.get('extra_users') or '[]')
        if isinstance(eu, list):
            for it in eu:
                if isinstance(it, dict) and it.get('phone'):
                    extra_users_list.append({'name': it.get('name') or '', 'phone': it.get('phone') or ''})
    except Exception:
        pass
    rec['extra_users_list'] = extra_users_list
    # V1.3 需求2：解析追加责任人 JSON → [{name, phone}]，供详情页展示
    extra_resp_list = []
    try:
        er = json.loads(rec.get('extra_responsible') or '[]')
        if isinstance(er, list):
            for it in er:
                if isinstance(it, dict) and it.get('phone'):
                    extra_resp_list.append({'name': it.get('name') or '', 'phone': it.get('phone') or ''})
    except Exception:
        pass
    rec['extra_responsible_list'] = extra_resp_list
    # V1.2 需求3：推送留痕（逗号分隔号码），模板直接 split 展示
    rec['notified_phones'] = rec.get('notified_phones') or ''
    # V1.4.5：推送留痕号码 → 姓名（人员台账优先，查不到再补船海智云通讯录，仍无则显示号码）
    notified_people = []
    _staff_all = []
    try:
        _staff_all = db.get_staff_list()
        staff_phone_name = {}
        for _s in _staff_all:
            if _s.get('电话') and _s.get('姓名'):
                staff_phone_name[_s['电话']] = (_s['姓名'] or '').strip()
        _chz_map = None
        for _ph in [x.strip() for x in (rec['notified_phones'] or '').split(',') if x.strip()]:
            nm = staff_phone_name.get(_ph) or ''
            if not nm:
                try:
                    if _chz_map is None:
                        import chz_push as _cp
                        _chz_map = _cp.ensure_users() or {}
                    nm = ((_chz_map.get(_ph) or {}).get('name') or '').strip()
                except Exception:
                    _chz_map = _chz_map or {}
            notified_people.append({'phone': _ph, 'name': nm or _ph, 'matched': bool(nm)})
    except Exception:
        notified_people = [{'phone': x.strip(), 'name': x.strip(), 'matched': False}
                           for x in (rec['notified_phones'] or '').split(',') if x.strip()]
    rec['notified_people'] = notified_people
    # 完整派工链（谁派给谁，按时间正序）：dispatch_log 每次派工追加一条
    raw_logs = db.get_dispatch_logs(rid)
    dispatch_logs = []
    for log in raw_logs:
        dispatch_logs.append({
            'id': log.get('id'),
            'from_user': extract_name(log.get('from_user') or ''),
            'to_user': extract_name(log.get('to_user') or ''),
            'note': log.get('note') or '',
            'created_at': log.get('created_at') or '',
        })
    can_manage = _is_logged_in() and _can_manage(rid)
    can_add_responsible = _can_add_responsible(rid)
    # V1.4 用户中心：催办能力与催办历史
    is_closed = rec.get('status') in ('已闭环', '已完成')
    can_urge = False
    urge_targets = []
    if _is_logged_in() and not is_closed:
        _me = _current_identity_phone()   # V1.16.22：免登录下要用认到的身份，否则催办永远判不出人
        _chain = _chain_people(rec, raw_logs)
        _is_mine = bool(_me) and (str(rec.get('submitted_by') or '') == _me)
        _is_resp = any(p['phone'] == _me for p in _chain)
        if _role_level() >= 2 or _is_mine or _is_resp:
            can_urge = True
            urge_targets = [p for p in _chain if p['phone'] != _me]
    urge_logs = []
    for ul in db.get_urge_logs(rid):
        urge_logs.append({
            'actor_name': ul.get('actor_name') or ul.get('actor_phone') or '',
            'target_names': ul.get('target_names') or '',
            'created_at': ul.get('created_at') or '',
        })
    th = get_thresholds_for(rec.get('ship_id'))
    # V1.17.3：门禁放行了 /detail 页（推送链接落点，必须匿名可读），但详情含手机号。
    #   未认证访客（点推送、或公网枚举 rid）只能看到掩码手机号，防拉取全厂手机号；
    #   已可信身份（SSO/密码）照旧看完整号码。
    _view_trusted = _identity_trusted()
    _appr = _approvers_of(rec)
    if not _view_trusted:
        _m = _mask_phone
        rec['extra_users_list'] = [dict(u, phone=_m(u['phone'])) for u in rec['extra_users_list']]
        rec['extra_responsible_list'] = [dict(r, phone=_m(r['phone'])) for r in rec['extra_responsible_list']]
        notified_people = [dict(np, phone=_m(np['phone'])) for np in notified_people]
        rec['notified_people'] = notified_people
        _appr = [dict(a, phone=_m(a['phone'])) for a in _appr]
    return render_biz('detail.html', ship_biz(rec.get('ship_id')), rec=rec, rid=rid,
                           dispatch_logs=dispatch_logs,
                           can_manage=can_manage,
                           can_add_responsible=can_add_responsible,
                           is_closed=is_closed,
                           can_urge=can_urge,
                           urge_targets=urge_targets,
                           urge_logs=urge_logs,
                           l1=th['l1'], l2=th['l2'],
                           logged_in=_is_logged_in(),
                           user_role=_current_role(),
                           user_name=session.get('user_name', ''),
                           staff_list=(_staff_all if _view_trusted else []),
                           notified_people=notified_people,
                           approvers_list=_appr,
                           can_approve=_can_approve_rec(rec),
                           approve_logs=db.get_approve_logs(rid))


# ============ V1.3 需求2：追加责任人 ============

@app.route('/api/search_users')
def api_search_users():
    """V1.3 需求2：从 users 表按 姓名/手机号/工号 三合一模糊检索已有账号。
    供「追加责任人」弹窗与配置端选人组件使用（登录即可检索）。

    🔴 V1.16.27 两处加固（实测访客 `?kw=` 空词能拿到全表 31 条含明文手机号，
      超管王平平的号就在里面 —— 等于给了冒用所需的全部素材）：
    ① **必须带搜索词**：原来 kw 为空照样查，把整个 users 表吐出来。
    ② 弱身份（自选身份）**不返回手机号**：只回姓名/工号/角色。
      真正的选人提交走 /add_responsible，那里会校验权限；
      「能选人」和「能拿到全厂手机号」不该是同一个权限。
    """
    if not _is_logged_in():
        return jsonify(ok=False, msg='请先登录'), 401
    kw = (request.args.get('kw') or '').strip()
    if not kw:
        # V1.16.27：空词 = 变相的全表导出，直接拒掉。
        return jsonify(ok=False, msg='请输入姓名 / 手机号 / 工号再检索'), 400
    users = db.search_users(kw)
    trusted = _identity_trusted()
    items = []
    for u in users:
        ph = u.get('phone') or ''
        items.append({
            'id': u.get('id'),
            'name': u.get('name') or '',
            # 弱身份只给掩码，可信身份（SSO）给真号 —— 否则选人弹窗没法用
            'phone': ph if trusted else _mask_phone(ph),
            'emp_no': u.get('emp_no') or '',
            'role': u.get('role') or '',
            'role_label': ROLE_LABELS.get(u.get('role'), ''),
            'status': u.get('status') or '',
        })
    return jsonify(ok=True, items=items, total=len(items), trusted=trusted)


@app.route('/add_responsible', methods=['POST'])
def add_responsible():
    """V1.3 需求2：手尾发出后追加责任人（从 users 表选人）。
    入口权限：超管 / 调试上级 / 发起该收尾的调试责任人 / 当前调试责任人。
    追加后立即推送通知给新责任人，后续 24h/48h 梯度推送由 monitor 自动同步（resp_phones）。"""
    if not _is_logged_in():
        return jsonify(ok=False, msg='请先登录'), 401
    rid = int(request.form.get('rid') or request.args.get('rid') or 0)
    if rid <= 0:
        return jsonify(ok=False, msg='缺少记录ID'), 400
    rec = db.get_record(rid)
    if not rec:
        return jsonify(ok=False, msg='记录不存在'), 404
    if rec.get('status') in ('已闭环', '已完成'):
        return jsonify(ok=False, msg=rec_text('该手尾已闭环，无需追加责任人', rec.get('ship_id'))), 400
    if not _can_add_responsible(rid):
        return jsonify(ok=False, msg=rec_text('权限不足：仅超管、调试上级、发起人或当前调试责任人可追加', rec.get('ship_id'))), 403

    # 解析所选账号（前端提交 JSON [{name, phone}, ...]）
    raw = request.form.get('items') or request.form.get('extra_responsible') or '[]'
    items = []
    try:
        arr = json.loads(raw)
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, dict):
                    name = (it.get('name') or '').strip()
                    phone = str(it.get('phone') or '').strip()
                    if re.fullmatch(r'1\d{10}', phone):
                        items.append({'name': name or '责任人', 'phone': phone})
    except Exception:
        return jsonify(ok=False, msg='参数格式错误'), 400
    if not items:
        return jsonify(ok=False, msg='请至少选择一位要追加的责任人'), 400

    # 过滤：已在记录相关字段中的号码不重复追加（调试责任人/上级/责任单位/抄送人/已追加责任人）
    existing_phones = set()
    for src in ('debug_users', 'debug_phones', 'debug_sup', 'install_user',
                'area_leader', 'dept_leader', 'extra_users', 'extra_responsible'):
        existing_phones |= set(re.findall(r'1[3-9]\d{9}', str(rec.get(src) or '')))
    fresh = [it for it in items if it['phone'] not in existing_phones]
    if not fresh:
        return jsonify(ok=False, msg=rec_text('所选账号均已在记录中（调试责任人/上级/责任单位/抄送人），无需重复追加', rec.get('ship_id'))), 400

    # V1.4.5 防重复：同一人 60s 内对同一手尾追加同一批人重复提交 → 拦截
    fp = _idem_fingerprint(_actor_key(), 'add_responsible', rid,
                           ",".join(sorted(it['phone'] for it in fresh)))
    if not _idem_allow(fp):
        return _dup_blocked_json()

    added = db.append_extra_responsible(rid, fresh)
    if not added:
        return jsonify(ok=False, msg=rec_text('所选账号已全部在该收尾的责任人列表中', rec.get('ship_id'))), 400

    # 立即推送通知给新追加的责任人（可点击链接查看详情）
    site_url = get_site_url()
    _durl = detail_url(rid, rec.get('ship_id'))
    ship = db.get_ship(int(rec.get('ship_id') or 1))
    try:
        msg = (
            f"👤【收尾责任人追加通知】\n"
            f"系统：{rec.get('sys_name') or ''}\n"
            f"描述：{(rec.get('description') or '')[:80]}\n"
            f"您已被添加为该收尾的责任人，请关注处理进度！\n"
            f"👉 点击或复制下方链接查看该收尾详情：\n"
            f"{_durl}"
        )
        send_push(rec_text(msg, rec.get('ship_id')), [it['phone'] for it in fresh],
                  (ship or {}).get("webhook") or "",
                  mobile_click_url=_durl, rid=rid)
    except Exception as e:
        logger.warning(f"追加责任人推送失败(不影响保存): {e}")

    logger.info(f"追加责任人 - 记录ID: {rid}, 新增 {len(fresh)} 人: "
                f"{[it['phone'] for it in fresh]}, 操作人: {_current_phone()}")
    names = '、'.join(it['name'] for it in fresh)
    return jsonify(ok=True, msg=f'已追加责任人：{names}')


# ============ V1.4 用户中心：我的手尾 / 催办 ============

# ============ V1.7：「我的」——不登录也能看和"我"有关的一切 ============
# 设计：免登录没有登录框，改用"自选我是谁"（不弹窗、可随时切换、记 180 天）。
# 用户 2026-09-29 要求：两块看板一进去就有「我的」，能看到与"我"有关的任何事情。
MY_COOKIE = 'my_who'


def _name_of_phone(phone):
    """手机号 → 姓名：人员台账 → 账号表 →「我是谁」名单（历史责任人）兜底。

    V1.16.23 修「名单上看得见、点下去认到人却是空白」：
      原来只查前两个源，但名单 _all_people() 还有第三个源（records 里出现过的责任人），
      于是只在历史记录里当过责任人的人（张平 / 杨志刚 / 王海博 / 黄穗蓉 / 安全负责人）
      名单上能看见，点选后 session['my_name'] 却写成空串 —— 页面上就是一片空白。
      现在兜底统一走 _people_name_map()，**名单和认人必须是同一份数据**。
    """
    if not phone:
        return ''
    try:
        st = db.find_staff_by_phone(phone)
        if st and st.get('姓名'):
            return st['姓名']
    except Exception:
        pass
    try:
        u = db.get_user_by_phone(phone) or {}
        if u.get('name'):
            return u['name']
    except Exception:
        pass
    # 兜底：与「我是谁」名单同源（台账 + 两档账号 + 记录里的历史责任人）
    try:
        return _people_name_map().get(phone, '')
    except Exception:
        return ''


_phone_exist_cache = {'set': None, 'map': None, 'lock': threading.Lock()}


def _people_name_map() -> dict:
    """系统里出现过的 **手机号 → 姓名** 全量映射（「我是谁」名单同源）。

    V1.16.23：直接复用 _all_people()，不再自己重写一套解析。
    之前这里和 _all_known_phones() 各写一套、且只认六段台账格式，
    结果「张平 - 13760715033」这种**两段式**写法取不到号 ⇒
    名单上看得见、点下去却被拒 / 认到人却显示空白。
    **名单、校验、显示姓名必须是同一份数据**，否则永远对不齐。

    缓存：两档名单各算一次就存下；进程级缓存，改台账后需重启（或等 5 分钟 TTL）。
    """
    with _phone_exist_cache['lock']:
        m = _phone_exist_cache['map']
        if m is not None and (time.time() - _phone_exist_cache.get('ts', 0)) < 300:
            return m
        m = {SUPER_ADMIN_PHONE: '王平平'}
        for _biz in ('debug', 'safety'):
            try:
                for p in _all_people(_biz):
                    ph = (p.get('phone') or '').strip()
                    nm = (p.get('name') or '').strip()
                    if ph and nm and ph not in m:
                        m[ph] = nm
            except Exception:
                pass
        _phone_exist_cache['map'] = m
        _phone_exist_cache['ts'] = time.time()
        return m


def _all_known_phones() -> set:
    """系统里出现过的所有真实手机号 = **「我是谁」名单里的全部手机号**。
    与 _people_name_map() 同源（同一个 set），杜绝两处口径不一致。"""
    try:
        return set(_people_name_map().keys())
    except Exception:
        with _phone_exist_cache['lock']:
            if _phone_exist_cache['set'] is not None:
                return _phone_exist_cache['set']
            s = {SUPER_ADMIN_PHONE}
            _phone_exist_cache['set'] = s
            return s


def _phone_exists(phone) -> bool:
    """V1.16.22：这个手机号在系统里是不是**真实存在的人**。
    自选身份 / ?as= 都要过这一关，防止凭空造一个号就把自己变成别人。

    ⚠ 判定必须与「我是谁」名单**同源**（都走 _all_people）：早先两处各写一套解析，
    只在历史记录里以「姓名 - 手机号」两段式出现的人（如 张平 - 13760715033）
    名单上能看见、点下去却被拒。"""
    phone = (phone or '').strip()
    if not phone:
        return False
    if _name_of_phone(phone):
        return True
    return phone in _all_known_phones()


def _my_salt() -> str:
    """V1.16.22：每次生成自选名单时的一次性随机盐。
    令牌 = md5(手机号 + 本次盐 + 序号)，盐不落在 HTML 里，页面源码无法反推手机号。"""
    return secrets.token_hex(8)


def _looks_like_token(v) -> bool:
    """V1.16.22：判断这个值是「令牌」而不是手机号。
    令牌 = md5 截断的 12 位十六进制；手机号 = 11 位数字。两者不会撞。"""
    v = str(v or '').strip()
    if len(v) != 12:
        return False
    return all(ch in '0123456789abcdefABCDEF' for ch in v)


def _resolve_pick_token(tok) -> str:
    """把自选页发回来的令牌还原成手机号（还原不出来就返回空）。"""
    tok = (tok or '').strip()
    if not tok:
        return ''
    try:
        return (session.get('who_tmap') or {}).get(tok, '')
    except Exception:
        return ''


def _chain_groups(people):
    """把责任链人员按角色分组，供模板渲染成彩色小标签（照参考图的样式）。

    返回 [{'tag': '调试上级', 'names': '张三', 'cls': 'g1'}, ...]；
    cls 只决定左侧色块颜色，模板不写逻辑。
    """
    order = ['调试上级', '调试责任人', '责任主管', '施工负责主管',
             '作业区主管', '部门主管', '被派工人', '追加责任人']
    color = {'调试上级': 'g1', '调试责任人': 'g2', '责任主管': 'g3',
             '施工负责主管': 'g3', '作业区主管': 'g3', '部门主管': 'g4',
             '被派工人': 'g5', '追加责任人': 'g5'}
    bucket = {}
    for p in (people or []):
        tag = (p.get('tag') or '相关人').strip() or '相关人'
        nm = (p.get('name') or '').strip()
        if not nm:
            continue
        bucket.setdefault(tag, [])
        if nm not in bucket[tag]:
            bucket[tag].append(nm)
    keys = [t for t in order if t in bucket] + [t for t in bucket if t not in order]
    return [{'tag': t, 'names': '·'.join(bucket[t]), 'cls': color.get(t, 'g5')} for t in keys]


def _my_identity():
    """当前「我」是谁：优先会话，其次 cookie。返回 (phone, name)"""
    ph = ''
    nm = ''
    try:
        ph = (session.get('my_phone') or '').strip()
        nm = (session.get('my_name') or '').strip()
    except Exception:
        pass
    if not ph:
        ph = (request.cookies.get(MY_COOKIE) or '').strip()
    if ph:
        nm = _name_of_phone(ph) or nm
    return (ph or None), (nm or '')


def _my_role_of(phone, biz='debug'):
    """手机号 → (角色, 层级)。按账号表里的真实角色算，不再"人人都是超管"；
    查不到账号的（只是通讯录里的普通同事）按普通责任人：只看与自己相关的手尾。"""
    role = ''
    try:
        u = db.get_user_by_phone(phone) or {}
        if (u.get('biz') or 'debug') == biz:
            role = (u.get('role') or '').strip()
    except Exception:
        role = ''
    return role, ROLE_LEVEL.get(role, 1)


def _my_pending_approvals(me, recs):
    """等我审批的（A2：违章处罚 + 我在审批人名单里 + 还没出结果）"""
    out = []
    if not me:
        return out
    for r in recs:
        if (r.get('rec_type') or '') != '违章处罚':
            continue
        if (r.get('approval_status') or '审批中') not in ('审批中', '待审批', ''):
            continue
        if any((p.get('phone') or '') == me for p in _approvers_of(r)):
            out.append(r)
    return out


def _my_highlights(me, my_name=''):
    """我提交的亮点 / 问题（A5）。

    V1.35：加姓名兜底 —— 早期版本提交时没写 submitter_phone（实测 9-30 被驳回那条
    亮点就是空手机号），只按手机号查会让这些历史记录永远找不到，驳回后既看不到
    理由也没法改了重提。仅在 submitter_phone 为空时才按提交人姓名匹配。"""
    if not me:
        return []
    conn = db.get_conn()
    try:
        cur = conn.cursor()
        ph = '%s' if db.resolve_mode() == 'mysql' else '?'
        nm = (my_name or '').strip()
        if nm:
            cur.execute("SELECT * FROM highlights WHERE submitter_phone = " + ph +
                        " OR ((submitter_phone IS NULL OR TRIM(submitter_phone) = '') "
                        "     AND submitter = " + ph + ")" +
                        " ORDER BY id DESC LIMIT 50", (me, nm))
        else:
            cur.execute("SELECT * FROM highlights WHERE submitter_phone = " + ph +
                        " ORDER BY id DESC LIMIT 50", (me,))
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def _all_people(biz='debug'):
    """「我是谁」的候选名单：人员台账 + 本业务账号 + 记录里出现过的责任人。
    单靠人员台账不够（本地台账只有几个人），所以三处合并、按手机号去重。
    每人带一个 tag：角色名（有账号的）或来源。"""
    out = {}

    def add(name, phone, tag=''):
        name = (name or '').strip()
        phone = (phone or '').strip()
        if not name or not phone:
            return
        if phone in out:
            if tag and not out[phone].get('tag'):
                out[phone]['tag'] = tag
            return
        out[phone] = {'name': name, 'phone': phone, 'tag': tag or ''}

    # 1) 人员台账
    try:
        for st in db.get_staff_list():
            dept = '/'.join([x for x in (st.get('二级部门'), st.get('作业区')) if x])
            add(st.get('姓名'), st.get('电话'), dept)
    except Exception:
        pass

    # 2) 本业务账号（角色名优先）
    try:
        for u in db.get_all_users():
            if (u.get('biz') or 'debug') == biz:
                add(u.get('name'), u.get('phone'),
                    ROLE_LABELS.get(u.get('role') or '', '') or '已开通账号')
    except Exception:
        pass

    # 2.5) V1.39：本业务档「名单授权」的人（按部门 / 人员批量开通，没建账号也能被选到）
    try:
        _roster_people = (debug_user_grant if biz != 'safety' else safety_user_grant).people()
        _roster_tag = '普通用户（名单）' if biz == 'safety' else '调试责任人（名单）'
        for p in _roster_people:
            add(p.get('name'), p.get('phone'), _roster_tag)
    except Exception:
        pass

    # 3) 记录里出现过的责任人（历史数据里很多人在台账/账号里都没有）
    try:
        for r in db.get_records():
            for f in ('debug_sup', 'debug_users', 'debug_phones',
                      'install_user', 'area_leader', 'dept_leader'):
                for p in _persons_from_staff_str(r.get(f), ''):
                    add(p.get('name'), p.get('phone'), '责任人')
    except Exception:
        pass

    return sorted(out.values(), key=lambda x: (x['name'], x['phone']))


@app.route('/my/whoami', methods=['POST'])
def my_whoami():
    """选 / 换「我是谁」。phone 传空 = 忘掉身份。

    V1.16.22：免登录下这是「自选身份」的正门，必须保留（很多同事没走 SSO 靠它进）。
    但要挡住拿它当冒用后门：手机号必须是系统里**真实存在的人**
    （原来随便 POST 一个号，_name_of_phone 查不到也照样写进会话）。

    🔴 V1.16.27：这条入口**只能是弱身份**（id_src='pick'），拿不到任何管理权。
    这样「选个名字冒充别人」最多只能看/提自己的事，动不了用户管理和名单授权。
    另外配了 sso_require（强制单点登录）时，这条入口直接关掉 ——
    认不到人就是访客，不再给「自己声明一个身份」的机会。
    """
    phone = (request.form.get('phone') or '').strip()
    biz = (request.form.get('biz') or 'debug').strip()
    if biz not in ('debug', 'safety'):
        biz = 'debug'

    # 🔴 V1.16.28-17：**换人审批**与 ID_LOCK 解耦。
    #   早先这段整块写在 `if ID_LOCK:` 里面，结果 id_lock=false（默认）时
    #   "管理员已批准、点『用新身份』"整条路根本不执行 —— phone 保持空，
    #   一路走到下面把 session 里的身份**清空了**（第四轮验收 U6 实测）。
    #   换句话说：审批是独立功能，不该看 id_lock 脸色。两个模式都放行。
    _cur_now = (session.get('my_phone') or '').strip()
    _rtok = (request.form.get('rtok') or '').strip()
    if _rtok:
        # 🔴 归属校验：这张单必须是**当前这个人自己提的**。
        #   原来只查状态是不是 approved —— 拿着别人的单号（页面上看得见，也能手敲）
        #   就能切成那个人，等于开了个冒用后门。expect_from 由服务端传，不信前端。
        _req = db.apply_id_switch_req(_rtok, expect_from=_cur_now) \
            if (_rtok.isdigit() and _cur_now) else {}
        if not _req:
            logger.warning('换人申请单不可用，拒绝切换：单号=%s 当前身份=%s',
                           _rtok, _cur_now or '未认人')
            if not _cur_now:
                return ("<script>alert('还没认到人，无法使用换人申请。');"
                        "location.href='/sso/entry';</script>")
            return ("<script>alert('换人申请还没通过管理员审批，或这张单不是你自己提的，"
                    "现在不能换人。');history.back();</script>")
        phone = (_req.get('to_phone') or '').strip()
        if not phone:
            return ("<script>alert('这张申请单没有目标人员，已退回。');history.back();</script>")
        logger.info('换人申请 %s 已生效：%s -> %s（审批人 %s）',
                    _rtok, _cur_now or '未认人', phone, _req.get('approver') or '')

    elif ID_LOCK:
        # 身份锁：不给"自己声明一个身份"这条路。只放行切回自己。
        if not _cur_now:
            logger.warning('身份锁模式：拒绝未认证访客自选身份')
            return ("<script>alert('本系统已锁定身份，请从船海智云进入。');"
                    "location.href='/sso/entry';</script>")
        if _looks_like_token(phone):
            phone = _resolve_pick_token(phone) or ''
        elif phone and phone != _cur_now:
            logger.warning('身份锁模式：拒绝未经审批的换人 %s -> %s', _cur_now, phone)
            return ("<script>alert('本系统不允许直接换人身份。\\n"
                    "如果你是代他人操作，请在「我的」页面提交换人申请，"
                    "管理员审批通过后才会生效。');history.back();</script>")

    if SSO_REQUIRE and not (session.get('my_phone') or '').strip():
        logger.warning('强制单点登录模式：拒绝自选身份（未认证访客）')
        return ("<script>alert('本系统已启用船海智云单点登录，请从船海智云进入本页面。');"
                "location.href='/sso/entry';</script>")
    # V1.16.22：未认到人的访客，页面上只有令牌（没有真手机号）—— 先用令牌换回手机号。
    # ⚠ 前端 whoPick() 会把同一个值**同时**写进 phone 和 tok 两个 hidden，
    #   所以这里不能只判「phone 为空」，否则令牌会被当成手机号去查人 → 必然报
    #   「没找到这个人」（真实页面点不动，但只传 tok 的测试脚本能过 —— 坑就在这）。
    #   判据：看着像令牌（12 位十六进制、不是 11 位手机号）就先去换手机号。
    _tok = (request.form.get('tok') or '').strip()
    if _tok and not phone:
        phone = _resolve_pick_token(_tok)
    elif _tok and _looks_like_token(phone):
        phone = _resolve_pick_token(_tok) or _resolve_pick_token(phone)
    elif _looks_like_token(phone):
        phone = _resolve_pick_token(phone)
    if phone and not _phone_exists(phone):
        logger.warning(f"自选身份被拒：手机号不在系统人员中 - {phone}")
        return ("<script>alert('没找到这个人，请从名单里选择。');history.back();</script>")
    name = _name_of_phone(phone)
    try:
        session['my_phone'] = phone
        session['my_name'] = name
        # 🔴 V1.16.27：标记身份来源 = 自选声明（弱身份）。
        #   管理权要的是 'sso'，所以这里无论选谁、选几次，都拿不到管理权。
        session['id_src'] = 'pick' if phone else ''
        # V1.16.23：同步 user_name —— 页面顶栏和 /api/session 读的是 user_name，
        # 而 user_name 原先只由 load_user 钩子从**账号表**刷新；
        # 只在历史记录里当过责任人的人（张平/杨志刚/王海博/黄穗蓉/安全负责人）没有账号，
        # ⇒ 选完人顶栏仍是一片空白（表现和「没选上」一模一样，容易误判）。
        if name:
            session['user_name'] = name
        if phone:
            session['logged_in'] = True
            # V1.16.28：自选身份也是"认到人"，同样登记在线
            _register_online(phone, name)
        else:
            session.pop('logged_in', None)
    except Exception:
        pass
    resp = redirect('/my?biz=' + biz)
    try:
        resp.set_cookie(MY_COOKIE, phone, max_age=60 * 60 * 24 * 180,
                        httponly=True, samesite='Lax')
    except Exception:
        pass
    return resp


@app.route('/my')
def my_center():
    """我的：不登录也能用。

    免登录模式下页面先让人自选「我是谁」（记 session + cookie），再按这个身份汇总
    和"我"有关的一切：我提出的 / 我是责任人的手尾 + 等我审批的违章处罚 + 我提交的亮点。
    已登录模式沿用登录身份。"""
    my_biz = request.args.get('biz') or 'debug'
    if my_biz not in ('debug', 'safety'):
        my_biz = 'debug'
    if not NO_LOGIN and not _is_logged_in():
        # 未登录时回本业务的看板列表（否则安全业务会被甩到调试看板）
        return redirect(url_for('safety_board') if my_biz == 'safety' else url_for('boards'))

    # V1.9：支持 /my?as=手机号 直接用某个身份打开（免登录下等价于「选了我自己」，
    # 好处是链接可以带身份分享 —— 比如给某人发「以你的视角看」的直达链接）
    #
    # V1.16.22 安全加固：原来这个参数**零凭据**就能把会话换成任意人的身份
    # （实测 /my?as=17786363880 直接变成超级管理员，地址栏改个数字就能冒用任何人）。
    # 现在要求：① 本次请求已经带着**可信凭据**（SSO 认到人，或上一轮已自选过身份），
    #          ② 且 as 的手机号必须能在本系统人员里查到（不给凭空造一个号）。
    # 不满足就当成普通访客走「自选身份」页，绝不静默提权。
    #
    # 🔴 V1.16.26 补洞：V1.16.22 只堵了「**没认到人**时直接 as=」，漏了
    #   「**已经认到人**之后 as= 别人」—— 实测任何同事自选完身份，手敲
    #   /my?as=17786363880 就变成超级管理员，/admin/users、/admin/thresholds、
    #   /sixs/new 全部进得去，而且**一个字都没留痕**（登录日志里查不到）。
    #   这等于「自选身份」变成了万能提权后门，比原来的问题更隐蔽。
    # 修法（三条同时上）：
    #   ① **禁止提权**：目标身份的角色等级必须 ≤ 当前身份的角色等级，
    #      否则一律拒绝（想看别人的视角可以，切成超管不行）。
    #   ② **切自己永远允许**（_as == _cur），这是「切换身份」页的正常用法。
    #   ③ **一切切换都留痕**：写 login_logs（谁、从谁、切到谁、哪个 IP），
    #      成功失败都记，管理员能在「登录日志」页看到。
    _as = (request.args.get('as') or '').strip()
    _as_ok = False
    _as_why = ''
    # 🔴 V1.16.28 身份锁：as= 这条路整条关掉。
    #   它等价于"不经过选人页、直接把会话换成别人"，而身份锁的立意就是
    #   "进来就是本人、要换人得走审批"。留着它，审批就形同虚设。
    #   注意仍然要写登录日志 —— 谁试了、试的是谁，都得留痕。
    if _as and ID_LOCK:
        try:
            db.add_login_log(
                (session.get('my_phone') or '') or '未认人',
                _client_ip(), False,
                '身份锁定：拒绝用 as= 切换身份 → %s(%s)' % (_name_of_phone(_as) or '?', _as),
                my_biz)
        except Exception:
            pass
        logger.warning('身份锁定：拒绝 as= 换身份 -> %s', _as)
        return ("<script>alert('本系统已锁定身份，不能用链接切换身份。\\n"
                "需要代他人操作请提交换人申请。');history.back();</script>")
    if _as:
        _cur = (session.get('my_phone') or '').strip() or (request.cookies.get(MY_COOKIE) or '').strip()
        if _cur:
            if _as == _cur:
                _as_ok = True                      # 切回自己：本来就持有凭据，放行
            elif not _name_of_phone(_as):
                _as_why = '目标不是系统内人员'
            elif _role_of_phone(_as, my_biz) > _role_of_phone(_cur, my_biz):
                # ① 关键：只允许「往同级或更低」切。防的就是 as=王平平 变超管。
                _as_why = '目标身份权限高于当前身份，已拒绝（防越权提权）'
            else:
                _as_ok = True
        else:
            # 还没有身份：只允许从**官方入口页**带过来的一次性自选（带 sso 标记），
            # 直接在地址栏敲 ?as= 的一律拒绝。
            if request.args.get('sso') == '1' and _name_of_phone(_as):
                _as_ok = True
        if _as_ok:
            try:
                session['my_phone'] = _as
                session['my_name'] = _name_of_phone(_as)
                # 🔴 V1.16.27：as= 换来的身份是**弱身份**。
                #   不降级的话，这条路径就成了"绕过 SSO 直接变可信"的旁门 ——
                #   哪怕前面把 as= 的越权堵住了，弱身份也绝不能被当成 sso。
                # 🔴 V1.16.30：进一步把它标成 'as'（**独立于 'pick'**）。
                #   因为 V1.16.30 让「未强制模式下 'pick' 也算可信」，
                #   如果 as= 继续写 'pick'，那这个零凭据的地址栏链接
                #   就顺带拿到了管理权 —— 等于把刚堵上的冒用后门又打开一遍。
                #   口径：'pick'（自己声明，走名单校验）可当可信；'as'（链接换人）永远不可信。
                # 🔴 V1.16.33：切回自己（_as == _cur）时本来就持有可信凭据（pick/sso/pwd），
                #   不能覆盖成 'as' 降级 —— 否则「我的」显示王平平、点「用户管理」却报权限不足。
                if _as != _cur:
                    session['id_src'] = 'as'
            except Exception:
                pass
        # ③ 留痕：切换成功、或被拒（拒的原因更有价值）都写进登录日志
        try:
            db.add_login_log(
                (_cur or '未认人'),
                _client_ip(),
                bool(_as_ok),
                ('身份切换成功 → %s(%s)' % (_name_of_phone(_as) or _as, _as)) if _as_ok
                else ('身份切换被拒：%s → %s（%s）' % (_cur or '未认人', _as, _as_why or '不符合条件')),
                my_biz)
            logger.warning('身份切换 %s：%s -> %s（%s）',
                           '成功' if _as_ok else '被拒', _cur or '未认人', _as, _as_why or '')
        except Exception:
            pass
    me, my_name = _my_identity()
    pick = bool(request.args.get('pick'))
    _labels = SAFETY_ROLE_LABELS if my_biz == 'safety' else ROLE_LABELS
    th = get_thresholds_for(None, my_biz)

    if not me or pick:
        # 还没选人（或主动点「切换身份」）：渲染"我是谁"选择页。
        #
        # V1.16.22 安全加固：**未认到人的访客，HTML 里一个完整手机号都不能出现**
        # （原来 /my?pick=1 明文列出全员 37 个号，含超管王平平）。
        # 做法：访客只拿到「姓名 + 掩码号 + 随机令牌」，真号存在服务端 session 里，
        #       点选时提交令牌，由 /my/whoami 反查真号 —— 页面源码里套不出号码。
        # 已经认到人（SSO 或自选过）才给全量，这是同事自己看的「以你的视角看」。
        _people = _all_people(my_biz)
        _rows = []
        if me:
            for p in _people:
                _ph = (p.get('phone') or '').strip()
                _rows.append({'姓名': p.get('name') or '', '电话': _ph, 'tok': _ph,
                              'ds': p.get('tag') or ''})
        else:
            _tmap = {}
            for i, p in enumerate(_people):
                _ph = (p.get('phone') or '').strip()
                if not _ph:
                    continue
                _tok = hashlib.md5(
                    ('%s|%s|%d' % (_ph, _my_salt(), i)).encode('utf-8')).hexdigest()[:12]
                _tmap[_tok] = _ph
                _rows.append({'姓名': p.get('name') or '', '电话': _mask_phone(_ph), 'tok': _tok,
                              'ds': p.get('tag') or ''})
            try:
                session['who_tmap'] = _tmap
            except Exception:
                pass
        return render_biz('my.html', my_biz, need_ident=True, pick=pick, sso=False,
                          staff_list=(_rows if not ID_LOCK else []),
                          # 🔴 V1.16.28 身份锁：不给名单，改成"我是代操作 → 提申请"。
                          id_lock=ID_LOCK,
                          rows=[], approvals=[], highlights=[],
                          user_name='', user_phone='', role='', role_label='',
                          l1=th['l1'], l2=th['l2'], global_view=False,
                          # 🔴 V1.16.27：告诉页面当前是强身份(SSO)还是弱身份(自选)，
                          #   以及要不要提示"建议从船海智云进入"。
                          ident_trusted=_identity_trusted(),
                          sso_require=SSO_REQUIRE)

    if NO_LOGIN:
        role, lvl = _my_role_of(me, my_biz)
    else:
        role, lvl = _current_role(), _role_level()

    _biz_ships_list = db.get_ships(my_biz)
    _biz_ships = {s['id']: (s.get('name') or '看板') for s in _biz_ships_list}
    ships = _biz_ships
    all_recs = [r for r in db.get_records() if r.get('ship_id') in _biz_ships]
    dispatch_map = db.get_dispatch_logs_map([r['id'] for r in all_recs])
    rows = []
    for r in all_recs:
        logs = dispatch_map.get(r['id'], [])
        people = _chain_people(r, logs)
        is_mine = (str(r.get('submitted_by') or '') == me)
        is_resp = any(p['phone'] == me for p in people)
        if lvl < 2 and not (is_mine or is_resp):
            continue
        rows.append({
            'id': r['id'],
            # 闭环判定在 Python 侧算好，模板不再拿中文状态做字符串比较
            'is_closed': (r.get('status') in ('已闭环', '已完成')),
            'ship_name': ships.get(r.get('ship_id') or 1, '看板'),
            'sys_name': r.get('sys_name') or '',
            'desc': (r.get('description') or ''),
            'submit_time': r.get('submit_time') or '',
            'close_time': r.get('close_time') or '',
            'status': r.get('status') or '未完成',
            'display_status': compute_display_status(r.get('status') or '未完成', r.get('submit_time'), r.get('ship_id'), r.get('deadline')),
            'is_mine': is_mine,
            'is_resp': is_resp,
            'chain_summary': _chain_summary(r, logs),
            'chain_groups': _chain_groups(people),
            'urge_cnt': len([p for p in people if p['phone'] != me]),
        })
    rows.sort(key=lambda x: (x['submit_time'] or ''), reverse=True)

    # A2：等我审批的违章处罚（带看板名一起给模板）
    appr_rows = []
    for r in _my_pending_approvals(me, all_recs):
        appr_rows.append({
            'id': r['id'],
            'ship_name': ships.get(r.get('ship_id') or 1, '看板'),
            'sys_name': r.get('sys_name') or '',
            'desc': (r.get('description') or ''),
            'submit_time': r.get('submit_time') or '',
            'submitter': r.get('submitter') or r.get('submitted_name') or '',
            'appr_count': len(_approvers_of(r)),
        })

    # A5：我提交的亮点 / 问题
    # V1.35：把驳回理由 / 原文 / 照片 / 归属一并发给模板 —— 「我的」页要能编辑重提、能撤回，
    #   以前这里只传状态，被驳回的记录在界面上等于消失（数据其实一直留在 highlights 表里）。
    hl_rows = []
    _, _hl_my_name = _my_identity()
    for h in _my_highlights(me, _hl_my_name):
        hl_rows.append({
            'id': h.get('id'),
            'kind': h.get('kind') or 'highlight',
            'title': h.get('title') or '',
            'descr': h.get('descr') or '',
            'status': h.get('status') or '',
            'reward': h.get('reward') or '',
            'punish': h.get('punish') or '',
            'created_at': h.get('created_at') or '',
            'ship_id': h.get('ship_id'),
            'ship_name': ships.get(h.get('ship_id'), '看板'),
            'photo_path': h.get('photo_path') or '',
            'reviewer': h.get('reviewer') or '',
            'review_note': h.get('review_note') or '',
            'review_time': h.get('review_time') or '',
        })
    # 有被驳回 / 已撤回的记录时，「我的」页那个区块默认展开，别让用户再找一遍
    hl_todo = sum(1 for h in hl_rows if h['status'] in ('rejected', 'withdrawn'))

    # A5b：待我审核的亮点（V1.17.0：审亮点入口从「亮点展示」挪到「我的」）
    hl_review_rows = []
    for h in db.get_highlights_multi(list(_biz_ships.keys()), 'pending'):
        if _hl_can_review(h, me, role):
            hl_review_rows.append({
                'id': h.get('id'),
                'kind': h.get('kind') or 'highlight',
                'title': h.get('title') or '',
                'descr': h.get('descr') or '',
                'photo_path': h.get('photo_path') or '',
                'submitter': h.get('submitter') or '',
                'created_at': h.get('created_at') or '',
                'ship_id': h.get('ship_id'),
                'ship_name': ships.get(h.get('ship_id'), '看板'),
                'appr_list': _approvers_of(h),
            })

    sso = bool(session.get('sso_at'))
    # 🔴 V1.16.28：告诉页面"身份锁开了"——「切换身份」要换成「我要代操作」。
    #   另外把"别处还开着一端"的情况也传下去，页面顶部给个提示 + 顶替入口。
    _others = _online_others(me) or {}
    # takeover 存的是 _ua_short 过的简写，别再 _ua_short 一次（会变成"电脑·电脑"）
    _others_ua = (_others.get('ua') or '')
    return render_biz('my.html', my_biz, need_ident=False, sso=sso,
                      rows=rows, approvals=appr_rows, highlights=hl_rows,
                      hl_todo=hl_todo,
                      hl_review=hl_review_rows,
                      # V1.19：亮点提交表单从「亮点展示」挪到「我的」，这里补传选人名单/区域/船
                      hl_reviewers=_safety_approver_list(),
                      hl_areas=_hl_areas(_biz_ships_list),
                      hl_ship_id=(_biz_ships_list[0]['id'] if _biz_ships_list else 0),
                      hl_ship=(db.get_ship(_biz_ships_list[0]['id']) if _biz_ships_list else None),
                      role=role, role_label=_labels.get(role, ''),
                      user_name=my_name or session.get('user_name', ''),
                      user_phone=me,
                      id_lock=ID_LOCK,
                      online_guard=ONLINE_GUARD,
                      online_elsewhere=_others_ua,
                      online_elsewhere_at=((_others.get('at') or '')
                                           or (_others.get('created_at') or '')[-8:]),
                      l1=th['l1'], l2=th['l2'], global_view=(lvl >= 2))


# ================= V1.16.28 在线管控 / 换人审批 =================

def _online_page_html(title, body, foot=''):
    """在线相关页面的统一外壳。刻意做成一整页自带样式：
    这几个页面常常是**唯一**能打开的页面（人被踢了以后哪都去不了），
    不能依赖 base.html / 静态资源，否则一脚踢出去就成了白屏。"""
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<title>' + title + '</title><style>'
        'body{margin:0;padding:16px 14px;background:#eef1f6;color:#1c2331;'
        'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",'
        '"Microsoft YaHei",sans-serif}'
        '.w{max-width:760px;margin:0 auto}'
        '.c{background:#fff;border:1px solid #e3e7ee;border-radius:14px;padding:16px;margin-bottom:11px}'
        '.h{font-size:18px;font-weight:700;margin:0 0 8px}'
        '.k{font-size:13.5px;line-height:1.85;color:#39415a}'
        '.k b{color:#111827}'
        '.warn{background:#fff7ed;border:1px solid #fed7aa;border-left:3px solid #ea580c;'
        'border-radius:10px;padding:11px 13px;font-size:13px;line-height:1.8;color:#7c2d12}'
        '.bad{background:#fef2f2;border:1px solid #fecaca;border-left:3px solid #dc2626;'
        'border-radius:10px;padding:11px 13px;font-size:13px;line-height:1.8;color:#991b1b}'
        '.ok{background:#f0fdf4;border:1px solid #bbf7d0;border-left:3px solid #16a34a;'
        'border-radius:10px;padding:11px 13px;font-size:13px;line-height:1.8;color:#14532d}'
        'table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}'
        'th,td{border-bottom:1px solid #eef1f6;padding:9px 6px;text-align:left;vertical-align:top}'
        'th{color:#6b7488;font-weight:600;font-size:12px;background:#f8fafc}'
        '.btn{display:inline-block;font-size:13px;padding:9px 15px;border-radius:9px;'
        'background:#1d4ed8;color:#fff;text-decoration:none;border:0;cursor:pointer;font-family:inherit}'
        '.btn2{background:#fff;color:#1d4ed8;border:1px solid #c7d2fe}'
        '.btnr{background:#dc2626;color:#fff}'
        '.btnr2{background:#fff;color:#dc2626;border:1px solid #fecaca}'
        '.sm{font-size:12px;color:#6b7488;line-height:1.8}'
        '.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:#6b7488}'
        'input,select,textarea{font-family:inherit;font-size:13.5px;padding:8px 10px;'
        'border:1px solid #d7dde7;border-radius:9px;background:#fff;width:100%;box-sizing:border-box}'
        'textarea{min-height:76px;resize:vertical}'
        'label{display:block;font-size:13px;color:#39415a;margin:11px 0 5px}'
        '.row{display:flex;gap:9px;flex-wrap:wrap;margin-top:12px}'
        '.pill{display:inline-block;font-size:11.5px;padding:2px 8px;border-radius:99px;'
        'background:#eef2ff;color:#3730a3;margin-left:6px}'
        '.pill.w{background:#fff7ed;color:#9a3412}'
        '.pill.g{background:#f0fdf4;color:#15803d}'
        '.pill.r{background:#fef2f2;color:#b91c1c}'
        '</style></head><body><div class="w">'
        + body + foot +
        '</div></body></html>')


def _safe_back(raw, default=''):
    """V1.16.30：校验"提交后跳回哪儿"，**只放行站内绝对路径**。

    为什么必须白名单：表单里的 `back` 是客户端可控的。
    直接 `redirect(request.form['back'])` 等于开放重定向漏洞 ——
    改一下这个参数就能把用户从"强制退出/审批"之后骗到钓鱼站上去。
    规则：以单个 `/` 开头（不是 `//`、不是 `/\`、不含 `://`）才算站内。
    任何不满足的一律回落到默认值。
    """
    try:
        s = (raw or '').strip()
    except Exception:
        s = ''
    if not s or not s.startswith('/'):
        return default
    if s.startswith('//') or s.startswith('/\\') or '://' in s or '\r' in s or '\n' in s:
        return default
    return s


def _ua_short(ua):
    """把 UA 压成一句人话：手机 / 电脑 + 大致浏览器。"""
    u = (ua or '').lower()
    if not u:
        return '未知设备'
    mob = ('android' in u) or ('iphone' in u) or ('ipad' in u) or ('mobile' in u)
    dev = '手机' if mob else '电脑'
    br = ''
    for k, v in (('edg', 'Edge'), ('chrome', 'Chrome'), ('firefox', 'Firefox'),
                 ('micromessenger', '微信'), ('wxwork', '企业微信'), ('safari', 'Safari')):
        if k in u:
            br = v
            break
    return dev + (('·' + br) if br else '')


def _online_view(biz=''):
    """在线名单的**数据准备**（V1.16.30 新增）。

    为什么要抽出来：`/online` 和「用户管理 → 在线管理」标签是同一件事的两个入口，
    权限口径（谁能看全部、谁能踢人、能不能跨档看）**必须完全一致**。
    复制一份的话，改了一处忘了另一处，就会出现「在用户管理里看得到全部人、
    在在线页只看得到自己」这种口径打架 —— 而且很难发现。

    返回 (rows, can_manage, others)：
      · rows       —— 已按业务档过滤、并按权限裁剪过的在线名单
      · can_manage —— 能否看全部人 + 能否强制退出
      · others     —— 「你这个账号在别处已经开着」那一条，没有就是 {}
    """
    me = _current_identity_phone()
    can_manage = False
    if me:
        try:
            # 按档看时权限也按那一档判 —— 调试档的上级不该从调试入口看到安全档名单。
            if biz:
                can_manage = bool(_biz_admin(biz) or _can_manage_biz(biz))
            elif _biz_admin('debug') or _biz_admin('safety'):
                can_manage = True
            else:
                can_manage = bool(_can_manage_biz('debug') or _can_manage_biz('safety'))
        except Exception:
            can_manage = False

    rows = []
    if ONLINE_GUARD:
        try:
            for r in db.get_online_list(biz=biz):
                ph = (r.get('phone') or '').strip()
                # 非管理权的人只看到"自己"
                if not can_manage and ph != me:
                    continue
                nm = (r.get('name') or '').strip() or _name_of_phone(ph) or '(无名)'
                rows.append({
                    'phone': ph,
                    'phone_show': _mask_phone(ph) if not can_manage else ph,
                    'name': nm,
                    'is_me': (ph == me),
                    'ua': _ua_short(r.get('ua')),
                    'ip': (r.get('ip') or ''),
                    'since': (r.get('created_at') or ''),
                    'seen': (r.get('last_seen') or ''),
                    'tok': (r.get('sess_tok') or ''),
                })
        except Exception as e:
            logger.warning('在线名单读取失败: %s' % e)

    others = _online_others(me) if me else {}
    # 🔴 V1.16.36（用户要求「超管一上线就可以用」）：超管豁免在线管控，
    #   所以「你这个账号在别处已经开着」这条红提示对超管没有意义 ——
    #   留着只会让人以为系统出问题了。在**数据层**清掉，`/online` 与
    #   「用户管理 → 在线管理」两个入口一次都覆盖（放渲染层要改两处，漏一处就打架）。
    if others and _online_super_exempt(me):
        others = {}
    return rows, can_manage, others


@app.route('/online')
def online_list_page():
    """在线名单：看现在谁在用这个系统。

    谁能看：① 自己（只想知道"我是不是在两处开着"）；
           ② 有管理权的人（`_can_manage_biz` 或超管）—— 才能看到全部人 + 踢人按钮。
    """
    me = _current_identity_phone()
    # V1.16.29：门户里两个业务档各挂一个在线管理入口，带 `?biz=` 按档过滤；
    #   不带参数 = 看全部（`/my` 里那个链接走的就是这条，保留原行为）。
    biz = (request.args.get('biz') or '').strip()
    if biz not in ('debug', 'safety'):
        biz = ''
    # 🔴 V1.16.30：数据准备抽到 `_online_view()`，与「用户管理 → 在线管理」标签共用一份。
    #   原来这段逻辑整段写在这里，现在两个入口各写一份的话，改一处忘一处
    #   就会出现「用户管理里看得到全部人、在线页只看得到自己」的口径打架。
    rows, can_manage, others = _online_view(biz)
    bizname = {'debug': '调试', 'safety': '安全'}.get(biz, '')
    o = []
    o.append('<div class="c">')
    o.append('<div class="h">当前在线%s%s</div>'
             % ('（%s业务）' % bizname if bizname else '',
                '（管理视图）' if can_manage else '（你自己的）'))
    if not ONLINE_GUARD:
        o.append('<div class="warn">在线管控未开启（config.json 的 online_guard = false）。'
                 '现在还看不出谁在线，也没法强制退出。</div>')
    # 🔴 V1.16.36（用户要求「超管一上线就可以用」）：把豁免规则摆明，
    #   否则管理员会疑惑"为什么超管那行没有强制退出按钮"。
    if ONLINE_GUARD and can_manage:
        o.append('<div class="k">超级管理员不受在线管控：一上线就能用，'
                 '别人顶不掉 TA，也点不了「强制退出」TA（防止把唯一超管锁在系统外）。</div>')
    if others:
        o.append('<div class="warn"><b>你这个账号在别处已经开着：</b>'
                 '%s，%s 起，最近活动 %s%s'
                 % (_mask_phone(others.get('phone') or ''),
                    (others.get('created_at') or '')[-8:],
                    (others.get('last_seen') or '')[-8:],
                    '（IP %s）' % (others.get('ip') or '未知'))
                 + '</div>')
    if not rows:
        o.append('<div class="k">现在没有人在用（超过 %d 分钟没动的不算在线）。</div>'
                 % db.ONLINE_IDLE_MIN)
    else:
        o.append('<table><tr><th>姓名</th><th>设备</th><th>最近活动</th>'
                 + ('<th>操作</th>' if can_manage else '') + '</tr>')
        for r in rows:
            o.append('<tr><td><b>%s</b>%s<div class="mono">%s</div></td>'
                     '<td>%s<div class="mono">IP %s</div></td>'
                     '<td>%s</td>'
                     % (_html_mod.escape(r['name']),
                        '<span class="pill g">是你</span>' if r['is_me'] else '',
                        _html_mod.escape(r['phone_show']),
                        r['ua'], r['ip'] or '-', (r['seen'] or '')[-8:]))
            if can_manage:
                if r['is_me']:
                    o.append('<td class="sm">当前这一端</td>')
                elif _online_super_exempt(r.get('phone') or ''):
                    # 🔴 V1.16.36：超管行不给「强制退出」——不给点，才不会手滑把自己踢出去。
                    o.append('<td class="sm">超管（不受管控）</td>')
                else:
                    o.append('<td><form method="post" action="/online/kick" '
                             'onsubmit="return confirm(\'确定把 %s 强制退出？\\n'
                             'TA 下一次操作就会被请出去。\');">'
                             '<input type="hidden" name="phone" value="%s">'
                             '<input type="hidden" name="tok" value="%s">'
                             '<input type="hidden" name="biz" value="%s">'
                             '<button class="btn btnr2" type="submit">强制退出</button>'
                             '</form></td>'
                             % (_html_mod.escape(r['name']),
                                _html_mod.escape(r['phone']),
                                _html_mod.escape(r['tok']),
                                _html_mod.escape(biz)))
            o.append('</tr>')
        o.append('</table>')
    o.append('</div>')

    # 顶替入口：别人已经开着时，给他一个"我来顶掉那一端"的按钮
    if others:
        o.append('<div class="c"><div class="h">要在这台设备上用？</div>')
        o.append('<div class="k">下面这个按钮会顶掉别处的那一端（相当于"我接手了"）。'
                 '按你定的口径，不做硬锁，所以随时可以顶。</div>')
        o.append('<form method="post" action="/online/kick">'
                 '<input type="hidden" name="phone" value="%s">'
                 '<input type="hidden" name="takeover" value="1">'
                 '<input type="hidden" name="biz" value="%s">'
                 '<div class="row"><button class="btn" type="submit">顶替并继续使用</button>'
                 '</div></form></div>'
                 % (_html_mod.escape(me or ''), _html_mod.escape(biz)))

    foot = ('<div class="row"><a class="btn btn2" href="/my">回到「我的」</a>'
            '<a class="btn btn2" href="/">回到首页</a></div>')
    return _online_page_html('当前在线', ''.join(o), foot)


@app.route('/online/kick', methods=['POST'])
def online_kick():
    """顶替 / 强制退出。

    两种用法共用一个入口，靠 take_``over 参数区分：
      · take=1  —— 自己顶掉别处那一端（只要自己认到人）；
      · 否则    —— 管理员强制踢某人（必须 `_biz_admin`，即该档超管）。
    """
    me = _current_identity_phone()
    _ph = (request.form.get('phone') or '').strip()
    _tok = (request.form.get('tok') or '').strip()
    _takeover = (request.form.get('takeover') or '').strip() in ('1', 'true', 'on')
    # 🔴 表单里那个 name="tok"（被踢那人的令牌）**只用于页面回显/审计，不要拿它当
    #   `only_others` 传给 kick_online** —— 那会把要踢的那一行自己排除掉（见下面）。
    # V1.16.29：踢完回原来那一档的名单（门户里两档各一个入口，踢完不该跳到"全部"）
    _biz = (request.form.get('biz') or '').strip()
    if _biz not in ('debug', 'safety'):
        _biz = ''
    # 🔴 V1.16.30：允许提交方指定"踢完回哪儿"（用户管理里的在线管理标签传
    #   /admin/users?biz=..&tab=online）。**必须白名单校验** —— 否则就是个
    #   开放重定向漏洞：改一下 back 就能把用户骗去任意站点（钓鱼）。
    _back = _safe_back(request.form.get('back'),
                       '/online?biz=' + _biz if _biz else '/online')

    if not me:
        return _online_page_html('请先进入', '<div class="c"><div class="bad">'
                                 '还没认到人，无法操作在线状态。</div></div>',
                                 '<div class="row"><a class="btn" href="/sso/entry">'
                                 '去单点登录</a></div>')

    if _takeover:
        # 顶替：只能顶**自己**别处的那一端，且不能带上别人的令牌
        if _ph and _ph != me:
            return _online_page_html('不能顶替', '<div class="c"><div class="bad">'
                                     '只能顶替自己别处的那一端。</div></div>')
        n = db.kick_online(me, by_name='本人顶替', only_others=_sess_token())
        logger.info('本人顶替别处会话：%s（%d 条）', _name_of_phone(me) or me, n)
        # 顶完把本端重新登记一次（take_over=True ⇒ 别处那条即使漏网也会被清掉）
        try:
            db.online_login(me, _name_of_phone(me), _sess_token(),
                            ua=_client_ua(), ip=_client_ip(),
                            biz=(_request_biz() or 'debug'), take_over=True)
        except Exception:
            pass
        session.pop('takeover', None)
        return redirect(_back)

    # 强制踢人：只有该档超管能做
    if not (_biz_admin('debug') or _biz_admin('safety')):
        logger.warning('强制退出被拒（无超管权）：%s 想踢 %s', me, _ph)
        return _online_page_html('权限不足', '<div class="c"><div class="bad">'
                                 '只有超级管理员可以强制退出别人。</div></div>',
                                 '<div class="row"><a class="btn btn2" href="%s">'
                                 '返回在线名单</a></div>' % _back)
    if not _ph:
        return _online_page_html('参数缺失', '<div class="c"><div class="warn">'
                                 '没指定要退出谁。</div></div>')
    # 🔴 V1.16.36（用户报障「我需要超管一上线就可以用」）：**不许踢超管**。
    #   兜住这个场景：在线名单上超管自己也在里面，手滑点自己那行的「强制退出」，
    #   就会把自己踢出系统 ⇒ 唯一超管被锁在门外，谁也进不去管理页。
    #   （前面 `_online_gate` 已经让超管不会被踢生效，这里是第二道：写入前就拒。）
    if _online_super_exempt(_ph):
        logger.warning('强制退出被拒（目标是超管）：%s 想踢 %s', me, _ph)
        return _online_page_html('不能强制退出', '<div class="c"><div class="bad">'
                                 '超级管理员不受在线管控，不能被强制退出。<br>'
                                 '（这是为了避免唯一超管把自己锁在系统外）</div></div>',
                                 '<div class="row"><a class="btn" href="%s">'
                                 '返回在线名单</a></div>' % _back)
    n = db.kick_online(_ph, by_name=_name_of_phone(me) or me)
    # 🔴 V1.16.29 修：这里原来传的是 `only_others=_tok`（表单里那个 hidden），
    #   而表单的 tok 是**被踢那个人**的令牌 ⇒ `kick_online` 拼出
    #   `AND sess_tok <> <受害者tok>`，恰好把要踢的那一行排除掉
    #   ⇒ 超管点「强制退出」返回 302 看着成功，其实一条都没踢（实测 rowcount=0）。
    #   `only_others` 是给**本人顶替**用的（"只踢别处那一端、别踢我自己"），
    #   超管踢人必须全踢（对方手机号跟超管不同，本来也不会误伤自己这一端）。
    try:
        db.add_login_log(me, _client_ip(), True,
                         '强制退出：%s(%s)（%d 条会话）' % (_name_of_phone(_ph) or '?', _ph, n),
                         _request_biz() or 'debug')
    except Exception:
        pass
    logger.warning('超管强制退出：%s 被 %s 踢出（%d 条）', _ph, me, n)
    return redirect(_back)


@app.route('/online/kicked')
def online_kicked_page():
    """「你已被强制退出」提示页。

    刻意写得非常直白：说清是谁被踢了、为什么（被顶替还是被超管踢）、
    下一步该怎么办（回船海智云重新进）。不写清楚的话，
    同事只会以为"系统坏了"，然后到处问。
    """
    nm = (session.get('kicked_name') or '').strip()
    ph = (session.get('kicked_phone') or '').strip()
    by = (session.get('kicked_by') or '').strip()
    for k in ('kicked_name', 'kicked_phone', 'kicked_by'):
        session.pop(k, None)

    if not ph:
        # 没有被踢的记录 = 主动访问（比如从收藏夹点进来的），当作普通说明页
        body = ('<div class="c"><div class="h">在线状态</div>'
                '<div class="k">你的身份是正常的。这里是"被强制退出"的提示页，'
                '你现在没有被踢出。</div></div>')
        return _online_page_html('在线状态', body,
                                 '<div class="row"><a class="btn" href="/">回到首页</a>'
                                 '<a class="btn btn2" href="/my">我的</a></div>')

    reason = ('原因：%s' % _html_mod.escape(by)) if by else '原因：同一账号在别处重新登录了'
    body = ('<div class="c">'
            '<div class="h">你已被强制退出</div>'
            '<div class="bad">%s（%s）<br>%s</div>'
            '<div class="k" style="margin-top:12px">'
            '系统里一个账号同一时间只保留一个地方在用。'
            '如果你刚才在另一台设备（手机 / 电脑）上重新进了，'
            '那边会把这一端顶掉；如果是管理员把你请出去的，'
            '请找他确认原因。<br><br>'
            '<b>下一步：</b>回船海智云重新点一次应用图标进入就行，身份会自动认出来。</div>'
            '</div>'
            % (_html_mod.escape(nm or '(未知人员)'), _mask_phone(ph), reason))
    return _online_page_html('你已被强制退出', body,
                             '<div class="row"><a class="btn" href="/sso/entry">'
                             '回船海智云重新进入</a>'
                             '<a class="btn btn2" href="/">首页</a></div>')


# ---------- 换人申请（代操作审批） ----------

@app.route('/my/idreq', methods=['GET', 'POST'])
def my_idreq():
    """「我的」里的换人申请：提单 + 看我的单子。

    用户口径是「只用于代操作，要审批」—— 所以这张单子的用途被写死在提示语里：
    帮别人填隐患 / 帮别人闭环这类**代操作**，才需要换人；
    想看别人的视角不走这里（那条路 id_lock 打开后已经关了）。
    """
    me = _current_identity_phone()
    my_biz = request.args.get('biz') or request.form.get('biz') or 'debug'
    if my_biz not in ('debug', 'safety'):
        my_biz = 'debug'
    if not me:
        return _online_page_html('请先进入', '<div class="c"><div class="warn">'
                                 '还没认到人，不能提交换人申请。</div></div>',
                                 '<div class="row"><a class="btn" href="/sso/entry">'
                                 '去单点登录</a></div>')

    msg = ''
    msg_kind = ''
    if request.method == 'POST':
        to_phone = (request.form.get('to_phone') or '').strip()
        reason = (request.form.get('reason') or '').strip()
        if not to_phone:
            msg, msg_kind = '请选择要切换到谁。', 'warn'
        elif to_phone == me:
            msg, msg_kind = '这就是你自己，不需要申请。', 'warn'
        elif not _phone_exists(to_phone):
            msg, msg_kind = '系统里查不到这个人，请从名单里选。', 'warn'
        elif len(reason) < 4:
            msg, msg_kind = '请写清楚为什么要代操作（至少 4 个字），管理员要据此判断。', 'warn'
        else:
            try:
                rid = db.add_id_switch_req(me, _name_of_phone(me), to_phone,
                                           _name_of_phone(to_phone), reason, my_biz)
                try:
                    db.add_login_log(me, _client_ip(), True,
                                     '提交换人申请 #%d → %s(%s)：%s'
                                     % (rid, _name_of_phone(to_phone), to_phone, reason[:40]),
                                     my_biz)
                except Exception:
                    pass
                logger.info('换人申请：%s → %s（单号 %d）', me, to_phone, rid)
                msg, msg_kind = ('申请已提交（单号 #%d）。'
                                 '**审批通过之前，你的身份不变**；'
                                 '批了之后回到这个页面点「用新身份」才生效。' % rid, 'ok')
            except Exception as e:
                msg, msg_kind = '提交失败：%s' % e, 'bad'

    o = []
    if msg:
        cls = {'ok': 'ok', 'warn': 'warn', 'bad': 'bad'}[msg_kind]
        o.append('<div class="c"><div class="%s">%s</div></div>' % (cls, msg))

    # 我提过的单子
    mine = []
    try:
        mine = db.my_id_switch_reqs(me)
    except Exception as e:
        logger.warning('换人申请列表读取失败: %s' % e)
    if mine:
        o.append('<div class="c"><div class="h">我提交过的申请</div><table>'
                 '<tr><th>单号</th><th>切到</th><th>状态</th><th>时间</th><th>操作</th></tr>')
        for r in mine:
            st = (r.get('status') or 'pending')
            pill = {'pending': '<span class="pill w">待审批</span>',
                    'approved': '<span class="pill g">已批准</span>',
                    'rejected': '<span class="pill r">已驳回</span>',
                    'applied': '<span class="pill">已生效</span>'}.get(st, st)
            note = ''
            if st == 'approved':
                note = ('<form method="post" action="/my/whoami" '
                        'onsubmit="return confirm(\'确认用新身份？\\n'
                        '切换后你就是 %s，直到重新从船海智云进入。\');">'
                        '<input type="hidden" name="rtok" value="%s">'
                        '<input type="hidden" name="biz" value="%s">'
                        '<button class="btn" type="submit">用新身份</button></form>'
                        % (_html_mod.escape(r.get('to_name') or r.get('to_phone') or ''),
                           int(r.get('id') or 0), _html_mod.escape(r.get('biz') or 'debug')))
            elif r.get('approver_note'):
                note = '<div class="sm">批注：%s</div>' % _html_mod.escape(r['approver_note'])
            o.append('<tr><td>#%s</td>'
                     '<td>%s<div class="mono">%s</div></td>'
                     '<td>%s%s</td>'
                     '<td class="sm">%s<br>%s</td>'
                     '<td>%s</td></tr>'
                     % (int(r.get('id') or 0),
                        _html_mod.escape(r.get('to_name') or '?'),
                        _mask_phone(r.get('to_phone') or ''),
                        pill,
                        ('<div class="sm">%s</div>' % _html_mod.escape(r.get('approver') or ''))
                        if r.get('approver') else '',
                        (r.get('created_at') or '')[5:16],
                        (r.get('handled_at') or '')[5:16],
                        note))
        o.append('</table></div>')

    # 提单表单（已批准的单子存在时就不再重复引导提单）
    if not [r for r in mine if (r.get('status') or '') == 'approved']:
        ppl = []
        try:
            for p in _all_people(my_biz):
                _p = (p.get('phone') or '').strip()
                if _p and _p != me:
                    ppl.append((_html_mod.escape(p.get('name') or ''), _p))
        except Exception:
            ppl = []
        o.append('<div class="c"><div class="h">我要代他人操作</div>')
        o.append('<div class="warn">本系统已锁定身份：进来的就是你本人。'
                 '只有**代别人操作**（帮同事填隐患、帮同事闭环）才需要换人，'
                 '而且要走管理员审批 —— <b>没批之前你始终是原来那个人</b>。</div>')
        o.append('<form method="post" action="/my/idreq">')
        o.append('<input type="hidden" name="biz" value="%s">' % _html_mod.escape(my_biz))
        o.append('<label>要切换到谁</label><select name="to_phone" required>')
        o.append('<option value="">— 请选择 —</option>')
        for nm2, ph2 in ppl[:400]:
            o.append('<option value="%s">%s（%s）</option>'
                     % (_html_mod.escape(ph2), nm2, _mask_phone(ph2)))
        o.append('</select>')
        o.append('<label>为什么要代操作</label>'
                 '<textarea name="reason" required '
                 'placeholder="例：我在值班，张工的隐患我代填并已电话确认"></textarea>')
        o.append('<div class="row"><button class="btn" type="submit">提交申请，等审批</button></div>')
        o.append('</form>')
        o.append('<div class="sm" style="margin-top:10px">'
                 '审批人：该业务档的超级管理员（当前：%s）。'
                 '审批记录会留痕，登录日志里也查得到。</div>'
                 % ('你自己（你就是超管）' if _biz_admin(my_biz) else '超管名单里的人'))
        o.append('</div>')

    foot = ('<div class="row"><a class="btn btn2" href="/my?biz=%s">回到「我的」</a>'
            '<a class="btn btn2" href="/online">当前在线</a></div>' % _html_mod.escape(my_biz))
    return _online_page_html('换人申请', ''.join(o), foot)


@app.route('/admin/idreq')
def admin_idreq():
    """换人审批：只有该档超管能进。"""
    biz = request.args.get('biz') or 'debug'
    if biz not in ('debug', 'safety'):
        biz = 'debug'
    if not _biz_admin(biz):
        return _permission_denied()

    rows = []
    try:
        # 🔴 V1.16.29 修：原来写死 `biz=''`（取全部两档），于是调试档超管在调试入口
        #   也能批安全档的换人单。按档入口既然分开了，这里就必须按档取。
        rows = db.get_id_switch_reqs(biz=biz)
    except Exception as e:
        logger.warning('审批列表读取失败: %s' % e)

    bzname = {'debug': '调试', 'safety': '安全'}.get(biz, '')
    o = ['<div class="c"><div class="h">换人申请审批%s</div>'
         % ('（%s业务）' % bzname if bzname else '')
         + '<div class="k">代操作需要换人时，申请人会在这里提单。'
         '<b>你点「批准」之后，申请人还要自己点一次「用新身份」才真正切换</b>'
         ' —— 换句话说管理员只是放行，不是替你改身份。</div>']
    if not rows:
        o.append('<div class="k" style="margin-top:10px">现在没有申请单。</div>')
    else:
        o.append('<table><tr><th>单号</th><th>申请人</th><th>要切成</th><th>理由</th>'
                 '<th>状态</th><th>操作</th></tr>')
        for r in rows:
            st = (r.get('status') or 'pending')
            pill = {'pending': '<span class="pill w">待审批</span>',
                    'approved': '<span class="pill g">已批准</span>',
                    'rejected': '<span class="pill r">已驳回</span>',
                    'applied': '<span class="pill">已生效</span>'}.get(st, st)
            acts = ''
            if st == 'pending':
                acts = (
                    '<form method="post" action="/admin/idreq/handle" style="display:inline">'
                    '<input type="hidden" name="rid" value="%s">'
                    '<input type="hidden" name="ok" value="1">'
                    # 🔴 必须带 biz：处理路由按 `request.form['biz']` 判权限，
                    #   不带就默认 debug ⇒ 安全档超管在这里点批准会被判成"不是调试档超管"。
                    '<input type="hidden" name="biz" value="%s">'
                    '<button class="btn" type="submit">批准</button></form> '
                    '<form method="post" action="/admin/idreq/handle" style="display:inline" '
                    'onsubmit="return prompt(\'驳回理由（可留空）\') !== null;">'
                    '<input type="hidden" name="rid" value="%s">'
                    '<input type="hidden" name="ok" value="0">'
                    '<input type="hidden" name="biz" value="%s">'
                    '<button class="btn btnr2" type="submit">驳回</button></form>'
                    % (int(r.get('id') or 0), _html_mod.escape(biz),
                       int(r.get('id') or 0), _html_mod.escape(biz)))
            o.append('<tr><td>#%s<div class="sm">%s</div></td>'
                     '<td>%s<div class="mono">%s</div></td>'
                     '<td>%s<div class="mono">%s</div></td>'
                     '<td class="sm">%s</td>'
                     '<td>%s%s</td><td>%s</td></tr>'
                     % (int(r.get('id') or 0), (r.get('created_at') or '')[5:16],
                        _html_mod.escape(r.get('from_name') or '?'),
                        _mask_phone(r.get('from_phone') or ''),
                        _html_mod.escape(r.get('to_name') or '?'),
                        _mask_phone(r.get('to_phone') or ''),
                        _html_mod.escape((r.get('reason') or '')[:80]),
                        pill,
                        ('<div class="sm">%s</div>' % _html_mod.escape(r.get('approver') or ''))
                        if r.get('approver') else '',
                        acts))
        o.append('</table>')
    o.append('</div>')
    foot = ('<div class="row"><a class="btn btn2" href="/my/idreq?biz=%s">我的申请</a>'
            '<a class="btn btn2" href="/online?biz=%s">当前在线</a>'
            '<a class="btn btn2" href="/">首页</a></div>'
            % (_html_mod.escape(biz), _html_mod.escape(biz)))
    return _online_page_html('换人申请审批', ''.join(o), foot)


@app.route('/admin/idreq/handle', methods=['POST'])
def admin_idreq_handle():
    """批 / 驳换人申请。只有该档超管能做（写入前再判一次，别只靠页面拦）。"""
    biz = (request.form.get('biz') or 'debug').strip()
    if biz not in ('debug', 'safety'):
        biz = 'debug'
    if not _biz_admin(biz):
        logger.warning('换人审批被拒（无超管权）：%s', _current_identity_phone())
        return _permission_denied()
    # 🔴 V1.16.30：同 /online/kick，处理完跳回提交方指定的页面（白名单校验过）。
    _back = _safe_back(request.form.get('back'), '/admin/idreq?biz=' + biz)
    rid = (request.form.get('rid') or '').strip()
    ok = (request.form.get('ok') or '').strip() in ('1', 'true', 'on')
    note = (request.form.get('note') or '').strip()
    if not rid.isdigit():
        return redirect(_back)
    done = False
    try:
        done = db.handle_id_switch_req(int(rid), ok,
                                       _name_of_phone(_current_identity_phone()) or '管理员',
                                       note)
    except Exception as e:
        logger.warning('换人审批写入失败: %s' % e)
    logger.warning('换人申请 #%s 被%s（处理人 %s）',
                   rid, '批准' if ok else '驳回', _current_identity_phone() or '?')
    try:
        db.add_login_log(_current_identity_phone(), _client_ip(), bool(done),
                         '换人申请 #%s %s' % (rid, '批准' if ok else '驳回'), biz)
    except Exception:
        pass
    return redirect(_back)


@app.route('/api/idreq/my')
def api_idreq_my():
    """我的换人申请单（JSON）。给页面做局部刷新用。"""
    me = _current_identity_phone()
    if not me:
        return jsonify(ok=False, msg='未认到人', items=[])
    try:
        rows = db.my_id_switch_reqs(me)
    except Exception as e:
        return jsonify(ok=False, msg=str(e), items=[]), 500
    items = []
    for r in rows:
        items.append({
            'id': r.get('id'),
            'to_name': r.get('to_name') or '',
            'to_phone': _mask_phone(r.get('to_phone') or ''),
            'reason': r.get('reason') or '',
            'status': r.get('status') or 'pending',
            'approver': r.get('approver') or '',
            'created_at': r.get('created_at') or '',
        })
    return jsonify(ok=True, items=items, total=len(items))


@app.route('/api/online/list')
def api_online_list():
    """在线名单（JSON）。管理权之外只看得到自己。

    V1.16.29：与页面 `/online` 一样支持 `?biz=debug|safety` 按档过滤。
    """
    me = _current_identity_phone()
    biz = (request.args.get('biz') or '').strip()
    if biz not in ('debug', 'safety'):
        biz = ''
    can_manage = False
    if me:
        try:
            if biz:
                can_manage = bool(_biz_admin(biz) or _can_manage_biz(biz))
            else:
                can_manage = bool(_biz_admin('debug') or _biz_admin('safety')
                                  or _can_manage_biz('debug') or _can_manage_biz('safety'))
        except Exception:
            can_manage = False
    items = []
    if ONLINE_GUARD and me:
        try:
            for r in db.get_online_list(biz=biz):
                ph = (r.get('phone') or '').strip()
                if not can_manage and ph != me:
                    continue
                items.append({
                    'phone': ph if can_manage else _mask_phone(ph),
                    'name': (r.get('name') or '').strip() or _name_of_phone(ph) or '(无名)',
                    'device': _ua_short(r.get('ua')),
                    'ip': r.get('ip') or '',
                    'seen': r.get('last_seen') or '',
                    'is_me': ph == me,
                })
        except Exception as e:
            return jsonify(ok=False, msg=str(e), items=[]), 500
    return jsonify(ok=True, items=items, total=len(items), biz=biz,
                   guard=ONLINE_GUARD, id_lock=ID_LOCK, can_manage=can_manage)


@app.route('/api/urge', methods=['POST'])
def api_urge():
    """催办手尾：向该手尾全部责任链人员同时推送（排除催办人本人），并写入 urge_log 留痕。
    权限：调试上级/超级管理员可催办所有；普通调试责任人仅可催办与自己相关的手尾。"""
    if not _is_logged_in():
        return jsonify(ok=False, msg='请先登录'), 401
    data = request.get_json(silent=True) or request.form
    rid = int(data.get('rid') or 0)
    if rid <= 0:
        return jsonify(ok=False, msg=rec_text('缺少手尾ID', None)), 400
    rec = db.get_record(rid)
    if not rec:
        return jsonify(ok=False, msg='记录不存在'), 404
    if rec.get('status') in ('已闭环', '已完成'):
        return jsonify(ok=False, msg=rec_text('该手尾已闭环，无需催办', rec.get('ship_id'))), 400

    me = _current_identity_phone()   # V1.16.22：免登录下用认到的身份，否则催办判不出是谁
    lvl = _role_level()
    logs = db.get_dispatch_logs(rid)
    people = _chain_people(rec, logs)
    if lvl < 2:
        is_mine = bool(me) and (str(rec.get('submitted_by') or '') == me)
        is_resp = any(p['phone'] == me for p in people)
        if not (is_mine or is_resp):
            return jsonify(ok=False, msg=rec_text('权限不足：仅可催办与自己相关的手尾', rec.get('ship_id'))), 403

    targets = [p for p in people if p['phone'] != me]
    if not targets:
        return jsonify(ok=False, msg=rec_text('该手尾暂无其他责任人可催办', rec.get('ship_id'))), 400

    # V1.4.5 防重复：同一人 60s 内对同一手尾重复催办 → 拦截（防重复推送全链）
    fp = _idem_fingerprint(_actor_key(), 'urge', rid)
    if not _idem_allow(fp):
        return _dup_blocked_json()

    site_url = get_site_url()
    _durl = detail_url(rid, rec.get('ship_id'))
    ship = db.get_ship(int(rec.get('ship_id') or 1))
    content = (
        "⏰【手尾催办通知】\n"
        f"系统：{rec.get('sys_name') or ''}\n"
        f"描述：{(rec.get('description') or '')[:80]}\n"
        f"发起时间：{rec.get('submit_time') or ''}\n"
        f"催办人：{session.get('user_name', '') or me}\n"
        "请相关责任人尽快整改反馈，避免影响收尾进度！\n"
        f"👉 点击或复制下方链接查看详情并上传整改照片：\n"
        f"{_durl}"
    )
    to_phones = [p['phone'] for p in targets]
    to_names = [p['name'] for p in targets]
    try:
        send_push(rec_text(content, rec.get('ship_id')), to_phones,
                  (ship or {}).get("webhook") or "",
                  mobile_click_url=_durl, rid=rid)
    except Exception as e:
        logger.warning(f"催办推送失败(不影响留痕): {e}")
    db.add_urge_log(rid, me, session.get('user_name', '') or me,
                    to_phones, to_names,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info(f"催办 - 记录ID: {rid}, 催办人: {me}, 目标: {to_names}")
    preview = "、".join(to_names[:5]) + (" 等" if len(to_names) > 5 else "")
    return jsonify(ok=True, msg=f'已催办 {len(targets)} 人：{preview}，将同时提醒全部相关责任人')


# ============ V1.3 需求4：超管配置超时提醒阈值 ============

def _thresholds_page(scope):
    """超时提醒阈值配置（V1.3 需求4，V1.5.3 按业务档拆开）。
    scope='debug'  → 船舶完整性看板用，配置写 config.json 的 thresholds
    scope='safety' → 安全隐患看板用，配置写 config.json 的 safety_thresholds
    ★ 两块看板的超时提醒配置完全独立：调试页只列调试看板，安全页只有安全看板，
      保存时只更新本页管的看板，另一档的配置原样保留。

    🔴 V1.16.27：门禁原来只判 `_is_logged_in() + _current_role()=='super_admin'`，
      实测**自选身份声明成超管号就能进**（免登录下 _is_logged_in 恒 True，
      而 _current_role 会按声明的号算出 super_admin）⇒ 阈值可被随便改。
      改为 _biz_admin()：要求船海智云认证过的可信身份。"""
    if not _biz_admin('debug' if scope != 'safety' else 'safety'):
        return _permission_denied()
    cfg_path = os.path.join(BASE_DIR, 'config.json')
    is_safety = (scope == 'safety')
    key = 'safety_thresholds' if is_safety else 'thresholds'
    # V1.5.3：两块看板都支持"按产品船单独设置"——各自只列本业务的产品船看板。
    # （ship_thresholds 用看板 id 做键，两块业务的 id 天然不重叠，
    #   保存时也只动本页出现过的 id，不会覆盖另一块业务的单船配置。）
    ships = db.get_ships('safety' if is_safety else 'debug')
    page_ep = 'safety_thresholds' if is_safety else 'admin_thresholds'

    if request.method == 'POST':
        try:
            l1 = int(request.form.get('l1') or 0)
            l2 = int(request.form.get('l2') or 0)
        except (TypeError, ValueError):
            return "<script>alert('请输入有效的数字小时数'); history.back();</script>"
        if l1 < 1 or l1 > 720:
            return "<script>alert('一级超时阈值须为 1~720 小时'); history.back();</script>"
        if l2 <= l1 or l2 > 1440:
            return "<script>alert('二级超时阈值须大于一级阈值且不超过 1440 小时'); history.back();</script>"

        # 本页出现的看板：逐条校验覆盖值（留空=跟随本档全局）
        overrides = {}
        for s in ships:
            sid = str(s['id'])
            raw1 = (request.form.get('ship_l1_' + sid) or '').strip()
            raw2 = (request.form.get('ship_l2_' + sid) or '').strip()
            if not raw1 and not raw2:
                continue
            try:
                v1 = int(raw1) if raw1 else 0
                v2 = int(raw2) if raw2 else 0
            except ValueError:
                return (f"<script>alert('产品船「{s.get('name','')}」的阈值请填数字，留空表示跟随全局');"
                        f" history.back();</script>")
            if v1 and (v1 < 1 or v1 > 720):
                return f"<script>alert('产品船「{s.get('name','')}」一级阈值须为 1~720 小时'); history.back();</script>"
            if v2 and (v2 > 1440 or (v1 and v2 <= v1) or (not v1 and v2 <= l1)):
                return (f"<script>alert('产品船「{s.get('name','')}」二级阈值须大于一级阈值；"
                        f"一级留空时须大于全局 {l1} 小时'); history.back();</script>")
            item = {}
            if v1:
                item['l1'] = v1
            if v2:
                item['l2'] = v2
            if item:
                overrides[sid] = item

        all_th = {}
        try:
            disk_cfg = {}
            if os.path.exists(cfg_path):
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    disk_cfg = json.load(f)
            disk_cfg.setdefault(key, {})['l1'] = l1
            disk_cfg[key]['l2'] = l2
            # 合并式保存：只更新本页出现过的看板，另一档的覆盖配置不动
            all_th = dict(disk_cfg.get('ship_thresholds') or {})
            for sid in [str(x['id']) for x in ships]:
                if sid in overrides:
                    all_th[sid] = overrides[sid]
                else:
                    all_th.pop(sid, None)
            if all_th:
                disk_cfg['ship_thresholds'] = all_th
            else:
                disk_cfg.pop('ship_thresholds', None)
            with open(cfg_path, 'w', encoding='utf-8') as f:
                json.dump(disk_cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            return "<script>alert('config.json 写入失败：" + str(e) + "'); history.back();</script>"
        # 同步内存中的 CONFIG（本次进程内立即生效）
        CONFIG.setdefault(key, {})['l1'] = l1
        CONFIG[key]['l2'] = l2
        CONFIG['ship_thresholds'] = all_th
        logger.info(f"超管修改超时阈值[{scope}] - 全局 {l1}h/{l2}h, 单独配置 {len(overrides)} 条产品船, "
                    f"操作人: {_current_phone()}")
        return redirect(url_for(page_ep))

    th = get_thresholds(None, group=scope)
    ship_rows = []
    for s in ships:
        t = get_thresholds(s['id'], group=scope)
        ship_rows.append({
            'id': s['id'],
            'name': s.get('name') or '',
            'ship_no': s.get('ship_no') or '',
            'l1': t['l1'],
            'l2': t['l2'],
            'custom': (t.get('scope') == 'ship'),
        })
    return render_biz('admin_thresholds.html', scope,
                      l1=th['l1'], l2=th['l2'],
                      ship_rows=ship_rows,
                      back_url=('/safety' if is_safety else '/boards'),
                      user_name=session.get('user_name', ''),
                      user_role=session.get('user_role', ''))


@app.route('/admin/thresholds', methods=['GET', 'POST'])
def admin_thresholds():
    """调试业务（船舶完整性看板）的超时提醒时间配置"""
    return _thresholds_page('debug')


@app.route('/safety/thresholds', methods=['GET', 'POST'])
def safety_thresholds():
    """V1.16.6（用户要求）：**安全管理不再有「超时提醒设置」**。
    隐患只按发起时手填的「整改截止时间」提醒一次，没有分级阈值可配，
    老链接/收藏夹一律回安全看板列表，免得进到一个"改了也不生效"的页面。
    （如需恢复：把本函数改回 `return _thresholds_page('safety')` 即可）"""
    return redirect('/safety')


@app.route('/admin/set_emp_no', methods=['POST'])
def admin_set_emp_no():
    """V1.3 需求1：超管为已有账号补/改工号（三合一检索的数据前提）"""
    if not _is_logged_in():
        return jsonify(ok=False, msg='权限不足'), 403
    data = request.get_json(silent=True) or request.form
    phone = (data.get('phone') or '').strip()
    # V1.5.3：超管可管任何账号；安全上级只能管安全业务（safety）的账号
    if not _can_manage_users_for_phone(phone, data.get('biz')):
        return jsonify(ok=False, msg='权限不足'), 403
    emp_no = (data.get('emp_no') or '').strip()
    if not phone:
        return jsonify(ok=False, msg='缺少手机号'), 400
    db.update_user_emp_no(phone, emp_no)
    logger.info(f"修改用户工号 - {phone} → {emp_no or '（清空）'}, 操作人: {_current_phone()}")
    return jsonify(ok=True, msg='工号已更新')


@app.route('/admin/sync_emp_no', methods=['POST'])
def admin_sync_emp_no():
    """V1.3.1：超管从船海智云通讯录按手机号自动匹配工号并写入账号（工号不手动填写）"""
    if not _is_logged_in():
        return jsonify(ok=False, msg='权限不足'), 403
    data = request.get_json(silent=True) or request.form
    phone = (data.get('phone') or '').strip()
    # V1.5.3：超管可管任何账号；安全上级只能管安全业务（safety）的账号
    if not _can_manage_users_for_phone(phone, data.get('biz')):
        return jsonify(ok=False, msg='权限不足'), 403
    if not phone:
        return jsonify(ok=False, msg='缺少手机号'), 400
    try:
        import chz_push
        info = (chz_push.ensure_users().get(phone) or {})
    except Exception as e:
        logger.warning(f"船海智云通讯录读取失败: {e}")
        return jsonify(ok=False, msg='通讯录读取失败，请检查网络后重试'), 500
    code = (info.get('code') or '').strip()
    if not code:
        return jsonify(ok=False, msg='船海智云通讯录中未找到该手机号，无法自动匹配工号'), 404
    db.update_user_emp_no(phone, code)
    logger.info(f"自动匹配工号 - {phone} → {code}, 操作人: {_current_phone()}")
    return jsonify(ok=True, msg=f'已从船海智云自动匹配工号：{code}', emp_no=code)


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# ============ V1.5.3 手机端看图：服务端压缩图（/photo/<filename>?w=1280） ============
THUMB_CACHE_DIR = os.path.join(BASE_DIR, 'uploads', '_thumb_cache')


@app.route('/photo/<filename>')
def photo_scaled(filename):
    """按宽度返回压缩后的图片，供手机端查看（原图 5712x4284 / 11MB，
    直接在手机上打开又慢又"一开就撑满屏"，这里先缩到指定宽度再返回）。

    - w 参数：目标宽度（像素），默认 1280，限定 200~4000
    - 首次访问生成并缓存到 uploads/_thumb_cache/，后续直接命中缓存
    - 文件名做安全校验，拒绝任何路径穿越
    """
    if not filename or '/' in filename or '\\' in filename or '..' in filename:
        abort(400)
    try:
        w = int(request.args.get('w') or 1280)
    except Exception:
        w = 1280
    w = max(200, min(w, 4000))

    src = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if not os.path.isfile(src):
        abort(404)

    os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
    cache = os.path.join(THUMB_CACHE_DIR, f'w{w}_{filename.rsplit(".", 1)[0]}.jpg')
    if (not os.path.isfile(cache)) or os.path.getmtime(cache) < os.path.getmtime(src):
        try:
            from PIL import Image as PILImage, ImageOps
            with PILImage.open(src) as pil:
                ow, oh = pil.size
                if ow <= w:
                    # 原图本来就比目标小：直接回原图，不做无意义的放大
                    return send_from_directory(app.config['UPLOAD_FOLDER'], filename,
                                               max_age=86400)
                try:
                    pil.draft('RGB', (w, w))
                except Exception:
                    pass
                im = pil.convert('RGB')
                try:
                    im = ImageOps.exif_transpose(im)
                except Exception:
                    pass
                ratio = w / float(im.size[0])
                im = im.resize((w, max(int(im.size[1] * ratio), 1)), PILImage.LANCZOS)
                im.save(cache, 'JPEG', quality=78, optimize=True, progressive=True)
        except Exception as e:
            logger.warning(f"生成压缩图失败 {filename}: {e}")
            return send_from_directory(app.config['UPLOAD_FOLDER'], filename, max_age=86400)
    return send_from_directory(THUMB_CACHE_DIR, os.path.basename(cache), max_age=86400)


@app.route('/export')
def export_excel():
    """实时导出看板数据为 Excel（按当前看板导出，含表头样式/冻结/筛选，异常照片与反馈照片嵌入真实图片）"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.drawing.image import Image as XlImage
    from openpyxl.drawing.spreadsheet_drawing import OneCellAnchor, AnchorMarker
    from openpyxl.drawing.xdr import XDRPositiveSize2D
    from PIL import Image as PILImage

    ship_id = int(request.args.get('ship_id') or 1)
    ship = db.get_ship(ship_id)
    ship_name = ship.get('name', '船舶完整性看板') if ship else '船舶完整性看板'
    # V1.5.3：photos=0 时只导出文字（不嵌图片，秒级返回）。
    # 前端在带图导出超时/失败时会自动用这个参数重试一次，保证"一定拿得到文件"。
    EXPORT_PHOTOS = (request.args.get('photos') or '1') != '0'
    _t0 = time.time()
    # 业务档：安全业务的导出不带"系统名称"列、术语换安全口径
    _biz = ship_biz(ship_id)
    _is_safety = (_biz == 'safety')

    records = []
    _raw_by_rid = {}
    for r in db.get_records_by_ship(ship_id):
        _raw_by_rid[r.get('id')] = r          # 责任链要用原始字段，这里留一份
        records.append({
            'rid': r.get('id'),
            'sys_name': r.get('sys_name') or '',
            'debug_user': r.get('debug_users') or '',          # 完整6段台账格式
            'debug_sup': extract_name(r.get('debug_sup') or ''),
            'desc': r.get('description') or '',
            'submit_time': r.get('submit_time') or '',
            'install_user': r.get('install_user') or '',        # 完整6段台账格式
            # ★ 修复：优先用记录显式保存的作业区名称；旧数据才回退台账反查。
            # 此前直接 extract_area(area_leader)，作业长为 2 段格式（姓名-电话）
            # 且台账查不到时会返回作业长姓名，导致该列错误显示人名。
            'area_leader': (r.get('area_name') or '').strip() or extract_area(r.get('area_leader') or ''),
            'area_leader_full': r.get('area_leader') or '',     # 作业长（完整6段）
            'dept_leader_full': r.get('dept_leader') or '',     # 部门长（完整6段）
            'status': compute_display_status(r.get('status') or '未完成', r.get('submit_time'), ship_id, r.get('deadline')),
            'fb_desc': r.get('fb_desc') or '',
            'fb_time': r.get('fb_time') or '',
            'close_time': r.get('close_time') or '',
        })

    # V1.5.7：导出不再带任何照片（用户要求），改为追加「责任链 / 派工记录 / 催办记录」三列
    headers = ['序号', '系统名称', '调试责任人', '调试上级责任人', '问题描述', '发起时间',
               '责任主管', '责任作业区', '作业长', '部门长', '当前状态',
               '反馈备注', '反馈时间', '闭环时间', '责任链', '派工记录', '催办记录']
    keys = ['sys_name', 'debug_user', 'debug_sup', 'desc', 'submit_time',
            'install_user', 'area_leader', 'area_leader_full', 'dept_leader_full', 'status',
            'fb_desc', 'fb_time', 'close_time', 'chain', 'dispatch_rec', 'urge_rec']
    if _is_safety:
        # 安全业务没有"选择系统"这一维度：不导这一列，术语也换成安全口径
        headers = [h for h in headers if h != '系统名称']
        keys = [k for k in keys if k != 'sys_name']
        for _a, _b in [('异常手尾照片', '隐患照片'), ('手尾', '隐患'), ('未闭环', '未整改'),
                       ('已闭环', '已整改'), ('闭环', '整改'),
                       ('调试上级责任人', '安全上级'), ('调试责任人', '安全负责人')]:
            headers = [h.replace(_a, _b) for h in headers]
    # V1.5.7：不再嵌入照片，照片列定位已移除

    wb = Workbook()
    ws = wb.active
    ws.title = ('安全隐患看板' if _is_safety else '船舶完整性看板')[:31]

    header_fill = PatternFill('solid', fgColor='4472C4')
    header_font = Font(color='FFFFFF', bold=True, size=11)
    center = Alignment(horizontal='center', vertical='center')
    thin = Side(style='thin', color='B0B0B0')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col_idx, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=col_idx, value=h)
        c.fill = header_fill
        c.font = header_font
        c.alignment = center
        c.border = border

    # ---- 图片嵌入辅助 ----
    EMU_PER_PX = 9525               # 1 像素 = 9525 EMU
    THUMB_W, THUMB_H, IMG_GAP = 150, 112, 4   # 缩略图宽/高/间距（像素）
    # ★ V1.5.3「照片一多就导不出来」根因修复（两处都是硬 bug）：
    #  ① 行高单位搞错了：openpyxl 的 row_dimensions.height 单位是"磅"不是像素，
    #     且 Excel 单行高度上限 409.5 磅（≈546 像素）。原实现按像素写高度，
    #     一条手尾挂 3 张以上照片就超过上限 → Excel 打开时提示"需要修复"。
    #  ② 照片竖着往下叠：每张占满 150 像素，超过 3 张必然超高，既压不住行高
    #     也让单行数据看起来失控。
    # 改为「一行 3 张、每格最多 MAX_IMGS_PER_CELL 张」的网格排布，行高按磅
    # 换算并夹到 409 磅以内；同时给单次导出加照片总数上限，防止超大看板卡死。
    PHOTOS_PER_ROW = 3              # 一行排几张照片
    MAX_IMGS_PER_CELL = 12          # 单个单元格最多嵌入几张（超出只提示不嵌入）
    MAX_IMGS_TOTAL = 240            # 单次导出照片总数上限（防御超大看板）
    ROW_MAX_PT = 409                # Excel 单行高度上限（磅）
    upload_dir = app.config['UPLOAD_FOLDER']
    _tmp_photo_files = []           # 统一转码的临时图片（wb.save 后才可删除）
    _img_count = {'n': 0}           # 已嵌入照片总数

    def _row_height_pt(n_imgs):
        """按嵌入张数计算行高（磅），夹到 Excel 单行上限以内。
        注意单位换算：Excel 行高以磅计，1 磅 = 4/3 像素。"""
        rows = max(1, (int(n_imgs) + PHOTOS_PER_ROW - 1) // PHOTOS_PER_ROW)
        px = rows * (THUMB_H + IMG_GAP) + 6
        return min(px * 0.75, ROW_MAX_PT)

    def _embed_photos(photo_str, col_idx, row_idx):
        """解析逗号分隔的文件名，将真实图片嵌入单元格；返回嵌入张数。
        ★ 统一转码为 JPEG 临时文件再嵌入：手机照片可能是 .mpo/.heic 等
        openpyxl 无法识别 MIME 的扩展名（会导致 wb.save 时 KeyError 500），
        转码后彻底规避，任何格式都能安全导出。"""
        if not EXPORT_PHOTOS:
            ws.cell(row=row_idx, column=col_idx, value=photo_str or '(无照片)')
            return 0
        if not photo_str or not photo_str.strip():
            ws.cell(row=row_idx, column=col_idx, value='(无照片)')
            return 0
        filenames = [f.strip() for f in photo_str.split(',') if f.strip()]
        valid = []
        for fn in filenames:
            fp = os.path.join(upload_dir, fn)
            if os.path.isfile(fp):
                valid.append(fp)
        if not valid:
            ws.cell(row=row_idx, column=col_idx, value='(文件缺失) ' + photo_str)
            return 0
        ws.cell(row=row_idx, column=col_idx, value='')      # 清空文字
        step_x, step_y = THUMB_W + IMG_GAP, THUMB_H + IMG_GAP
        count = 0
        skipped = 0
        for idx, fp in enumerate(valid):
            if count >= MAX_IMGS_PER_CELL or _img_count['n'] >= MAX_IMGS_TOTAL:
                skipped = len(valid) - idx
                break
            try:
                with PILImage.open(fp) as pil:
                    ow, oh = pil.size
                    ratio = min(THUMB_W / ow, THUMB_H / oh, 1.0)
                    dw, dh = max(int(ow * ratio), 1), max(int(oh * ratio), 1)
                    # ★ 性能关键：JPEG/MPO 先 draft() 让解码器直接按 1/2、1/4、1/8
                    # 抽取解码，手机原图 5712x4284 / 11MB 每张要 1~2 秒且吃 70MB
                    # 内存，draft 后只解到接近目标尺寸，速度快 10 倍以上、
                    # 内存降到 1/64——这是"照片一多导出就转不出来"的主要性能瓶颈。
                    try:
                        pil.draft('RGB', (dw, dh))
                    except Exception:
                        pass
                    img_pil = pil.convert('RGB')
                    try:
                        from PIL import ImageOps as _ImageOps
                        img_pil = _ImageOps.exif_transpose(img_pil)
                    except Exception:
                        pass
                    if img_pil.size != (dw, dh):
                        img_pil = img_pil.resize((dw, dh), PILImage.LANCZOS)
                    # 统一转码为 JPEG 临时文件（后缀 .jpg，MIME 一定能识别）。
                    # 注意：openpyxl 在 wb.save() 时才真正读取图片，临时文件
                    # 必须存活到保存结束，导出结束时统一清理。
                    tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
                    tmp_name = tmp.name
                    tmp.close()
                    img_pil.save(tmp_name, 'JPEG', quality=82)
                    _tmp_photo_files.append(tmp_name)
                    img = XlImage(tmp_name)
                    img.width = dw
                    img.height = dh
                    # 网格排布：一行 PHOTOS_PER_ROW 张，超出换行，行高才不会爆
                    off_x = (idx % PHOTOS_PER_ROW) * step_x
                    off_y = (idx // PHOTOS_PER_ROW) * step_y
                    marker = AnchorMarker(
                        col=col_idx - 1, colOff=(2 + off_x) * EMU_PER_PX,
                        row=row_idx - 1, rowOff=off_y * EMU_PER_PX
                    )
                    ext = XDRPositiveSize2D(cx=dw * EMU_PER_PX, cy=dh * EMU_PER_PX)
                    img.anchor = OneCellAnchor(_from=marker, ext=ext)
                    ws.add_image(img)
                    count += 1
                    _img_count['n'] += 1
            except Exception as e:
                logger.warning(f"导出嵌入照片失败 {fp}: {e}")
        if skipped > 0:
            # 超出上限的照片不再嵌入，在上方写一行说明，避免用户以为丢了
            note = ws.cell(row=row_idx, column=col_idx,
                           value=f'(本行共 {len(valid)} 张，已嵌入前 {count} 张，其余 {skipped} 张请在系统内查看)')
            note.alignment = top_wrap
            note.font = Font(color='C00000', size=10)
        if count == 0:
            ws.cell(row=row_idx, column=col_idx, value='(无法嵌入) ' + photo_str)
        return count

    top_wrap = Alignment(vertical='top', wrap_text=True)   # 复用，避免每格新建
    # V1.5.7：一次性批量取全部派工记录（避免逐条查询）
    _rids = [rec.get('rid') for rec in records if rec.get('rid')]
    _dlog_map = db.get_dispatch_logs_map(_rids) if _rids else {}
    for i, rec in enumerate(records, start=2):
        # 责任链 / 派工记录 / 催办记录（逐条生成，挂到 rec 上供 keys 取值）
        _rid = rec.get('rid')
        _raw = _raw_by_rid.get(_rid) or {}
        _dlogs = _dlog_map.get(_rid) or []
        rec['chain'] = biz_text(_chain_summary(_raw, _dlogs), _biz)
        rec['dispatch_rec'] = _fmt_dispatch(_dlogs)
        rec['urge_rec'] = _fmt_urge(db.get_urge_logs(_rid) if _rid else [])
        # 多行文本给足行高，否则 Excel 里只看得到第一行
        _lines = max([len((rec[_k] or '').split('\n')) for _k in ('chain', 'dispatch_rec', 'urge_rec')] + [1])
        if _lines > 1:
            ws.row_dimensions[i].height = min(15 * _lines + 6, 400)
        vals = [i - 1] + [rec.get(k, '') for k in keys]
        for col_idx, v in enumerate(vals, 1):
            c = ws.cell(row=i, column=col_idx, value=v)
            c.border = border
            c.alignment = top_wrap
        # V1.5.7：不再嵌入任何照片（原来这里会把照片列的文字替换成图片）

    # 列宽（中文字符按 2 计）；V1.5.7 起没有照片列，新增三列给足宽度
    width_by_name = {'序号': 6, '系统名称': 16, '调试责任人': 30, '调试上级责任人': 22,
                     '问题描述': 40, '发起时间': 20, '责任主管': 30, '责任作业区': 18,
                     '作业长': 30, '部门长': 30, '当前状态': 18, '反馈备注': 40,
                     '反馈时间': 20, '闭环时间': 20, '整改时间': 20,
                     '责任链': 48, '派工记录': 42, '催办记录': 36,
                     '安全负责人': 30, '安全上级': 22}
    for col_idx, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width_by_name.get(h, 18)

    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 22
    if records:
        ws.auto_filter.ref = f'A1:{get_column_letter(len(headers))}{len(records) + 1}'

    bio = io.BytesIO()
    try:
        wb.save(bio)
    except Exception as e:
        # 带图导出失败（极端情况：坏图/超大看板）→ 自动降级为不带图再导出一次，
        # 保证用户"任何时候都能拿到一份 Excel"，不会只看到一个报错页。
        logger.error(f"导出看板 Excel 失败（photos={EXPORT_PHOTOS}）: {e}")
        for _t in _tmp_photo_files:
            try:
                os.remove(_t)
            except Exception:
                pass
        if EXPORT_PHOTOS:
            return redirect(url_for('export_excel', ship_id=ship_id, photos=0))
        raise
    bio.seek(0)
    # 清理转码临时图片（此时 openpyxl 已把图片数据写入 zip）
    for t in _tmp_photo_files:
        try:
            os.remove(t)
        except Exception:
            pass
    fname = f'{ship_name}_{datetime.now().strftime("%Y%m%d_%H%M")}.xlsx'
    # V1.5.10：允许前端指定下载文件名 —— 内置浏览器里走"直链下载"时也能用上用户自定义的名字
    _fn = (request.args.get('fname') or '').strip()
    if _fn:
        _fn = re.sub(r'[\\/:*?"<>|]', '_', _fn)[:80].strip() or 'export'
        if not _fn.lower().endswith('.xlsx'):
            _fn += '.xlsx'
        fname = _fn
    logger.info(
        f"导出看板 Excel - 看板: {ship_name}(#{ship_id}), 记录数: {len(records)}, "
        f"嵌入照片: {_img_count['n']} 张, 是否带图: {EXPORT_PHOTOS}, "
        f"耗时: {time.time() - _t0:.1f}s, 体积: {bio.getbuffer().nbytes / 1024:.0f}KB"
    )
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ============ 看板统计分析 ============

def _build_area_dept_map():
    """从人员台账构建 作业区 -> 所属部门 映射（优先二级部门，缺省用一级部门）"""
    m = {}
    try:
        for s in db.get_staff_list():
            area = (s.get('作业区') or '').strip()
            if not area:
                continue
            dept = (s.get('二级部门') or s.get('一级部门') or '').strip()
            if dept and area not in m:
                m[area] = dept
    except Exception:
        pass
    return m


def _compute_stats(records, area_dept_map=None, ship_id=None):
    """从记录列表计算统计指标，返回 stats dict 供 API/页面使用。
    V1.3 需求4：超时三档（小于L1 / L1-L2 / 大于L2）按记录的工作日小时数动态计算，
    阈值取超管配置（config.json thresholds），与推送/看板展示同步生效。
    V1.5.3：支持按产品船单独配置——传入 ship_id 时用该船自己的阈值分档。"""
    if area_dept_map is None:
        area_dept_map = {}
    _th = get_thresholds_for(ship_id)
    _l1, _l2 = _th["l1"], _th["l2"]
    total = len(records)
    if total == 0:
        return {
            'total': 0, 'closed': 0, 'open_lt24': 0, 'open_24_48': 0, 'open_gt48': 0,
            'closure_rate': 0, 'open_lt24_rate': 0, 'overdue_24_48_rate': 0, 'overdue_gt48_rate': 0,
            'areas': [], 'depts': [],
            'area_dist': [], 'dept_dist': [],
            'area_stats': [], 'dept_stats': [],
            'l1': _l1, 'l2': _l2,
        }

    def _bucket(r):
        """超时档位：closed=已闭环, lt=小于L1, mid=L1~L2, gt=大于L2"""
        if r.get('status') == '已闭环':
            return 'closed'
        h = r.get('hours')
        if h is None:
            return 'lt'
        if h < _l1:
            return 'lt'
        if h <= _l2:
            return 'mid'
        return 'gt'

    buckets = [_bucket(r) for r in records]
    closed = buckets.count('closed')
    open_lt24 = buckets.count('lt')
    open_24_48 = buckets.count('mid')
    open_gt48 = buckets.count('gt')

    # 作业区分布
    area_map = {}
    for r in records:
        area = r.get('area_name') or '未指定'
        for a in area.split(','):
            a = a.strip()
            if not a:
                a = '未指定'
            area_map[a] = area_map.get(a, 0) + 1
    area_dist = [{'name': k, 'count': v} for k, v in sorted(area_map.items(), key=lambda x: -x[1])]

    # 责任部门分布
    dept_map = {}
    for r in records:
        dept = r.get('dept_name') or '未知部门'
        for d in dept.split(','):
            d = d.strip()
            if not d:
                d = '未知部门'
            dept_map[d] = dept_map.get(d, 0) + 1
    dept_dist = [{'name': k, 'count': v} for k, v in sorted(dept_map.items(), key=lambda x: -x[1])]

    # 各作业区闭环率 / 超期率（含所属部门）
    def _split_set(val, default):
        return {x.strip() or default for x in (val or default).split(',')}

    area_stats = []
    for area_name in area_map:
        recs = [r for r in records if area_name in _split_set(r.get('area_name'), '未指定')]
        a_total = len(recs)
        _bk = [_bucket(r) for r in recs]
        a_closed = _bk.count('closed')
        a_lt24 = _bk.count('lt')
        a_24_48 = _bk.count('mid')
        a_gt48 = _bk.count('gt')
        area_stats.append({
            'name': area_name,
            'dept': area_dept_map.get(area_name, ''),
            'total': a_total,
            'closed': a_closed,
            'open_lt24': a_lt24,
            'open_24_48': a_24_48,
            'open_gt48': a_gt48,
            'closure_rate': round(a_closed / a_total * 100, 1) if a_total else 0,
            'overdue_24_48_rate': round(a_24_48 / a_total * 100, 1) if a_total else 0,
            'overdue_gt48_rate': round(a_gt48 / a_total * 100, 1) if a_total else 0,
        })
    area_stats.sort(key=lambda x: -x['total'])

    # 各责任部门闭环率 / 超期率（保留供 API 参考）
    dept_stats = []
    for dept_name in dept_map:
        dept_records = [r for r in records if dept_name in _split_set(r.get('dept_name'), '未知部门')]
        d_total = len(dept_records)
        _bk = [_bucket(r) for r in dept_records]
        d_closed = _bk.count('closed')
        d_lt24 = _bk.count('lt')
        d_24_48 = _bk.count('mid')
        d_gt48 = _bk.count('gt')
        dept_stats.append({
            'name': dept_name,
            'total': d_total,
            'closed': d_closed,
            'open_lt24': d_lt24,
            'open_24_48': d_24_48,
            'open_gt48': d_gt48,
            'closure_rate': round(d_closed / d_total * 100, 1) if d_total else 0,
            'overdue_24_48_rate': round(d_24_48 / d_total * 100, 1) if d_total else 0,
            'overdue_gt48_rate': round(d_gt48 / d_total * 100, 1) if d_total else 0,
        })
    dept_stats.sort(key=lambda x: -x['total'])

    return {
        'total': total,
        'closed': closed,
        'open_lt24': open_lt24,
        'open_24_48': open_24_48,
        'open_gt48': open_gt48,
        'closure_rate': round(closed / total * 100, 1) if total else 0,
        'open_lt24_rate': round(open_lt24 / total * 100, 1) if total else 0,
        'overdue_24_48_rate': round(open_24_48 / total * 100, 1) if total else 0,
        'overdue_gt48_rate': round(open_gt48 / total * 100, 1) if total else 0,
        'area_dist': area_dist,
        'dept_dist': dept_dist,
        'area_stats': area_stats,
        'dept_stats': dept_stats,
        'l1': _l1, 'l2': _l2,
    }


@app.route('/api/stats/<int:ship_id>')
def api_stats(ship_id):
    """看板统计数据 JSON（供前端图表渲染）"""
    ship = db.get_ship(ship_id)
    if not ship:
        return jsonify(ok=False, msg='看板不存在'), 404
    records = _build_stats_records(ship_id)
    stats = _compute_stats(records, area_dept_map=_build_area_dept_map(), ship_id=ship_id)
    return jsonify(ok=True, ship_name=ship.get('name', ''), stats=stats)


def _build_stats_records(ship_id: int) -> list:
    """构建统计用记录列表：显式作业区/部门优先，旧数据回退台账反查。
    V1.3 需求4：附带 status + hours（工作日小时），统计按动态阈值分档，不依赖展示文案字符串。"""
    records = []
    for r in db.get_records_by_ship(ship_id):
        records.append({
            'status': r.get('status') or '未完成',
            'hours': _work_hours_since(r.get('submit_time')),
            'area_name': (r.get('area_name') or '').strip() or extract_area(r.get('area_leader') or ''),
            'dept_name': (r.get('dept_name') or '').strip() or _smart_extract_dept(r.get('dept_leader') or '', level=2),
        })
    return records


@app.route('/statistics/<int:ship_id>')
def statistics(ship_id):
    """看板统计分析页面"""
    ship = db.get_ship(ship_id)
    if not ship:
        return "看板不存在或已被删除", 404
    records = _build_stats_records(ship_id)
    stats = _compute_stats(records, area_dept_map=_build_area_dept_map(), ship_id=ship_id)
    chart_height = max(280, len(stats['area_stats']) * 45)
    th = get_thresholds_for(ship_id)
    return render_biz('stats.html', ship_biz(ship_id), ship=ship, stats=stats,
                           chart_height=chart_height,
                           l1=th['l1'], l2=th['l2'],
                           user_role=session.get('user_role', ''),
                           user_name=session.get('user_name', ''))


# ==================== V1.6 A2 违章处罚审批 ====================

REC_TYPES = ['隐患整改', '违章处罚', '5S']
APPROVE_WAYS = ['教育', '处罚']


def _approvers_of(rec):
    """解析记录的审批人快照 JSON → [{name, phone, role}]"""
    out = []
    try:
        v = json.loads((rec or {}).get('approvers') or '[]')
        if isinstance(v, str):
            # 兼容早期被二次编码的历史数据
            v = json.loads(v or '[]')
        if isinstance(v, list):
            for it in v:
                if isinstance(it, dict) and (it.get('phone') or it.get('name')):
                    out.append({'name': (it.get('name') or '').strip(),
                                'phone': (it.get('phone') or '').strip()})
    except Exception:
        pass
    return out


def _approval_all_phones(*person_fields):
    """V1.70：违章处罚「发起审批 / 审批出结果」两次全员提醒的收件人。

    传进来的可以是台账串（姓名+手机号）、审批人/抄送人 JSON、纯手机号，
    统一用 _extract_phones 提号码并去重保序；空值自动忽略。
    口径 = 责任人 + 安全员 + 安全上级 + 发起人 + 指定审批人 + 追加抄送人。"""
    return _extract_phones(*person_fields)


def _current_actor_name() -> str:
    """当前操作人姓名：台账优先 → 会话 → 手机号；免登录（不认人）时统一记「管理员」"""
    try:
        # V1.16.22：用「认到的身份」，免登录下 _current_phone() 恒空 ⇒ 永远退化成「管理员」
        st = db.find_staff_by_phone(_current_identity_phone())
        if st and st.get('姓名'):
            return st['姓名']
    except Exception:
        pass
    return session.get('user_name', '') or _current_phone() or '管理员'


def _can_approve_rec(rec) -> bool:
    """谁可以审批这条记录（V1.16.36：审批权只归超管 + 发起人显式指定的审批人）：
    · 被指定的审批人本人（任一人通过即生效）—— 业务流程，不算管理权；
    · **超管**始终可以；
    · 没指定审批人时：安全档只有**有审核权的人**（生产管理室/安全设备室）；
      调试档 V1.16.36 起**不再给普通管理兜底**（原来 `in _SYS_MANAGE_ROLES`
      含 debug_sup ⇒ 科室下面的人默认能批处罚）。"""
    if not rec:
        return False
    ap = _approvers_of(rec)
    me = _current_identity_phone()
    if ap and any((x.get('phone') or '') == me for x in ap):
        return True
    # 🔴 V1.16.27：超管身份要**可信**（船海智云认证过）。
    #   原来只看 _current_role()=='super_admin'，免登录 + 自选身份声明成超管号
    #   就能拿到违章处罚的审批权 —— 等于任何人都能批处罚。
    if _current_role() == 'super_admin' and _identity_trusted():
        return True
    if _ship_biz_of(rec.get('ship_id')) == 'safety':
        return (not ap) and _is_safety_reviewer(me)
    # V1.16.36：调试档没指定审批人 ⇒ 只有超管（上面已返回），这里一律 False。
    return False


def _notify_approval_done(rec, rid, actor_name, approved, way='', fine='', note=''):
    """V1.17.0 / V1.70：违章处罚审批出结果后的推送 + 抄送。

    · 通过 / 驳回：都给「所有人」再提醒一遍（责任人 + 安全员 + 安全上级 + 发起人
      + 指定审批人 + 追加抄送人，去重）。
    · 长期抄送名单仍沿用原业务四人组，避免把审批人带进后续 24h/48h 超时提醒。
    推送一律带详情链接（点进去能看隐患详情）。"""
    try:
        _ship = db.get_ship(rec.get('ship_id')) or {}
        _biz = _ship.get('biz') or 'debug'
        _durl = detail_url(rid, rec.get('ship_id'))
        _sub = (rec.get('submitted_by') or '').strip()
        if approved:
            # 长期抄送名单沿用原业务口径（责任人/安全员/上级/发起人），
            # 避免把审批人带进后续 24h/48h 超时提醒；本次「全员提醒」另算。
            _notify = (_extract_phones(rec.get('debug_users')) +
                       _extract_phones(rec.get('install_user')) +
                       _extract_phones(rec.get('debug_sup')))
            if _sub:
                _notify.append(_sub)
            # V1.70：审批通过后给「所有人」再提醒一遍（含指定审批人与追加抄送人）
            _send_to = _approval_all_phones(
                rec.get('debug_users'), rec.get('install_user'), rec.get('debug_sup'),
                rec.get('submitted_by'), rec.get('approvers'), rec.get('extra_users'))
            _msg = (
                f"✅【违章处罚已通过】\n"
                f"隐患类别：{rec.get('sys_name') or ''}\n"
                f"描述：{rec.get('description') or ''}\n"
                f"审批人：{actor_name}\n"
                f"处理方式：{way or '教育'}{('，罚款 ¥' + fine) if (way == '处罚' and fine) else ''}\n"
                f"👉 点击或复制下方链接查看隐患详情：\n"
                f"{_durl}"
            )
            send_push(biz_text(_msg, _biz), _send_to, mobile_click_url=_durl, rid=rid)
            # 加进长期抄送名单（后续超时提醒也带他们）
            try:
                _cur_cc = json.loads((rec.get('extra_users') or '') or '[]')
                if not isinstance(_cur_cc, list):
                    _cur_cc = []
                _cc_phones = {str(x.get('phone') or '') for x in _cur_cc if isinstance(x, dict)}
                for _p in _notify:
                    _p = (_p or '').strip()
                    if _p and _p not in _cc_phones:
                        _cur_cc.append({'name': _name_of_phone(_p) or _p, 'phone': _p})
                        _cc_phones.add(_p)
                db.update_record_fields(rid, {'extra_users': json.dumps(_cur_cc, ensure_ascii=False)})
            except Exception:
                pass
        else:
            # V1.70：驳回也按「所有人」口径提醒一遍（原来只通知发起人）
            _all_rej = _approval_all_phones(
                rec.get('debug_users'), rec.get('install_user'), rec.get('debug_sup'),
                rec.get('submitted_by'), rec.get('approvers'), rec.get('extra_users'))
            if not _all_rej:
                return
            _msg = (
                f"⛔【违章处罚已驳回】\n"
                f"隐患类别：{rec.get('sys_name') or ''}\n"
                f"描述：{rec.get('description') or ''}\n"
                f"审批人：{actor_name}\n"
                f"{('评语：' + note) if note else ''}\n"
                f"👉 点击或复制下方链接查看隐患详情：\n"
                f"{_durl}"
            )
            send_push(biz_text(_msg, _biz), _all_rej, mobile_click_url=_durl, rid=rid)
    except Exception as e:
        logger.warning(f"审批结果推送失败(不影响审批): {e}")


@app.route('/api/approve', methods=['POST'])
def api_approve():
    """违章处罚审批：发起人指定的审批人，**任一人通过即生效**，其余人无需再处理。"""
    data = request.get_json(silent=True) or request.form
    rid = int(data.get('rid') or 0)
    action = (data.get('action') or 'approve').strip()
    rec = db.get_record(rid)
    if not rec:
        return jsonify(ok=False, msg='记录不存在或已被删除'), 404
    _biz = ship_biz(rec.get('ship_id'))
    st = (rec.get('approval_status') or '').strip()
    if st in ('已通过', '已驳回'):
        return jsonify(ok=False, msg=biz_text('这条记录已经审批过了（%s）' % st, _biz)), 409
    if not _can_approve_rec(rec):
        return jsonify(ok=False, msg=biz_text('你不是这条记录的审批人，无法审批', _biz)), 403
    note = (data.get('note') or '').strip()
    actor_name = _current_actor_name()
    actor_phone = _current_identity_phone()   # V1.16.22：审批留痕要记真手机号
    if action == 'reject':
        db.set_record_approval(rid, '已驳回', actor_name=actor_name, actor_phone=actor_phone, note=note)
        db.add_approve_log(rid, actor_name, actor_phone, 'reject', note)
        logger.info(f"违章处罚驳回 - rid={rid} by {actor_name}")
        _notify_approval_done(rec, rid, actor_name, approved=False, note=note)
        return jsonify(ok=True, msg=biz_text('已驳回，记录留档', _biz))
    way = (data.get('handle_way') or '').strip()
    fine = (data.get('fine_amount') or '').strip()
    # V1.18：审批人不填金额时沿用发起人预填的罚款金额（可修改、不清空）
    if not fine:
        fine = (rec.get('fine_amount') or '').strip()
    if way and way not in APPROVE_WAYS:
        return jsonify(ok=False, msg='处理方式只能是「教育」或「处罚」'), 400
    if way == '处罚' and not fine:
        return jsonify(ok=False, msg='选择「处罚」时必须填写罚款金额'), 400
    db.set_record_approval(rid, '已通过', actor_name=actor_name, actor_phone=actor_phone,
                           note=note, handle_way=way, fine_amount=fine)
    db.add_approve_log(rid, actor_name, actor_phone, 'approve', note, way, fine)
    logger.info(f"违章处罚通过 - rid={rid} by {actor_name} way={way} fine={fine}")
    _notify_approval_done(rec, rid, actor_name, approved=True, way=way, fine=fine, note=note)
    return jsonify(ok=True, msg=biz_text('已通过（一人通过即生效），其余审批人无需再处理', _biz),
                   handle_way=way, fine_amount=fine)


# ==================== V1.6 A5 亮点展示 ====================

HL_KINDS = [('highlight', '亮点'), ('issue', '问题')]
HL_KIND_MAP = dict(HL_KINDS)
HL_STATUS_MAP = {'pending': '待审核', 'approved': '已通过', 'rejected': '已驳回'}


def _hl_ship_from_req():
    """亮点页面：解析 ship_id / biz，返回 (biz, ships, ship_id)"""
    biz = (request.args.get('biz') or request.form.get('biz') or 'debug')
    if biz not in ('debug', 'safety'):
        biz = 'debug'
    ship_id = int(request.args.get('ship_id') or request.form.get('ship_id') or 0)
    if ship_id:
        _s = db.get_ship(ship_id)
        if _s:
            biz = _s.get('biz') or biz
    ships = db.get_ships(biz)
    ids = [x['id'] for x in ships]
    if ship_id not in ids:
        ship_id = ids[0] if ids else 0
    return biz, ships, ship_id


def _hl_areas(ships):
    """V1.16.8：亮点页的「区域」下拉来源。

    产品船看板 ⇒ 取船号（例：2441）；地面场地 / 没填船号的看板 ⇒ 取它的名字（例：东区地面场地）。
    返回 [('船号',...), ('场地',...), ('设备',...)]，空组自动丢掉（V1.42）。
    V1.32：分类统一走 `_is_place_board()`（与 boards.html 同源）。原来「没填船号就当场地」
    会把 btype=产品船、船号写在 name 里的看板（如 2556）误分到场地组。"""
    no_grp, place_grp, dev_grp = [], [], []
    for s in (ships or []):
        nm = (s.get('name') or '').strip()
        no = (s.get('ship_no') or '').strip()
        item = {'id': s.get('id'), 'label': no or nm, 'sub': nm}
        _hlcat = _board_cat(s)
        if _hlcat == 'device':
            dev_grp.append(item)
        elif _hlcat == 'place':
            place_grp.append(item)
        else:
            no_grp.append(item)
    out = []
    _g = board_categories.group_labels()
    if no_grp:
        out.append((_g['ship'], no_grp))
    if place_grp:
        out.append((_g['place'], place_grp))
    if dev_grp:
        out.append((_g['device'], dev_grp))
    return out


def _hl_can_review(h, me, role):
    """谁能审这条亮点 / 问题（V1.16.36：审批权只归超管 + 提交人显式指定的审批人）：

    - **提交人自己在提交时就指定了审批人**（V1.10），被指定的人（任一人）即可审；
    - **超管**始终可以审；
    - 没指定审批人的老数据：安全档只有有审核权的人可代审；调试档 V1.16.36 起
      **不再给普通管理兜底**（原来 `in _SYS_MANAGE_ROLES` 含 debug_sup
      ⇒ 调试上级默认能审亮点）。
    """
    appr = _approvers_of(h) if h else []
    me = (me or '').strip() or _current_identity_phone()
    if not me:
        return role in _SYS_MANAGE_ROLES
    if role == 'super_admin':
        return True
    if appr:
        return any((p.get('phone') or '') == me for p in appr)
    # V1.12：没指定审批人时，安全档只有有审核权的人可代审
    if _ship_biz_of((h or {}).get('ship_id')) == 'safety':
        return _is_safety_reviewer(me)
    return role in _SYS_MANAGE_ROLES


def _hl_clean_approvers(raw):
    """把前端传来的审批人 JSON 清洗成规范格式（必须带 11 位手机号）"""
    if not (raw or '').strip():
        return ''
    try:
        arr = json.loads(raw)
    except Exception:
        return ''
    if not isinstance(arr, list):
        return ''
    out = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        ph = ''.join(ch for ch in str(it.get('phone') or '') if ch.isdigit())
        if len(ph) == 11:
            out.append({'name': (it.get('name') or '').strip(), 'phone': ph})
    return json.dumps(out, ensure_ascii=False) if out else ''


@app.route('/hl')
def hl_index():
    """亮点展示（全员可看）：只有审核通过的才进展示墙；待审核区给有审核权限的人看。
    V1.22：展示墙支持按「全部 / 产品船 / 场地 / 设备」范围筛选（V1.42 加设备）。
    V1.32：范围筛选的类型判断统一走 `_is_place_board()`，修掉「产品船没填船号被算成场地」。"""
    biz, ships, ship_id = _hl_ship_from_req()
    scope = (request.args.get('scope') or 'all').strip()
    if scope not in ('all', 'ship', 'place', 'device'):
        scope = 'all'
    # 每条看板的类型（产品船/场地/设备）+ 展示名，用于范围筛选与卡片归属
    id2type = {s['id']: _board_cat(s) for s in ships}
    id2label = {s['id']: (s.get('ship_no') or s.get('name') or '').strip() for s in ships}
    if scope == 'all':
        scope_ids = [s['id'] for s in ships]
    else:
        scope_ids = [s['id'] for s in ships if id2type.get(s['id']) == scope]
    # V1.10：审批人由提交人指定 ⇒ 逐条算「我能不能审」
    me, my_nm = _my_identity()
    role = _current_role()
    approved = db.get_highlights_multi(scope_ids, 'approved') if scope_ids else []
    pending = db.get_highlights(ship_id, 'pending') if ship_id else []
    for h in pending:
        h['can_i'] = _hl_can_review(h, me, role)
        h['appr_list'] = _approvers_of(h)
    can_review = any(h.get('can_i') for h in pending)
    for h in approved:
        h['appr_list'] = _approvers_of(h)
        h['area_type'] = id2type.get(h.get('ship_id'), 'ship')
        h['area_label'] = id2label.get(h.get('ship_id'), '')
    return render_biz('hl.html', biz, ships=ships, ship_id=ship_id,
                      areas=_hl_areas(ships), scope=scope,
                      ship=db.get_ship(ship_id) if ship_id else None,
                      approved=approved, pending=pending, can_review=can_review,
                      people=_all_people(biz), reviewers=_safety_approver_list(), me=me,
                      kinds=HL_KINDS, kind_map=HL_KIND_MAP, status_map=HL_STATUS_MAP,
                      logged_in=_is_logged_in(),
                      user_role=role, user_name=my_nm or session.get('user_name', ''))


@app.route('/hl/new', methods=['POST'])
def hl_new():
    """提交亮点 / 问题（安全员、建造师、任何人可提交，等上级审核后才展示）"""
    biz, ships, ship_id = _hl_ship_from_req()
    if not ship_id:
        return redirect(url_for('hl_index', biz=biz))
    kind = (request.form.get('kind') or 'highlight').strip()
    if kind not in HL_KIND_MAP:
        kind = 'highlight'
    title = (request.form.get('title') or '').strip()
    descr = (request.form.get('descr') or '').strip()
    if not title and not descr:
        return redirect(url_for('hl_index', ship_id=ship_id, biz=biz))
    photo = ''
    f = request.files.get('photo')
    if f and f.filename:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'):
            ext = '.jpg'
        fn = 'hl_%d_%s%s' % (int(time.time()), os.urandom(4).hex(), ext)
        try:
            f.save(os.path.join(app.config['UPLOAD_FOLDER'], fn))
            photo = fn
        except Exception:
            logger.exception('亮点照片保存失败')
    appr_json = _hl_clean_approvers(request.form.get('approvers'))
    # V1.17.0：亮点审批人只能是安全超管（与违章处罚一致）
    _bad_appr = _reject_non_approver(appr_json)
    if _bad_appr:
        return rec_text("<script>alert('审批人「%s」不是安全超管。亮点审批人只能是"
                        "安全超管，请重新选择。'); history.back();</script>"
                        % _bad_appr, ship_id)
    db.add_highlight(ship_id, kind, title[:200], descr, photo,
                     _current_actor_name(), _current_identity_phone(), approvers=appr_json)
    logger.info(f"提交{'亮点' if kind == 'highlight' else '问题'} - ship#{ship_id}「{title}」")
    # V1.17.0：提交完回到亮点页（审亮点入口已挪到「我的」，这里只回展示墙/发起）
    return redirect(url_for('hl_index', ship_id=ship_id, biz=biz))


@app.route('/hl/review', methods=['POST'])
def hl_review():
    """上级审核亮点：定「奖励 / 罚款」金额，通过后才进展示墙（可奖可罚）"""
    biz, ships, ship_id = _hl_ship_from_req()
    hid = int(request.form.get('hid') or 0)
    # V1.10：只有「提交人指定的审批人」（或超管 / 没认到人时的管理角色）能审这一条
    _rec = db.get_highlight(hid)
    _me, _nm = _my_identity()
    if not _rec or not _hl_can_review(_rec, _me, _current_role()):
        logger.warning(f"亮点审核被拒 - hid={hid} me={_me or '(未认到人)'} role={_current_role()}")
        return render_biz('hl.html', biz, ships=ships, ship_id=ship_id,
                          areas=_hl_areas(ships),
                          approved=db.get_highlights(ship_id, 'approved') if ship_id else [],
                          pending=db.get_highlights(ship_id, 'pending') if ship_id else [],
                          can_review=False, kinds=HL_KINDS, kind_map=HL_KIND_MAP,
                          status_map=HL_STATUS_MAP, logged_in=_is_logged_in(),
                          user_role=_current_role(), people=_all_people(biz),
                                 reviewers=_safety_approver_list(), me=_me,
                          user_name=_nm or session.get('user_name', ''),
                          ship=db.get_ship(ship_id) if ship_id else None), 403
    ok = (request.form.get('action') or 'approve') == 'approve'
    db.review_highlight(hid, ok, _current_actor_name(),
                        (request.form.get('note') or '').strip(),
                        (request.form.get('reward') or '').strip(),
                        (request.form.get('punish') or '').strip())
    logger.info(f"亮点审核 - hid={hid} {'通过' if ok else '驳回'} by {_current_actor_name()}")
    # V1.17.0：审亮点入口挪到「我的」后，审批完按来源跳回（my.html 的表单带 back=/my?biz=...）。
    _back = _safe_back(request.form.get('back') or '',
                       url_for('hl_index', ship_id=ship_id, biz=biz))
    return redirect(_back)


@app.route('/hl/<int:hid>/del', methods=['POST'])
def hl_delete(hid):
    biz, ships, ship_id = _hl_ship_from_req()
    # V1.12：安全档只有超管能删亮点
    if _can_manage_biz(biz):
        db.delete_highlight(hid)
    return redirect(url_for('hl_index', ship_id=ship_id, biz=biz))


# ---------- V1.35：提出人自己处理被驳回 / 已撤回的亮点 ----------
# 背景：驳回只把 status 改成 rejected，数据一直留在 highlights 表里，
# 但展示墙只查 approved、待审区只查 pending，「我的」页那个列表 V1.24 又被删了
# ⇒ 提交人既看不到驳回理由，也没法改了重提。这里把入口补回「我的」页。

def _hl_mine_rec(hid):
    """只认「这条记录确实是当前这个人提交的」。
    🔴 校验只比对传进来的这条 rec 与当前身份，不去查别人的记录 / session 里的临时名单。
    V1.35：口径必须和 _my_highlights 完全一致 —— 历史记录没记手机号时按提交人姓名
    兜底，否则列表里看得见、按钮却点不动。"""
    me, my_name = _my_identity()
    rec = db.get_highlight(hid)
    if not rec or not me:
        return None, me
    rec_phone = (rec.get('submitter_phone') or '').strip()
    if rec_phone:
        if rec_phone != me:
            return None, me
    else:
        nm = (my_name or '').strip()
        if not nm or (rec.get('submitter') or '').strip() != nm:
            return None, me
    return rec, me


def _hl_my_back(rec):
    """操作完回「我的」，业务档按这条亮点所属看板算，别把安全档甩回调试档。"""
    biz = ship_biz(rec.get('ship_id')) if rec else 'debug'
    return url_for('my_center', biz=biz)


@app.route('/hl/<int:hid>/resubmit', methods=['POST'])
def hl_resubmit(hid):
    """V1.35：驳回 / 撤回后，提出人改完内容重新提交 —— 回 pending，清掉上一轮审核痕迹。"""
    rec, me = _hl_mine_rec(hid)
    if not rec:
        logger.warning(f"亮点重提被拒 - hid={hid} 非本人或记录不存在")
        return redirect(url_for('my_center'))
    if (rec.get('status') or '') not in ('rejected', 'withdrawn'):
        return redirect(_hl_my_back(rec))
    title = (request.form.get('title') or '').strip()[:200]
    descr = (request.form.get('descr') or '').strip()
    if not title and not descr:
        return redirect(_hl_my_back(rec))
    # 照片可选：不传就保留原来那张
    photo = ''
    f = request.files.get('photo')
    if f and f.filename:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'):
            ext = '.jpg'
        fn = 'hl_%d_%s%s' % (int(time.time()), os.urandom(4).hex(), ext)
        try:
            f.save(os.path.join(app.config['UPLOAD_FOLDER'], fn))
            photo = fn
        except Exception:
            logger.exception('亮点重提照片保存失败')
    conn = db.get_conn()
    try:
        cur = conn.cursor()
        ph = '%s' if db.resolve_mode() == 'mysql' else '?'
        if photo:
            cur.execute("UPDATE highlights SET title=" + ph + ", descr=" + ph + ", photo_path=" + ph +
                        ", status='pending', reviewer='', review_note='', review_time='', "
                        "reward='', punish='' WHERE id=" + ph,
                        (title, descr, photo, hid))
        else:
            cur.execute("UPDATE highlights SET title=" + ph + ", descr=" + ph +
                        ", status='pending', reviewer='', review_note='', review_time='', "
                        "reward='', punish='' WHERE id=" + ph,
                        (title, descr, hid))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"亮点重新提交 - hid={hid} by {me}")
    return redirect(_hl_my_back(rec))


@app.route('/hl/<int:hid>/withdraw', methods=['POST'])
def hl_withdraw(hid):
    """V1.35：提出人撤回自己还没通过 / 被驳回的亮点。数据留档，只改状态，不删记录。"""
    rec, me = _hl_mine_rec(hid)
    if not rec:
        logger.warning(f"亮点撤回被拒 - hid={hid} 非本人或记录不存在")
        return redirect(url_for('my_center'))
    if (rec.get('status') or '') not in ('pending', 'rejected'):
        return redirect(_hl_my_back(rec))
    conn = db.get_conn()
    try:
        cur = conn.cursor()
        ph = '%s' if db.resolve_mode() == 'mysql' else '?'
        cur.execute("UPDATE highlights SET status='withdrawn' WHERE id=" + ph, (hid,))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"亮点撤回 - hid={hid} by {me}")
    return redirect(_hl_my_back(rec))


if __name__ == '__main__':
    # 生产环境请用 Gunicorn 等 WSGI 服务器，见 部署方案.html
    app.run(host=CONFIG.get('host', '0.0.0.0'), port=int(CONFIG.get('port', 5000)), debug=False)
