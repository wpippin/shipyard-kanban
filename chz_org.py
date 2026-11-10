# -*- coding: utf-8 -*-
"""船海智云组织架构（部门树 + 人员）本地缓存，供「按部门选人 / 按部门定审核权」使用（V1.13）

数据来源：《船海智云对外服务接口 V2.3》
- 2.4 `GET /api/app-site/sync_manage/sync_org`
  一次返回 syncUserList（人员，带 divisionId/divisionName/divisionFullName）
  与 syncDivisionList（部门集合，带 id/name/pId/fullName/sort）
- 2.5 `/sync_manage/sync_org_user`、2.6 `/sync_manage/sync_org_division` 为按时间段增量（本模块暂用全量）

缓存文件 `chz_org.json`（约 2MB）：
    {"saved_at": "...", "depts": [{"i","n","p","f","s"}], "users": [["姓名","手机号","部门id","工号"], ...]}

用法：
    python chz_org.py             # 打印统计（部门数/人数/总装二部子部门）
    python chz_org.py --refresh   # 重新拉取并刷新缓存
"""
import io
import json
import os
import sys
import datetime

try:
    from config import BASE_DIR
except Exception:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CACHE_FILE = os.path.join(BASE_DIR, 'chz_org.json')
_C = None


# ---------------- 拉取 ----------------

def fetch_from_chz(verbose=True):
    """拉全量组织架构 → (depts, users)"""
    import chz_push as c
    token = c.get_token()
    params = {'syncId': c.APP_ID, 'isAll': 'true',
              'isSyncCodingUser': 'false', 'companyId': c.COMPANY_ID}
    data = c._req('GET', '/api/app-site/sync_manage/sync_org',
                  params=params, token=token, timeout=60)
    d = data or {}
    depts = []
    for x in (d.get('syncDivisionList') or []):
        if x.get('operation') == 3 or str(x.get('isEnable')).lower() == 'false':
            continue
        depts.append({
            'i': str(x.get('id') or ''),
            'n': (x.get('name') or '').strip(),
            'p': str(x.get('pId') or ''),
            'f': (x.get('fullName') or '').replace('\\', '/'),
            's': int(x.get('sort') or 0),
        })
    users = []
    for u in (d.get('syncUserList') or []):
        if u.get('operation') == 3 or str(u.get('isEnable')).lower() == 'false':
            continue
        phone = c._norm_phone(u.get('phone') or u.get('shortPhone') or '')
        if not phone:
            continue
        users.append([(u.get('name') or '').strip(), phone,
                      str(u.get('divisionId') or ''), (u.get('code') or '').strip()])
    if verbose:
        print('  [组织架构] 部门 %d 个，人员 %d 人' % (len(depts), len(users)))
    return depts, users


def refresh(verbose=True):
    global _C
    depts, users = fetch_from_chz(verbose=verbose)
    data = {'saved_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'depts': depts, 'users': users}
    try:
        tmp = CACHE_FILE + '.tmp'
        with io.open(tmp, 'w', encoding='utf-8') as f:
            f.write(json.dumps(data, ensure_ascii=False, separators=(',', ':')))
        os.replace(tmp, CACHE_FILE)
        if verbose:
            print('  [组织架构] 已缓存 %s（%.1f MB）'
                  % (os.path.basename(CACHE_FILE), os.path.getsize(CACHE_FILE) / 1048576.0))
    except Exception as e:
        print('  [组织架构] 缓存写入失败: %s' % e)
    _C = _build(data)
    return _C


# ---------------- 加载与索引 ----------------

def _build(data):
    depts = data.get('depts') or []
    users = data.get('users') or []
    by_id = {d['i']: d for d in depts}
    # ⚠️ 实测：船海智云返回的 pId 大量指向不存在的父部门（1590 个部门里断链很多），
    #    直接拿 pId 建树会得到几十个假"根"。所以**改用部门全称按 / 切分反推父子**，稳得多。
    by_full = {}
    for d in depts:
        f = (d.get('f') or '').strip('/')
        if f:
            by_full.setdefault(f, d)
    for d in depts:
        f = (d.get('f') or '').strip('/')
        if '/' in f:
            par = by_full.get('/'.join(f.split('/')[:-1]))
            d['_p'] = par['i'] if par else ''
        else:
            d['_p'] = ''
    kids = {}
    for d in depts:
        kids.setdefault(d.get('_p') or '', []).append(d['i'])
    for k in kids:
        kids[k].sort(key=lambda i: (by_id.get(i, {}).get('s') or 0, by_id.get(i, {}).get('n') or ''))
    direct = {}
    u_by_phone = {}
    by_dept = {}
    for u in users:
        name, phone, did = u[0], u[1], u[2]
        code = u[3] if len(u) > 3 else ''
        direct[did] = direct.get(did, 0) + 1
        u_by_phone[phone] = {'name': name, 'phone': phone, 'deptId': did, 'code': code}
        by_dept.setdefault(did, []).append(u_by_phone[phone])
    # 子树人数（含自己）
    total = {}

    def _tot(did):
        if did in total:
            return total[did]
        n = direct.get(did, 0)
        for c in kids.get(did, []):
            n += _tot(c)
        total[did] = n
        return n

    for did in list(by_id.keys()):
        _tot(did)
    return {'saved_at': data.get('saved_at') or '', 'by_id': by_id, 'kids': kids,
            'direct': direct, 'total': total, 'u_by_phone': u_by_phone, 'by_dept': by_dept,
            'n_users': len(users)}


def load(verbose=False):
    global _C
    if _C is not None:
        return _C
    try:
        with io.open(CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _C = _build(data)
        if verbose:
            print('  [组织架构] 使用缓存（%s）：部门 %d，人员 %d'
                  % (_C['saved_at'], len(_C['by_id']), _C['n_users']))
        return _C
    except Exception:
        _C = _build({'depts': [], 'users': []})
        if verbose:
            print('  [组织架构] 无缓存 —— 请先跑 python chz_org.py --refresh')
        return _C


def ready() -> bool:
    return bool(load().get('by_id'))


# ---------------- 查询 ----------------

def dept(dept_id):
    return load()['by_id'].get(dept_id or '')


def short_full(full, keep=3):
    """部门全称瘦身：公司/总装二部/安全设备室 → 总装二部/安全设备室"""
    parts = [x for x in (full or '').split('/') if x]
    return '/'.join(parts[-keep:]) if parts else ''


def children(pid, with_total=True):
    """某部门的直接子部门（pid 为空 = 根）"""
    s = load()
    out = []
    for did in s['kids'].get(pid or '', []):
        d = s['by_id'].get(did)
        if not d:
            continue
        item = {'id': d['i'], 'name': d['n'], 'full': d['f'],
                'short': short_full(d['f']), 'direct': s['direct'].get(did, 0)}
        if with_total:
            item['total'] = s['total'].get(did, 0)
        out.append(item)
    return out


def roots():
    """顶层（通常是各公司）"""
    s = load()
    tops = [d for d in s['by_id'].values() if not d.get('_p')]
    return sorted([{'id': d['i'], 'name': d['n'], 'full': d['f'], 'short': d['f'],
                    'direct': s['direct'].get(d['i'], 0),
                    'total': s['total'].get(d['i'], 0)} for d in tops],
                  key=lambda x: -x['total'])


def path_to(dept_id, keep=4):
    """面包屑：从顶层到该部门"""
    s = load()
    out = []
    cur = s['by_id'].get(dept_id or '')
    n = 0
    while cur and n < 10:
        out.append({'id': cur['i'], 'name': cur['n']})
        cur = s['by_id'].get(cur.get('_p') or '')
        n += 1
    return list(reversed(out))[-keep:]


def subtree_ids(dept_id):
    """该部门及其所有下级部门 id"""
    s = load()
    out = []
    stack = [dept_id]
    while stack:
        cur = stack.pop()
        if not cur:
            continue
        out.append(cur)
        stack.extend(s['kids'].get(cur, []))
    return out


def count_people(dept_id, deep=True):
    s = load()
    if not deep:
        return s['direct'].get(dept_id or '', 0)
    return s['total'].get(dept_id or '', 0)


def people(dept_id='', kw='', limit=300, deep=True):
    """取人员：按部门（默认含下级）+ 关键字（姓名/工号/手机号）过滤"""
    s = load()
    ids = set(subtree_ids(dept_id)) if (dept_id and deep) else ({dept_id} if dept_id else None)
    kw = (kw or '').strip()
    out = []
    if ids is None:
        # 无部门限定时遍历全部（3 万人，只在搜索时走这条路）
        src = s['u_by_phone'].values()
    else:
        src = []
        for i in ids:
            src.extend(s['by_dept'].get(i, []))
    for u in src:
        if kw and (kw not in (u['name'] or '')) and (kw not in (u['phone'] or '')) \
                and (kw not in (u.get('code') or '')):
            continue
        d = s['by_id'].get(u['deptId']) or {}
        out.append({'name': u['name'], 'phone': u['phone'], 'code': u.get('code') or '',
                    'deptId': u['deptId'], 'dept': short_full(d.get('f') or '')})
        if len(out) >= limit:
            break
    out.sort(key=lambda x: (x['dept'], x['name']))
    return out


def dept_of_phone(phone):
    s = load()
    u = s['u_by_phone'].get((phone or '').strip())
    if not u:
        return None
    d = s['by_id'].get(u['deptId']) or {}
    return {'deptId': u['deptId'], 'dept': short_full(d.get('f') or ''), 'full': d.get('f') or '',
            'name': u['name']}


def search_depts(kw, limit=40):
    """按名字/全称搜部门（含人数），用于"按部门选"时的快速定位"""
    s = load()
    kw = (kw or '').strip()
    if not kw:
        return []
    out = []
    for d in s['by_id'].values():
        if kw in d['n'] or kw in d['f']:
            out.append({'id': d['i'], 'name': d['n'], 'full': d['f'], 'short': short_full(d['f']),
                        'direct': s['direct'].get(d['i'], 0), 'total': s['total'].get(d['i'], 0)})
    out.sort(key=lambda x: (-x['total'], x['full']))
    return out[:limit]


if __name__ == '__main__':
    if '--refresh' in sys.argv:
        refresh()
    st = load(verbose=True)
    print('缓存时间: %s' % st['saved_at'])
    print('部门 %d 个 ｜ 人员 %d 人' % (len(st['by_id']), st['n_users']))
    print('顶层: %s' % '、'.join('%s(%d)' % (r['name'], r['total']) for r in roots()))
    for kw in ('总装二部', '安全设备室', '生产管理室'):
        for d in search_depts(kw)[:4]:
            print('  %-30s 直属 %3d ｜ 含下级 %3d' % (d['short'], d['direct'], d['total']))
