# -*- coding: utf-8 -*-
"""
main.py - 单 exe 启动入口（船舶收尾异常管理看板）

打包后的 .exe 双击运行：
  1. 确定数据目录（exe 所在目录），首次运行把内置初始数据复制过去
  2. 以线程方式启动「巡检推送程序」（monitor.py，每 10 秒巡检 + Excel 写回）
  3. 启动 Web 服务（Flask），并自动打开浏览器

安全说明：
  - 本文件是明文启动器，不含业务逻辑；业务代码已用 Cython 编译为 .pyd 二进制随 exe 分发。
  - 数据目录 = exe 所在目录（shipyard.db / uploads / config.json / app.log 等），
    升级 exe 版本不会覆盖已有数据。
  - 生产环境密钥（admin 密码、船海智云账号等）统一放在外部 config.json 中，
    exe 内部不再携带 config.json，避免把密钥打包进可执行文件。
"""
import os
import sys
import shutil
import json
import secrets
import string
import threading
import time
import webbrowser


DEFAULT_CFG = {
    "db": {"type": "auto", "host": "127.0.0.1", "port": 3306, "user": "root", "password": "", "database": "shipyard"},
    "admin": {"username": "admin", "password": "", "remember_days": 7},
    "secret_key": "",
    "webhook_url": "",
    "chz": {"enabled": False, "base_url": "https://qyzd.cssccloud.net", "companyId": "3",
            "account": "", "password": "", "uuid": "", "appId": "", "msgType": 2,
            "sourceTargetType": 0, "user_cache_file": "chz_users.json"},
    "site_url": "http://127.0.0.1:5000",
    "host": "0.0.0.0",
    "port": 5000
}


def _random_pass(length=18):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    while True:
        p = ''.join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in p) and any(c.isupper() for c in p)
                and any(c.isdigit() for c in p) and any(c in "!@#$%^&*-_" for c in p)):
            return p


def _data_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def ensure_config():
    """
    生产安全：exe 不内置 config.json。
    若运行目录没有 config.json，则生成一个带随机强密码的模板并退出，
    提示管理员先编辑好配置再启动。
    """
    base = _data_dir()
    cfg_path = os.path.join(base, "config.json")
    if os.path.exists(cfg_path):
        return cfg_path

    # 生成随机管理员密码，写入模板后退出
    tmpl = json.loads(json.dumps(DEFAULT_CFG))
    password = _random_pass()
    tmpl["admin"]["password"] = password

    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(tmpl, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[错误] 无法生成默认配置文件 {cfg_path}: {e}")
        sys.exit(1)

    print("=" * 60)
    print("  首次运行：已生成默认 config.json")
    print(f"  路径: {cfg_path}")
    print(f"  默认管理员密码: {password}")
    print("  请按生产环境编辑该文件（修改 admin 密码、site_url、船海智云账号等）后重新启动。")
    print("=" * 60)
    print("  已同步生成 首次运行默认密码.txt，请妥善保管。")
    print("=" * 60)
    try:
        with open(os.path.join(base, "首次运行默认密码.txt"), "w", encoding="utf-8") as f:
            f.write(f"默认管理员账号：{tmpl['admin']['username']}\n")
            f.write(f"默认管理员密码：{password}\n")
            f.write("请登录后尽快修改密码，并完善船海智云推送配置。\n")
    except Exception:
        pass
    sys.exit(0)


def bootstrap():
    """确定数据目录并把内置初始资源复制过去（仅首次运行）。"""
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(os.path.abspath(sys.executable))
        res = getattr(sys, '_MEIPASS', base)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
        res = base

    # 首次运行：把初始数据文件从内置资源复制到数据目录（已有则保留，绝不覆盖）
    # 注意：config.json 不在这里复制，必须由外部提供或首次运行时生成模板。
    for name in ("shipyard.db", "holidays.json", "chz_users.json"):
        src = os.path.join(res, name)
        dst = os.path.join(base, name)
        if os.path.isfile(src) and not os.path.exists(dst):
            try:
                shutil.copy2(src, dst)
                print(f"[初始化] 已生成 {name}")
            except Exception as e:
                print(f"[初始化] 复制 {name} 失败: {e}")

    # 上传照片目录
    src_u = os.path.join(res, "uploads")
    dst_u = os.path.join(base, "uploads")
    if os.path.isdir(src_u):
        try:
            os.makedirs(dst_u, exist_ok=True)
            for fn in os.listdir(src_u):
                sf = os.path.join(src_u, fn)
                df = os.path.join(dst_u, fn)
                if os.path.isfile(sf) and not os.path.exists(df):
                    shutil.copy2(sf, df)
            print(f"[初始化] 上传照片目录就绪（{dst_u}）")
        except Exception as e:
            print(f"[初始化] 准备上传目录失败: {e}")

    # 兜底：所有相对路径都以数据目录为基准
    try:
        os.chdir(base)
    except Exception:
        pass
    return base


def main():
    print("=" * 58)
    print("  船舶收尾异常管理看板  启动中...")
    print("=" * 58)

    # 生产安全：外部 config.json 必须存在
    ensure_config()
    base = bootstrap()

    # 业务模块在 bootstrap 之后导入
    from config import CONFIG
    import monitor
    import app as webapp

    port = int(CONFIG.get("port", 5000))
    host = CONFIG.get("host", "0.0.0.0")

    # 1) 巡检推送线程（含 Excel 写回）
    threading.Thread(target=monitor.run_forever, daemon=True).start()
    print(f"[启动] 巡检推送程序已启动（每 {monitor.POLL_INTERVAL} 秒巡检）")

    # 2) 稍后自动打开浏览器
    def _open_browser():
        time.sleep(2.5)
        try:
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:
            pass
    threading.Thread(target=_open_browser, daemon=True).start()

    # 3) Web 服务（前台阻塞）
    print(f"[启动] 看板地址: http://127.0.0.1:{port}   （数据目录: {base}）")
    print("关闭本窗口即停止服务。")
    webapp.app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
