# -*- coding: utf-8 -*-
"""V1.46 安全看板「类别」配置（独立模块，不再写死在 app.py 里）。

设计要点
--------
* 一个类别 = 一个字符串 key（直接存进 ships.btype）+ 一个 behavior（行为）。
* behavior 只有三种，决定系统行为，**不随类别数量增长**：
    ship   —— 必填船号、列表显示船号、算产品船（6S / 系统清单都绑它）
    place  —— 不填船号、发起隐患时必选「场地位置」
    device —— 不填船号、不强制「场地位置」、在独立设备页也能看到
  超管新增一类时只要选一种 behavior，后端所有 if 判断都走 behavior，
  所以「加新类别」= 加一条配置，零改代码、零重编译。
* 「产品船」是 builtin：不可删、不可改 behavior（船号 / 6S / 系统清单依赖）。
* 被现有看板引用过的类别不能删，只能停用（enabled=false）——停用的类别
  不再出现在新建下拉，但老看板仍能识别、仍能在列表里被筛到。
* 配置文件 board_categories.json 丢失 / 损坏时自动回落到 DEFAULTS。

读写均为进程内 mtime 缓存 + 原子落盘（os.replace），不会写坏文件。
"""

import io
import json
import os
import threading
import time

_CONFIG_FILE = 'board_categories.json'
BEHAVIORS = ('ship', 'place', 'device')
BEHAVIOR_LABELS = {
    'ship': '产品船类（要填船号）',
    'place': '场地类（不填船号，发隐患要选场地位置）',
    'device': '设备类（不填船号，不强制场地位置）',
}

DEFAULTS = [
    {'key': '产品船', 'behavior': 'ship', 'builtin': True, 'enabled': True, 'order': 1},
    {'key': '地面场地', 'behavior': 'place', 'builtin': False, 'enabled': True, 'order': 2},
    {'key': '其他', 'behavior': 'place', 'builtin': False, 'enabled': True, 'order': 3},
    {'key': '设备', 'behavior': 'device', 'builtin': False, 'enabled': True, 'order': 4},
]

MAX_CATS = 12
MAX_KEY_LEN = 12

_lock = threading.Lock()
_cache = None
_cache_mtime = -1.0


def _path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _CONFIG_FILE)


def _normalize(raw):
    """把任意来源的配置洗成规范列表；不合法就回落到 DEFAULTS。"""
    if not isinstance(raw, dict):
        return [dict(x) for x in DEFAULTS]
    items = raw.get('categories')
    if not isinstance(items, list) or not items:
        return [dict(x) for x in DEFAULTS]
    out, seen = [], set()
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        key = str(it.get('key') or '').strip()
        beh = str(it.get('behavior') or '').strip()
        if not key or len(key) > MAX_KEY_LEN or key in seen:
            continue
        if beh not in BEHAVIORS:
            beh = 'place'
        seen.add(key)
        out.append({
            'key': key,
            'behavior': beh,
            'builtin': bool(it.get('builtin')),
            'enabled': bool(it.get('enabled', True)),
            'order': i + 1,
        })
    if not out:
        return [dict(x) for x in DEFAULTS]
    # 产品船必须存在且为 ship：历史数据/船号逻辑都依赖它
    ship_keys = [c for c in out if c['behavior'] == 'ship']
    if not ship_keys:
        out.insert(0, dict(DEFAULTS[0]))
    for c in out:
        if c['key'] == '产品船':
            c['builtin'] = True
            c['behavior'] = 'ship'
    out.sort(key=lambda c: c['order'])
    for i, c in enumerate(out):
        c['order'] = i + 1
    return out


def load():
    """返回当前类别列表（list[dict]），按 order 升序。带 mtime 缓存。"""
    global _cache, _cache_mtime
    p = _path()
    try:
        mt = os.path.getmtime(p)
    except OSError:
        mt = -1.0
    with _lock:
        if _cache is not None and mt == _cache_mtime:
            return [dict(x) for x in _cache]
        raw = None
        if mt >= 0:
            try:
                with io.open(p, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
            except Exception:
                raw = None
        _cache = _normalize(raw)
        _cache_mtime = mt
        return [dict(x) for x in _cache]


def all_cats():
    return load()


def all_keys():
    return tuple(c['key'] for c in load())


def enabled_cats():
    return [c for c in load() if c['enabled']]


def behavior_of(btype):
    """类别名 → behavior。未知 / 空值按原有历史口径兜底为 place。"""
    k = (btype or '').strip()
    if not k:
        return 'place'
    for c in load():
        if c['key'] == k:
            return c['behavior']
    return 'place'


def is_valid(btype):
    k = (btype or '').strip()
    return any(c['key'] == k for c in load())


def default_key(behavior):
    """取某 behavior 下第一个启用的类别名；没有就取第一个（含停用）。"""
    cats = load()
    for c in cats:
        if c['enabled'] and c['behavior'] == behavior:
            return c['key']
    for c in cats:
        if c['behavior'] == behavior:
            return c['key']
    return '产品船'


def key_of(ship):
    """看板 dict → 用于筛选/显示的类别 key。历史空 btype 按船号推断。"""
    ship = ship or {}
    bt = (ship.get('btype') or '').strip()
    if bt and is_valid(bt):
        return bt
    no = (ship.get('ship_no') or '').strip()
    return default_key('ship' if no else 'place')


def group_labels():
    """给旧文案（亮点展示等）用的 behavior → 中文组名。"""
    out = {}
    for b in BEHAVIORS:
        out[b] = default_key(b)
    return out


def validate(payload, used_keys=()):
    """校验超管提交：返回 (ok, msg, cleaned_list)。

    used_keys: 数据库里真实被引用的 btype 集合，用来阻止「删掉还有看板的类别」。
    """
    try:
        rows = payload.get('categories')
        if not isinstance(rows, list):
            return False, '提交格式不对：缺少类别列表', None
        if len(rows) > MAX_CATS:
            return False, '最多 %d 个类别，请先删掉几个再加' % MAX_CATS, None
        used = set(str(k).strip() for k in used_keys if str(k).strip())
        old = {c['key']: c for c in load()}
        cleaned, seen = [], set()
        for i, r in enumerate(rows):
            if not isinstance(r, dict):
                continue
            key = str(r.get('key') or '').strip()
            beh = str(r.get('behavior') or '').strip()
            if not key:
                continue                       # 空白行直接跳过
            if len(key) > MAX_KEY_LEN:
                return False, '类别名「%s」超过 %d 个字，请改短' % (key, MAX_KEY_LEN), None
            if any(ch in key for ch in (',', '|', '"', "'", '<', '>', '\n', '\r')):
                return False, '类别名不能包含逗号、竖线、引号或尖括号：%s' % key, None
            if key in seen:
                return False, '类别名重复：%s' % key, None
            seen.add(key)
            if beh not in BEHAVIORS:
                return False, '「%s」的行为类型不合法' % key, None
            builtin = bool(r.get('builtin')) or key == '产品船'
            if key == '产品船' and beh != 'ship':
                return False, '「产品船」是内置类别，行为必须保持产品船类', None
            if builtin and key in old and old[key]['behavior'] != beh:
                return False, '内置类别「%s」的行为不能修改' % key, None
            cleaned.append({
                'key': key,
                'behavior': beh,
                'builtin': builtin,
                'enabled': bool(r.get('enabled', True)),
                'order': i + 1,
            })
        if not cleaned:
            return False, '至少要保留一个类别', None
        if not any(c['behavior'] == 'ship' for c in cleaned):
            return False, '至少要保留一个「产品船类」类别（船号 / 6S 依赖它）', None
        if '产品船' not in seen:
            return False, '内置类别「产品船」不能删除', None
        # 有看板在用的类别不允许消失（改名等价于删除旧 key）。
        # 只拦「当前配置里启用着的」key —— 库里遗留的、配置里早就没有的
        # 历史 btype 不该把保存卡死，它们由 behavior_of() 兜底成 place。
        protected = set(k for k, c in old.items() if c['enabled']) & used
        missing = protected - seen
        if missing:
            return False, ('这些类别下还有看板，不能删除或改名：%s。'
                           '请先把这些看板的类型改成别的类别，或者把该类别设为「停用」。'
                           % '、'.join(sorted(missing))), None
        cleaned.sort(key=lambda c: c['order'])
        for i, c in enumerate(cleaned):
            c['order'] = i + 1
        return True, '', cleaned
    except Exception as e:
        return False, '校验失败：%s' % e, None


def save(payload, used_keys=()):
    """校验 + 原子落盘。返回 (ok, msg)。"""
    global _cache, _cache_mtime
    ok, msg, cleaned = validate(payload, used_keys)
    if not ok:
        return False, msg
    p = _path()
    tmp = p + '.tmp'
    try:
        with _lock:
            with io.open(tmp, 'w', encoding='utf-8') as f:
                f.write(json.dumps({'version': 146, 'categories': cleaned},
                                   ensure_ascii=False, indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
            _cache = cleaned
            _cache_mtime = os.path.getmtime(p)
        return True, '类别已保存，共 %d 类' % len(cleaned)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False, '保存失败：%s' % e


def used_keys_from_ships(ships):
    return sorted(set((s.get('btype') or '').strip() for s in (ships or [])
                      if (s.get('btype') or '').strip()))
