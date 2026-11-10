# -*- coding: utf-8 -*-
"""调试业务「调试责任人」名单（V1.16.17：由固定超管王平平在「用户管理」页维护）

用途：调试业务（船舶完整性看板）自己的「调试责任人」名单 —— 加进名单的人打开系统即按【调试责任人】渲染。
能力：查看 / 发起 / 反馈闭环；被显式加进本名单的人，会从「调试上级责任人」退回「调试责任人」。

⚠️ 与其它三份名单**完全独立**，各有一份 module 级缓存，互不影响：
   · admin_grant_config.json        安全档 超管
   · debug_admin_grant_config.json  调试档 超管
   · debug_sup_config.json          调试档 调试上级责任人（本文件）
   · debug_user_grant_config.json   调试档 调试责任人
   · safety_review_config.json      安全档 审核人
   改这里的部门解析逻辑时，请几份一起改（见 debug_user_grant.py 顶部说明）。

配置存 `debug_user_grant_config.json`：
   {"depts":[{"path":"总装二部/综合管理室","deep":true}],
    "people":[{"name":"张三","phone":"138...","dept":"..."}],
    "excluded":[{"name":"李四","phone":"139..."}]}
默认**空**。

为什么按部门授权：一个科室几十人不可能真去建几十个账号；名单方式改完**即时生效**，不写业务库。

用法：
    python debug_user_grant.py
    python debug_user_grant.py --set-depts 总装二部/综合管理室
    python debug_user_grant.py --reset
"""

import io
import json
import os
import sys

try:
    from config import BASE_DIR
except Exception:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, 'debug_user_grant_config.json')
_CFG = None


def _now():
    import datetime
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _read_file():
    try:
        with io.open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return None


def config(force=False):
    global _CFG
    if _CFG is not None and not force:
        return _CFG
    d = _read_file()
    if not d:
        d = {'depts': [], 'people': []}
    d.setdefault('depts', [])
    d.setdefault('people', [])
    d.setdefault('excluded', [])          # V1.16.15：从「部门授权」里单独剔掉的人
    _CFG = d
    return _CFG


def save_config(depts=None, people=None, excluded=None):
    global _CFG
    cur = config()
    d = {
        'saved_at': _now(),
        'depts': depts if depts is not None else cur.get('depts') or [],
        'people': people if people is not None else cur.get('people') or [],
        # V1.16.15：excluded = 在部门授权范围内、但被单独取消超管的人（按手机号/姓名）
        'excluded': excluded if excluded is not None else cur.get('excluded') or [],
    }
    d['depts'] = [x for x in d['depts'] if isinstance(x, dict) and (x.get('path') or x.get('id'))]
    d['people'] = [x for x in d['people'] if isinstance(x, dict) and (x.get('phone') or x.get('name'))]
    d['excluded'] = [x for x in d['excluded'] if isinstance(x, dict) and (x.get('phone') or x.get('name'))]
    # V1.16.16：被重新写回「people」的人，自动从排除名单里去掉。
    #   否则「取消超管（部门授权 → 写进 excluded）」之后再把人加回来，is_member 仍会判 False，
    #   表现为「加了不生效」。
    def _same_person(x, y):
        px, py = (x.get('phone') or '').strip(), (y.get('phone') or '').strip()
        nx, ny = (x.get('name') or '').strip(), (y.get('name') or '').strip()
        return bool((px and px == py) or (nx and nx == ny))
    d['excluded'] = [x for x in d['excluded'] if not any(_same_person(x, p) for p in d['people'])]
    tmp = CONFIG_FILE + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(d, ensure_ascii=False, indent=1))
    os.replace(tmp, CONFIG_FILE)
    _CFG = d
    return d


def depts():
    return [x for x in (config().get('depts') or []) if isinstance(x, dict)]


def explicit_people():
    return [x for x in (config().get('people') or []) if isinstance(x, dict)]


def excluded_people():
    """被单独剔出「调试责任人」名单的人（在部门授权范围内也照样不算）"""
    return [x for x in (config().get('excluded') or []) if isinstance(x, dict)]


def _excluded_match(phone='', name=''):
    phone = (phone or '').strip()
    name = (name or '').strip()
    for x in excluded_people():
        if phone and (x.get('phone') or '').strip() == phone:
            return True
        if name and (x.get('name') or '').strip() == name:
            return True
    return False


def is_configured() -> bool:
    """名单是否被配置过（空名单 = 只有固定超管，不算配过）"""
    return bool(depts() or explicit_people())


# ---------------- 部门解析（同 safety_review.py） ----------------

def _org():
    try:
        import chz_org as org
        return org if org.ready() else None
    except Exception:
        return None


def resolve_dept_ids(path_or_id):
    if not (path_or_id or '').strip():
        return []
    try:
        import chz_org as org
        st = org.load()
    except Exception:
        return []
    if not st.get('by_id'):
        return []
    key = path_or_id.strip()
    if key in st['by_id']:
        return [key]
    out = []
    for d in st['by_id'].values():
        full = (d.get('f') or '')
        if key and (full.endswith(key) or key in full):
            out.append(d['i'])
    return out


def dept_info(item):
    path = (item.get('path') or '').strip()
    did = (item.get('id') or '').strip()
    ids = [did] if did else resolve_dept_ids(path)
    deep = item.get('deep', True)
    n = 0
    full = path
    try:
        import chz_org as org
        for i in ids:
            n += org.count_people(i, deep=deep)
            d = org.dept(i)
            if d and not path:
                full = org.short_full(d.get('f') or '')
    except Exception:
        pass
    return {'path': path, 'id': (ids[0] if ids else ''), 'ids': ids, 'deep': bool(deep),
            'count': n, 'full': full}


def _sort_people(rows):
    return sorted(rows, key=lambda x: (x.get('dept') or '', x.get('name') or ''))


def dept_people():
    """只由「部门 / 科室」授权带出来的人（不含单独指定、不含已排除）"""
    out = {}
    org = _org()
    if org:
        st = org.load()
        for item in depts():
            deep = item.get('deep', True)
            for did in dept_info(item)['ids']:
                for i in (org.subtree_ids(did) if deep else [did]):
                    for u in st['by_dept'].get(i, []):
                        d = st['by_id'].get(u['deptId']) or {}
                        out[u['phone']] = {'name': u['name'], 'phone': u['phone'],
                                           'dept': org.short_full(d.get('f') or '')}
    return _sort_people([p for p in out.values()
                         if not _excluded_match(p.get('phone'), p.get('name'))])


def people():
    """当前名单里的所有人（部门成员 + 单独指定 - 已排除），[{name, phone, dept}]"""
    out = {}
    for p in dept_people():
        out[p.get('phone')] = p
    for p in explicit_people():
        ph = (p.get('phone') or '').strip()
        nm = (p.get('name') or '').strip()
        if ph:
            out[ph] = {'name': nm or ph, 'phone': ph, 'dept': p.get('dept') or ''}
        elif nm:
            out['name:' + nm] = {'name': nm, 'phone': '', 'dept': p.get('dept') or ''}
    return _sort_people([p for p in out.values()
                         if not _excluded_match(p.get('phone'), p.get('name'))])


def is_explicit(phone='', name=''):
    """是不是「单独指定」的调试责任人（这种可以直接取消；部门带出来的要先排除）"""
    phone = (phone or '').strip()
    name = (name or '').strip()
    for p in explicit_people():
        if phone and (p.get('phone') or '').strip() == phone:
            return True
        if name and (p.get('name') or '').strip() == name:
            return True
    return False


def is_dept_member(phone='', name=''):
    """是否由某个「部门 / 科室」授权带出来的调试责任人"""
    phone = (phone or '').strip()
    name = (name or '').strip()
    if not phone and not name:
        return False
    for p in dept_people():
        if phone and (p.get('phone') or '').strip() == phone:
            return True
        if name and (p.get('name') or '').strip() == name:
            return True
    return False


def remove_person(phone='', name=''):
    """V1.16.15：取消某个人的「调试责任人」授权（用户管理页表格用）
    · 只在「单独指定」里 → 直接删掉；
    · 还落在某个部门授权范围内 → 写进排除名单（部门保留，只剔掉这个人）。"""
    phone = (phone or '').strip()
    name = (name or '').strip()
    if not phone and not name:
        return False
    keep = []
    for p in explicit_people():
        if (phone and (p.get('phone') or '').strip() == phone) or \
           (name and (p.get('name') or '').strip() == name):
            continue
        keep.append(p)
    exc = excluded_people()
    if is_dept_member(phone, name) and not _excluded_match(phone, name):
        exc.append({'name': name, 'phone': phone})
    save_config(people=keep, excluded=exc)
    return True


def add_person(phone='', name='', dept=''):
    """V1.16.15：把某人设为「调试责任人」（用户管理页表格用）
    取消排除 + 写入「单独指定」名单，即时生效，不建账号。"""
    phone = (phone or '').strip()
    name = (name or '').strip()
    if not phone and not name:
        return False
    exc = [x for x in excluded_people()
           if not ((phone and (x.get('phone') or '').strip() == phone) or
                   (name and (x.get('name') or '').strip() == name))]
    ppl = explicit_people()
    if not is_explicit(phone, name):
        ppl.append({'name': name or phone, 'phone': phone, 'dept': dept or ''})
    save_config(people=ppl, excluded=exc)
    return True


def count() -> int:
    return len(people())


def is_member(phone='', name='', dept='') -> bool:
    """某人是否在「调试责任人」名单里"""
    phone = (phone or '').strip()
    name = (name or '').strip()
    dept = (dept or '').strip()
    if not phone and not name and not dept:
        return False
    # V1.16.15：被单独取消超管的人，即使还落在部门授权范围内也不算超管
    if (phone or name) and _excluded_match(phone, name):
        return False
    for p in explicit_people():
        if phone and (p.get('phone') or '').strip() == phone:
            return True
        if name and (p.get('name') or '').strip() == name:
            return True
    org = _org()
    if org:
        st = org.load()
        info = org.dept_of_phone(phone) if phone else None
        if not info and name:
            for u in st['u_by_phone'].values():
                if u['name'] == name:
                    info = {'deptId': u['deptId']}
                    break
        if info:
            did = info.get('deptId') or ''
            for item in depts():
                for root in dept_info(item)['ids']:
                    idset = set(org.subtree_ids(root)) if item.get('deep', True) else {root}
                    if did in idset:
                        return True
        return False
    if dept:
        for item in depts():
            if (item.get('path') or '') and item['path'] in dept:
                return True
    return False


def summary():
    return {
        'depts': [dept_info(x) for x in depts()],
        'people': explicit_people(),
        'excluded': excluded_people(),
        'count': count(),
        'saved_at': config().get('saved_at') or '',
    }


if __name__ == '__main__':
    if '--set-depts' in sys.argv:
        i = sys.argv.index('--set-depts')
        raw = sys.argv[i + 1] if i + 1 < len(sys.argv) else ''
        paths = [x.strip() for x in raw.split(',') if x.strip()]
        save_config(depts=[{'path': p, 'deep': True} for p in paths], people=[], excluded=[])
        print('已保存部门：%s' % paths)
    elif '--reset' in sys.argv:
        save_config(depts=[], people=[], excluded=[])
        print('已清空调试业务「调试责任人」名单')
    s = summary()
    print('配置文件 : %s' % CONFIG_FILE)
    print('部门     :')
    for d in s['depts']:
        print('   %-30s 含下级 %3d 人 (deep=%s)' % (d['full'] or d['path'], d['count'], d['deep']))
    print('单独指定 : %s' % ('、'.join((x.get('name') or x.get('phone') or '') for x in s['people']) or '（无）'))
    print('名单人数 : %d（不含固定超管）' % s['count'])
