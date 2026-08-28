# coding=utf-8
"""
TrendRadar 情报仪表盘生成器
==========================

从 TrendRadar 的 SQLite 数据库生成美观的现代化 HTML 仪表盘。
在爬虫运行后由 GitHub Actions 调用，也支持本地（Windows / Ubuntu）运行。

功能概览：
- 读取当日热榜新闻数据库与 RSS 数据库
- 解析 frequency_words.txt 进行关键词分类
- 从 TrendRadar 生成的 index.html 中提取 AI 分析
- 生成自包含的 HTML 仪表盘（ECharts 图表、明暗主题、响应式布局）
- 归档历史报告至 _site/reports/，最多保留 60 份

用法：
    python dashboard.py
"""

from __future__ import annotations

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
#  常量定义
# ═══════════════════════════════════════════════════════════════

# 上海时区（UTC+8）
SHANGHAI_TZ = timezone(timedelta(hours=8))

# 站点输出目录（GitHub Pages 部署目录）
SITE_DIR = Path("_site")
REPORTS_DIR = SITE_DIR / "reports"

# 最多保留的历史报告数量
MAX_HISTORY_REPORTS = 60

# ECharts CDN
ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"

# 分类配色方案（10 色循环）
CATEGORY_PALETTE = [
    "#1677ff", "#52c41a", "#faad14", "#f5222d", "#722ed1",
    "#13c2c2", "#eb2f96", "#fa8c16", "#2f54eb", "#a0d911",
]

# 平台标签配色（15 色循环，通过哈希稳定分配）
PLATFORM_PALETTE = [
    "#1677ff", "#52c41a", "#faad14", "#f5222d", "#722ed1",
    "#13c2c2", "#eb2f96", "#fa8c16", "#2f54eb", "#a0d911",
    "#e91869", "#08979c", "#c41d7f", "#ad8b00", "#1d39c4",
]

# 排名前三的奖牌色
MEDAL_COLORS: Dict[int, str] = {
    1: "#fbbf24",  # 金牌
    2: "#cbd5e1",  # 银牌
    3: "#d97706",  # 铜牌
}


# ═══════════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════════

def now_shanghai() -> datetime:
    """获取当前上海时间。"""
    return datetime.now(SHANGHAI_TZ)


def today_str() -> str:
    """返回当日日期字符串 YYYY-MM-DD（上海时区）。"""
    return now_shanghai().strftime("%Y-%m-%d")


def now_display() -> str:
    """返回当前日期时间字符串，用于页面展示。"""
    return now_shanghai().strftime("%Y-%m-%d %H:%M")


def timestamp_label() -> str:
    """返回用于归档文件名的时间戳 YYYY-MM-DD-HHMM。"""
    return now_shanghai().strftime("%Y-%m-%d-%H%M")


def format_hhmm(time_str: Optional[str]) -> str:
    """
    将数据库中的时间字段格式化为 HH:MM。

    数据库中 first_crawl_time / last_crawl_time 可能存储为
    "HH-MM" 格式（如 "07-41"）或完整时间戳，此处统一格式化。
    """
    if not time_str:
        return "--:--"
    time_str = str(time_str).strip()
    # "HH-MM" → "HH:MM"
    m = re.match(r"^(\d{1,2})-(\d{2})$", time_str)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    # 完整时间戳 "YYYY-MM-DD HH:MM:SS" → "HH:MM"
    m = re.match(r"^\d{4}-\d{2}-\d{2}\s+(\d{1,2}:\d{2})", time_str)
    if m:
        return m.group(1)
    # "HH:MM" 已经是目标格式
    m = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return time_str


def truncate(text: str, length: int = 20) -> str:
    """截断文本到指定长度，超出部分用省略号表示。"""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= length:
        return text
    return text[:length] + "..."


def color_for_index(index: int, palette: List[str]) -> str:
    """根据索引从调色板中循环取色。"""
    return palette[index % len(palette)]


def color_for_platform(platform_id: str) -> str:
    """根据平台 ID 稳定地分配一个标签颜色（跨进程确定性）。"""
    # 使用确定性哈希，避免 Python 内置 hash() 的随机化导致颜色每次运行不同
    h = 0
    for ch in platform_id:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return PLATFORM_PALETTE[h % len(PLATFORM_PALETTE)]


# ═══════════════════════════════════════════════════════════════
#  频率词配置解析
# ═══════════════════════════════════════════════════════════════

class WordEntry:
    """单个关键词条目，支持普通子串匹配与正则匹配。"""

    def __init__(self, raw: str):
        self.display_name: Optional[str] = None
        self.is_regex: bool = False
        self.pattern: Optional[re.Pattern] = None
        self.word: str = raw.strip()

        # 拆分 "=> 别名"
        if "=>" in self.word:
            parts = re.split(r"\s*=>\s*", self.word, 1)
            self.word = parts[0].strip()
            if len(parts) > 1 and parts[1].strip():
                self.display_name = parts[1].strip()

        # 判断是否为正则表达式 /pattern/flags
        regex_m = re.match(r"^/(.+)/([a-z]*)$", self.word)
        if regex_m:
            pattern_str = regex_m.group(1)
            try:
                self.pattern = re.compile(pattern_str, re.IGNORECASE)
                self.is_regex = True
                self.word = pattern_str
            except re.error:
                # 正则编译失败则退化为普通字符串
                self.is_regex = False

    def matches(self, title_lower: str) -> bool:
        """判断该关键词是否在标题中匹配（标题已转为小写）。"""
        if self.is_regex and self.pattern:
            return bool(self.pattern.search(title_lower))
        return self.word.lower() in title_lower


class WordGroup:
    """一组关键词，包含必须词、普通词、过滤词、组别名与最大条数。"""

    def __init__(self) -> None:
        self.required: List[WordEntry] = []
        self.normal: List[WordEntry] = []
        self.filters: List[WordEntry] = []
        self.alias: Optional[str] = None
        self.max_count: int = 0

    @property
    def display_name(self) -> str:
        """生成组的显示名称。"""
        if self.alias:
            return self.alias
        parts: List[str] = []
        for w in self.normal + self.required:
            parts.append(w.display_name or w.word)
        return " / ".join(parts) if parts else "未命名分组"

    def matches(self, title_lower: str) -> bool:
        """
        判断标题是否匹配本组。

        规则：
        1. 如果有过滤词命中则不匹配
        2. 如果有必须词，所有必须词都要匹配
        3. 如果有普通词，任意一个匹配即可
        4. 两者皆无则不匹配
        """
        # 过滤词检查
        for f in self.filters:
            if f.matches(title_lower):
                return False
        # 必须词检查
        if self.required:
            if not all(r.matches(title_lower) for r in self.required):
                return False
        # 普通词检查
        if self.normal:
            if not any(n.matches(title_lower) for n in self.normal):
                return False
        # 至少需要一种词
        return bool(self.required or self.normal)


def parse_frequency_words(file_path: Path) -> Tuple[List[WordGroup], List[str]]:
    """
    解析 frequency_words.txt 文件。

    Args:
        file_path: 配置文件路径

    Returns:
        (词组列表, 全局过滤词列表)
    """
    if not file_path.exists():
        print(f"  [警告] 频率词配置文件不存在: {file_path}")
        return [], []

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 按空行分割为段落
    paragraphs = re.split(r"\n\s*\n", content)

    groups: List[WordGroup] = []
    global_filters: List[str] = []
    current_section = "WORD_GROUPS"

    for para in paragraphs:
        # 去除注释行和空行
        lines: List[str] = []
        for line in para.split("\n"):
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                lines.append(stripped)

        if not lines:
            continue

        # 检查区域标记
        if lines[0].startswith("[") and lines[0].endswith("]"):
            section_name = lines[0][1:-1].strip().upper()
            if section_name in ("GLOBAL_FILTER", "WORD_GROUPS"):
                current_section = section_name
                lines = lines[1:]

        # 全局过滤区
        if current_section == "GLOBAL_FILTER":
            for line in lines:
                # 跳过特殊语法行
                if line.startswith(("!", "+", "@", "[")):
                    continue
                global_filters.append(line)
            continue

        # 词组区
        group = WordGroup()
        # 检查第一行是否为组别名
        if lines and lines[0].startswith("[") and lines[0].endswith("]"):
            potential = lines[0][1:-1].strip()
            if potential.upper() not in ("GLOBAL_FILTER", "WORD_GROUPS"):
                group.alias = potential
                lines = lines[1:]

        for line in lines:
            if line.startswith("@"):
                # 最大条数限制
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


def categorize_title(
    title: str,
    groups: List[WordGroup],
    global_filters: List[str],
) -> Optional[str]:
    """
    将标题分类到第一个匹配的词组。

    Args:
        title: 新闻标题
        groups: 词组列表
        global_filters: 全局过滤词

    Returns:
        匹配到的分类名称，若被全局过滤则返回 None，未匹配返回 "未分类"
    """
    if not isinstance(title, str) or not title.strip():
        return None

    title_lower = title.lower()

    # 全局过滤检查
    for gw in global_filters:
        if gw.lower() in title_lower:
            return None

    # 按顺序匹配词组
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
        (新闻列表, 平台ID→名称映射, 最新抓取时间 HH-MM)
    """
    if not db_path.exists():
        print(f"  [警告] 新闻数据库不存在: {db_path}")
        return [], {}, None

    print(f"  读取新闻数据库: {db_path.name}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # 读取平台映射
        platform_map: Dict[str, str] = {}
        try:
            for row in conn.execute("SELECT id, name FROM platforms"):
                platform_map[row["id"]] = row["name"]
        except sqlite3.OperationalError:
            pass

        # 读取新闻条目
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
                })
        except sqlite3.OperationalError as e:
            print(f"  [警告] 读取 news_items 失败: {e}")

        # 读取最新抓取时间
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
    """
    读取 RSS 数据库。

    Returns:
        (RSS条目列表, 源ID→名称映射)
    """
    if not db_path.exists():
        print(f"  [提示] RSS 数据库不存在: {db_path}（继续执行）")
        return [], {}

    print(f"  读取 RSS 数据库: {db_path.name}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # 读取 RSS 源映射
        feed_map: Dict[str, str] = {}
        try:
            for row in conn.execute("SELECT id, name FROM rss_feeds"):
                feed_map[row["id"]] = row["name"]
        except sqlite3.OperationalError:
            pass

        # 读取 RSS 条目
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


# ═══════════════════════════════════════════════════════════════
#  AI 分析提取
# ═══════════════════════════════════════════════════════════════

class _AISectionExtractor(HTMLParser):
    """
    从 HTML 中提取 class 或 id 包含 "ai" 的区域。

    使用 HTMLParser 跟踪标签嵌套，准确提取整个 AI 分析区块的 HTML。
    """

    def __init__(self) -> None:
        super().__init__()
        self._recording = False
        self._depth = 0
        self._parts: List[str] = []
        self._found_tag: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attrs_dict = dict(attrs)
        class_attr = (attrs_dict.get("class") or "").lower()
        id_attr = (attrs_dict.get("id") or "").lower()

        # 检测 AI 区域的起始标签
        if not self._recording:
            is_ai = (
                "ai-section" in class_attr
                or "ai_analysis" in class_attr
                or "ai-analysis" in class_attr
                or id_attr.startswith("ai")
                or "ai-analysis" in id_attr
                or "ai_analysis" in id_attr
            )
            if is_ai:
                self._recording = True
                self._depth = 1
                self._found_tag = tag
                self._parts.append(self._render_open_tag(tag, attrs))
                return

        if self._recording:
            # 自关闭标签不增加深度
            if tag not in ("br", "hr", "img", "input", "meta", "link"):
                self._depth += 1
            self._parts.append(self._render_open_tag(tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        if self._recording:
            self._parts.append(f"</{tag}>")
            # 对所有结束标签减少深度（与开始标签的深度递增配对）
            self._depth -= 1
            if self._depth <= 0:
                self._recording = False

    def handle_data(self, data: str) -> None:
        if self._recording:
            self._parts.append(data)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if self._recording:
            self._parts.append(self._render_open_tag(tag, attrs, self_closing=True))

    def handle_entityref(self, name: str) -> None:
        if self._recording:
            self._parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._recording:
            self._parts.append(f"&#{name};")

    @staticmethod
    def _render_open_tag(
        tag: str,
        attrs: List[Tuple[str, Optional[str]]],
        self_closing: bool = False,
    ) -> str:
        """重新渲染开始标签。"""
        attr_str = ""
        for name, value in attrs:
            if value is None:
                attr_str += f" {name}"
            else:
                attr_str += f' {name}="{html_escape(value, quote=True)}"'
        if self_closing:
            return f"<{tag}{attr_str} />"
        return f"<{tag}{attr_str}>"

    def get_result(self) -> str:
        """获取提取到的 AI 区域 HTML。"""
        return "".join(self._parts).strip()


def extract_ai_analysis(html_path: Path) -> str:
    """
    从 TrendRadar 生成的 index.html 中提取 AI 分析区域的 HTML。

    Args:
        html_path: index.html 文件路径

    Returns:
        AI 分析区域的 HTML 字符串，若未找到则返回空字符串
    """
    if not html_path.exists():
        print(f"  [提示] 未找到 TrendRadar 报告: {html_path}")
        return ""

    try:
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        print(f"  [警告] 读取 index.html 失败: {e}")
        return ""

    parser = _AISectionExtractor()
    try:
        parser.feed(content)
    except Exception as e:
        print(f"  [警告] 解析 index.html 时出错: {e}")
        return ""

    result = parser.get_result()
    if result:
        print("  已从 index.html 提取 AI 分析内容")
    else:
        print("  未在 index.html 中找到 AI 分析区域")
    return result


# ═══════════════════════════════════════════════════════════════
#  数据处理与热度计算
# ═══════════════════════════════════════════════════════════════

def calc_news_weight(rank: int, crawl_count: int) -> float:
    """
    计算新闻热度权重。

    公式：
        weight = (50 - min(rank, 50)) * 0.6
               + crawl_count * 3 * 0.3
               + (1 if rank <= 5 else 0) * 10 * 0.1

    排名越靠前（rank 越小）权重越高，在榜时间越长（crawl_count 越大）权重越高。
    """
    rank_score = (50 - min(rank, 50)) * 0.6
    crawl_score = crawl_count * 3 * 0.3
    top_bonus = 10 * 0.1 if rank <= 5 else 0
    return round(rank_score + crawl_score + top_bonus, 2)


def process_news(
    news_items: List[Dict[str, Any]],
    platform_map: Dict[str, str],
    groups: List[WordGroup],
    global_filters: List[str],
    latest_crawl: Optional[str],
) -> Dict[str, Any]:
    """
    对新闻数据进行分类、排序与统计。

    Returns:
        包含分类新闻、统计数据、图表数据的字典
    """
    categorized: Dict[str, List[Dict[str, Any]]] = {}
    platform_counts: Dict[str, int] = {}
    all_news: List[Dict[str, Any]] = []
    new_count = 0
    hit_count = 0

    for item in news_items:
        title = item["title"]
        category = categorize_title(title, groups, global_filters)

        # 被全局过滤的新闻跳过
        if category is None:
            continue

        # 平台名称
        platform_name = platform_map.get(item["platform_id"], item["platform_id"])

        # 计算热度权重
        weight = calc_news_weight(item["rank"], item["crawl_count"])

        processed = {
            **item,
            "platform_name": platform_name,
            "category": category,
            "weight": weight,
            "time_display": format_hhmm(item["last_crawl_time"]),
        }
        all_news.append(processed)

        # 分类计数
        categorized.setdefault(category, []).append(processed)

        # 平台计数
        platform_counts[platform_name] = platform_counts.get(platform_name, 0) + 1

        # 命中分类数（排除"未分类"）
        if category != "未分类":
            hit_count += 1

        # 新增热点：crawl_count == 1 或首次抓取时间为最新抓取批次
        is_new = item["crawl_count"] == 1
        if latest_crawl and item.get("first_crawl_time") == latest_crawl:
            is_new = True
        if is_new:
            new_count += 1

    # 每个分类内按权重降序排列
    for cat in categorized:
        categorized[cat].sort(key=lambda x: x["weight"], reverse=True)

    # 按分类新闻数量降序排列分类
    sorted_categories = sorted(
        categorized.items(), key=lambda x: len(x[1]), reverse=True
    )

    # 平台分布（Top 10）
    sorted_platforms = sorted(
        platform_counts.items(), key=lambda x: x[1], reverse=True
    )

    # 热度排行（Top 10）
    hottest = sorted(all_news, key=lambda x: x["weight"], reverse=True)[:10]

    return {
        "all_news": all_news,
        "categorized": dict(sorted_categories),
        "platform_counts": platform_counts,
        "sorted_platforms": sorted_platforms,
        "hottest": hottest,
        "total": len(all_news),
        "hit_count": hit_count,
        "new_count": new_count,
    }


def process_rss(
    rss_items: List[Dict[str, Any]],
    feed_map: Dict[str, str],
) -> Dict[str, List[Dict[str, Any]]]:
    """
    将 RSS 条目按订阅源分组。

    Returns:
        源名称 → 条目列表 的有序字典（按条目数降序）
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in rss_items:
        feed_name = feed_map.get(item["feed_id"], item["feed_id"])
        item["feed_name"] = feed_name
        # 格式化发布时间
        pub = item.get("published_at", "")
        if pub:
            # 尝试提取 YYYY-MM-DD HH:MM
            m = re.match(r"(\d{4}-\d{2}-\d{2})[T\s](\d{2}:\d{2})", pub)
            if m:
                item["published_display"] = f"{m.group(1)} {m.group(2)}"
            else:
                item["published_display"] = pub[:16]
        else:
            item["published_display"] = ""
        grouped.setdefault(feed_name, []).append(item)

    # 按条目数降序排列
    return dict(sorted(grouped.items(), key=lambda x: len(x[1]), reverse=True))


# ═══════════════════════════════════════════════════════════════
#  历史报告管理
# ═══════════════════════════════════════════════════════════════

def scan_history_reports() -> List[Dict[str, str]]:
    """
    扫描 _site/reports/ 目录，构建历史报告列表。

    Returns:
        历史报告列表，每项包含 filename, date_label, path，按日期降序排列
    """
    reports: List[Dict[str, str]] = []
    if not REPORTS_DIR.exists():
        return reports

    for f in REPORTS_DIR.glob("*.html"):
        # 从文件名解析日期 YYYY-MM-DD-HHMM.html
        m = re.match(r"(\d{4}-\d{2}-\d{2})-(\d{2})(\d{2})", f.stem)
        if m:
            date_label = f"{m.group(1)} {m.group(2)}:{m.group(3)}"
        else:
            date_label = f.stem
        reports.append({
            "filename": f.name,
            "date_label": date_label,
            "path": f"reports/{f.name}",
        })

    reports.sort(key=lambda x: x["filename"], reverse=True)
    return reports


def cleanup_old_reports() -> None:
    """删除超出数量限制的旧报告。"""
    if not REPORTS_DIR.exists():
        return
    reports = sorted(REPORTS_DIR.glob("*.html"), reverse=True)
    if len(reports) > MAX_HISTORY_REPORTS:
        for old in reports[MAX_HISTORY_REPORTS:]:
            try:
                old.unlink()
                print(f"  已清理旧报告: {old.name}")
            except OSError:
                pass


# ═══════════════════════════════════════════════════════════════
#  HTML 生成
# ═══════════════════════════════════════════════════════════════

def _render_news_card(item: Dict[str, Any], index: int = 0) -> str:
    """渲染单条新闻卡片 HTML。"""
    rank = item.get("rank", 999)
    title = html_escape(item.get("title", ""))
    url = item.get("url", "")
    platform_name = html_escape(item.get("platform_name", ""))
    platform_id = item.get("platform_id", "")
    time_disp = item.get("time_display", "--:--")
    crawl_count = item.get("crawl_count", 1)
    weight = item.get("weight", 0)

    # 排名样式
    if rank in MEDAL_COLORS:
        rank_style = f'background:{MEDAL_COLORS[rank]};color:#fff;font-weight:700;'
    else:
        rank_style = "background:var(--rank-bg);color:var(--rank-text);"

    # 平台标签颜色
    tag_color = color_for_platform(platform_id)

    # 标题链接
    if url:
        title_html = f'<a href="{html_escape(url, quote=True)}" target="_blank" rel="noopener noreferrer" class="news-title">{title}</a>'
    else:
        title_html = f'<span class="news-title">{title}</span>'

    # 新增标记
    new_badge = ""
    if item.get("crawl_count") == 1:
        new_badge = '<span class="badge-new">NEW</span>'

    return f"""
    <div class="news-card" data-weight="{weight}">
      <div class="news-rank" style="{rank_style}">{rank}</div>
      <div class="news-body">
        <div class="news-title-row">{title_html}{new_badge}</div>
        <div class="news-meta">
          <span class="source-tag" style="background:{tag_color}20;color:{tag_color};border:1px solid {tag_color}40;">{platform_name}</span>
          <span class="meta-time">{time_disp}</span>
          <span class="meta-crawl">在榜 {crawl_count} 次</span>
        </div>
      </div>
    </div>"""


def _render_rss_card(item: Dict[str, Any]) -> str:
    """渲染单条 RSS 资讯卡片 HTML。"""
    title = html_escape(item.get("title", ""))
    url = item.get("url", "")
    feed_name = html_escape(item.get("feed_name", ""))
    pub = item.get("published_display", "")
    summary = html_escape(item.get("summary", ""))

    if url:
        title_html = f'<a href="{html_escape(url, quote=True)}" target="_blank" rel="noopener noreferrer" class="news-title">{title}</a>'
    else:
        title_html = f'<span class="news-title">{title}</span>'

    summary_html = ""
    if summary:
        # 去除 HTML 标签并截断
        clean = re.sub(r"<[^>]+>", "", summary)
        if len(clean) > 120:
            clean = clean[:120] + "..."
        summary_html = f'<div class="rss-summary">{html_escape(clean)}</div>'

    return f"""
    <div class="news-card rss-card">
      <div class="news-body">
        <div class="news-title-row">{title_html}</div>
        {summary_html}
        <div class="news-meta">
          <span class="source-tag" style="background:var(--accent)20;color:var(--accent);border:1px solid var(--accent)40;">{feed_name}</span>
          <span class="meta-time">{pub}</span>
        </div>
      </div>
    </div>"""


def build_html(
    news_data: Dict[str, Any],
    rss_grouped: Dict[str, List[Dict[str, Any]]],
    ai_html: str,
    history_reports: List[Dict[str, str]],
    date_str: str,
    time_str: str,
    rel_prefix: str = "",
) -> str:
    """
    生成完整的自包含 HTML 仪表盘。

    Args:
        news_data: 处理后的新闻数据
        rss_grouped: 按源分组的 RSS 数据
        ai_html: AI 分析区域 HTML
        history_reports: 历史报告列表
        date_str: 报告日期
        time_str: 报告时间
        rel_prefix: 相对路径前缀（归档页面用 "../"）
    """
    categorized = news_data["categorized"]
    total_news = news_data["total"]
    total_rss = sum(len(v) for v in rss_grouped.values())
    hit_count = news_data["hit_count"]
    new_count = news_data["new_count"]

    # ── 历史报告下拉选项 ──
    history_options = f'<option value="">选择历史报告</option>'
    for rep in history_reports[:60]:
        history_options += (
            f'<option value="{rel_prefix}{rep["path"]}">{rep["date_label"]}</option>'
        )

    # ── 摘要卡片 ──
    summary_cards = f"""
    <div class="summary-grid">
      <div class="summary-card">
        <div class="summary-icon" style="background:#1677ff20;color:#1677ff;">
          <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 16l4-8 4 4 4-6"/></svg>
        </div>
        <div class="summary-info"><div class="summary-value">{total_news}</div><div class="summary-label">热榜新闻总数</div></div>
      </div>
      <div class="summary-card">
        <div class="summary-icon" style="background:#52c41a20;color:#52c41a;">
          <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 11a9 9 0 0 1 9 9"/><path d="M4 4a16 16 0 0 1 16 16"/><circle cx="5" cy="19" r="1"/></svg>
        </div>
        <div class="summary-info"><div class="summary-value">{total_rss}</div><div class="summary-label">RSS 资讯数</div></div>
      </div>
      <div class="summary-card">
        <div class="summary-icon" style="background:#722ed120;color:#722ed1;">
          <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>
        </div>
        <div class="summary-info"><div class="summary-value">{hit_count}</div><div class="summary-label">分类命中数</div></div>
      </div>
      <div class="summary-card">
        <div class="summary-icon" style="background:#fa8c1620;color:#fa8c16;">
          <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
        </div>
        <div class="summary-info"><div class="summary-value">{new_count}</div><div class="summary-label">新增热点数</div></div>
      </div>
    </div>"""

    # ── 分类导航 Pills ──
    pills = ['<button class="pill active" data-target="sec-overview">概览</button>']
    for idx, (cat, items) in enumerate(categorized.items()):
        cat_id = f"cat-{idx}"
        color = color_for_index(idx, CATEGORY_PALETTE)
        pills.append(
            f'<button class="pill" data-target="{cat_id}" '
            f'style="--pill-color:{color};">'
            f'{html_escape(cat)} <span class="pill-count">{len(items)}</span></button>'
        )
    if rss_grouped:
        pills.append('<button class="pill" data-target="sec-rss" style="--pill-color:#52c41a;">RSS 资讯</button>')
    pills.append('<button class="pill" data-target="sec-hotlist" style="--pill-color:#f5222d;">完整热榜</button>')
    pills_html = "\n".join(pills)

    # ── 分类新闻区域 ──
    category_sections = []
    chart_pie_data = []
    for idx, (cat, items) in enumerate(categorized.items()):
        cat_id = f"cat-{idx}"
        color = color_for_index(idx, CATEGORY_PALETTE)
        cards = "".join(_render_news_card(it, i) for i, it in enumerate(items[:50]))
        more_count = max(0, len(items) - 50)
        more_html = f'<div class="more-hint">还有 {more_count} 条未显示</div>' if more_count else ""

        category_sections.append(f"""
    <section id="{cat_id}" class="category-section" style="--cat-color:{color};">
      <div class="category-header">
        <div class="category-dot" style="background:{color};"></div>
        <h2 class="category-title">{html_escape(cat)}</h2>
        <span class="category-count">{len(items)}</span>
      </div>
      <div class="news-list">{cards}{more_html}</div>
    </section>""")

        # 饼图数据（前 8 个分类）
        chart_pie_data.append({"name": cat, "value": len(items)})

    categories_html = "\n".join(category_sections)

    # ── 饼图：前 8 个分类，其余合并为"其他" ──
    if len(chart_pie_data) > 8:
        top8 = chart_pie_data[:8]
        others_value = sum(d["value"] for d in chart_pie_data[8:])
        top8.append({"name": "其他", "value": others_value})
        chart_pie_data = top8

    # ── 平台柱状图数据（Top 10） ──
    platform_bar_data = news_data["sorted_platforms"][:10]

    # ── 热度排行数据（Top 10） ──
    hottest_data = []
    for it in news_data["hottest"]:
        hottest_data.append({
            "title": truncate(it["title"], 20),
            "full_title": it["title"],
            "weight": it["weight"],
            "url": it.get("url", ""),
            "platform": it.get("platform_name", ""),
        })

    # ── RSS 区域 ──
    rss_sections = []
    if rss_grouped:
        for feed_name, items in rss_grouped.items():
            cards = "".join(_render_rss_card(it) for it in items[:30])
            rss_sections.append(f"""
      <div class="rss-feed-group">
        <h3 class="rss-feed-title">{html_escape(feed_name)} <span class="category-count">{len(items)}</span></h3>
        <div class="news-list">{cards}</div>
      </div>""")
    rss_html = "\n".join(rss_sections)

    # ── AI 分析区域 ──
    if ai_html:
        ai_section = f"""
    <section class="ai-dashboard-card">
      <div class="ai-card-header">
        <div class="ai-card-icon">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a4 4 0 0 1 4 4v1a4 4 0 0 1-8 0V6a4 4 0 0 1 4-4z"/><path d="M8 14s-4 2-4 6h16c0-4-4-6-4-6"/></svg>
        </div>
        <h2 class="ai-card-title">AI 智能分析</h2>
      </div>
      <div class="ai-card-body">{ai_html}</div>
    </section>"""
    else:
        ai_section = """
    <section class="ai-dashboard-card ai-placeholder">
      <div class="ai-card-header">
        <div class="ai-card-icon" style="opacity:0.5;">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a4 4 0 0 1 4 4v1a4 4 0 0 1-8 0V6a4 4 0 0 1 4-4z"/><path d="M8 14s-4 2-4 6h16c0-4-4-6-4-6"/></svg>
        </div>
        <h2 class="ai-card-title" style="opacity:0.6;">AI 智能分析</h2>
      </div>
      <div class="ai-card-body ai-empty-hint">AI 分析未生成</div>
    </section>"""

    # ── 完整热榜（按平台 Tab 切换） ──
    platform_news: Dict[str, List[Dict[str, Any]]] = {}
    for item in news_data["all_news"]:
        pname = item["platform_name"]
        platform_news.setdefault(pname, []).append(item)

    # 按新闻数量排序平台
    sorted_platform_names = sorted(
        platform_news.keys(),
        key=lambda p: len(platform_news[p]),
        reverse=True,
    )

    tab_buttons = []
    tab_panels = []
    for pidx, pname in enumerate(sorted_platform_names):
        pitems = sorted(platform_news[pname], key=lambda x: x["rank"])
        active = " active" if pidx == 0 else ""
        tab_buttons.append(
            f'<button class="tab-btn{active}" data-tab="tab-{pidx}">{html_escape(pname)} '
            f'<span class="tab-count">{len(pitems)}</span></button>'
        )
        cards = "".join(_render_news_card(it) for it in pitems[:100])
        tab_panels.append(
            f'<div class="tab-panel{active}" id="tab-{pidx}"><div class="news-list">{cards}</div></div>'
        )

    hotlist_html = f"""
    <section id="sec-hotlist" class="hotlist-section">
      <div class="section-title-row">
        <h2 class="section-title">完整热榜</h2>
        <button class="collapse-btn" onclick="toggleHotlist(this)" aria-label="折叠/展开">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
      </div>
      <div class="hotlist-content">
        <div class="tab-bar">{"".join(tab_buttons)}</div>
        <div class="tab-panels">{"".join(tab_panels)}</div>
      </div>
    </section>"""

    # ── 图表数据 JSON ──
    chart_data_json = json.dumps({
        "pie": chart_pie_data,
        "platforms": [{"name": n, "value": v} for n, v in platform_bar_data],
        "hottest": hottest_data,
        "categoryColors": CATEGORY_PALETTE,
    }, ensure_ascii=False)

    # ── 组装完整 HTML ──
    return _HTML_TEMPLATE.replace("__ECHARTS_CDN__", ECHARTS_CDN) \
        .replace("__TITLE__", "TrendRadar 情报仪表盘") \
        .replace("__DATE__", date_str) \
        .replace("__TIME__", time_str) \
        .replace("__HISTORY_OPTIONS__", history_options) \
        .replace("__REL_PREFIX__", rel_prefix) \
        .replace("__SUMMARY_CARDS__", summary_cards) \
        .replace("__PILLS__", pills_html) \
        .replace("__AI_SECTION__", ai_section) \
        .replace("__CATEGORIES__", categories_html) \
        .replace("__RSS_SECTION__", rss_html) \
        .replace("__HOTLIST__", hotlist_html) \
        .replace("__CHART_DATA__", chart_data_json) \
        .replace("__FOOTER_TIME__", now_display())


# ═══════════════════════════════════════════════════════════════
#  HTML / CSS / JS 模板
# ═══════════════════════════════════════════════════════════════

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<script src="__ECHARTS_CDN__"></script>
<style>
/* ═══════════════════════════════════════════════════════════
   CSS 变量与主题
   ═══════════════════════════════════════════════════════════ */
:root {
  --bg: #f5f7fa;
  --card-bg: #ffffff;
  --text: #1a1a2e;
  --text-secondary: #5a5a7a;
  --text-muted: #9999b0;
  --border: #e8eaf0;
  --accent: #1677ff;
  --accent-light: #e6f4ff;
  --shadow: 0 2px 12px rgba(0,0,0,0.06);
  --shadow-hover: 0 4px 20px rgba(0,0,0,0.1);
  --radius: 12px;
  --rank-bg: #f0f2f5;
  --rank-text: #999;
  --header-bg: linear-gradient(135deg, #1677ff 0%, #4096ff 100%);
  --pill-bg: #f0f2f5;
  --pill-active-bg: #1677ff;
  --pill-active-text: #fff;
}
[data-theme="dark"] {
  --bg: #0f0f1a;
  --card-bg: #1a1a2e;
  --text: #e8e8f0;
  --text-secondary: #a0a0c0;
  --text-muted: #6a6a8a;
  --border: #2a2a3e;
  --accent: #4096ff;
  --accent-light: #111d3a;
  --shadow: 0 2px 12px rgba(0,0,0,0.3);
  --shadow-hover: 0 4px 20px rgba(0,0,0,0.4);
  --rank-bg: #2a2a3e;
  --rank-text: #888;
  --header-bg: linear-gradient(135deg, #0f0f1a 0%, #1a1a3e 100%);
  --pill-bg: #2a2a3e;
  --pill-active-bg: #4096ff;
  --pill-active-text: #fff;
}

/* ═══════════════════════════════════════════════════════════
   基础重置
   ═══════════════════════════════════════════════════════════ */
* { margin: 0; padding: 0; box-sizing: border-box; }
html { scroll-behavior: smooth; scroll-padding-top: 80px; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
               "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue",
               Helvetica, Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  transition: background 0.3s, color 0.3s;
  min-height: 100vh;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ═══════════════════════════════════════════════════════════
   顶部导航栏
   ═══════════════════════════════════════════════════════════ */
.header {
  position: sticky; top: 0; z-index: 100;
  background: var(--header-bg);
  color: #fff;
  padding: 0 24px;
  height: 64px;
  display: flex; align-items: center; justify-content: space-between;
  box-shadow: 0 2px 12px rgba(0,0,0,0.15);
  backdrop-filter: blur(12px);
}
.header-left { display: flex; align-items: center; gap: 16px; }
.header-logo {
  width: 36px; height: 36px;
  background: rgba(255,255,255,0.2);
  border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
  font-size: 20px; font-weight: 700;
}
.header-title { font-size: 20px; font-weight: 700; letter-spacing: 0.5px; }
.header-subtitle { font-size: 12px; opacity: 0.8; margin-top: 2px; }
.header-right { display: flex; align-items: center; gap: 12px; }
.history-select {
  padding: 8px 12px;
  border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.3);
  background: rgba(255,255,255,0.15);
  color: #fff;
  font-size: 13px;
  cursor: pointer;
  outline: none;
  backdrop-filter: blur(8px);
  transition: background 0.2s;
}
.history-select:hover { background: rgba(255,255,255,0.25); }
.history-select option { color: #333; background: #fff; }
.theme-toggle {
  width: 40px; height: 40px;
  border-radius: 10px;
  border: 1px solid rgba(255,255,255,0.3);
  background: rgba(255,255,255,0.15);
  color: #fff;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: all 0.2s;
  backdrop-filter: blur(8px);
}
.theme-toggle:hover { background: rgba(255,255,255,0.25); transform: rotate(15deg); }

/* ═══════════════════════════════════════════════════════════
   主内容区
   ═══════════════════════════════════════════════════════════ */
.main {
  max-width: 1280px;
  margin: 0 auto;
  padding: 24px 16px 40px;
}

/* ═══════════════════════════════════════════════════════════
   摘要卡片
   ═══════════════════════════════════════════════════════════ */
.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 24px;
}
.summary-card {
  background: var(--card-bg);
  border-radius: var(--radius);
  padding: 20px;
  display: flex; align-items: center; gap: 16px;
  box-shadow: var(--shadow);
  transition: all 0.3s ease;
  border: 1px solid var(--border);
}
.summary-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--shadow-hover);
}
.summary-icon {
  width: 48px; height: 48px;
  border-radius: 12px;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.summary-value { font-size: 28px; font-weight: 700; line-height: 1.2; }
.summary-label { font-size: 13px; color: var(--text-secondary); margin-top: 4px; }

/* ═══════════════════════════════════════════════════════════
   图表区域
   ═══════════════════════════════════════════════════════════ */
.charts-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 24px;
}
.chart-card {
  background: var(--card-bg);
  border-radius: var(--radius);
  padding: 20px;
  box-shadow: var(--shadow);
  border: 1px solid var(--border);
}
.chart-card.full-width { grid-column: 1 / -1; }
.chart-title {
  font-size: 15px; font-weight: 600;
  margin-bottom: 12px;
  color: var(--text);
}
.chart-container { width: 100%; height: 320px; }

/* ═══════════════════════════════════════════════════════════
   AI 分析卡片
   ═══════════════════════════════════════════════════════════ */
.ai-dashboard-card {
  background: linear-gradient(135deg, #667eea15 0%, #764ba215 100%);
  border: 1px solid #667eea30;
  border-radius: var(--radius);
  padding: 24px;
  margin-bottom: 24px;
  transition: all 0.3s;
}
.ai-dashboard-card:hover { box-shadow: var(--shadow-hover); }
.ai-card-header { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
.ai-card-icon {
  width: 40px; height: 40px;
  border-radius: 10px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: #fff;
  display: flex; align-items: center; justify-content: center;
}
.ai-card-title { font-size: 18px; font-weight: 700; }
.ai-card-body { font-size: 14px; line-height: 1.8; }
.ai-empty-hint { color: var(--text-muted); font-style: italic; padding: 8px 0; }

/* TrendRadar 原始 AI 区块样式兼容 */
.ai-section {
  background: var(--card-bg);
  border-radius: var(--radius);
  padding: 20px;
  border: 1px solid var(--border);
}
.ai-section-header { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
.ai-section-title { font-size: 16px; font-weight: 700; }
.ai-section-badge {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: #fff;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  font-weight: 600;
}
.ai-blocks-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
}
.ai-block {
  background: var(--bg);
  border-radius: 10px;
  padding: 16px;
  border: 1px solid var(--border);
}
.ai-block-title {
  font-size: 14px; font-weight: 600;
  margin-bottom: 8px;
  color: var(--accent);
}
.ai-block-content { font-size: 13px; color: var(--text-secondary); line-height: 1.8; }
.ai-warning { color: #f5222d; padding: 12px; background: #fff1f0; border-radius: 8px; }
.ai-info { color: var(--text-secondary); padding: 12px; }
[data-theme="dark"] .ai-warning { background: #2a1215; color: #ff7875; }

/* ═══════════════════════════════════════════════════════════
   分类导航 Pills
   ═══════════════════════════════════════════════════════════ */
.pills-bar {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding: 4px 0 16px;
  margin-bottom: 8px;
  scrollbar-width: thin;
  -webkit-overflow-scrolling: touch;
}
.pills-bar::-webkit-scrollbar { height: 4px; }
.pills-bar::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
.pill {
  flex-shrink: 0;
  padding: 8px 16px;
  border-radius: 20px;
  border: 1px solid var(--border);
  background: var(--card-bg);
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
  transition: all 0.25s ease;
  display: inline-flex; align-items: center; gap: 6px;
}
.pill:hover { border-color: var(--accent); color: var(--accent); }
.pill.active {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}
.pill-count {
  background: rgba(0,0,0,0.08);
  padding: 1px 7px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
}
.pill.active .pill-count { background: rgba(255,255,255,0.25); }

/* ═══════════════════════════════════════════════════════════
   分类区域与新闻卡片
   ═══════════════════════════════════════════════════════════ */
.category-section {
  margin-bottom: 28px;
  padding-left: 12px;
  border-left: 3px solid var(--cat-color, var(--accent));
}
.category-header {
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 14px;
}
.category-dot { width: 10px; height: 10px; border-radius: 50%; }
.category-title { font-size: 18px; font-weight: 700; }
.category-count {
  background: var(--pill-bg);
  color: var(--text-secondary);
  font-size: 12px;
  padding: 2px 10px;
  border-radius: 10px;
  font-weight: 600;
}
.news-list { display: flex; flex-direction: column; gap: 10px; }
.news-card {
  background: var(--card-bg);
  border-radius: 10px;
  padding: 14px 16px;
  display: flex; gap: 14px;
  box-shadow: var(--shadow);
  border: 1px solid var(--border);
  transition: all 0.25s ease;
}
.news-card:hover {
  transform: translateX(4px);
  box-shadow: var(--shadow-hover);
  border-color: var(--accent);
}
.news-rank {
  width: 32px; height: 32px;
  border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  font-size: 14px; font-weight: 600;
  flex-shrink: 0;
}
.news-body { flex: 1; min-width: 0; }
.news-title-row {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 6px;
}
.news-title {
  font-size: 14px;
  font-weight: 500;
  color: var(--text);
  line-height: 1.5;
}
a.news-title:hover { color: var(--accent); text-decoration: none; }
.badge-new {
  background: #f5222d;
  color: #fff;
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 700;
  flex-shrink: 0;
  animation: pulse 2s infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}
.news-meta { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.source-tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 4px;
  font-weight: 500;
}
.meta-time, .meta-crawl {
  font-size: 12px;
  color: var(--text-muted);
}
.meta-crawl::before { content: "🔥 "; }
.rss-summary {
  font-size: 13px;
  color: var(--text-secondary);
  margin-bottom: 6px;
  line-height: 1.5;
}
.more-hint {
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
  padding: 8px;
}

/* ═══════════════════════════════════════════════════════════
   RSS 区域
   ═══════════════════════════════════════════════════════════ */
.rss-section { margin-bottom: 28px; }
.rss-feed-group { margin-bottom: 20px; }
.rss-feed-title {
  font-size: 15px; font-weight: 600;
  margin-bottom: 10px;
  display: flex; align-items: center; gap: 8px;
}
.rss-card .news-rank { display: none; }

/* ═══════════════════════════════════════════════════════════
   完整热榜（Tab 切换）
   ═══════════════════════════════════════════════════════════ */
.hotlist-section {
  margin-bottom: 28px;
  background: var(--card-bg);
  border-radius: var(--radius);
  padding: 20px;
  box-shadow: var(--shadow);
  border: 1px solid var(--border);
}
.section-title-row {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 16px;
}
.section-title { font-size: 18px; font-weight: 700; }
.collapse-btn {
  background: none; border: none;
  color: var(--text-secondary);
  cursor: pointer;
  padding: 6px;
  border-radius: 6px;
  display: flex; align-items: center;
  transition: all 0.2s;
}
.collapse-btn:hover { background: var(--pill-bg); }
.collapse-btn.collapsed svg { transform: rotate(-90deg); }
.collapse-btn svg { transition: transform 0.3s; }
.hotlist-content.collapsed { display: none; }
.tab-bar {
  display: flex; gap: 6px;
  overflow-x: auto;
  padding-bottom: 8px;
  margin-bottom: 16px;
  border-bottom: 1px solid var(--border);
  scrollbar-width: thin;
}
.tab-btn {
  flex-shrink: 0;
  padding: 8px 14px;
  border: none;
  background: none;
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  border-radius: 8px 8px 0 0;
  transition: all 0.2s;
  white-space: nowrap;
  display: inline-flex; align-items: center; gap: 6px;
}
.tab-btn:hover { color: var(--accent); background: var(--accent-light); }
.tab-btn.active {
  color: var(--accent);
  border-bottom: 2px solid var(--accent);
  font-weight: 600;
}
.tab-count {
  background: var(--pill-bg);
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 10px;
}
.tab-btn.active .tab-count { background: var(--accent-light); color: var(--accent); }
.tab-panel { display: none; }
.tab-panel.active { display: block; }

/* ═══════════════════════════════════════════════════════════
   页脚
   ═══════════════════════════════════════════════════════════ */
.footer {
  text-align: center;
  padding: 24px 16px;
  color: var(--text-muted);
  font-size: 13px;
  border-top: 1px solid var(--border);
  margin-top: 20px;
}

/* ═══════════════════════════════════════════════════════════
   回到顶部按钮
   ═══════════════════════════════════════════════════════════ */
.back-to-top {
  position: fixed;
  bottom: 32px; right: 32px;
  width: 44px; height: 44px;
  border-radius: 50%;
  background: var(--accent);
  color: #fff;
  border: none;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 4px 16px rgba(22,119,255,0.4);
  opacity: 0;
  visibility: hidden;
  transform: translateY(10px);
  transition: all 0.3s ease;
  z-index: 99;
}
.back-to-top.visible {
  opacity: 1; visibility: visible; transform: translateY(0);
}
.back-to-top:hover { transform: translateY(-3px); box-shadow: 0 6px 20px rgba(22,119,255,0.5); }

/* ═══════════════════════════════════════════════════════════
   响应式布局
   ═══════════════════════════════════════════════════════════ */
@media (max-width: 1024px) {
  .summary-grid { grid-template-columns: repeat(2, 1fr); }
  .charts-grid { grid-template-columns: 1fr; }
}
@media (max-width: 640px) {
  .header { padding: 0 12px; height: 56px; }
  .header-title { font-size: 16px; }
  .header-subtitle { display: none; }
  .header-logo { width: 32px; height: 32px; font-size: 16px; }
  .main { padding: 16px 10px 32px; }
  .summary-grid { grid-template-columns: repeat(2, 1fr); gap: 10px; }
  .summary-card { padding: 14px; gap: 10px; }
  .summary-icon { width: 40px; height: 40px; }
  .summary-value { font-size: 22px; }
  .summary-label { font-size: 12px; }
  .chart-container { height: 260px; }
  .news-card { padding: 10px 12px; gap: 10px; }
  .news-rank { width: 28px; height: 28px; font-size: 12px; }
  .news-title { font-size: 13px; }
  .category-title { font-size: 16px; }
  .section-title { font-size: 16px; }
  .ai-blocks-grid { grid-template-columns: 1fr; }
  .back-to-top { bottom: 20px; right: 20px; width: 40px; height: 40px; }
  .history-select { max-width: 120px; font-size: 12px; }
}
</style>
</head>
<body>

<!-- ═══════════ 顶部导航栏 ═══════════ -->
<header class="header">
  <div class="header-left">
    <div class="header-logo">T</div>
    <div>
      <div class="header-title">__TITLE__</div>
      <div class="header-subtitle">__DATE__ __TIME__</div>
    </div>
  </div>
  <div class="header-right">
    <select class="history-select" onchange="navigateHistory(this)" aria-label="历史报告">
      __HISTORY_OPTIONS__
    </select>
    <button class="theme-toggle" onclick="toggleTheme()" aria-label="切换明暗主题" title="切换明暗主题">
      <svg id="theme-icon-sun" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:none;"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
      <svg id="theme-icon-moon" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
    </button>
  </div>
</header>

<!-- ═══════════ 主内容 ═══════════ -->
<main class="main" id="sec-overview">

  <!-- 摘要卡片 -->
  __SUMMARY_CARDS__

  <!-- 图表区域 -->
  <div class="charts-grid">
    <div class="chart-card">
      <div class="chart-title">分类分布</div>
      <div class="chart-container" id="chart-pie"></div>
    </div>
    <div class="chart-card">
      <div class="chart-title">平台分布 Top 10</div>
      <div class="chart-container" id="chart-platform"></div>
    </div>
    <div class="chart-card full-width">
      <div class="chart-title">热度排行 Top 10</div>
      <div class="chart-container" id="chart-hot" style="height:380px;"></div>
    </div>
  </div>

  <!-- AI 分析 -->
  __AI_SECTION__

  <!-- 分类导航 -->
  <nav class="pills-bar" id="pills-bar">
    __PILLS__
  </nav>

  <!-- 分类新闻 -->
  __CATEGORIES__

  <!-- RSS 资讯 -->
  <section id="sec-rss" class="rss-section">
    <div class="category-header">
      <div class="category-dot" style="background:#52c41a;"></div>
      <h2 class="category-title">RSS 资讯</h2>
    </div>
    __RSS_SECTION__
  </section>

  <!-- 完整热榜 -->
  __HOTLIST__

</main>

<!-- ═══════════ 页脚 ═══════════ -->
<footer class="footer">
  由 TrendRadar 自动生成 · __FOOTER_TIME__
</footer>

<!-- ═══════════ 回到顶部 ═══════════ -->
<button class="back-to-top" id="backToTop" onclick="scrollToTop()" aria-label="回到顶部">
  <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg>
</button>

<!-- ═══════════ 图表数据 ═══════════ -->
<script>
window.DASHBOARD_DATA = __CHART_DATA__;
</script>

<!-- ═══════════ 交互脚本 ═══════════ -->
<script>
(function() {
  'use strict';

  var data = window.DASHBOARD_DATA || {};
  var charts = {};

  // ── 主题管理 ──
  function getTheme() {
    return localStorage.getItem('tr-theme') || 'light';
  }
  function setTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('tr-theme', theme);
    updateThemeIcon(theme);
    // 重新渲染图表以应用主题色
    setTimeout(renderCharts, 50);
  }
  function updateThemeIcon(theme) {
    document.getElementById('theme-icon-sun').style.display = theme === 'dark' ? 'block' : 'none';
    document.getElementById('theme-icon-moon').style.display = theme === 'dark' ? 'none' : 'block';
  }
  window.toggleTheme = function() {
    var current = getTheme();
    setTheme(current === 'dark' ? 'light' : 'dark');
  };

  // ── 历史报告导航 ──
  window.navigateHistory = function(sel) {
    if (sel.value) {
      window.location.href = sel.value;
    }
  };

  // ── 回到顶部 ──
  window.scrollToTop = function() {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };
  window.addEventListener('scroll', function() {
    var btn = document.getElementById('backToTop');
    if (window.pageYOffset > 400) {
      btn.classList.add('visible');
    } else {
      btn.classList.remove('visible');
    }
  });

  // ── 折叠完整热榜 ──
  window.toggleHotlist = function(btn) {
    var content = btn.closest('.hotlist-section').querySelector('.hotlist-content');
    btn.classList.toggle('collapsed');
    content.classList.toggle('collapsed');
  };

  // ── Pill 导航 ──
  document.querySelectorAll('.pill').forEach(function(pill) {
    pill.addEventListener('click', function() {
      var target = document.getElementById(pill.dataset.target);
      if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
  });

  // 滚动时高亮当前 pill
  var sections = [];
  document.querySelectorAll('.pill').forEach(function(pill) {
    var el = document.getElementById(pill.dataset.target);
    if (el) sections.push({ el: el, pill: pill });
  });
  window.addEventListener('scroll', function() {
    var scrollPos = window.pageYOffset + 120;
    var current = sections[0];
    sections.forEach(function(s) {
      if (s.el.offsetTop <= scrollPos) current = s;
    });
    if (current) {
      document.querySelectorAll('.pill').forEach(function(p) { p.classList.remove('active'); });
      current.pill.classList.add('active');
    }
  });

  // ── Tab 切换 ──
  document.querySelectorAll('.tab-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var tabId = btn.dataset.tab;
      btn.closest('.hotlist-section').querySelectorAll('.tab-btn').forEach(function(b) {
        b.classList.remove('active');
      });
      btn.closest('.hotlist-section').querySelectorAll('.tab-panel').forEach(function(p) {
        p.classList.remove('active');
      });
      btn.classList.add('active');
      document.getElementById(tabId).classList.add('active');
      // 窗口变化时重绘图表
      setTimeout(function() { Object.values(charts).forEach(function(c) { if (c) c.resize(); }); }, 100);
    });
  });

  // ── ECharts 渲染 ──
  function isDark() {
    return document.documentElement.getAttribute('data-theme') === 'dark';
  }
  function chartTextColor() {
    return isDark() ? '#a0a0c0' : '#666';
  }
  function chartAxisLineColor() {
    return isDark() ? '#2a2a3e' : '#e8eaf0';
  }
  function chartSplitLineColor() {
    return isDark() ? '#1e1e30' : '#f0f2f5';
  }
  function chartTooltipBg() {
    return isDark() ? '#1a1a2e' : '#fff';
  }

  function renderPieChart() {
    var el = document.getElementById('chart-pie');
    if (!el || typeof echarts === 'undefined') return;
    if (!charts.pie) charts.pie = echarts.init(el);
    var colors = (data.categoryColors || []).slice(0, 8);
    charts.pie.setOption({
      tooltip: {
        trigger: 'item',
        backgroundColor: chartTooltipBg(),
        borderColor: chartAxisLineColor(),
        textStyle: { color: chartTextColor() }
      },
      legend: {
        bottom: 0,
        textStyle: { color: chartTextColor(), fontSize: 11 },
        type: 'scroll'
      },
      color: colors.concat(['#999']),
      series: [{
        type: 'pie',
        radius: ['42%', '70%'],
        center: ['50%', '42%'],
        avoidLabelOverlap: true,
        itemStyle: { borderRadius: 6, borderColor: isDark() ? '#1a1a2e' : '#fff', borderWidth: 2 },
        label: { show: false },
        emphasis: {
          label: { show: true, fontSize: 14, fontWeight: 'bold', color: chartTextColor() }
        },
        data: data.pie || []
      }]
    });
  }

  function renderPlatformChart() {
    var el = document.getElementById('chart-platform');
    if (!el || typeof echarts === 'undefined') return;
    if (!charts.platform) charts.platform = echarts.init(el);
    var pData = data.platforms || [];
    var names = pData.map(function(d) { return d.name; }).reverse();
    var values = pData.map(function(d) { return d.value; }).reverse();
    charts.platform.setOption({
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        backgroundColor: chartTooltipBg(),
        borderColor: chartAxisLineColor(),
        textStyle: { color: chartTextColor() }
      },
      grid: { left: 100, right: 24, top: 10, bottom: 20 },
      xAxis: {
        type: 'value',
        axisLine: { lineStyle: { color: chartAxisLineColor() } },
        axisLabel: { color: chartTextColor(), fontSize: 11 },
        splitLine: { lineStyle: { color: chartSplitLineColor() } }
      },
      yAxis: {
        type: 'category',
        data: names,
        axisLine: { lineStyle: { color: chartAxisLineColor() } },
        axisLabel: { color: chartTextColor(), fontSize: 11 }
      },
      series: [{
        type: 'bar',
        data: values,
        barWidth: '60%',
        itemStyle: {
          borderRadius: [0, 4, 4, 0],
          color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
            { offset: 0, color: '#1677ff' },
            { offset: 1, color: '#69b1ff' }
          ])
        },
        label: {
          show: true,
          position: 'right',
          color: chartTextColor(),
          fontSize: 11
        }
      }]
    });
  }

  function renderHotChart() {
    var el = document.getElementById('chart-hot');
    if (!el || typeof echarts === 'undefined') return;
    if (!charts.hot) charts.hot = echarts.init(el);
    var hData = (data.hottest || []).slice().reverse();
    var names = hData.map(function(d) { return d.title; });
    var values = hData.map(function(d) { return d.weight; });
    var urls = hData.map(function(d) { return d.url; });
    charts.hot.setOption({
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        backgroundColor: chartTooltipBg(),
        borderColor: chartAxisLineColor(),
        textStyle: { color: chartTextColor() },
        formatter: function(params) {
          var p = params[0];
          var idx = p.dataIndex;
          var item = hData[idx];
          var html = '<b>' + (item.full_title || item.name) + '</b><br/>';
          html += '平台: ' + (item.platform || '') + '<br/>';
          html += '热度: ' + p.value;
          return html;
        }
      },
      grid: { left: 10, right: 40, top: 10, bottom: 20, containLabel: true },
      xAxis: {
        type: 'value',
        axisLine: { lineStyle: { color: chartAxisLineColor() } },
        axisLabel: { color: chartTextColor(), fontSize: 11 },
        splitLine: { lineStyle: { color: chartSplitLineColor() } }
      },
      yAxis: {
        type: 'category',
        data: names,
        axisLine: { lineStyle: { color: chartAxisLineColor() } },
        axisLabel: {
          color: chartTextColor(),
          fontSize: 12,
          width: 160,
          overflow: 'truncate'
        }
      },
      series: [{
        type: 'bar',
        data: values.map(function(v, i) {
          return {
            value: v,
            itemStyle: {
              borderRadius: [0, 4, 4, 0],
              color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
                { offset: 0, color: i >= values.length - 3 ? '#fa8c16' : '#722ed1' },
                { offset: 1, color: i >= values.length - 3 ? '#ffc069' : '#b37feb' }
              ])
            }
          };
        }),
        barWidth: '55%',
        label: {
          show: true,
          position: 'right',
          color: chartTextColor(),
          fontSize: 11,
          formatter: '{c}'
        }
      }]
    });
    // 点击跳转
    charts.hot.off('click');
    charts.hot.on('click', function(params) {
      var url = urls[params.dataIndex];
      if (url) window.open(url, '_blank');
    });
  }

  function renderCharts() {
    renderPieChart();
    renderPlatformChart();
    renderHotChart();
  }

  // 窗口大小变化时重绘
  var resizeTimer;
  window.addEventListener('resize', function() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function() {
      Object.values(charts).forEach(function(c) { if (c) c.resize(); });
    }, 200);
  });

  // ── 初始化 ──
  setTheme(getTheme());
  if (typeof echarts !== 'undefined') {
    renderCharts();
  } else {
    // ECharts 加载失败时显示提示
    console.warn('ECharts 未能加载，图表区域将不可用');
  }
})();
</script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════
#  主函数
# ═══════════════════════════════════════════════════════════════

def main() -> int:
    """脚本入口。"""
    print("=" * 60)
    print("  TrendRadar 情报仪表盘生成器")
    print("=" * 60)

    # 确定根目录（脚本所在目录，兼容从任意位置运行）
    root_dir = Path(__file__).resolve().parent
    # 如果脚本所在目录没有 config，尝试当前工作目录
    if not (root_dir / "config").exists():
        root_dir = Path.cwd()
    print(f"  工作目录: {root_dir}")

    date_str = today_str()
    time_str = now_display()
    print(f"  报告日期: {date_str}（Asia/Shanghai）")

    # ── 1. 读取频率词配置 ──
    print("\n[1/6] 解析频率词配置...")
    freq_path = root_dir / "config" / "frequency_words.txt"
    groups, global_filters = parse_frequency_words(freq_path)
    print(f"  共加载 {len(groups)} 个词组，{len(global_filters)} 个全局过滤词")

    # ── 2. 读取新闻数据库 ──
    print("\n[2/6] 读取热榜新闻数据库...")
    news_db_path = root_dir / "output" / "news" / f"{date_str}.db"
    news_items, platform_map, latest_crawl = load_news_db(news_db_path)

    # ── 3. 读取 RSS 数据库 ──
    print("\n[3/6] 读取 RSS 数据库...")
    rss_db_path = root_dir / "output" / "rss" / f"{date_str}.db"
    rss_items, feed_map = load_rss_db(rss_db_path)

    # ── 4. 提取 AI 分析 ──
    print("\n[4/6] 提取 AI 分析内容...")
    index_html_path = root_dir / "index.html"
    ai_html = extract_ai_analysis(index_html_path)

    # ── 5. 数据处理 ──
    print("\n[5/6] 处理数据与分类...")
    news_data = process_news(
        news_items, platform_map, groups, global_filters, latest_crawl
    )
    rss_grouped = process_rss(rss_items, feed_map)
    print(f"  新闻总数: {news_data['total']}")
    print(f"  分类命中: {news_data['hit_count']}")
    print(f"  新增热点: {news_data['new_count']}")
    print(f"  RSS 资讯: {sum(len(v) for v in rss_grouped.values())}")

    # ── 6. 生成 HTML ──
    print("\n[6/6] 生成 HTML 仪表盘...")

    # 确保输出目录存在
    site_path = root_dir / SITE_DIR
    reports_path = root_dir / REPORTS_DIR
    site_path.mkdir(parents=True, exist_ok=True)
    reports_path.mkdir(parents=True, exist_ok=True)

    # 扫描历史报告（在写入新报告之前）
    history_reports = scan_history_reports()
    print(f"  发现 {len(history_reports)} 份历史报告")

    # 生成主页 HTML
    html_main = build_html(
        news_data=news_data,
        rss_grouped=rss_grouped,
        ai_html=ai_html,
        history_reports=history_reports,
        date_str=date_str,
        time_str=time_str,
        rel_prefix="",
    )

    # 写入 _site/index.html
    index_output = site_path / "index.html"
    with open(index_output, "w", encoding="utf-8") as f:
        f.write(html_main)
    print(f"  已写入: {index_output}")

    # 写入归档副本 _site/reports/YYYY-MM-DD-HHMM.html
    report_filename = f"{timestamp_label()}.html"
    report_output = reports_path / report_filename

    # 归档页面使用相对路径前缀 "../"
    html_report = build_html(
        news_data=news_data,
        rss_grouped=rss_grouped,
        ai_html=ai_html,
        history_reports=history_reports,
        date_str=date_str,
        time_str=time_str,
        rel_prefix="../",
    )
    with open(report_output, "w", encoding="utf-8") as f:
        f.write(html_report)
    print(f"  已归档: {report_output}")

    # 清理旧报告
    cleanup_old_reports()

    print("\n" + "=" * 60)
    print("  仪表盘生成完成！")
    print(f"  主页: {index_output}")
    print(f"  归档: {report_output}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
