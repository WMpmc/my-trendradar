# coding=utf-8
"""
TrendRadar 销售情报仪表盘 —— 「早间简报」版
=============================================

为科华（Kehua）UPS 销售（广东区 · 股份制银行方向）打造的每日情报仪表盘。

数据流：
    output/news/YYYY-MM-DD.db   热榜新闻（news_items / platforms / rank_history ...）
    output/rss/YYYY-MM-DD.db    RSS 资讯（rss_items / rss_feeds）
    config/frequency_words.txt  关键词分类配置
    index.html                  TrendRadar 生成的报告（从中提取 AI 分析五板块）

产出：
    _site/index.html                     最新仪表盘（GitHub Pages 入口）
    _site/reports/YYYY-MM-DD-HHMM.html   归档副本（站点内）
    reports/YYYY-MM-DD-HHMM.html         归档副本（提交 git，跨运行持久化）

设计要点：
    - 默认落地页为「今日简报」：日期大标题 + 统计卡 + AI 五张彩色卡片 + 两张小图表
    - 左侧固定导航（移动端为顶部横向标签栏），JS 无刷新切换视图
    - 新闻为紧凑单行列表（类邮箱收件箱），一屏可浏览多条
    - 销售优先导航：商机信号 / 科华动态 / 股份制银行 / UPS与电源 / 数据中心 /
      AI与大模型 / 友商动态（华为·维谛·科士达·伊顿·施耐德·台达）/ 行业资讯 / 总榜
    - 明暗双主题、历史报告下拉、回到顶部、移动端响应式

用法：
    python dashboard.py                  # 生成今日简报
    python dashboard.py --date 2025-12-27  # 重新生成指定日期（补票/调试用）

仅依赖标准库（sqlite3 / json / re / html.parser / pathlib / datetime）。
跨平台：Windows + Ubuntu。时区：Asia/Shanghai (UTC+8)。
"""

from __future__ import annotations

import html as html_lib
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ═══════════════════════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════════════════════

SHANGHAI_TZ = timezone(timedelta(hours=8))

SITE_DIR = Path("_site")
REPORTS_SITE_DIR = SITE_DIR / "reports"     # 站点内归档（随 Pages 部署）
REPORTS_GIT_DIR = Path("reports")          # git 持久化归档（跨运行保留）

MAX_HISTORY_REPORTS = 60
ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"

# 分类配色（图表 / 标签循环用）
CATEGORY_PALETTE = [
    "#2563eb", "#16a34a", "#f97316", "#dc2626", "#9333ea",
    "#0d9488", "#db2777", "#ca8a04", "#4f46e5", "#65a30d",
]

# 友商品牌关键词（标题扫描用，小写匹配）
COMPETITORS: List[Tuple[str, str, List[str]]] = [
    ("comp-huawei", "华为", ["华为", "鸿蒙", "昇腾", "鲲鹏", "海思", "任正非",
                            "余承东", "huawei", "harmonyos", "数字能源"]),
    ("comp-vertiv", "维谛", ["维谛", "vertiv", "艾默生"]),
    ("comp-kstar", "科士达", ["科士达", "kstar"]),
    ("comp-eaton", "伊顿", ["伊顿", "eaton"]),
    ("comp-schneider", "施耐德", ["施耐德", "schneider", "apc"]),
    ("comp-delta", "台达", ["台达", "中达电通", "delta"]),
]

# 销售优先固定视图：(视图id, 名称, 图标, 分类名关键词, 标题关键词)
# 分类名命中即「认领」该分类（不再出现在其他分类中）；标题命中为补充抓取。
FIXED_VIEWS: List[Dict[str, Any]] = [
    {
        "id": "kehua", "label": "科华动态", "icon": "🏢",
        "cat_kw": ["科华", "kehua"],
        "title_kw": ["科华", "kehua", "科华数据", "科华技术"],
        "title_re": None,
    },
    {
        "id": "banks", "label": "股份制银行", "icon": "🏦",
        "cat_kw": ["银行"],
        "title_kw": ["银行", "浦发", "兴业银行", "民生银行", "广发银行",
                     "平安银行", "光大银行", "华夏银行", "浙商银行", "渤海银行",
                     "恒丰银行", "中信银行", "招商银行", "股份制"],
        "title_re": None,
    },
    {
        "id": "ups", "label": "UPS与电源", "icon": "⚡",
        "cat_kw": ["ups", "电源", "蓄电池", "储能", "配电"],
        "title_kw": ["ups", "不间断电源", "蓄电池", "锂电", "储能", "电源",
                     "配电", "逆变器", "微模块", "光伏", "充电桩"],
        "title_re": None,
    },
    {
        "id": "idc", "label": "数据中心", "icon": "🏭",
        "cat_kw": ["数据中心", "idc", "机房", "算力"],
        "title_kw": ["数据中心", "idc", "智算中心", "算力中心", "机房",
                     "东数西算", "服务器", "液冷"],
        "title_re": None,
    },
    {
        "id": "ai", "label": "AI与大模型", "icon": "🤖",
        "cat_kw": ["大模型", "人工智能"],
        "title_kw": ["大模型", "人工智能", "gpt", "deepseek", "智能体", "agent"],
        "title_re": re.compile(r"(?<![a-z])ai(?![a-z])"),
        "cat_re": re.compile(r"(?<![a-z])ai(?![a-z])"),
    },
]

# AI 五板块样式：(标题关键词组, key, 颜色, 图标, 是否高亮加宽)
AI_BLOCK_STYLES: List[Tuple[Tuple[str, ...], str, str, str, bool]] = [
    (("核心热点", "热点态势", "热点"), "core", "#2563eb", "📈", False),
    (("舆论", "争议", "风向", "情绪"), "sentiment", "#f97316", "💬", False),
    (("异动", "弱信号", "信号"), "signals", "#9333ea", "🔎", False),
    (("rss", "洞察", "深度"), "rss", "#0d9488", "📰", False),
    (("研判", "策略", "建议", "行动", "展望"), "strategy", "#16a34a", "🎯", True),
]
AI_BLOCK_DEFAULT = ("other", "#64748b", "📝", False)

WEEKDAYS_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# ═══════════════════════════════════════════════════════════════
#  时间工具
# ═══════════════════════════════════════════════════════════════

def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def today_str() -> str:
    return now_shanghai().strftime("%Y-%m-%d")


def now_display() -> str:
    return now_shanghai().strftime("%Y-%m-%d %H:%M")


def timestamp_label() -> str:
    return now_shanghai().strftime("%Y-%m-%d-%H%M")


def briefing_badge(hour: int) -> str:
    """根据小时返回简报时段徽章。"""
    if 5 <= hour < 11:
        return "早间简报"
    if 11 <= hour < 14:
        return "午间简报"
    return "晚间简报"


def format_hhmm(time_str: Optional[str]) -> str:
    """将数据库时间字段统一格式化为 HH:MM（兼容 'HH-MM' 与完整时间戳）。"""
    if not time_str:
        return "--:--"
    time_str = str(time_str).strip()
    m = re.match(r"^(\d{1,2})-(\d{2})$", time_str)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.match(r"^\d{4}-\d{2}-\d{2}[T\s](\d{1,2}):(\d{2})", time_str)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return time_str


def format_pub_time(pub: str) -> str:
    """RSS published_at → 'MM-DD HH:MM' 紧凑展示。"""
    if not pub:
        return ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T\s](\d{2}):(\d{2})", pub)
    if m:
        return f"{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}"
    return pub[:16]


# ═══════════════════════════════════════════════════════════════
#  频率词配置解析（分类引擎，保持稳定）
# ═══════════════════════════════════════════════════════════════

class WordEntry:
    """单个关键词条目，支持普通子串匹配与 /正则/ 匹配。"""

    def __init__(self, raw: str):
        self.display_name: Optional[str] = None
        self.is_regex = False
        self.pattern: Optional[re.Pattern] = None
        self.word = raw.strip()

        if "=>" in self.word:
            parts = re.split(r"\s*=>\s*", self.word, 1)
            self.word = parts[0].strip()
            if len(parts) > 1 and parts[1].strip():
                self.display_name = parts[1].strip()

        regex_m = re.match(r"^/(.+)/([a-z]*)$", self.word)
        if regex_m:
            pattern_str = regex_m.group(1)
            try:
                self.pattern = re.compile(pattern_str, re.IGNORECASE)
                self.is_regex = True
                self.word = pattern_str
            except re.error:
                self.is_regex = False

    def matches(self, title_lower: str) -> bool:
        if self.is_regex and self.pattern:
            return bool(self.pattern.search(title_lower))
        return self.word.lower() in title_lower


class WordGroup:
    """一组关键词：必须词(+)、普通词、过滤词(!)、组别名、最大条数(@)。"""

    def __init__(self) -> None:
        self.required: List[WordEntry] = []
        self.normal: List[WordEntry] = []
        self.filters: List[WordEntry] = []
        self.alias: Optional[str] = None
        self.max_count = 0

    @property
    def display_name(self) -> str:
        if self.alias:
            return self.alias
        parts = [w.display_name or w.word for w in self.normal + self.required]
        return " / ".join(parts) if parts else "未命名分组"

    def matches(self, title_lower: str) -> bool:
        for f in self.filters:
            if f.matches(title_lower):
                return False
        if self.required and not all(r.matches(title_lower) for r in self.required):
            return False
        if self.normal and not any(n.matches(title_lower) for n in self.normal):
            return False
        return bool(self.required or self.normal)


def parse_frequency_words(file_path: Path) -> Tuple[List[WordGroup], List[str]]:
    """解析 frequency_words.txt → (词组列表, 全局过滤词列表)。"""
    if not file_path.exists():
        print(f"  [警告] 频率词配置文件不存在: {file_path}")
        return [], []

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    paragraphs = re.split(r"\n\s*\n", content)
    groups: List[WordGroup] = []
    global_filters: List[str] = []
    current_section = "WORD_GROUPS"

    for para in paragraphs:
        lines: List[str] = []
        for line in para.split("\n"):
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                lines.append(stripped)
        if not lines:
            continue

        if lines[0].startswith("[") and lines[0].endswith("]"):
            section_name = lines[0][1:-1].strip().upper()
            if section_name in ("GLOBAL_FILTER", "WORD_GROUPS"):
                current_section = section_name
                lines = lines[1:]

        if current_section == "GLOBAL_FILTER":
            for line in lines:
                if line.startswith(("!", "+", "@", "[")):
                    continue
                global_filters.append(line)
            continue

        group = WordGroup()
        if lines and lines[0].startswith("[") and lines[0].endswith("]"):
            potential = lines[0][1:-1].strip()
            if potential.upper() not in ("GLOBAL_FILTER", "WORD_GROUPS"):
                group.alias = potential
                lines = lines[1:]

        for line in lines:
            if line.startswith("@"):
                try:
                    count = int(line[1:])
                    if count > 0:
                        group.max_count = count
                except ValueError:
                    pass
            elif line.startswith("!"):
                group.filters.append(WordEntry(line[1:]))
            elif line.startswith("+"):
                group.required.append(WordEntry(line[1:]))
            else:
                group.normal.append(WordEntry(line))

        if group.required or group.normal:
            groups.append(group)

    return groups, global_filters


def categorize_title(title: str, groups: List[WordGroup],
                     global_filters: List[str]) -> Optional[str]:
    """标题 → 第一个匹配的词组名；全局过滤命中返回 None；未匹配返回 '未分类'。"""
    if not isinstance(title, str) or not title.strip():
        return None
    title_lower = title.lower()
    for gw in global_filters:
        if gw.lower() in title_lower:
            return None
    for group in groups:
        if group.matches(title_lower):
            return group.display_name
    return "未分类"


# ═══════════════════════════════════════════════════════════════
#  数据库读取
# ═══════════════════════════════════════════════════════════════

def load_news_db(db_path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, str], Optional[str]]:
    """
    读取热榜新闻数据库。

    Returns:
        (新闻列表, 平台ID→名称映射, 最新抓取批次时间)
        新闻字段：id/title/platform_id/rank/url/first_crawl_time/last_crawl_time/
                 crawl_count/ranks(list)/rank_timeline(list[(HH:MM, rank)])
    """
    if not db_path.exists():
        print(f"  [警告] 新闻数据库不存在: {db_path}")
        return [], {}, None

    print(f"  读取新闻数据库: {db_path.name}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        platform_map: Dict[str, str] = {}
        try:
            for row in conn.execute("SELECT id, name FROM platforms"):
                platform_map[row["id"]] = row["name"]
        except sqlite3.OperationalError:
            pass

        news_items: List[Dict[str, Any]] = []
        try:
            for row in conn.execute(
                "SELECT id, title, platform_id, rank, url, mobile_url, "
                "first_crawl_time, last_crawl_time, crawl_count "
                "FROM news_items"
            ):
                news_items.append({
                    "id": row["id"],
                    "title": row["title"] or "",
                    "platform_id": row["platform_id"] or "",
                    "rank": row["rank"] if row["rank"] is not None else 999,
                    "url": row["url"] or row["mobile_url"] or "",
                    "first_crawl_time": row["first_crawl_time"],
                    "last_crawl_time": row["last_crawl_time"],
                    "crawl_count": row["crawl_count"] or 1,
                    "ranks": [],
                    "rank_timeline": [],
                })
        except sqlite3.OperationalError as e:
            print(f"  [警告] 读取 news_items 失败: {e}")

        # 排名轨迹（rank_history）
        try:
            timeline: Dict[int, List[Tuple[str, int]]] = {}
            for row in conn.execute(
                "SELECT news_item_id, rank, crawl_time FROM rank_history ORDER BY id"
            ):
                timeline.setdefault(row["news_item_id"], []).append(
                    (row["crawl_time"], row["rank"] if row["rank"] is not None else 0)
                )
            for item in news_items:
                tl = timeline.get(item["id"], [])
                item["rank_timeline"] = [
                    (format_hhmm(t), r) for t, r in tl
                ]
                item["ranks"] = [r for _, r in tl]
        except sqlite3.OperationalError:
            pass

        latest_crawl: Optional[str] = None
        try:
            row = conn.execute(
                "SELECT crawl_time FROM crawl_records ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                latest_crawl = row["crawl_time"]
        except sqlite3.OperationalError:
            pass

        print(f"    共读取 {len(news_items)} 条新闻，{len(platform_map)} 个平台")
        return news_items, platform_map, latest_crawl
    finally:
        conn.close()


def load_rss_db(db_path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """读取 RSS 数据库 → (RSS条目列表, 源ID→名称映射)。"""
    if not db_path.exists():
        print(f"  [提示] RSS 数据库不存在: {db_path}（继续执行）")
        return [], {}

    print(f"  读取 RSS 数据库: {db_path.name}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        feed_map: Dict[str, str] = {}
        try:
            for row in conn.execute("SELECT id, name FROM rss_feeds"):
                feed_map[row["id"]] = row["name"]
        except sqlite3.OperationalError:
            pass

        rss_items: List[Dict[str, Any]] = []
        try:
            for row in conn.execute(
                "SELECT id, title, feed_id, url, published_at, summary, first_crawl_time "
                "FROM rss_items ORDER BY published_at DESC"
            ):
                rss_items.append({
                    "id": row["id"],
                    "title": row["title"] or "",
                    "feed_id": row["feed_id"] or "",
                    "url": row["url"] or "",
                    "published_at": row["published_at"] or "",
                    "summary": row["summary"] or "",
                    "first_crawl_time": row["first_crawl_time"],
                })
        except sqlite3.OperationalError as e:
            print(f"  [警告] 读取 rss_items 失败: {e}")

        print(f"    共读取 {len(rss_items)} 条 RSS 资讯，{len(feed_map)} 个订阅源")
        return rss_items, feed_map
    finally:
        conn.close()


def find_latest_db(data_dir: Path, prefix: str) -> Optional[Path]:
    """目录中最新的 YYYY-MM-DD.db 文件（按文件名降序）。"""
    if not data_dir.exists():
        return None
    dbs = sorted(data_dir.glob(f"{prefix}*.db"), reverse=True)
    return dbs[0] if dbs else None


# ═══════════════════════════════════════════════════════════════
#  AI 分析提取（从 TrendRadar 生成的 index.html）
# ═══════════════════════════════════════════════════════════════

class _AIBlockParser(HTMLParser):
    """
    提取 TrendRadar 报告中的 AI 分析板块。

    目标结构（见 trendradar/ai/formatter.py render_ai_analysis_html_rich）：
        <div class="ai-section">
          <div class="ai-section-header">...<div class="ai-section-title">✨ AI 热点分析</div>
          <div class="ai-blocks-grid">
            <div class="ai-block">
              <div class="ai-block-title">核心热点态势</div>
              <div class="ai-block-content">正文（<br> 换行）</div>
            </div>
            ...
          </div>
        </div>
    失败/跳过时为 <div class="ai-warning"> / <div class="ai-info">。
    """

    def __init__(self) -> None:
        super().__init__()
        self.blocks: List[Dict[str, str]] = []
        self.message: str = ""
        self._stack: List[str] = []          # 角色栈：section/block/title/content/message
        self._title_buf: List[str] = []
        self._content_buf: List[str] = []
        self._message_buf: List[str] = []

    @staticmethod
    def _role(attrs: List[Tuple[str, Optional[str]]]) -> Optional[str]:
        tokens: set = set()
        for name, value in attrs:
            if name == "class" and value:
                tokens = set(value.lower().split())
                break
        # 按 class 词元精确匹配：避免 ai-blocks-grid 误命中 ai-block、
        # ai-section-header/title/badge 误命中 ai-section
        if "ai-block-title" in tokens:
            return "title"
        if "ai-block-content" in tokens:
            return "content"
        if "ai-block" in tokens:
            return "block"
        if tokens & {"ai-warning", "ai-error", "ai-info"}:
            return "message"
        if "ai-section" in tokens:
            return "section"
        return None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag == "div":
            # 每个 div 都入栈（role=None 的装饰元素也入栈），
            # 保证开闭一一配对，角色栈不会因嵌套装饰 div 而错位
            role = self._role(attrs)
            self._stack.append(role)
            if role == "block":
                self._title_buf = []
                self._content_buf = []
            elif role == "message":
                self._message_buf = []
        elif tag == "br" and self._stack and self._stack[-1] == "content":
            self._content_buf.append("\n")

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag == "br" and self._stack and self._stack[-1] == "content":
            self._content_buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag != "div" or not self._stack:
            return
        role = self._stack.pop()
        if role == "block":
            title = "".join(self._title_buf).strip()
            content = "".join(self._content_buf).strip()
            if title:
                self.blocks.append({"title": title, "content": content})
        elif role == "message":
            msg = "".join(self._message_buf).strip()
            if msg and not self.message:
                self.message = msg

    def handle_data(self, data: str) -> None:
        if not self._stack:
            return
        top = self._stack[-1]
        if top == "title":
            self._title_buf.append(data)
        elif top == "content":
            self._content_buf.append(data)
        elif top == "message":
            self._message_buf.append(data)


def extract_ai_blocks(html_path: Path) -> List[Dict[str, str]]:
    """
    从 TrendRadar 生成的 index.html 提取 AI 分析板块。

    Returns:
        [{"title": 板块名, "content": 纯文本正文}, ...]；失败/无内容返回 []。
        若 AI 区域存在但为警告/提示信息，返回 [{"title": "", "content": 提示}]。
    """
    if not html_path.exists():
        print(f"  [提示] 未找到 TrendRadar 报告: {html_path}")
        return []

    try:
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        print(f"  [警告] 读取 index.html 失败: {e}")
        return []

    parser = _AIBlockParser()
    try:
        parser.feed(content)
    except Exception as e:  # 解析器容错
        print(f"  [警告] 解析 index.html 时出错: {e}")
        return []

    if parser.blocks:
        names = "、".join(b["title"] for b in parser.blocks)
        print(f"  已提取 AI 分析 {len(parser.blocks)} 个板块：{names}")
        return parser.blocks

    if parser.message:
        print(f"  AI 分析区域存在但不可用：{parser.message[:60]}")
        return [{"title": "", "content": parser.message}]

    print("  未在 index.html 中找到 AI 分析区域")
    return []


def split_bullets(text: str, max_len: int = 120) -> List[str]:
    """
    将 AI 板块长文本切分为短要点。

    切分规则：
        - 换行符切分
        - 行内序号 "1." "2、" 前断开
        - 中文句读 "。；！？" 后断开（过短片段保留不切）
        - 去掉 "• - * · ○ ▪" 等项目符号与序号前缀
        - 【标签】扁平化为 "标签：" 前缀
    """
    if not text:
        return []
    text = html_lib.unescape(text)

    # 行内序号前换行（排除小数/版本号，如 2.0）
    text = re.sub(r"(?<=[^\d.\n])\s*(\d{1,2})[.、]\s*", r"\n\1. ", text)
    # 句读标点后换行
    text = re.sub(r"([。；！？])\s*", r"\1\n", text)

    bullets: List[str] = []
    seen = set()
    for raw in text.split("\n"):
        s = raw.strip()
        if not s:
            continue
        # 去项目符号（• · - * ▪ 等）
        s = re.sub(r"^[·•▪◦※*\-–—]+\s*", "", s)
        # 去序号前缀
        s = re.sub(r"^\d{1,2}[.、]\s*", "", s)
        # 扁平化 【标签】： → 标签：
        s = re.sub(r"【([^】]+)】\s*[:：]?", r"\1：", s)
        s = s.strip("：: \t")
        # 去掉分句残留的末尾分号/逗号/顿号
        s = s.rstrip("；;，,、 \t")
        if len(s) < 4:
            continue
        if len(s) > max_len:
            s = s[:max_len].rstrip() + "…"
        if s in seen:
            continue
        seen.add(s)
        bullets.append(s)
    return bullets


def style_ai_block(title: str) -> Dict[str, Any]:
    """根据板块标题匹配颜色/图标/是否高亮。"""
    low = title.lower()
    for keywords, key, color, icon, wide in AI_BLOCK_STYLES:
        if any(kw.lower() in low for kw in keywords):
            return {"key": key, "color": color, "icon": icon, "wide": wide}
    key, color, icon, wide = AI_BLOCK_DEFAULT
    return {"key": key, "color": color, "icon": icon, "wide": wide}


# ═══════════════════════════════════════════════════════════════
#  数据处理
# ═══════════════════════════════════════════════════════════════

def calc_news_weight(rank: int, crawl_count: int) -> float:
    """热度权重：排名越靠前越高、在榜越久越高。"""
    rank_score = (50 - min(rank, 50)) * 0.6
    crawl_score = crawl_count * 3 * 0.3
    top_bonus = 10 * 0.1 if rank <= 5 else 0
    return round(rank_score + crawl_score + top_bonus, 2)


def _trend_strings(item: Dict[str, Any]) -> Tuple[str, str]:
    """由排名轨迹生成 (紧凑趋势 '24→18', 悬浮提示 '00:47 #24 · ...')。"""
    tl = item.get("rank_timeline") or []
    ranks = [r for _, r in tl if r and r > 0]
    if not ranks:
        return "", ""
    first, last = ranks[0], ranks[-1]
    compact = f"{first}→{last}" if first != last else f"{first}"
    tip_points = [f"{t} #{r}" for t, r in tl if r and r > 0][-8:]
    tip = " · ".join(tip_points)
    return compact, tip


def process_news(
    news_items: List[Dict[str, Any]],
    platform_map: Dict[str, str],
    groups: List[WordGroup],
    global_filters: List[str],
    latest_crawl: Optional[str],
) -> Dict[str, Any]:
    """分类、排序、统计热榜新闻。"""
    categorized: Dict[str, List[Dict[str, Any]]] = {}
    platform_counts: Dict[str, int] = {}
    all_news: List[Dict[str, Any]] = []
    new_count = 0
    hit_count = 0

    for item in news_items:
        category = categorize_title(item["title"], groups, global_filters)
        if category is None:
            continue

        platform_name = platform_map.get(item["platform_id"], item["platform_id"])
        weight = calc_news_weight(item["rank"], item["crawl_count"])
        trend, trend_tip = _trend_strings(item)

        is_new = item["crawl_count"] == 1
        if latest_crawl and item.get("first_crawl_time") == latest_crawl:
            is_new = True
        if is_new:
            new_count += 1

        processed = {
            "id": item["id"],
            "title": item["title"],
            "platform_id": item["platform_id"],
            "platform_name": platform_name,
            "rank": item["rank"],
            "url": item["url"],
            "crawl_count": item["crawl_count"],
            "time_display": format_hhmm(item["last_crawl_time"]),
            "first_display": format_hhmm(item["first_crawl_time"]),
            "is_new": is_new,
            "weight": weight,
            "category": category,
            "trend": trend,
            "trend_tip": trend_tip,
        }
        all_news.append(processed)
        categorized.setdefault(category, []).append(processed)
        platform_counts[platform_name] = platform_counts.get(platform_name, 0) + 1
        if category != "未分类":
            hit_count += 1

    for cat in categorized:
        categorized[cat].sort(key=lambda x: x["weight"], reverse=True)

    sorted_categories = sorted(categorized.items(), key=lambda x: len(x[1]), reverse=True)
    sorted_platforms = sorted(platform_counts.items(), key=lambda x: x[1], reverse=True)

    return {
        "all_news": all_news,
        "categorized": dict(sorted_categories),
        "platform_counts": platform_counts,
        "sorted_platforms": sorted_platforms,
        "total": len(all_news),
        "hit_count": hit_count,
        "new_count": new_count,
    }


def process_rss(rss_items: List[Dict[str, Any]],
                feed_map: Dict[str, str]) -> List[Dict[str, Any]]:
    """RSS 按订阅源分组 → [{name, items:[...]}]（按条数降序）。"""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in rss_items:
        feed_name = feed_map.get(item["feed_id"], item["feed_id"]) or "未知来源"
        summary = re.sub(r"<[^>]+>", "", item.get("summary", "") or "").strip()
        if len(summary) > 100:
            summary = summary[:100] + "…"
        grouped.setdefault(feed_name, []).append({
            # 压缩键名，与前端 newsRowHTML 的 RSS 分支对齐
            "t": item["title"],
            "u": item["url"],
            "f": feed_name,
            "pub": format_pub_time(item.get("published_at", "")),
            "summary": summary,
        })

    groups_out = [
        {"name": name, "items": items}
        for name, items in sorted(grouped.items(), key=lambda x: len(x[1]), reverse=True)
    ]
    return groups_out


# ═══════════════════════════════════════════════════════════════
#  销售视图构建
# ═══════════════════════════════════════════════════════════════

def _cat_matches(cat_name: str, kws: List[str], regex: Optional[re.Pattern]) -> bool:
    low = cat_name.lower()
    if any(kw.lower() in low for kw in kws):
        return True
    if regex and regex.search(low):
        return True
    return False


def _title_matches(title_lower: str, kws: List[str],
                   regex: Optional[re.Pattern]) -> bool:
    if any(kw.lower() in title_lower for kw in kws):
        return True
    if regex and regex.search(title_lower):
        return True
    return False


def _compact_news_item(it: Dict[str, Any]) -> Dict[str, Any]:
    """压缩新闻条目为前端 JSON 字段。"""
    return {
        "i": it["id"],
        "t": it["title"],
        "p": it["platform_name"],
        "r": it["rank"],
        "u": it["url"],
        "l": it["time_display"],
        "c": it["crawl_count"],
        "n": it["is_new"],
        "w": it["weight"],
        "tr": it["trend"],
        "tt": it["trend_tip"],
    }


def build_sales_views(news_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    按销售优先级构建导航视图。

    顺序：商机信号 → 科华动态 → 股份制银行 → UPS与电源 → 数据中心 →
          AI与大模型 → 友商动态(6 个子品牌) → （行业资讯/总榜由外部追加）→
          其他有新闻的分类
    """
    categorized: Dict[str, List[Dict[str, Any]]] = news_data["categorized"]
    all_news: List[Dict[str, Any]] = news_data["all_news"]

    # 初始化固定视图
    views: Dict[str, Dict[str, Any]] = {}
    for v in FIXED_VIEWS:
        views[v["id"]] = {
            "id": v["id"], "label": v["label"], "icon": v["icon"],
            "type": "news", "items": [],
        }
    comp_views: Dict[str, Dict[str, Any]] = {}
    for cid, label, _kws in COMPETITORS:
        comp_views[cid] = {"id": cid, "label": label, "icon": "🏷️",
                           "type": "news", "items": []}

    claimed_cats: set = set()

    # 1) 按分类名认领
    for cat, items in categorized.items():
        cat_low = cat.lower()
        matched = False
        for v in FIXED_VIEWS:
            if _cat_matches(cat_low, v["cat_kw"], v.get("cat_re")):
                views[v["id"]]["items"].extend(items)
                claimed_cats.add(cat)
                matched = True
                break
        if matched:
            continue
        for cid, label, kws in COMPETITORS:
            if label in cat or any(kw in cat_low for kw in kws):
                comp_views[cid]["items"].extend(items)
                claimed_cats.add(cat)
                break

    # 2) 按标题关键词补充抓取（去重）
    def add_if_absent(bucket: List[Dict[str, Any]], item: Dict[str, Any],
                      seen: set) -> None:
        if item["id"] not in seen:
            seen.add(item["id"])
            bucket.append(item)

    for v in FIXED_VIEWS:
        seen = {it["id"] for it in views[v["id"]]["items"]}
        for it in all_news:
            if _title_matches(it["title"].lower(), v["title_kw"], v.get("title_re")):
                add_if_absent(views[v["id"]]["items"], it, seen)

    for cid, label, kws in COMPETITORS:
        seen = {it["id"] for it in comp_views[cid]["items"]}
        for it in all_news:
            if _title_matches(it["title"].lower(), kws, None):
                add_if_absent(comp_views[cid]["items"], it, seen)

    # 3) 视图内排序
    for v in views.values():
        v["items"].sort(key=lambda x: x["weight"], reverse=True)
    for v in comp_views.values():
        v["items"].sort(key=lambda x: x["weight"], reverse=True)

    # 4) 商机信号 = 科华/银行/UPS/IDC + 友商 的并集
    signal_ids: set = set()
    signal_items: List[Dict[str, Any]] = []
    for vid in ("kehua", "banks", "ups", "idc"):
        for it in views[vid]["items"]:
            if it["id"] not in signal_ids:
                signal_ids.add(it["id"])
                signal_items.append(it)
    for v in comp_views.values():
        for it in v["items"]:
            if it["id"] not in signal_ids:
                signal_ids.add(it["id"])
                signal_items.append(it)
    signal_items.sort(key=lambda x: x["weight"], reverse=True)

    # 5) 按优先级组装（空视图不输出）
    ordered: List[Dict[str, Any]] = []
    if signal_items:
        ordered.append({
            "id": "signals", "label": "商机信号", "icon": "🔴",
            "type": "news",
            "items": [_compact_news_item(it) for it in signal_items],
        })
    for v in FIXED_VIEWS:
        bucket = views[v["id"]]
        if bucket["items"]:
            ordered.append({
                "id": bucket["id"], "label": bucket["label"],
                "icon": bucket["icon"], "type": "news",
                "items": [_compact_news_item(it) for it in bucket["items"]],
            })

    comp_children = [
        {
            "id": cid, "label": label, "icon": "🏷️", "type": "news",
            "items": [_compact_news_item(it) for it in comp_views[cid]["items"]],
        }
        for cid, label, _kws in COMPETITORS if comp_views[cid]["items"]
    ]
    if comp_children:
        total_comp = sum(len(c["items"]) for c in comp_children)
        ordered.append({
            "id": "competitors", "label": "友商动态", "icon": "🔧",
            "type": "group", "count": total_comp, "children": comp_children,
        })

    # 6) 其他未被认领的分类（按条数降序）
    other_idx = 0
    for cat, items in categorized.items():
        if cat in claimed_cats:
            continue
        other_idx += 1
        ordered.append({
            "id": f"other-{other_idx}", "label": cat, "icon": "📂",
            "type": "news",
            "items": [_compact_news_item(it) for it in items],
        })

    return ordered


def build_hotlist(news_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """总榜：按平台分组，组内按排名升序。"""
    platform_news: Dict[str, List[Dict[str, Any]]] = {}
    for item in news_data["all_news"]:
        platform_news.setdefault(item["platform_name"], []).append(item)

    result = []
    for pname in sorted(platform_news.keys(),
                        key=lambda p: len(platform_news[p]), reverse=True):
        items = sorted(platform_news[pname], key=lambda x: x["rank"])
        result.append({
            "n": pname,
            "items": [_compact_news_item(it) for it in items],
        })
    return result


# ═══════════════════════════════════════════════════════════════
#  历史报告
# ═══════════════════════════════════════════════════════════════

def _scan_dir(d: Path) -> Dict[str, Dict[str, str]]:
    """扫描目录下 YYYY-MM-DD-HHMM.html → {filename: {file, label}}。"""
    found: Dict[str, Dict[str, str]] = {}
    if not d.exists():
        return found
    for f in d.glob("*.html"):
        m = re.match(r"(\d{4}-\d{2}-\d{2})-(\d{2})(\d{2})", f.stem)
        label = f"{m.group(1)} {m.group(2)}:{m.group(3)}" if m else f.stem
        found[f.name] = {"file": f.name, "label": label}
    return found


def scan_history_reports() -> List[Dict[str, str]]:
    """扫描 _site/reports/ 与 reports/ 两个目录，合并去重（按文件名降序）。"""
    merged: Dict[str, Dict[str, str]] = {}
    # git 持久化目录优先（历史更全），站点目录补充
    merged.update(_scan_dir(REPORTS_GIT_DIR))
    merged.update(_scan_dir(REPORTS_SITE_DIR))
    reports = sorted(merged.values(), key=lambda r: r["file"], reverse=True)
    return reports[:MAX_HISTORY_REPORTS]


def cleanup_old_reports() -> None:
    """两个归档目录各自仅保留最新 MAX_HISTORY_REPORTS 份。"""
    for d in (REPORTS_SITE_DIR, REPORTS_GIT_DIR):
        if not d.exists():
            continue
        files = sorted(d.glob("*.html"), reverse=True)
        for old in files[MAX_HISTORY_REPORTS:]:
            try:
                old.unlink()
                print(f"  已清理旧报告: {d.name}/{old.name}")
            except OSError:
                pass


# ═══════════════════════════════════════════════════════════════
#  数据载荷
# ═══════════════════════════════════════════════════════════════

def build_payload(
    news_data: Dict[str, Any],
    rss_groups: List[Dict[str, Any]],
    ai_blocks: List[Dict[str, str]],
    history: List[Dict[str, str]],
    target_date: str,
    data_date: Optional[str],
) -> Dict[str, Any]:
    now = now_shanghai()

    # AI 卡片
    ai_cards: List[Dict[str, Any]] = []
    ai_found = False
    ai_message = ""
    for blk in ai_blocks:
        title, content = blk.get("title", ""), blk.get("content", "")
        if not title:
            ai_message = content or "AI 分析暂不可用"
            continue
        ai_found = True
        style = style_ai_block(title)
        bullets = split_bullets(content)
        if not bullets:
            continue
        ai_cards.append({
            "key": style["key"],
            "title": title,
            "icon": style["icon"],
            "color": style["color"],
            "wide": style["wide"],
            "bullets": bullets,
        })

    # 图表数据
    cat_items = list(news_data["categorized"].items())
    pie = [{"name": c, "value": len(its)} for c, its in cat_items]
    if len(pie) > 8:
        top = pie[:8]
        top.append({"name": "其他", "value": sum(d["value"] for d in pie[8:])})
        pie = top
    bar = [{"name": n, "value": v} for n, v in news_data["sorted_platforms"][:8]]

    # 销售视图
    sales_views = build_sales_views(news_data)

    # RSS 视图
    rss_view = None
    rss_total = sum(len(g["items"]) for g in rss_groups)
    if rss_groups:
        rss_view = {"id": "rss", "label": "行业资讯", "icon": "📰",
                    "type": "rss", "groups": rss_groups, "count": rss_total}

    # 总榜视图
    hotlist = build_hotlist(news_data)
    hotlist_view = None
    if hotlist:
        hotlist_view = {"id": "hotlist", "label": "总榜", "icon": "📋",
                        "type": "hotlist", "platforms": hotlist,
                        "count": news_data["total"]}

    # 导航顺序：销售视图（信号/科华/.../友商/其他分类）→ 行业资讯 → 总榜
    nav_views = list(sales_views)
    if rss_view:
        nav_views.append(rss_view)
    if hotlist_view:
        nav_views.append(hotlist_view)

    date_obj = now
    try:
        date_obj = datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        pass

    payload = {
        "meta": {
            "dateBig": f"{date_obj.year}年{date_obj.month}月{date_obj.day}日",
            "weekday": WEEKDAYS_CN[date_obj.weekday()],
            "badge": briefing_badge(now.hour),
            "genTime": now_display(),
            "dataDate": data_date or target_date,
            "stale": bool(data_date and data_date != target_date),
        },
        "stats": {
            "news": news_data["total"],
            "rss": rss_total,
            "hit": news_data["hit_count"],
            "new": news_data["new_count"],
        },
        "ai": {
            "found": ai_found,
            "message": ai_message,
            "cards": ai_cards,
        },
        "charts": {"pie": pie, "bar": bar, "palette": CATEGORY_PALETTE},
        "views": nav_views,
        "history": history,
    }
    return payload


# ═══════════════════════════════════════════════════════════════
#  HTML 模板
# ═══════════════════════════════════════════════════════════════

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TrendRadar 销售情报简报</title>
<script src="__ECHARTS_CDN__"></script>
<style>
/* ───────────────────────────── 主题变量 ───────────────────────────── */
:root {
  --sidebar-bg: #0a1628;
  --sidebar-bg2: #0d1d36;
  --sidebar-text: #b8c4d9;
  --sidebar-text-dim: #6b7a94;
  --sidebar-active: #2563eb;
  --bg: #f1f4f9;
  --card-bg: #ffffff;
  --text: #16233b;
  --text-2: #55617a;
  --text-3: #8b96ab;
  --border: #e4e9f2;
  --hover: #f4f7fc;
  --accent: #2563eb;
  --accent-soft: #e8f0fe;
  --shadow: 0 1px 3px rgba(16,42,82,.06), 0 4px 16px rgba(16,42,82,.05);
  --gold: #f5b53d; --silver: #a8b3c5; --bronze: #d08a3e;
  --danger: #dc2626;
}
[data-theme="dark"] {
  --sidebar-bg: #060d1a;
  --sidebar-bg2: #0a1628;
  --bg: #0a1120;
  --card-bg: #111d33;
  --text: #e2e9f5;
  --text-2: #9fadc6;
  --text-3: #66758f;
  --border: #1e2c46;
  --hover: #16233d;
  --accent: #4d8dff;
  --accent-soft: #15233f;
  --shadow: 0 1px 3px rgba(0,0,0,.4);
}
* { margin:0; padding:0; box-sizing:border-box; }
html { scroll-behavior:smooth; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
               "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
  background: var(--bg); color: var(--text);
  line-height: 1.55; transition: background .25s, color .25s;
}
a { text-decoration:none; color:inherit; }
button { font-family:inherit; cursor:pointer; border:none; background:none; color:inherit; }
::-webkit-scrollbar { width:8px; height:8px; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius:4px; }

/* ───────────────────────────── 侧边栏 ───────────────────────────── */
.sidebar {
  position:fixed; top:0; left:0; bottom:0; width:220px; z-index:50;
  background: linear-gradient(180deg, var(--sidebar-bg) 0%, var(--sidebar-bg2) 100%);
  color: var(--sidebar-text);
  display:flex; flex-direction:column;
  border-right:1px solid rgba(255,255,255,.05);
}
.sidebar-brand {
  padding:18px 16px 14px; display:flex; align-items:center; gap:10px;
  border-bottom:1px solid rgba(255,255,255,.06);
}
.brand-mark {
  width:34px; height:34px; border-radius:9px; flex:none;
  background: linear-gradient(135deg,#2563eb,#1e40af);
  display:flex; align-items:center; justify-content:center;
  font-weight:800; font-size:16px; color:#fff;
}
.brand-name { font-size:14px; font-weight:700; color:#fff; line-height:1.3; }
.brand-sub { font-size:11px; color:var(--sidebar-text-dim); margin-top:1px; }
.sidebar-nav { flex:1; overflow-y:auto; padding:10px 8px; }
.nav-label {
  font-size:10.5px; color:var(--sidebar-text-dim); letter-spacing:1px;
  padding:10px 10px 4px; font-weight:600;
}
.nav-item {
  display:flex; align-items:center; gap:9px; width:100%;
  padding:8px 10px; border-radius:8px; font-size:13.5px; color:var(--sidebar-text);
  transition: background .15s, color .15s; white-space:nowrap; text-align:left;
}
.nav-item:hover { background:rgba(255,255,255,.07); color:#fff; }
.nav-item.active { background:var(--sidebar-active); color:#fff; font-weight:600; }
.nav-item .ico { width:20px; text-align:center; flex:none; font-size:14px; }
.nav-item .txt { flex:1; overflow:hidden; text-overflow:ellipsis; }
.nav-item .cnt {
  font-size:11px; padding:1px 7px; border-radius:10px; flex:none;
  background:rgba(255,255,255,.12); color:inherit; font-weight:600;
}
.nav-item.active .cnt { background:rgba(255,255,255,.25); }
.nav-item.child { padding-left:34px; font-size:13px; }
.nav-item.child .ico { font-size:11px; width:14px; }
.sidebar-foot { padding:10px 12px 14px; border-top:1px solid rgba(255,255,255,.06); }
.foot-select {
  width:100%; padding:7px 8px; border-radius:8px; font-size:12px;
  background:rgba(255,255,255,.08); color:var(--sidebar-text);
  border:1px solid rgba(255,255,255,.1); outline:none; margin-bottom:8px;
}
.foot-select option { color:#1a2438; background:#fff; }
.foot-row { display:flex; gap:8px; }
.foot-btn {
  flex:1; padding:7px 0; border-radius:8px; font-size:12px;
  background:rgba(255,255,255,.08); color:var(--sidebar-text);
  border:1px solid rgba(255,255,255,.1); display:flex; align-items:center;
  justify-content:center; gap:5px; transition:background .15s;
}
.foot-btn:hover { background:rgba(255,255,255,.16); color:#fff; }
.foot-time { font-size:10.5px; color:var(--sidebar-text-dim); margin-top:8px; text-align:center; }

/* ───────────────────────────── 移动端顶栏 ───────────────────────────── */
.mobile-bar {
  display:none; position:sticky; top:0; z-index:60;
  background:var(--sidebar-bg); color:#fff;
  padding:8px 10px; align-items:center; gap:8px;
}
.mobile-tabs { display:flex; gap:6px; overflow-x:auto; flex:1; scrollbar-width:none; }
.mobile-tabs::-webkit-scrollbar { display:none; }
.mtab {
  flex:none; padding:6px 12px; border-radius:16px; font-size:12.5px;
  background:rgba(255,255,255,.1); color:var(--sidebar-text); white-space:nowrap;
}
.mtab.active { background:var(--sidebar-active); color:#fff; font-weight:600; }
.mtab .cnt { font-size:10.5px; opacity:.85; margin-left:3px; }
.micon-btn {
  flex:none; width:32px; height:32px; border-radius:8px; font-size:14px;
  background:rgba(255,255,255,.1); color:#fff; display:flex; align-items:center; justify-content:center;
}

/* ───────────────────────────── 主内容 ───────────────────────────── */
.content { margin-left:220px; padding:26px 30px 60px; max-width:1280px; }
.view { display:none; animation:fade .25s ease; }
.view.active { display:block; }
@keyframes fade { from{opacity:0; transform:translateY(6px);} to{opacity:1; transform:none;} }

/* 简报头 */
.brief-head { margin-bottom:18px; }
.brief-date { font-size:30px; font-weight:800; letter-spacing:.5px; display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; }
.brief-week { font-size:17px; font-weight:600; color:var(--text-2); }
.brief-badge {
  font-size:12.5px; font-weight:700; color:#fff; padding:4px 13px;
  border-radius:14px; background:linear-gradient(135deg,#2563eb,#1d4ed8);
  vertical-align:middle;
}
.brief-stale { font-size:12.5px; color:#b45309; background:#fef3c7; border:1px solid #fde68a; padding:5px 12px; border-radius:8px; margin-top:10px; display:inline-block; }
[data-theme="dark"] .brief-stale { color:#fbbf24; background:#3a2d12; border-color:#5a4718; }

/* 统计卡 */
.stat-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin:18px 0 22px; }
.stat-card {
  background:var(--card-bg); border:1px solid var(--border); border-radius:12px;
  padding:14px 16px; display:flex; align-items:center; gap:12px; box-shadow:var(--shadow);
}
.stat-ico { width:38px; height:38px; border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:18px; flex:none; }
.stat-val { font-size:22px; font-weight:800; line-height:1.1; }
.stat-lab { font-size:12px; color:var(--text-3); margin-top:2px; }

/* AI 卡片 */
.ai-grid { display:grid; grid-template-columns:repeat(2,1fr); gap:14px; margin-bottom:22px; }
.ai-card {
  background:var(--card-bg); border:1px solid var(--border); border-left:4px solid var(--cc);
  border-radius:12px; padding:15px 17px; box-shadow:var(--shadow); position:relative;
}
.ai-card.wide { grid-column:span 2; background:linear-gradient(180deg, color-mix(in srgb, var(--cc) 7%, var(--card-bg)), var(--card-bg)); }
.ai-card-head { display:flex; align-items:center; gap:9px; margin-bottom:10px; }
.ai-card-ico {
  width:30px; height:30px; border-radius:8px; flex:none; font-size:15px;
  display:flex; align-items:center; justify-content:center;
  background:color-mix(in srgb, var(--cc) 14%, transparent);
}
.ai-card-title { font-size:15px; font-weight:700; color:var(--cc); }
.ai-card-tag { margin-left:auto; font-size:10.5px; font-weight:700; color:var(--cc);
  background:color-mix(in srgb, var(--cc) 12%, transparent); padding:2px 8px; border-radius:10px; }
.ai-bullets { list-style:none; }
.ai-bullets li {
  position:relative; padding:4px 0 4px 16px; font-size:13px; color:var(--text-2);
  line-height:1.65;
}
.ai-bullets li::before {
  content:""; position:absolute; left:2px; top:12px; width:6px; height:6px;
  border-radius:50%; background:var(--cc);
}
.ai-bullets li.hidden-bullet { display:none; }
.ai-expand {
  margin-top:8px; font-size:12.5px; font-weight:600; color:var(--cc);
  padding:4px 0; display:inline-flex; align-items:center; gap:4px;
}
.ai-placeholder {
  grid-column:span 2; background:var(--card-bg); border:1px dashed var(--border);
  border-radius:12px; padding:34px; text-align:center; color:var(--text-3);
}
.ai-placeholder .ph-ico { font-size:34px; opacity:.5; margin-bottom:10px; }
.ai-placeholder .ph-msg { font-size:13px; margin-top:6px; color:var(--text-3); }

/* 图表 */
.chart-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:8px; }
.chart-card { background:var(--card-bg); border:1px solid var(--border); border-radius:12px; padding:14px 16px; box-shadow:var(--shadow); }
.chart-title { font-size:13.5px; font-weight:700; color:var(--text-2); margin-bottom:6px; }
.chart-box { height:250px; width:100%; }

/* 视图标题 */
.view-head { display:flex; align-items:center; gap:10px; margin:4px 0 14px; }
.view-title { font-size:21px; font-weight:800; }
.view-count { font-size:12.5px; color:var(--accent); background:var(--accent-soft); padding:3px 11px; border-radius:12px; font-weight:700; }
.view-sub { font-size:12.5px; color:var(--text-3); margin-left:auto; }

/* 搜索框 */
.list-toolbar {
  position:sticky; top:10px; z-index:20; margin-bottom:12px;
  display:flex; gap:8px; align-items:center;
}
.search-box {
  flex:1; display:flex; align-items:center; gap:8px;
  background:var(--card-bg); border:1px solid var(--border); border-radius:10px;
  padding:8px 13px; box-shadow:var(--shadow);
}
.search-box input {
  flex:1; border:none; outline:none; background:transparent;
  font-size:13.5px; color:var(--text); font-family:inherit;
}
.search-box .s-ico { color:var(--text-3); font-size:14px; }
.search-clear { font-size:12px; color:var(--text-3); padding:2px 6px; border-radius:6px; }
.search-clear:hover { background:var(--hover); color:var(--text); }

/* 新闻行（收件箱式） */
.news-list { background:var(--card-bg); border:1px solid var(--border); border-radius:12px; overflow:hidden; box-shadow:var(--shadow); }
.news-row {
  display:flex; align-items:center; gap:12px; padding:9px 14px;
  border-bottom:1px solid var(--border); transition:background .12s;
}
.news-row:last-child { border-bottom:none; }
.news-row:hover { background:var(--hover); }
.rank-badge {
  width:26px; height:26px; border-radius:50%; flex:none;
  display:flex; align-items:center; justify-content:center;
  font-size:12px; font-weight:700; background:var(--accent-soft); color:var(--text-3);
}
.rank-badge.r1 { background:linear-gradient(135deg,#f7c948,#f59e0b); color:#fff; }
.rank-badge.r2 { background:linear-gradient(135deg,#c3ccd9,#94a3b8); color:#fff; }
.rank-badge.r3 { background:linear-gradient(135deg,#e0a063,#c07a2e); color:#fff; }
.rank-badge.dot { background:transparent; color:var(--accent); font-size:16px; }
.row-main { flex:1; min-width:0; }
.row-title {
  display:block; font-size:14px; color:var(--text); font-weight:500;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
a.row-title:hover { color:var(--accent); }
.row-meta { display:flex; align-items:center; gap:8px; margin-top:2px; font-size:11.5px; color:var(--text-3); }
.src-tag {
  padding:1px 8px; border-radius:9px; font-size:11px; font-weight:600;
  background:var(--accent-soft); color:var(--accent); flex:none;
}
.row-trend { cursor:help; }
.row-summary { font-size:12px; color:var(--text-3); margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.row-side { flex:none; display:flex; align-items:center; gap:10px; font-size:11.5px; color:var(--text-3); }
.badge-new {
  background:#fee2e2; color:#dc2626; font-size:10px; font-weight:800;
  padding:1px 6px; border-radius:6px; letter-spacing:.5px;
}
[data-theme="dark"] .badge-new { background:#3d1a1a; color:#f87171; }
.row-crawl { white-space:nowrap; }

/* RSS 分组 */
.rss-group { margin-bottom:18px; }
.rss-group-head {
  display:flex; align-items:center; gap:9px; margin:6px 2px 8px;
  font-size:14px; font-weight:700; color:var(--text-2);
}
.rss-group-head .g-bar { width:4px; height:15px; border-radius:2px; background:var(--accent); }
.rss-group-head .g-cnt { font-size:11.5px; color:var(--text-3); font-weight:500; }

/* 总榜平台 tabs */
.plat-tabs { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:12px; }
.plat-tab {
  padding:6px 14px; border-radius:16px; font-size:13px; font-weight:600;
  background:var(--card-bg); border:1px solid var(--border); color:var(--text-2);
  transition:all .15s;
}
.plat-tab .cnt { font-size:11px; color:var(--text-3); margin-left:4px; }
.plat-tab.active { background:var(--accent); border-color:var(--accent); color:#fff; }
.plat-tab.active .cnt { color:rgba(255,255,255,.8); }

.more-btn {
  width:100%; padding:11px; font-size:13px; font-weight:600; color:var(--accent);
  background:var(--card-bg); border:1px dashed var(--border); border-top:none;
  border-radius:0 0 12px 12px;
}
.more-btn:hover { background:var(--hover); }
.empty-hint {
  padding:40px; text-align:center; color:var(--text-3); font-size:13.5px;
  background:var(--card-bg); border:1px dashed var(--border); border-radius:12px;
}

/* 回到顶部 */
.back-top {
  position:fixed; right:26px; bottom:26px; width:42px; height:42px; border-radius:50%;
  background:var(--accent); color:#fff; display:flex; align-items:center; justify-content:center;
  box-shadow:0 4px 14px rgba(37,99,235,.4); opacity:0; pointer-events:none;
  transition:opacity .2s, transform .2s; z-index:70;
}
.back-top.show { opacity:1; pointer-events:auto; }
.back-top:hover { transform:translateY(-2px); }

/* ───────────────────────────── 响应式 ───────────────────────────── */
@media (max-width: 920px) {
  .sidebar { display:none; }
  .mobile-bar { display:flex; }
  .content { margin-left:0; padding:16px 14px 50px; }
  .brief-date { font-size:23px; }
  .stat-grid { grid-template-columns:repeat(2,1fr); gap:10px; }
  .ai-grid { grid-template-columns:1fr; }
  .ai-card.wide { grid-column:span 1; }
  .ai-placeholder { grid-column:span 1; }
  .chart-grid { grid-template-columns:1fr; }
  .row-side { gap:7px; }
  .row-crawl { display:none; }
  .view-sub { display:none; }
}
</style>
</head>
<body>

<!-- 侧边栏（桌面） -->
<aside class="sidebar">
  <div class="sidebar-brand">
    <div class="brand-mark">TR</div>
    <div>
      <div class="brand-name">销售情报简报</div>
      <div class="brand-sub">TrendRadar · 科华UPS</div>
    </div>
  </div>
  <nav class="sidebar-nav" id="sidebarNav"></nav>
  <div class="sidebar-foot">
    <select class="foot-select" id="historySelect" aria-label="历史报告">
      <option value="">📁 历史报告</option>
    </select>
    <div class="foot-row">
      <button class="foot-btn" id="themeBtnSide" onclick="App.toggleTheme()">🌓 明暗</button>
      <button class="foot-btn" onclick="App.goHome()">🏠 最新</button>
    </div>
    <div class="foot-time" id="footTime"></div>
  </div>
</aside>

<!-- 移动端顶栏 -->
<div class="mobile-bar">
  <div class="brand-mark" style="width:30px;height:30px;font-size:13px;">TR</div>
  <div class="mobile-tabs" id="mobileTabs"></div>
  <button class="micon-btn" onclick="App.toggleTheme()" aria-label="明暗主题">🌓</button>
</div>

<!-- 主内容 -->
<main class="content" id="content"></main>

<button class="back-top" id="backTop" onclick="window.scrollTo({top:0,behavior:'smooth'})" aria-label="回到顶部">
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg>
</button>

<script>window.DASH_DATA = __DATA__;</script>
<script>
(function(){
  'use strict';
  var D = window.DASH_DATA || {};
  var state = { view:'briefing', charts:{}, inited:false };

  function esc(s){
    return String(s==null?'':s).replace(/[&<>"']/g, function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }

  /* ─────────────── 主题 ─────────────── */
  function getTheme(){ return localStorage.getItem('tr-theme') || 'light'; }
  function setTheme(t){
    document.documentElement.setAttribute('data-theme', t);
    localStorage.setItem('tr-theme', t);
    setTimeout(renderCharts, 60);
  }
  function toggleTheme(){ setTheme(getTheme()==='dark' ? 'light' : 'dark'); }

  /* ─────────────── 历史报告 ─────────────── */
  function reportHref(file){
    var p = location.pathname.replace(/\\/g,'/');
    if (/\/reports\//.test(p)) return file;       // 归档页：同目录
    return 'reports/' + file;                     // 主页：reports/ 子目录
  }
  function fillHistory(){
    var sel = document.getElementById('historySelect');
    (D.history||[]).forEach(function(h){
      var o = document.createElement('option');
      o.value = reportHref(h.file); o.textContent = h.label;
      sel.appendChild(o);
    });
    sel.onchange = function(){ if(sel.value) location.href = sel.value; };
  }
  function goHome(){
    var p = location.pathname.replace(/\\/g,'/');
    location.href = /\/reports\//.test(p) ? '../index.html' : (location.pathname + location.search);
  }

  /* ─────────────── 导航 ─────────────── */
  function navEntries(){
    var list = [{id:'briefing', label:'今日简报', icon:'📊', count:null}];
    (D.views||[]).forEach(function(v){
      list.push({id:v.id, label:v.label, icon:v.icon, count:viewCount(v), child:false});
      if(v.type==='group'){
        (v.children||[]).forEach(function(c){
          list.push({id:c.id, label:c.label, icon:'·', count:viewCount(c), child:true, parent:v.id});
        });
      }
    });
    return list;
  }
  function viewCount(v){
    if(v.type==='rss') return (v.count!=null?v.count:(v.groups||[]).length);
    if(v.type==='group') return v.count||0;
    if(v.type==='hotlist') return v.count||0;
    return (v.items||[]).length;
  }
  function findView(id){
    if(id==='briefing') return null;
    var views = D.views||[];
    for(var i=0;i<views.length;i++){
      if(views[i].id===id) return views[i];
      if(views[i].type==='group'){
        var ch = views[i].children||[];
        for(var j=0;j<ch.length;j++) if(ch[j].id===id) return ch[j];
      }
    }
    return null;
  }

  function buildNav(){
    var sb = document.getElementById('sidebarNav');
    var mb = document.getElementById('mobileTabs');
    var entries = navEntries();
    var sbHtml = '', mbHtml = '';
    entries.forEach(function(e){
      var cnt = e.count==null ? '' : '<span class="cnt">'+e.count+'</span>';
      sbHtml += '<button class="nav-item'+(e.child?' child':'')+'" data-view="'+e.id+'">'
        + '<span class="ico">'+e.icon+'</span><span class="txt">'+esc(e.label)+'</span>'+cnt+'</button>';
      mbHtml += '<button class="mtab" data-view="'+e.id+'">'+e.icon+' '+esc(e.label)
        + (e.count==null?'':'<span class="cnt">'+e.count+'</span>')+'</button>';
    });
    sb.innerHTML = sbHtml; mb.innerHTML = mbHtml;
    var all = document.querySelectorAll('[data-view]');
    all.forEach(function(btn){
      btn.addEventListener('click', function(){ switchView(btn.getAttribute('data-view')); });
    });
  }

  function markActive(id){
    document.querySelectorAll('.nav-item,.mtab').forEach(function(b){
      b.classList.toggle('active', b.getAttribute('data-view')===id);
    });
  }

  function switchView(id){
    state.view = id;
    markActive(id);
    var content = document.getElementById('content');
    content.innerHTML = '';
    window.scrollTo({top:0});
    if(id==='briefing'){
      content.appendChild(renderBriefing());
      setTimeout(renderCharts, 80);
    } else {
      var v = findView(id);
      content.appendChild(renderListView(v));
    }
  }

  /* ─────────────── 简报视图 ─────────────── */
  function statCard(ico, color, val, label){
    return '<div class="stat-card"><div class="stat-ico" style="background:'+color+'18;color:'+color+';">'
      + ico+'</div><div><div class="stat-val">'+val+'</div><div class="stat-lab">'+label+'</div></div></div>';
  }

  function aiCardHTML(card){
    var dots = (card.bullets||[]).map(function(b,i){
      return '<li class="'+(i>=5?'hidden-bullet':'')+'">'+esc(b)+'</li>';
    }).join('');
    var expand = (card.bullets||[]).length>5
      ? '<button class="ai-expand" onclick="App.toggleBullets(this)">展开更多 ▾</button>' : '';
    return '<div class="ai-card'+(card.wide?' wide':'')+'" style="--cc:'+card.color+';">'
      + '<div class="ai-card-head"><div class="ai-card-ico">'+card.icon+'</div>'
      + '<div class="ai-card-title">'+esc(card.title)+'</div>'
      + (card.wide?'<span class="ai-card-tag">重点</span>':'') + '</div>'
      + '<ul class="ai-bullets">'+dots+'</ul>'+expand+'</div>';
  }

  function renderBriefing(){
    var m = D.meta||{}, s = D.stats||{}, ai = D.ai||{};
    var el = document.createElement('div');
    el.className = 'view active';
    var html = '';

    html += '<div class="brief-head"><div class="brief-date">'
      + '<span>'+esc(m.dateBig)+'</span><span class="brief-week">'+esc(m.weekday)+'</span>'
      + '<span class="brief-badge">'+esc(m.badge)+'</span></div>';
    if(m.stale){
      html += '<div class="brief-stale">⚠ 今日数据库尚未生成，当前展示 '+esc(m.dataDate)+' 的数据</div>';
    }
    html += '</div>';

    html += '<div class="stat-grid">'
      + statCard('📈','#2563eb',s.news||0,'热榜新闻')
      + statCard('📰','#0d9488',s.rss||0,'RSS资讯')
      + statCard('🎯','#9333ea',s.hit||0,'分类命中')
      + statCard('⚡','#f97316',s.new||0,'新增热点')
      + '</div>';

    if(ai.found && (ai.cards||[]).length){
      html += '<div class="ai-grid">' + ai.cards.map(aiCardHTML).join('') + '</div>';
    } else {
      html += '<div class="ai-grid"><div class="ai-placeholder"><div class="ph-ico">🤖</div>'
        + '<div style="font-size:15px;font-weight:700;color:var(--text-2);">AI 简报暂未生成</div>'
        + '<div class="ph-msg">'+esc(ai.message||'运行 TrendRadar AI 分析后，此处将展示核心热点、舆论风向、弱信号、RSS 洞察与策略建议五个板块')+'</div></div></div>';
    }

    html += '<div class="chart-grid">'
      + '<div class="chart-card"><div class="chart-title">分类分布</div><div class="chart-box" id="chartPie"></div></div>'
      + '<div class="chart-card"><div class="chart-title">平台新闻数 Top 8</div><div class="chart-box" id="chartBar"></div></div>'
      + '</div>';

    el.innerHTML = html;
    return el;
  }

  function toggleBullets(btn){
    var card = btn.closest('.ai-card');
    var hidden = card.querySelectorAll('.hidden-bullet');
    var expanded = btn.getAttribute('data-open')==='1';
    hidden.forEach(function(li){ li.style.display = expanded ? 'none' : 'list-item'; });
    btn.setAttribute('data-open', expanded?'0':'1');
    btn.textContent = expanded ? '展开更多 ▾' : '收起 ▴';
  }

  /* ─────────────── 列表视图 ─────────────── */
  function rankBadge(r, isRss){
    if(isRss) return '<div class="rank-badge dot">●</div>';
    var cls = r===1?'r1':r===2?'r2':r===3?'r3':'';
    return '<div class="rank-badge '+cls+'">'+(r>0&&r<999?r:'–')+'</div>';
  }

  function newsRowHTML(it, isRss){
    var title = isRss
      ? it.t
      : it.t;
    var link = it.u ? '<a class="row-title" href="'+esc(it.u)+'" target="_blank" rel="noopener noreferrer">'+esc(title)+'</a>'
                    : '<span class="row-title">'+esc(title)+'</span>';
    var meta, side='';
    if(isRss){
      meta = '<span class="src-tag">'+esc(it.f||'')+'</span>'
        + (it.pub?'<span>'+esc(it.pub)+'</span>':'');
      if(it.s_summary || it.summary){}
    } else {
      meta = '<span class="src-tag">'+esc(it.p||'')+'</span>'
        + (it.tt?'<span class="row-trend" title="'+esc(it.tt)+'">轨迹 '+(it.tr||'')+'</span>':'');
      side = (it.n?'<span class="badge-new">NEW</span>':'')
        + '<span class="row-time">'+esc(it.l||'')+'</span>'
        + '<span class="row-crawl">在榜'+esc(it.c||1)+'次</span>';
    }
    var summary = (isRss && it.summary) ? '<div class="row-summary">'+esc(it.summary)+'</div>' : '';
    return '<div class="news-row" data-q="'+esc((it.t||'').toLowerCase())+'">'
      + rankBadge(it.r, isRss)
      + '<div class="row-main">'+link+'<div class="row-meta">'+meta+'</div>'+summary+'</div>'
      + '<div class="row-side">'+side+'</div></div>';
  }

  function listWrap(inner, count){
    return '<div class="list-toolbar"><div class="search-box">'
      + '<span class="s-ico">🔍</span><input type="text" placeholder="筛选本列表标题…" class="list-search">'
      + '<button class="search-clear" style="display:none;">清空</button></div></div>'
      + inner;
  }

  function bindSearch(scope){
    var input = scope.querySelector('.list-search');
    if(!input) return;
    var clear = scope.querySelector('.search-clear');
    function apply(){
      var q = input.value.trim().toLowerCase();
      clear.style.display = q ? 'block' : 'none';
      var rows = scope.querySelectorAll('.news-row');
      var vis = 0;
      rows.forEach(function(r){
        var hit = !q || r.getAttribute('data-q').indexOf(q)>=0;
        r.style.display = hit ? '' : 'none';
        if(hit) vis++;
      });
      var eh = scope.querySelector('.empty-hint.search-empty');
      if(!q && eh) eh.remove();
      if(q && !vis && !eh){
        var d = document.createElement('div');
        d.className='empty-hint search-empty'; d.textContent='没有匹配「'+input.value+'」的条目';
        scope.querySelector('.news-list,.rss-groups,.plat-panel').appendChild(d);
      } else if(q && vis && eh){ eh.remove(); }
    }
    input.addEventListener('input', apply);
    clear.addEventListener('click', function(){ input.value=''; apply(); input.focus(); });
  }

  function bindMore(scope){
    scope.querySelectorAll('.more-btn').forEach(function(btn){
      btn.addEventListener('click', function(){
        var list = btn.previousElementSibling;
        list.querySelectorAll('.news-row.overflow').forEach(function(r){ r.classList.remove('overflow'); r.style.display=''; });
        btn.remove();
      });
    });
  }

  function capRows(htmlList){
    // 超过 50 条：其后行加 overflow（CSS 由 JS 控制 display）
  }

  function renderNewsList(items, isRss){
    if(!items || !items.length){
      return '<div class="empty-hint">暂无相关条目</div>';
    }
    var rows = items.map(function(it, i){
      var html = newsRowHTML(it, isRss);
      if(i>=50){
        html = html.replace('<div class="news-row"', '<div class="news-row overflow" style="display:none;"');
      }
      return html;
    }).join('');
    var more = items.length>50 ? '<button class="more-btn">显示更多（还有 '+(items.length-50)+' 条）▾</button>' : '';
    return '<div class="news-list">'+rows+'</div>'+more;
  }

  function renderListView(v){
    var el = document.createElement('div');
    el.className = 'view active';
    if(!v){ el.innerHTML = '<div class="empty-hint">视图不存在</div>'; return el; }

    var head = '<div class="view-head"><span style="font-size:22px;">'+v.icon+'</span>'
      + '<div class="view-title">'+esc(v.label)+'</div>'
      + '<span class="view-count">'+viewCount(v)+'</span></div>';

    var body = '';
    if(v.type==='rss'){
      body = (v.groups||[]).map(function(g){
        var rows = g.items.map(function(it,i){
          var html = newsRowHTML(it, true);
          if(i>=50) html = html.replace('<div class="news-row"', '<div class="news-row overflow" style="display:none;"');
          return html;
        }).join('');
        var more = g.items.length>50 ? '<button class="more-btn">显示更多（还有 '+(g.items.length-50)+' 条）▾</button>' : '';
        return '<div class="rss-group"><div class="rss-group-head"><span class="g-bar"></span>'
          + esc(g.name)+'<span class="g-cnt">'+g.items.length+' 条</span></div>'
          + '<div class="news-list">'+rows+'</div>'+more+'</div>';
      }).join('');
      el.innerHTML = head + listWrap('<div class="rss-groups">'+body+'</div>');
    } else if(v.type==='hotlist'){
      var tabs = (v.platforms||[]).map(function(p,i){
        return '<button class="plat-tab'+(i===0?' active':'')+'" data-pi="'+i+'">'+esc(p.n)
          +'<span class="cnt">'+p.items.length+'</span></button>';
      }).join('');
      var panels = (v.platforms||[]).map(function(p,i){
        var rows = p.items.map(function(it,j){
          var html = newsRowHTML(it, false);
          if(j>=100) html = html.replace('<div class="news-row"', '<div class="news-row overflow" style="display:none;"');
          return html;
        }).join('');
        var more = p.items.length>100 ? '<button class="more-btn">显示更多（还有 '+(p.items.length-100)+' 条）▾</button>' : '';
        return '<div class="plat-panel" data-pi="'+i+'" style="'+(i===0?'':'display:none;')+'">'
          + '<div class="news-list">'+rows+'</div>'+more+'</div>';
      }).join('');
      el.innerHTML = head + listWrap('<div class="plat-tabs">'+tabs+'</div><div class="plat-panels">'+panels+'</div>');
      el.querySelectorAll('.plat-tab').forEach(function(t){
        t.addEventListener('click', function(){
          el.querySelectorAll('.plat-tab').forEach(function(x){x.classList.remove('active');});
          t.classList.add('active');
          var pi = t.getAttribute('data-pi');
          el.querySelectorAll('.plat-panel').forEach(function(p){
            p.style.display = p.getAttribute('data-pi')===pi ? '' : 'none';
          });
        });
      });
    } else {
      // 商机信号视图顶部嵌入策略建议卡
      var strategyTop = '';
      if(v.id==='signals' && D.ai && D.ai.found){
        var strat = (D.ai.cards||[]).filter(function(c){return c.key==='strategy';})[0];
        if(strat){
          strategyTop = '<div style="margin-bottom:14px;">'+aiCardHTML(strat)+'</div>';
        }
      }
      body = renderNewsList(v.items||[], false);
      el.innerHTML = head + strategyTop + listWrap(body);
    }

    bindSearch(el);
    bindMore(el);
    return el;
  }

  /* ─────────────── 图表 ─────────────── */
  function isDark(){ return document.documentElement.getAttribute('data-theme')==='dark'; }
  function cText(){ return isDark() ? '#9fadc6' : '#55617a'; }
  function cLine(){ return isDark() ? '#1e2c46' : '#e4e9f2'; }
  function cSplit(){ return isDark() ? '#16233d' : '#eef2f8'; }
  function cTipBg(){ return isDark() ? '#111d33' : '#fff'; }

  function renderCharts(){
    if(typeof echarts==='undefined') return;
    if(state.view!=='briefing') return;
    var pieEl = document.getElementById('chartPie');
    var barEl = document.getElementById('chartBar');
    var palette = (D.charts&&D.charts.palette) || [];

    if(pieEl){
      if(!state.charts.pie) state.charts.pie = echarts.init(pieEl);
      state.charts.pie.setOption({
        tooltip:{trigger:'item', backgroundColor:cTipBg(), borderColor:cLine(),
          textStyle:{color:cText(), fontSize:12}},
        legend:{bottom:0, type:'scroll', textStyle:{color:cText(), fontSize:11}, itemWidth:10, itemHeight:10},
        color: palette.concat(['#8b96ab']),
        series:[{type:'pie', radius:['45%','70%'], center:['50%','42%'],
          itemStyle:{borderRadius:5, borderColor:isDark()?'#111d33':'#fff', borderWidth:2},
          label:{show:false},
          emphasis:{label:{show:true, fontSize:13, fontWeight:'bold', color:cText()}},
          data:(D.charts&&D.charts.pie)||[]}]
      }, true);
    }
    if(barEl){
      if(!state.charts.bar) state.charts.bar = echarts.init(barEl);
      var d = ((D.charts&&D.charts.bar)||[]).slice().reverse();
      state.charts.bar.setOption({
        tooltip:{trigger:'axis', axisPointer:{type:'shadow'}, backgroundColor:cTipBg(),
          borderColor:cLine(), textStyle:{color:cText(), fontSize:12}},
        grid:{left:8, right:30, top:8, bottom:8, containLabel:true},
        xAxis:{type:'value', axisLine:{lineStyle:{color:cLine()}},
          axisLabel:{color:cText(), fontSize:11}, splitLine:{lineStyle:{color:cSplit()}}},
        yAxis:{type:'category', data:d.map(function(x){return x.name;}),
          axisLine:{lineStyle:{color:cLine()}}, axisTick:{show:false},
          axisLabel:{color:cText(), fontSize:11.5, width:92, overflow:'truncate'}},
        series:[{type:'bar', data:d.map(function(x){return x.value;}), barWidth:'58%',
          itemStyle:{borderRadius:[0,4,4,0],
            color:new echarts.graphic.LinearGradient(0,0,1,0,[
              {offset:0,color:'#1d4ed8'},{offset:1,color:'#60a5fa'}])},
          label:{show:true, position:'right', color:cText(), fontSize:11}}]
      }, true);
    }
    Object.keys(state.charts).forEach(function(k){ state.charts[k].resize(); });
  }

  window.addEventListener('resize', function(){
    Object.keys(state.charts).forEach(function(k){ if(state.charts[k]) state.charts[k].resize(); });
  });
  window.addEventListener('scroll', function(){
    document.getElementById('backTop').classList.toggle('show', window.pageYOffset>400);
  });

  /* ─────────────── 初始化 ─────────────── */
  function init(){
    document.getElementById('footTime').textContent = '生成于 ' + ((D.meta||{}).genTime||'');
    setTheme(getTheme());
    fillHistory();
    buildNav();
    switchView('briefing');
    state.inited = true;
  }

  window.App = { toggleTheme:toggleTheme, toggleBullets:toggleBullets, goHome:goHome };
  init();
})();
</script>
</body>
</html>
"""


def build_html(payload: Dict[str, Any]) -> str:
    """将数据载荷注入 HTML 模板。"""
    data_json = json.dumps(payload, ensure_ascii=False)
    # 防止 </script> 注入破坏页面
    data_json = data_json.replace("</", "<\\/")
    return _HTML_TEMPLATE.replace("__ECHARTS_CDN__", ECHARTS_CDN) \
                         .replace("__DATA__", data_json)


# ═══════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 60)
    print("  TrendRadar 销售情报仪表盘（早间简报版）")
    print("=" * 60)

    root_dir = Path(__file__).resolve().parent
    if not (root_dir / "config").exists():
        root_dir = Path.cwd()
    print(f"  工作目录: {root_dir}")

    # 可选：--date YYYY-MM-DD 重新生成指定日期
    target_date = today_str()
    if len(sys.argv) >= 3 and sys.argv[1] == "--date":
        target_date = sys.argv[2]
    print(f"  目标日期: {target_date}（Asia/Shanghai）")

    # 1) 频率词
    print("\n[1/6] 解析频率词配置...")
    groups, global_filters = parse_frequency_words(
        root_dir / "config" / "frequency_words.txt")
    print(f"  共加载 {len(groups)} 个词组，{len(global_filters)} 个全局过滤词")

    # 2) 新闻库（缺失时回退到最近一天）
    print("\n[2/6] 读取热榜新闻数据库...")
    news_db = root_dir / "output" / "news" / f"{target_date}.db"
    data_date: Optional[str] = None
    if not news_db.exists():
        latest = find_latest_db(root_dir / "output" / "news", "")
        if latest:
            news_db = latest
            data_date = latest.stem
            print(f"  [提示] 当日库不存在，回退到最近数据库: {latest.name}")
    news_items, platform_map, latest_crawl = load_news_db(news_db)

    # 3) RSS 库（与新闻同日；否则取最近）
    print("\n[3/6] 读取 RSS 数据库...")
    rss_db = root_dir / "output" / "rss" / f"{(data_date or target_date)}.db"
    if not rss_db.exists():
        latest_rss = find_latest_db(root_dir / "output" / "rss", "")
        if latest_rss:
            rss_db = latest_rss
            print(f"  [提示] 回退到最近 RSS 数据库: {latest_rss.name}")
    rss_items, feed_map = load_rss_db(rss_db)

    # 4) AI 分析
    print("\n[4/6] 提取 AI 分析内容...")
    ai_blocks = extract_ai_blocks(root_dir / "index.html")

    # 5) 数据处理
    print("\n[5/6] 处理数据与分类...")
    news_data = process_news(news_items, platform_map, groups,
                             global_filters, latest_crawl)
    rss_groups = process_rss(rss_items, feed_map)
    print(f"  新闻总数: {news_data['total']}")
    print(f"  分类命中: {news_data['hit_count']}")
    print(f"  新增热点: {news_data['new_count']}")
    print(f"  RSS 资讯: {sum(len(g['items']) for g in rss_groups)}")

    # 6) 生成 HTML
    print("\n[6/6] 生成 HTML 仪表盘...")
    site_dir = root_dir / SITE_DIR
    reports_site = root_dir / REPORTS_SITE_DIR
    reports_git = root_dir / REPORTS_GIT_DIR
    for d in (site_dir, reports_site, reports_git):
        d.mkdir(parents=True, exist_ok=True)

    history = scan_history_reports()
    print(f"  发现 {len(history)} 份历史报告（_site/reports + reports 合并）")

    payload = build_payload(news_data, rss_groups, ai_blocks, history,
                            target_date, data_date)
    html = build_html(payload)

    index_out = site_dir / "index.html"
    index_out.write_text(html, encoding="utf-8")
    print(f"  已写入: {index_out.relative_to(root_dir)}")

    report_name = f"{timestamp_label()}.html"
    site_report = reports_site / report_name
    git_report = reports_git / report_name
    site_report.write_text(html, encoding="utf-8")
    git_report.write_text(html, encoding="utf-8")
    print(f"  已归档: {site_report.relative_to(root_dir)}")
    print(f"  已备份: {git_report.relative_to(root_dir)}（git 持久化）")

    cleanup_old_reports()

    print("\n" + "=" * 60)
    print("  仪表盘生成完成！")
    print(f"  主页: {index_out}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
