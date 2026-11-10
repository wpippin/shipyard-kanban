# 船舶总装收尾异常管理看板

厂区内网使用的船舶总装 / 调试阶段收尾异常（隐患）登记、整改、审批与统计看板。
Web 端 Flask 应用 + 后台巡检进程，手机浏览器为主要使用终端。

> 本仓库为**私有源码仓库**，只包含可公开于团队内部的代码与模板。
> 现场配置密钥、业务数据库、人员通讯录、现场照片、编译产物均**不在仓库内**（见「不包含的内容」）。

## 技术栈

| 层 | 内容 |
| --- | --- |
| 后端 | Python 3.11 + Flask + Jinja2 + PyMySQL |
| 前端 | 原生 HTML / CSS / JS，Bootstrap 5、Chart.js、qrcode 均为 `/static/` 本地文件 |
| 样式 | `static/css/v16.css` 的 `--v16-*` 令牌为唯一色板；`tokens.css` 是 `--sy-*` 别名层 |
| 主题 | 亮 / 暗双主题，读 `localStorage.shipyard_theme`，给 `<html>` 挂 `data-theme="dark"` |
| 巡检 | `monitor.py` 定时扫库，按分级规则推送提醒 |
| 打包 | `build_v116.py` 用 Cython 把 18 个模块编译为 `.so` |

依赖见 `requirements.txt`。

## 目录结构

```
├── app.py / db.py / config.py ...   业务与数据层（共 18 个编译模块）
├── main.py / monitor.py             启动入口 / 后台巡检
├── build_v116.py                    Cython 编译脚本
├── templates/                       17 个 Jinja2 页面，各自独立（无 base.html 继承）
├── static/
│   ├── css/  js/                    自研样式脚本 + 本地第三方库
│   └── downloads/                   导入用 Excel 模板
├── config.example.json              脱敏配置样例（键结构齐全，值均为占位）
├── requirements.txt                 Python 依赖清单
└── 一键启动.bat / 防火墙放行.bat      Windows 本机启动与端口放行
```

## 本地运行（Windows）

1. 安装 Python 3.11，执行 `pip install -r requirements.txt`。
2. 把 `config.example.json` 复制为 `config.json`，按现场环境填写数据库、密钥、推送等配置。
   **`config.json` 已被 `.gitignore` 排除，切勿提交。**
3. 双击 `一键启动.bat`，或分别运行 `python init_db.py`、`python monitor.py`、`python app.py`。
4. 浏览器打开 `http://127.0.0.1:5000`。

## 服务器部署与编译红线

- 🔴 **改动任何 `.py` 必须重新编译**：`rm -f *.so *.c` → `python build_v116.py`（应输出 `BUILD 18 MODULES` / `BUILD DONE`、生成 18 个 `.so`）→ 再重启服务。`.so` 优先加载，不重编译则新代码被**静默忽略且不报错**。
- 只改 `templates/` 或 `static/` 时**只需重启**，不用编译。
- 编译前删除旧 `.c` / `.so`，否则构建工具判定「已最新」会跳过。
- 顺序永远是「编译完 → 再重启」；编译期间目录里没有 `.so`，此时重启必崩。
- 新增 `.py` 模块必须加进 `build_v116.py` 的 `ALL_MODULES`，并保持 `always_allow_keywords=True`（否则带参数的路由全 500）。
- 重启只按 PID 结束进程，不要用 `pkill -f` 之类的模糊匹配。

## 前端改动约定

1. 保留所有 `{% ... %}` / `{{ ... }}` 标签原样，安全档与调试档靠条件分支换词。
2. 🈲 任何公网 CDN —— 厂区手机出不了公网；CSS/JS 一律 `/static/` 本地文件。
3. 颜色只用 `var(--v16-*)` / `var(--sy-*)`，圆角 / 间距 / 字阶沿用 `--rd-*` / `--sp-*` / `--fs-*`，不写死色值。
4. 加载顺序：`tokens → bootstrap → 页面内联 style → theme.css → v16.css → 页面专有 css → theme.js`，**`v16.css` 必须最后**。
5. 保持既有 DOM id、函数名、form action、fetch 接口、隐藏字段不变，后端依赖它们。
6. 🈲 原生 `confirm()` / `alert()`，内嵌 WebView 会吞掉系统弹窗，改用页面内自定义弹层。
7. 手机端 390px 优先；桌面端规则包进 `@media (min-width:768px)`。
8. 独立页面（不继承 base）需自行读取主题并写 `html[data-theme="dark"]`。

## 不包含的内容（外发脱敏）

以下文件因含密钥、真实人员手机号、业务数据或为构建产物，已被 `.gitignore` 排除，需现场自行准备：

- `config.json` 及备份、`*_grant_config.json`、`safety_reviewers.json` 等授权与审核人名单
- `chz_users.json` / `chz_org.json` 等企业人员与组织通讯录
- `*.db` / `*.sqlite*`、运行日志、现场上传照片（`uploads/` 等）
- `*.so` / `*.c` / `*.exe` / `dist/` / `build*/` 等编译打包产物
- 各类 `.bak*` 备份、历史补丁 zip、内部部署文档与预览稿

> 注意：业务源码中仍有少量硬编码手机号，其中 `app.py` 的超管账号是登录权限判断常量，
> 修改会导致登录失效，故保留原样。私有仓库请按单位规定控制成员范围。

## 维护说明

本仓库只保存**当前可运行的完整源码**，不采用补丁包 / 增量包分发方式：
更新直接覆盖对应源文件，改 `.py` 后按上文重新编译，改模板或静态文件后重启即可。
版本过程文档、变更说明、部署补丁 zip 均不纳入版本管理。
