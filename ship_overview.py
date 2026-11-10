# -*- coding: utf-8 -*-
"""V1.48 综合数据总览 —— 只读聚合层（阶段 1）。

设计边界（严格遵守需求「不改变现有数据存储结构和业务逻辑」）：
  * 只用现有 db 层函数读数据：get_ships / get_records_by_ship / sixs 表
  * 不写库、不建表、不加字段、不改任何现有接口
  * 统计口径与单船页保持一致：已整改 == status in ('已闭环','已完成')
  * 产品船界定：board_categories 的 behavior == 'ship'（类别可配后不漏）
  * 安全档 = 【安全档全部看板】：产品船 / 场地 / 设备（V1.63 起不再只圈产品船）
  * 调试档 = 全部看板，【不按类别归类】；6S 仍只对产品船评分
  * 6S 是「一船一份评分」不是问题条目 ⇒ 只给诚实守信指标，
    不造「6S 问题总数 / 已整改 / 待处理」

本模块不 import app（避免循环依赖），可直接单测 / 供 test_client 调用。
"""
import json
import re
from collections import Counter

import db
import board_categories

# 与 app._ships_with_counts / _compute_stats 完全一致的闭环口径
CLOSED_STATUS = ('已闭环', '已完成')

# 明细下钻上限：现场库可能很大，一次不吐全表（对齐 /api/search_users 的教训）
MAX_DETAILS = 500


def _behavior(ship):
    """看板 -> behavior，复刻 app._board_cat 的历史兼容口径。
    btype 有值走配置；为空但有船号算产品船；配置里已不存在的值归 place。"""
    bt = ((ship or {}).get('btype') or '').strip()
    no = ((ship or {}).get('ship_no') or '').strip()
    if bt:
        return board_categories.behavior_of(bt)
    return 'ship' if no else 'place'


def _ship_no(ship):
    """sixs 用字符串船号弱关联：优先 ship_no；为空时从看板名里抽数字船号。

    本地实测有 3 张看板 ship_no 为空、船号写在名字里（"2556" / "111111" /
    "2499船安全隐患看板"），直接 JOIN 会全部匹配不上 —— 口径陷阱⑤。
    """
    no = ((ship or {}).get('ship_no') or '').strip()
    if no:
        return no
    m = re.search(r'\d{3,}', (ship or {}).get('name') or '')
    return m.group(0) if m else ''


def _sixs_items_map():
    """6S 标准编号 -> (大项, 内容)，用于把扣分编号翻译成人话。"""
    try:
        import sixs_data
        return dict((code, (g, txt)) for g, code, txt in sixs_data.sixs_items())
    except Exception:
        return {}


def _sixs_rows():
    """读取 6S 评分（全表，无 biz 列）。zero-write。"""
    conn = db.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT ship_no, score, deduct, picks FROM sixs ORDER BY id DESC")
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def _sixs_summary(scope, item_map):
    """6S 诚实守信指标：参评船数 / 平均分 / 最低分 / 扣分条目 / 高频扣分项。

    口径：只统计船号能匹配到本次【产品船】集合的评分记录，避免把
    调试档的船（如 2555）算进安全档总览。6S 表没有 biz 列 ⇒ 靠船号归一化匹配。
    """
    allowed = set(s['_no'] for s in scope if s.get('_no'))
    # V1.51：6S 改为「一船一周一份」后，总览仍按【每船最新一周的一份评分】统计，
    # 否则同一条船的多个周会被重复计入参评船数 / 平均分 / 扣分条目。
    # _sixs_rows() 已 ORDER BY id DESC ⇒ 每船第一条就是最新一周。
    _latest = {}
    for r in _sixs_rows():
        no = (r.get('ship_no') or '').strip()
        if no and no in allowed and no not in _latest:
            _latest[no] = r
    picked = list(_latest.values())
    scores = []
    for r in picked:
        try:
            scores.append(float(r.get('score') or 0))
        except (TypeError, ValueError):
            pass

    code_counter = Counter()
    deduct_total = 0
    for r in picked:
        try:
            p = json.loads(r.get('picks') or '{}')
        except Exception:
            p = {}
        if isinstance(p, dict):
            deduct_total += len(p)
            for code in p:
                code_counter[str(code)] += 1

    top = []
    for code, n in code_counter.most_common(5):
        g, txt = item_map.get(code, ('', ''))
        top.append({'code': code, 'count': n, 'group': g,
                    'text': (txt or '')[:38]})

    # 逐船得分（供汇总表「6S 得分」列），同船号多份取最新一条
    by_no = {}
    for r in _sixs_rows():
        no = (r.get('ship_no') or '').strip()
        if no and no in allowed and no not in by_no:
            by_no[no] = r.get('score')

    return {
        'scored_ships': len(picked),
        'avg_score': round(sum(scores) / len(scores), 1) if scores else 0,
        'min_score': min(scores) if scores else 0,
        'deduct_items': deduct_total,
        'deduct_total_records': len(picked),
        'top_items': top,
        'by_no': by_no,
        'has_data': bool(picked),
    }


def labels(biz):
    """术语字典：一套模板服务两个业务档，由后端注入，避开 SAFETY_LABEL_MAP
    整页字符串替换产生「隐患的隐患类别」这类病句。"""
    if biz == 'safety':
        return {
            'biz_name': '安全业务',
            'title': '数据总览',
            'scope': '安全档全部看板（产品船 / 场地 / 设备）',
            'scope_short': '看板',
            'item': '隐患',
            'done': '已整改',
            'open': '未整改',
            'rate': '整改率',
            'cat': '隐患类别',
            'unit': '责任单位',
            'board': '安全隐患看板',
            'board_url': '/safety',
            'detail_prefix': '/safety/board/',
            'sixs_url': '/sixs?biz=safety',
            'hl_url': '/hl?biz=safety',
        }
    return {
        'biz_name': '调试业务',
        'title': '船舶完整性总体视图',
        'scope': '调试档全部看板',
        'scope_short': '看板',
        'item': '手尾',
        'done': '已闭环',
        'open': '未闭环',
        'rate': '闭环率',
        'cat': '系统名称',
        'unit': '责任单位',
        'board': '船舶完整性管理看板',
        'board_url': '/boards',
        'detail_prefix': '/board/',
        'sixs_url': '',
        'hl_url': '',
    }


def build(biz='safety', detail_limit=MAX_DETAILS):
    """返回总览数据包（纯 dict，可直接 tojson 给前端）。

    结构：
      kpi        指标卡
      rows       跨船汇总表（末行合计由前端算，保持单一数据源）
      details    明细下钻（cap MAX_DETAILS，精简字段，不含手机号）
      charts     图表用的现成分组（前端也可从 details 自行 pivot）
      sixs       6S 诚实守信指标（仅安全档）
      dims       透视可选维度
      meta       口径说明 / 截断标记
    """
    # detail_limit=None 表示不限（导出 Excel 用），默认 MAX_DETAILS（页面用）
    limit = 10 ** 9 if detail_limit is None else int(detail_limit)
    biz = 'safety' if biz == 'safety' else 'debug'
    L = labels(biz)
    ships = [dict(s) for s in (db.get_ships(biz) or [])]
    for s in ships:
        s['_behavior'] = _behavior(s)
        s['_catkey'] = board_categories.key_of(s)
        s['_no'] = _ship_no(s)

    # V1.63（用户 2026-11-10）：安全档不再只圈产品船，场地 / 设备看板一起进总览；
    # 调试档沿用 2026-11-09 口径：全部看板，不按类别分组。两档都不按类别归类。
    scope = ships[:]

    rows = []
    details = []
    cat_counter = Counter()
    unit_counter = Counter()
    state_counter = Counter()
    month_counter = Counter()

    for s in scope:
        try:
            recs = db.get_records_by_ship(int(s['id'])) or []
        except Exception:
            recs = []
        closed = sum(1 for r in recs
                     if (r.get('status') or '') in CLOSED_STATUS)
        total = len(recs)
        opened = total - closed
        rows.append({
            'id': s['id'],
            'name': s.get('name') or '',
            'no': s.get('_no') or '',
            'cat': s.get('_catkey') or '',
            'total': total,
            'closed': closed,
            'open': opened,
            'rate': round(closed * 100.0 / total, 1) if total else 0,
        })
        for r in recs:
            is_closed = (r.get('status') or '') in CLOSED_STATUS
            st = L['done'] if is_closed else L['open']
            cat = (r.get('sys_name') or '').strip() or '未分类'
            unit = (r.get('area_name') or '').strip() or '未指定'
            month = (r.get('submit_time') or '')[:7] or '未知'
            ship_name = s.get('name') or ''
            cat_counter[cat] += 1
            unit_counter[unit] += 1
            state_counter[st] += 1
            month_counter[month] += 1
            if len(details) < limit:
                details.append({
                    'id': r.get('id'),
                    'ship_id': s['id'],
                    'ship': ship_name,
                    'no': s.get('_no') or '',
                    'cat': cat,
                    'unit': unit,
                    'state': st,
                    'closed': bool(is_closed),
                    'month': month,
                    'date': (r.get('submit_time') or '')[:10],
                    'desc': (r.get('description') or '')[:60],
                    'desc_full': r.get('description') or '',
                })

    n_ships = len(scope)
    n_total = sum(x['total'] for x in rows)
    n_closed = sum(x['closed'] for x in rows)
    n_open = sum(x['open'] for x in rows)

    item_map = _sixs_items_map()
    # 6S 是「一船一份评分」，场地 / 设备看板没有船号 ⇒ 仍只按产品船统计，
    # 避免把场地看板算进参评船数 / 平均分。
    sixs = _sixs_summary([s for s in scope if s['_behavior'] == 'ship'], item_map) if biz == 'safety' else None
    if sixs:
        for x in rows:
            x['sixs'] = sixs['by_no'].get(x['no'])

    kpi = [
        {'key': 'ships', 'label': L['scope_short'] + '数', 'value': n_ships, 'unit': '张'},
        {'key': 'total', 'label': L['item'] + '总数', 'value': n_total, 'unit': '条'},
        {'key': 'open', 'label': L['open'], 'value': n_open, 'unit': '条', 'tone': 'warn'},
        {'key': 'closed', 'label': L['done'], 'value': n_closed, 'unit': '条', 'tone': 'ok'},
        {'key': 'rate', 'label': L['rate'], 'value': (round(n_closed * 100.0 / n_total, 1)
                                                  if n_total else 0), 'unit': '%'},
    ]
    if sixs:
        kpi.append({'key': 'sixs_avg', 'label': '6S 平均分',
                    'value': sixs['avg_score'], 'unit': '分'})

    return {
        'biz': biz,
        'L': L,
        'kpi': kpi,
        'rows': rows,
        'details': details,
        'sixs': sixs,
        'charts': {
            'by_ship': [{'name': x['name'][:12], 'total': x['total'],
                         'closed': x['closed'], 'open': x['open']} for x in rows],
            'by_cat': [{'name': k, 'count': v}
                       for k, v in cat_counter.most_common(8)],
            'by_state': [{'name': k, 'count': v} for k, v in state_counter.items()],
            'by_unit': [{'name': k, 'count': v}
                        for k, v in unit_counter.most_common(8)],
            'by_month': [{'name': k, 'count': month_counter[k]}
                         for k in sorted(month_counter.keys())],
        },
        'dims': [
            {'key': 'cat', 'label': L['cat']},
            {'key': 'state', 'label': '整改状态' if biz == 'safety' else '闭环状态'},
            {'key': 'unit', 'label': L['unit']},
            {'key': 'month', 'label': '月份'},
        ],
        'meta': {
            'scope': L['scope'],
            'closed_rule': "status in ('已闭环','已完成')，与单船页一致",
            'details_capped': n_total > limit,
            'details_total': n_total,
            'details_shown': len(details),
            'all_ships': len(ships),
            'excluded': len(ships) - n_ships,
            'sixs_note': '6S 为一船一份评分，仅统计可诚实给出的指标；'
                         '不含「问题总数/已整改/待处理」',
            'cat_note': '隐患类别取 sys_name（37/37 有值），不取几乎全空的 rec_type',
        },
    }
