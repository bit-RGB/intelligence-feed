# 📰 Intelligence Feed · 资讯聚合

🏷️ **当前版本：v0.1.0** · 📜 **许可证：MIT**

Hermes Desktop 的多频道资讯插件：在一个页面阅读半导体、AI、汽车、音乐资讯和微博热搜。

> 📌 非 Hermes 官方插件。当前主要在 Windows + Hermes Desktop 环境验证；单独安装 Hermes CLI 不会提供桌面页面。

## ✨ 功能

- 🗂️ **多频道看板**：全部、半导体、AI、汽车、音乐、微博热搜和收藏夹。
- ⭐ **阅读管理**：关键词搜索、已读 / 未读筛选、收藏、分页与未读数量。
- 📡 **多来源聚合**：RSS / Atom、HTML 列表及微博热搜适配器；相近资讯聚合为事件。
- 🌐 **网页预览**：在桌面端阅读原网页，保留原文入口。
- ⚙️ **来源设置**：单独启停来源，停用后保留历史数据与收藏。
- ⚡ **无 LLM 采集**：Python 标准库抓取与 SQLite 存储，不需要大模型 API Key。
- 🌏 **可选本地翻译**：支持 CTranslate2 英文标题翻译，未安装模型仍可正常采集和阅读原文。

## 🧰 环境要求

- 支持桌面插件 SDK 与 `dashboard/plugin_api.py` 的 Hermes Desktop。
- Python 3.11+；后端依赖 FastAPI 和 Pydantic 2。
- 能通过 HTTPS 访问所选资讯来源。
- 开发测试需要 pytest、httpx；前端导航测试需要 Node.js 18+。

## 🚀 安装

本仓库是统一插件包：Python 后端在根目录，前端位于 `desktop/plugin.js`。

1. 使用 Hermes 的插件管理命令安装本 GitHub 仓库并启用：

   ```bash
   hermes plugins install bit-RGB/intelligence-feed --enable --yes-deps
   ```

2. 完全退出并重新打开 Hermes Desktop，让后端加载新 API。
3. 在桌面端插件管理页面启用 **资讯**。前端默认关闭，和 Python 插件启用状态是两个开关。
4. 打开侧边栏「资讯」，点击更新按钮采集首批资讯。

如安装后没有页面，可在命令面板运行 **Reload desktop plugins**。API 提示 404 时，检查当前 profile 是否已启用 `intelligence-feed`，并重启 Desktop 后端。

安装机制参考：[Hermes 插件文档](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins) · [Desktop Plugin SDK](https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk)。

### 🔄 手动采集

在插件根目录执行：

```bash
python scripts/run_update.py
```

命令输出采集结果 JSON。也可由操作系统任务计划或 Hermes cron 周期运行此脚本；**安装插件不会自动创建定时任务**。`scripts/intelligence_feed_update.py` 是 Windows 默认安装路径的兼容启动器，不适用于任意自定义安装路径。

## 📡 来源配置

编辑 `config/sources.json`。默认包括爱集微、芯查查、IT之家、电子发烧友、TrendForce、量子位、OpenAI、Anthropic、Mistral、NVIDIA、GitHub Blog、TechCrunch、Nature、第一电动、凤凰网音乐、新浪音乐、中华网音乐和微博热搜。

各条目指定：

| 字段 | 说明 |
| --- | --- |
| `id` / `name` | 唯一标识与显示名称 |
| `domain` | `semiconductor`、`ai`、`automotive`、`music`、`weibo` |
| `adapter` | `feed`、`html-list` 或 `weibo-hot` |
| `url` | HTTPS 来源地址 |
| `include_url_regex` | HTML 列表正文链接白名单，避免采到导航页 |
| `max_items` | 单次收取上限 |
| `enabled` | 默认启用状态；页面可保存单独的来源开关 |

新来源应先实际抓取并检查标题、链接是否为正文，再加入默认清单。网站结构、访问限制和网络条件可能变化，不保证每次采集所有来源均可用。

## 🔒 数据与隐私

数据库不在源码仓库中。Windows 默认位置：

```text
%LOCALAPPDATA%/hermes/data/intelligence-feed/intelligence.db
```

设置环境变量 `INTELLIGENCE_FEED_DATA_DIR` 可覆盖数据目录。当前默认数据目录不是按 Hermes profile 隔离的；需要物理隔离时应为不同进程设置不同目录。

仓库不包含个人数据库、阅读记录、收藏、密钥、日志或翻译模型。插件会连接配置的第三方资讯站点；打开原文或内嵌预览同样会访问对应站点。

### 🌏 可选英文标题翻译

翻译模块延迟加载 `ctranslate2` 和 `sentencepiece`。如需使用，在运行后端的 Python 环境安装这两个依赖，并自行准备兼容的 Marian CTranslate2 英译中模型。模型目录需要 `model.bin`、`source.spm`、`target.spm` 及模型所需词表等文件。

用 `INTELLIGENCE_FEED_TRANSLATION_MODEL_DIR` 指向模型目录；默认位于数据目录下的 `models/opus-mt-en-zh-ctranslate2`。本仓库不附带模型、不自动下载模型，模型和依赖的许可证需另行遵守。

## 🛠️ 开发与测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/ -q
node --test tests/test_desktop_navigation.mjs
```

测试使用 fixture 和模拟网络，不依赖在线站点可用性；前端测试不替代实际 Desktop UI 验收。前端是原生 ESM，无 npm 构建步骤。

```text
config/sources.json       默认来源清单
desktop/plugin.js        Hermes Desktop 页面
dashboard/plugin_api.py  FastAPI 后端
intelligence_feed/       采集、解析、事件聚合、数据库和翻译
scripts/                 无 LLM 采集入口
tests/                   Python / Node 测试及 fixtures
```

修改前端后，注意 Hermes 可能加载 `desktop-plugins/intelligence-feed/plugin.js` 的运行时副本，而不是包内文件；开发时需同步或由宿主重新加载。修改 Python 后端后需重启后端进程。

## 📜 许可证与内容声明

插件代码使用 [MIT License](LICENSE)。第三方网站的文章、标题、商标和图片权利归原权利人所有，不属于本仓库的 MIT 授权范围。使用者应遵守来源网站的条款、访问频率限制和适用规则，不要将采集内容当作本项目开源内容重新分发。
