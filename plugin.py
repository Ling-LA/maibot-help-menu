"""指令菜单插件 v1.0 | ling_help-menu

Auto-discovers all enabled plugins' commands and presents them as a
beautiful image-based hierarchical menu. Supports AI-assisted HTML
layout via Bailian API, with Pillow fallback.

Commands:
    /帮助 /菜单 /help /功能          → Level 1: main menu（所有有命令的插件列表）
    /帮助 <number> /菜单 <number>    → Level 2: 指定插件的命令详情（按菜单序号）
    /<trigger>帮助 /<trigger>菜单    → Level 2: 通过插件触发词直达
    /帮助刷新 /菜单刷新              → 强制刷新菜单缓存
"""

from __future__ import annotations

from collections import OrderedDict
from html import unescape
from pathlib import Path
from typing import Any

import asyncio
import base64
import hashlib
import io
import json
import re
import textwrap
import time

import aiohttp
from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None  # type: ignore[assignment]

# ─── paths & constants ─────────────────────────────────────────

PLUGIN_DIR = Path(__file__).parent
TEMP_DIR = PLUGIN_DIR / "temp"
CACHE_FILE = PLUGIN_DIR / "menu_cache.json"
CACHE_LAYOUT_VERSION = "2"

PLUGIN_ID_SELF = "ling.help-menu"

# ─── global state ──────────────────────────────────────────────

_menu_tree: list[dict[str, Any]] = []
"""Cached menu tree: list of plugin entries, each containing index, plugin_id,
plugin_name, description, commands (list of {name, trigger, description}).
"""

_menu_plugin_hash: str = ""
"""SHA-256 hash of sorted plugin id list used to detect plugin list changes."""

_menu_last_refresh: float = 0.0
"""Unix timestamp of last menu discovery."""

_discovery_lock = asyncio.Lock()
"""Prevent concurrent discovery calls."""

# ─── PIL colours (shared fallback palette) ─────────────────────

COLOR_BG = (255, 245, 250)          # light pink
COLOR_HEADER = (255, 126, 182)      # primary pink
COLOR_HEADER_END = (255, 160, 200)  # gradient end
COLOR_CELL_A = (255, 255, 255)      # white
COLOR_CELL_B = (255, 240, 245)      # pale pink
COLOR_TEXT_DARK = (80, 60, 70)      # dark mauve
COLOR_TEXT_MID = (150, 120, 140)    # mid mauve
COLOR_TEXT_LIGHT = (255, 255, 255)  # white
COLOR_DIVIDER = (220, 190, 200)     # soft divider
COLOR_ACCENT = (255, 200, 220)      # accent pink
COLOR_TAG_BG = (255, 126, 182)      # tag background


# ═══════════════════════════════════════════════════════════════
#  Config models
# ═══════════════════════════════════════════════════════════════

class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="启用插件")
    config_version: str = Field(default="1.1.0", description="配置版本")
    ai_layout_enabled: bool = Field(default=True, description="启用AI辅助布局")
    ai_model: str = Field(default="qwen-plus", description="AI模型名称")
    ai_api_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="AI API地址",
    )
    ai_api_key: str = Field(
        default="sk-your-api-key-here",
        description="AI API密钥",
    )
    cache_ttl_minutes: int = Field(default=60, description="缓存有效期（分钟）")


class HelpMenuConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)


# ═══════════════════════════════════════════════════════════════
#  Font helpers
# ═══════════════════════════════════════════════════════════════

def _load_fonts() -> tuple[Any, Any, Any, Any, Any]:
    """Return (title, normal, small, xs, mono) PIL font objects.

    Tries common Windows CJK font paths first, then falls back to default.
    """
    font_paths = [
        "C:/Windows/Fonts/msyh.ttc",     # Microsoft YaHei
        "C:/Windows/Fonts/msyhbd.ttc",   # Microsoft YaHei Bold
        "C:/Windows/Fonts/simhei.ttf",   # SimHei
        "C:/Windows/Fonts/simsun.ttc",   # SimSun
    ]

    def _try_load(size: int) -> Any:
        for fp in font_paths:
            try:
                return ImageFont.truetype(fp, size)
            except (OSError, IOError):
                continue
        return ImageFont.load_default()

    return (
        _try_load(20),   # title
        _try_load(14),   # normal
        _try_load(12),   # small
        _try_load(10),   # xs
        _try_load(13),   # mono (for triggers)
    )


# ═══════════════════════════════════════════════════════════════
#  Help texts
# ═══════════════════════════════════════════════════════════════

def _help_text(plugin_count: int = 0) -> str:
    return (
        "🌸 **指令菜单** | Help Menu\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "/帮助 /菜单 /help            主菜单（所有插件）\n"
        "/帮助 <序号>                  查看插件详情\n"
        "/<命令>帮助 /<命令>菜单       直接查看插件命令\n"
        "/帮助刷新 /菜单刷新            刷新菜单缓存\n"
        + (f"\n📋 已发现 **{plugin_count}** 个插件提供命令" if plugin_count else "")
    )


# ═══════════════════════════════════════════════════════════════
#  Menu discovery
# ═══════════════════════════════════════════════════════════════

def _plugin_id_hash(plugins: list[dict[str, Any]]) -> str:
    """Compute a stable hash from sorted plugin ids."""
    ids = sorted(p.get("id", "") for p in plugins if p.get("id"))
    return hashlib.sha256("|".join(ids).encode()).hexdigest()


def _split_regex_alternatives(value: str) -> list[str]:
    """拆分不含嵌套组的正则分支，保留转义后的竖线。"""
    alternatives: list[str] = []
    current: list[str] = []
    depth = 0
    escaped = False
    for char in value:
        if escaped:
            current.extend(("\\", char))
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            current.append(char)
        elif char == "|" and depth == 0:
            alternatives.append("".join(current))
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    alternatives.append("".join(current))
    return alternatives


def _find_group_end(pattern: str, start: int) -> int:
    """返回正则分组的右括号位置，找不到时返回 -1。"""
    depth = 0
    escaped = False
    in_class = False
    for index in range(start, len(pattern)):
        char = pattern[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif not in_class and char == "(":
            depth += 1
        elif not in_class and char == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _literal_variants(expression: str) -> list[str]:
    """展开命令头部的简单分支、字符类和可选字面量。"""
    variants = [""]
    index = 0
    while index < len(expression):
        char = expression[index]
        if char == "\\":
            if expression.startswith((r"\s", r"\S", r"\d", r"\w"), index):
                break
            if index + 1 < len(expression):
                variants = [value + expression[index + 1] for value in variants]
                index += 2
                continue
            break
        if char == "[":
            end = expression.find("]", index + 1)
            if end < 0:
                break
            choices = [item for item in expression[index + 1:end] if item not in "^-"]
            if not choices:
                break
            variants = [value + choices[0].lower() for value in variants]
            index = end + 1
            continue
        if char == "(":
            end = _find_group_end(expression, index)
            if end < 0:
                break
            content = expression[index + 1:end]
            if content.startswith("?:"):
                content = content[2:]
            elif match := re.match(r"\?P<(?P<name>[A-Za-z_]\w*)>", content):
                placeholder = f"<{match.group('name')}>"
                variants = [value + placeholder for value in variants]
                index = end + 1
                continue
            elif content.startswith("?"):
                break
            choices = _split_regex_alternatives(content)
            literal_choices = [item for choice in choices for item in _literal_variants(choice)]
            if not literal_choices:
                break
            quantifier = expression[end + 1] if end + 1 < len(expression) else ""
            if quantifier in "?*":
                literal_choices.insert(0, "")
                end += 1
            variants = [value + choice for value in variants for choice in literal_choices]
            index = end + 1
            continue
        if char in " \\.^$|+*?{[@":
            break
        variants = [value + char for value in variants]
        index += 1
    return list(dict.fromkeys(value for value in variants if value))


def _parameter_suffix(pattern: str) -> str:
    """从命令正则提取命名及未命名参数，生成必填/可选用法提示。"""
    parameters: list[str] = []
    named_spans: list[tuple[int, int]] = []
    for match in re.finditer(r"\(\?P<(?P<name>[A-Za-z_]\w*)>", pattern):
        name = match.group("name")
        end = _find_group_end(pattern, match.start())
        if end < 0:
            continue
        named_spans.append((match.start(), end))
        prefix = pattern[max(0, match.start() - 16):match.start()]
        optional = (end + 1 < len(pattern) and pattern[end + 1] in "?*") or bool(
            re.search(r"\(\?:[^()]*$", prefix)
        )
        token = f"[{name}]" if optional else f"<{name}>"
        if token not in parameters:
            parameters.append(token)

    # 旧式插件常用未命名捕获组声明参数。仅处理首个空白匹配之后的叶子组，
    # 避免把命令头部的 `(帮助|菜单)` 之类分支误当作参数。
    parameter_zone = pattern.find(r"\s")
    generic_index = 0
    for match in re.finditer(r"(?<!\\)\((?!\?)", pattern):
        if parameter_zone < 0 or match.start() < parameter_zone:
            continue
        end = _find_group_end(pattern, match.start())
        if end < 0 or any(start <= match.start() <= span_end for start, span_end in named_spans):
            continue
        content = pattern[match.start() + 1:end]
        if re.search(r"(?<!\\)\((?!\?)", content):
            continue
        generic_index += 1
        label = f"参数{generic_index}"
        prefix = pattern[max(0, match.start() - 16):match.start()]
        optional = (end + 1 < len(pattern) and pattern[end + 1] in "?*") or bool(
            re.search(r"\(\?:[^()]*$", prefix)
        )
        token = f"[{label}]" if optional else f"<{label}>"
        parameters.append(token)
    return " " + " ".join(parameters) if parameters else ""


def _extract_command_usages(pattern: str, aliases: Any = None) -> list[str]:
    """从 Command 正则和 aliases 提取完整、稳定且去重后的命令用法。"""
    expression = str(pattern or "").strip().lstrip("^")
    expression = re.sub(r"^\\s[*+?]?", "", expression)
    expression = re.sub(r"^\(\?:\^\|\.\*\)", "", expression)
    has_slash = False
    slash_match = re.match(r"(?:\[/／\]|\[／/\]|[/／])", expression)
    if slash_match:
        has_slash = True
        expression = expression[slash_match.end():]
    else:
        embedded_matches = list(re.finditer(r"/(?![?*+])(?=[\w\u4e00-\u9fff])", expression))
        embedded = embedded_matches[-1] if embedded_matches else None
        if embedded:
            has_slash = True
            expression = expression[embedded.end():]

    suffix = _parameter_suffix(expression)
    usages: list[str] = []
    for head in _literal_variants(expression):
        head_suffix = suffix
        for placeholder in re.findall(r"<([A-Za-z_]\w*)>", head):
            head_suffix = head_suffix.replace(f" <{placeholder}>", "").replace(f" [{placeholder}]", "")
        usages.append(("/" if has_slash else "") + head + head_suffix)
    alias_values = aliases if isinstance(aliases, list) else []
    for alias in alias_values:
        value = str(alias or "").strip()
        if value and has_slash and not value.startswith(("/", "／")):
            value = "/" + value
        if value:
            usages.append(value)

    normalized: list[str] = []
    for usage in usages:
        value = re.sub(r"\s+", " ", usage).strip().replace("／", "/")
        if value and value not in normalized:
            normalized.append(value[:80])
    return normalized or [f"/{str(pattern or '?')[:30]}"]


def _extract_command_trigger(pattern: str) -> str:
    """兼容旧调用：返回正则解析出的第一条命令用法。"""
    return _extract_command_usages(pattern)[0]


async def _discover_commands(
    ctx: Any, logger: Any,
) -> list[dict[str, Any]]:
    """Query all registered plugins from the host and build the menu tree.

    Returns a list of plugin entries.  Each entry is:
        {
            "index": 1,
            "plugin_id": "ling.avatar-meme",
            "plugin_name": "QQ头像表情包",
            "description": "...",
            "commands": [
                {"name": "cmd_internal_name", "trigger": "/表情", "description": "..."},
                ...
            ],
        }
    """
    try:
        raw = await ctx.component.get_all_plugins()
        logger.info(f"[help-menu debug] get_all_plugins type={type(raw).__name__}")
    except Exception as exc:
        logger.warning(f"获取插件列表失败: {exc}")
        return []

    # Normalise to list
    if isinstance(raw, dict):
        if "plugins" in raw and isinstance(raw["plugins"], dict):
            plugins = list(raw["plugins"].values())
        else:
            plugins = list(raw.values())
    elif isinstance(raw, list):
        plugins = raw
    else:
        return []

    tree: list[dict[str, Any]] = []
    plugin_details: dict[str, dict[str, Any]] = {}

    # ── first pass: collect plugin IDs and basic info ──
    for plugin in plugins:
        if not isinstance(plugin, dict):
            continue
        pid = plugin.get("name", "")
        if pid and pid != PLUGIN_ID_SELF:
            # Store basic info for later enrichment
            plugin_details[pid] = plugin

    # ── enrich: SDK get_plugin_info primary, _manifest.json fallback ──
    for pid in list(plugin_details.keys()):
        try:
            info = await ctx.component.get_plugin_info(pid)
            if isinstance(info, dict) and info.get("name"):
                name = str(info["name"])
                # Only trust the SDK name if it looks like a real display name
                # (contains CJK, or doesn't look like a dotted plugin_id).
                if re.search(r'[一-鿿]', name) or not re.match(r'^[\w.-]+$', name):
                    plugin_details[pid]["_info"] = info
                    continue
        except Exception:
            pass

    # Fallback: scan sibling directories for plugins still missing names.
    # Only runs when SDK didn't provide a usable display name.
    def _has_display_name(p: dict) -> bool:
        i = p.get("_info")
        if not isinstance(i, dict):
            return False
        n = str(i.get("name", ""))
        return bool(re.search(r'[一-鿿]', n) or not re.match(r'^[\w.-]+$', n))

    missing = {pid for pid, p in plugin_details.items() if not _has_display_name(p)}
    if missing:
        plugins_root = PLUGIN_DIR.parent
        if plugins_root.exists():
            for entry in plugins_root.iterdir():
                if not entry.is_dir():
                    continue
                mf_path = entry / "_manifest.json"
                if not mf_path.exists():
                    continue
                try:
                    mf = json.loads(mf_path.read_text(encoding="utf-8"))
                    if isinstance(mf, dict):
                        mf_pid = mf.get("id", "")
                        if mf_pid in missing and mf.get("name"):
                            plugin_details[mf_pid]["_info"] = mf
                            missing.discard(mf_pid)
                            if not missing:
                                break
                except Exception:
                    pass
            logger.info(
                f"[help-menu] SDK enriched {len(plugin_details) - len(missing)}/"
                f"{len(plugin_details)} plugins, manifest fallback for {len(missing)}"
            )

    for plugin in plugins:
        if not isinstance(plugin, dict):
            continue
        pid = plugin.get("name", "")
        if pid == PLUGIN_ID_SELF:
            continue  # skip self

        components = plugin.get("components", [])
        if not isinstance(components, list):
            continue

        commands: list[dict[str, Any]] = []
        seen_commands: set[tuple[str, tuple[str, ...]]] = set()
        for comp in components:
            if not isinstance(comp, dict):
                continue
            if comp.get("type", "").upper() != "COMMAND":
                continue
            if comp.get("enabled") is False:
                continue
            meta = comp.get("metadata", {})
            if not isinstance(meta, dict):
                continue
            pattern = str(meta.get("command_pattern") or "")
            aliases = meta.get("aliases", [])
            usages = (
                _extract_command_usages(pattern, aliases)
                if pattern
                else _extract_command_usages(f"/{comp.get('name', '?')}", aliases)
            )
            command_key = (str(comp.get("name") or "?"), tuple(usages))
            if command_key in seen_commands:
                continue
            seen_commands.add(command_key)
            commands.append({
                "name": comp.get("name", "?"),
                "trigger": usages[0],
                "usages": usages,
                "description": str(meta.get("description") or ""),
            })

        if not commands:
            continue  # skip plugins with no Command components

        # ── get display name ──
        info = plugin.get("_info", {})
        pname = ""
        if isinstance(info, dict):
            pname = info.get("name", "") or info.get("display_name", "")
        if not pname:
            pname = plugin.get("plugin_name", "") or plugin.get("display_name", "") or plugin.get("title", "")
        if not pname:
            pname = pid  # fallback to plugin id

        # ── get description ──
        pdesc = ""
        if isinstance(info, dict):
            pdesc = info.get("description", "") or info.get("introduction", "")
        if not pdesc:
            pdesc = plugin.get("_detail_description", "") or plugin.get("description", "")

        tree.append({
            "plugin_id": pid,
            "plugin_name": pname,
            "description": pdesc,
            "commands": commands,
        })

    # Assign stable 1-based indices sorted by plugin_id
    tree.sort(key=lambda x: x["plugin_id"])
    for i, entry in enumerate(tree):
        entry["index"] = i + 1

    return tree


async def _refresh_menu_tree(
    plugin: HelpMenuPlugin, force: bool = False,
) -> list[dict[str, Any]]:
    """Refresh the cached menu tree if needed (or forced).

    Persists the tree to CACHE_FILE and updates globals.
    Thread-safe via _discovery_lock.
    """
    global _menu_tree, _menu_plugin_hash, _menu_last_refresh

    async with _discovery_lock:
        cfg = plugin.config.plugin
        ttl_seconds = cfg.cache_ttl_minutes * 60

        # Quick early-return when cache is fresh enough
        now = time.time()
        if not force and _menu_tree and _menu_last_refresh > 0:
            if now - _menu_last_refresh < ttl_seconds:
                return _menu_tree

        # ── discover ──
        tree = await _discover_commands(plugin.ctx, plugin.ctx.logger)
        plugin.ctx.logger.info(f"指令菜单发现 {len(tree)} 个含命令的插件")

        # ── persist ──
        try:
            TEMP_DIR.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(
                json.dumps(tree, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            plugin.ctx.logger.warning(f"保存菜单缓存失败: {exc}")

        _menu_tree = tree
        _menu_last_refresh = now
        return tree


# ═══════════════════════════════════════════════════════════════
#  Image generation — AI (Bailian → HTML → render.html2png)
# ═══════════════════════════════════════════════════════════════

_AI_SHARED_RULES = """
## 统一设计规范（不得自由更换风格）
1. 页面根节点必须是 `<main class="menu-shell">`，固定宽度 480px，box-sizing: border-box。
2. body 必须 margin: 0，背景色 #fff5fa；menu-shell 内边距 16px，字体仅使用 Microsoft YaHei、PingFang SC、sans-serif。
3. 顶部必须使用 `<header class="menu-header">`，粉色渐变 #ff7eb3 到 #ffa0c8，圆角 16px，白色标题。
4. 内容只能使用 `<section class="menu-list">` 和 `<article class="menu-card">`；卡片白底、1px #f3cfdd 边框、12px 圆角、12px 内边距、8px 间距，禁止交替更换颜色。
5. 命令文本必须使用 `<code class="command-usage">`；说明使用 `<p class="item-description">`。所有页面字号、间距、圆角和颜色必须一致。
6. 不得新增、改写、翻译、合并或遗漏 JSON 中的任何条目；严格保持输入顺序。空说明不要虚构内容。
7. 禁止表格、绝对定位、外部资源、JavaScript、动画和横向滚动。
8. 页面底部统一显示“发送 /帮助 返回主菜单”。
"""


_AI_MAIN_PROMPT = textwrap.dedent("""\
请严格按照统一设计规范，把下面的菜单数据渲染为完整 HTML。

## 硬性约束
1. **只使用内联 CSS**，禁止任何外部资源引用（不要 link、不要 @import、不要 CDN）
2. 每个插件必须单独使用一张 menu-card，显示 index、plugin_name、description 和 command_count
3. 不允许自行增删文字或改变插件顺序

## 菜单数据（JSON）
{data}

## 数据结构说明
每个插件项包含：
- index: 菜单序号
- plugin_name: 插件名称
- description: 插件简介
- command_count: Command 组件数量

## 输出要求
**只输出完整的 HTML 代码**（从 <!DOCTYPE html> 到 </html>），
**不要**包含任何解释、说明、markdown 代码块标记。
""") + _AI_SHARED_RULES

_AI_PLUGIN_PROMPT = textwrap.dedent("""\
请严格按照统一设计规范，把下面的插件命令数据渲染为完整 HTML。

## 硬性约束
1. **只使用内联 CSS**，禁止任何外部资源引用（不要 link、不要 @import、不要 CDN）
2. 每条 commands 必须单独使用一张 menu-card
3. trigger 必须完整原样显示，禁止省略其中任何别名或参数
4. subs 必须按原顺序全部显示在对应卡片中

## 插件数据（JSON）
{data}

## 数据结构说明
- name: 插件名称
- description: 插件简介
- commands: 命令列表，每项结构如下：
  - trigger: 命令触发词
  - description: 命令说明（单条命令时）
  - subs: 可选，当一条 trigger 对应多个子功能时，列出所有子功能描述

## 渲染要求
- 如果 commands[i] 有 subs 字段，请用缩进或树形结构展示 trigger 及其下属的子功能列表
- 如果 commands[i] 没有 subs 字段，正常展示 trigger + description

## 输出要求
**只输出完整的 HTML 代码**（从 <!DOCTYPE html> 到 </html>），
**不要**包含任何解释、说明、markdown 代码块标记。
""") + _AI_SHARED_RULES


def _extract_html(raw: str) -> str:
    """Extract HTML content from AI response (may be wrapped in ```html blocks)."""
    # Strip markdown fences
    m = re.search(r"```(?:html)?\s*\n?(.*?)```", raw, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Find <!DOCTYPE or <html
    m = re.search(r"(<!DOCTYPE[^>]*>.*)", raw, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"(<html[^>]*>.*)", raw, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return raw.strip()


def _validate_ai_html(html: str, menu_type: str, data: dict[str, Any]) -> bool:
    """检查 AI 是否采用统一骨架并完整保留菜单关键字段。"""
    lowered = html.casefold()
    if "menu-shell" not in lowered or "menu-header" not in lowered or "menu-card" not in lowered:
        return False
    plain_text = unescape(re.sub(r"<[^>]+>", " ", html))
    required: list[str] = []
    if menu_type == "main":
        required.extend(str(item.get("plugin_name") or "") for item in data.get("plugins", []))
    else:
        required.append(str(data.get("name") or ""))
        for command in data.get("commands", []):
            required.append(str(command.get("trigger") or ""))
            required.extend(str(item or "") for item in command.get("subs", []))
    return all(not value or value in plain_text for value in required)


async def _ai_generate_html(
    plugin: HelpMenuPlugin,
    _type: str,
    data: dict[str, Any],
) -> str | None:
    """Call Bailian API to generate HTML for the menu.

    Args:
        _type: "main" or "plugin"
        data: menu data dict

    Returns HTML string or None on failure.
    """
    cfg = plugin.config.plugin
    if not cfg.ai_layout_enabled:
        return None

    prompt_tpl = _AI_MAIN_PROMPT if _type == "main" else _AI_PLUGIN_PROMPT
    prompt = prompt_tpl.format(data=json.dumps(data, ensure_ascii=False, indent=2))

    url = f"{cfg.ai_api_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg.ai_api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": cfg.ai_model,
        "messages": [
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 4096,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    plugin.ctx.logger.warning(f"Bailian API error {resp.status}: {text[:200]}")
                    return None
                result = await resp.json()
    except asyncio.TimeoutError:
        plugin.ctx.logger.warning("Bailian API 超时")
        return None
    except Exception as exc:
        plugin.ctx.logger.warning(f"Bailian API 异常: {exc}")
        return None

    # Extract content
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        plugin.ctx.logger.warning(f"Bailian 响应格式异常: {exc}")
        return None

    html = _extract_html(str(content))
    if not _validate_ai_html(html, _type, data):
        plugin.ctx.logger.warning("Bailian 返回的菜单未满足统一布局或内容完整性要求，改用 Pillow 布局")
        return None
    return html


async def _render_html_to_image(
    plugin: HelpMenuPlugin, html: str,
) -> bytes | None:
    """Render HTML to PNG via the runner's browser render capability."""
    try:
        result = await plugin.ctx.render.html2png(
            html,
            selector="body",
            viewport={"width": 480, "height": 800},
            device_scale_factor=2.0,
            full_page=True,
            wait_until="networkidle",
            wait_for_timeout_ms=2000,
            render_timeout_ms=15000,
        )
    except Exception as exc:
        plugin.ctx.logger.warning(f"html2png 渲染失败: {exc}")
        return None

    # result is normally {"image_base64": "...", ...}
    if isinstance(result, dict):
        b64 = result.get("image_base64", "")
        if b64:
            return base64.b64decode(b64)
    if isinstance(result, str):
        # Might already be base64
        try:
            return base64.b64decode(result)
        except Exception:
            pass
    return None


# ═══════════════════════════════════════════════════════════════
#  Image generation — PIL fallback
# ═══════════════════════════════════════════════════════════════

def _draw_gradient_rect(
    draw: ImageDraw.ImageDraw,
    x0: int, y0: int, x1: int, y1: int,
    color_top: tuple[int, int, int],
    color_bottom: tuple[int, int, int],
) -> None:
    """Draw a vertical gradient-filled rectangle."""
    for y in range(y0, y1):
        r = int(color_top[0] + (color_bottom[0] - color_top[0]) * (y - y0) / max(1, y1 - y0))
        g = int(color_top[1] + (color_bottom[1] - color_top[1]) * (y - y0) / max(1, y1 - y0))
        b = int(color_top[2] + (color_bottom[2] - color_top[2]) * (y - y0) / max(1, y1 - y0))
        draw.line([(x0, y), (x1, y)], fill=(r, g, b))


def _wrap_text(text: str, font: Any, max_width: int) -> list[str]:
    """Simple CJK-aware text wrapping."""
    if not hasattr(font, "getlength"):
        return [text]  # fallback for default font
    lines: list[str] = []
    current = ""
    for ch in text:
        test = current + ch
        if font.getlength(test) > max_width:
            if current:
                lines.append(current)
            current = ch
        else:
            current = test
    if current:
        lines.append(current)
    return lines or [text]


def _build_main_menu_image(tree: list[dict[str, Any]]) -> bytes | None:
    """Generate Level-1 main menu image via Pillow."""
    if Image is None:
        return None

    f_title, f_norm, f_sm, f_xs, _ = _load_fonts()
    pad_x, pad_y = 16, 10
    header_h = 64
    footer_h = 36
    row_h_min = 46
    img_w = 480

    # ── Pre-calc row heights: some plugins may need 2-line names ──
    name_avail_w = img_w - pad_x - 4 - 30 - 14 - 110
    row_heights: list[int] = []
    for entry in tree:
        name = entry.get("plugin_name", "")
        name_lines = _wrap_text(name, f_norm, name_avail_w)
        rows_needed = max(len(name_lines), 1)
        # Each text line ~18px; min row_h_min, max 2 lines comfortably
        rh = max(row_h_min, 22 + rows_needed * 16)
        row_heights.append(rh)

    total = len(tree)
    img_h = header_h + sum(row_heights) + footer_h + pad_y * 2

    img = Image.new("RGB", (img_w, img_h), COLOR_BG)
    draw = ImageDraw.Draw(img)

    # ── header ──
    _draw_gradient_rect(draw, 0, 0, img_w, header_h, COLOR_HEADER, COLOR_HEADER_END)
    draw.text((pad_x, 10), "🌸 指令菜单  |  Help Menu", fill=COLOR_TEXT_LIGHT, font=f_title)
    draw.text((pad_x, 36), f"共 {total} 个插件提供命令", fill=(255, 220, 230), font=f_xs)
    # corner decoration
    draw.ellipse([img_w - 50, -20, img_w + 10, 40], fill=(255, 255, 255, 50), outline=None)

    # ── rows ──
    cur_y = header_h + pad_y
    for i, entry in enumerate(tree):
        row_h = row_heights[i]
        y = cur_y
        bg = COLOR_CELL_A if i % 2 == 0 else COLOR_CELL_B
        draw.rectangle([(pad_x - 4, y), (img_w - pad_x + 4, y + row_h)], fill=bg)

        # Index badge
        idx = entry.get("index", i + 1)
        idx_str = f"{idx:02d}"
        badge_x = pad_x + 4
        badge_w = 30
        badge_h = 24
        badge_y = y + (row_h - badge_h) // 2
        draw.rounded_rectangle(
            [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
            radius=6, fill=COLOR_HEADER,
        )
        tw = f_xs.getlength(idx_str) if hasattr(f_xs, "getlength") else len(idx_str) * 7
        draw.text(
            (badge_x + (badge_w - tw) / 2, badge_y + 3),
            idx_str, fill=COLOR_TEXT_LIGHT, font=f_xs,
        )

        # Plugin name — wrap to 2 lines if needed
        name = entry.get("plugin_name", "")
        name_lines = _wrap_text(name, f_norm, name_avail_w)
        text_x = badge_x + badge_w + 14
        # Vertical centering for name + desc
        content_top = y + (row_h - (min(len(name_lines), 2) * 16 + (6 if entry.get("description") else 0))) // 2 + 2
        draw.text((text_x, content_top), name_lines[0], fill=COLOR_TEXT_DARK, font=f_norm)
        if len(name_lines) > 1:
            draw.text((text_x, content_top + 17), name_lines[1], fill=COLOR_TEXT_DARK, font=f_norm)

        # Description
        desc = entry.get("description", "")
        if desc:
            desc_wrapped = _wrap_text(desc, f_xs, name_avail_w)
            desc_h = len(name_lines) * 17 + 2
            draw.text((text_x, content_top + desc_h), desc_wrapped[0], fill=COLOR_TEXT_MID, font=f_xs)

        # Command count tag
        count = len(entry.get("commands", []))
        tag = f"{count} 命令"
        tw2 = f_xs.getlength(tag) if hasattr(f_xs, "getlength") else len(tag) * 7
        tag_x = img_w - pad_x - tw2 - 14
        draw.rounded_rectangle(
            [tag_x - 4, y + row_h // 2 - 12, tag_x + tw2 + 4, y + row_h // 2 + 12],
            radius=8, fill=COLOR_TAG_BG,
        )
        draw.text((tag_x, y + row_h // 2 - 8), tag, fill=COLOR_TEXT_LIGHT, font=f_xs)

        cur_y += row_h

    # ── footer ──
    fy = cur_y + pad_y
    draw.line([(pad_x, fy - 4), (img_w - pad_x, fy - 4)], fill=COLOR_DIVIDER)
    draw.text((pad_x, fy + 2), "发送 /帮助 <序号> 查看详情  |  /帮助刷新 刷新缓存", fill=COLOR_TEXT_MID, font=f_xs)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _build_plugin_menu_image(entry: dict[str, Any]) -> bytes | None:
    """Generate Level-2 per-plugin menu image via Pillow with adaptive row heights."""
    if Image is None:
        return None

    f_title, f_norm, f_sm, f_xs, f_mono = _load_fonts()
    pad_x, pad_y = 16, 12
    header_h = 74
    footer_h = 36
    row_h_min = 46
    img_w = 480

    commands: list = entry.get("commands", [])
    total = len(commands) + sum(len(cmd.get("subs", [])) - 1 for cmd in commands if cmd.get("subs"))
    avail_w = img_w - pad_x * 2 - 10

    # Pre-calc row heights based on text content (including subs)
    row_heights: list[int] = []
    for cmd in commands:
        trigger = cmd.get("trigger", "")
        desc = cmd.get("description", "")
        subs = cmd.get("subs", [])
        trig_lines = _wrap_text(trigger, f_norm, avail_w)
        desc_lines = _wrap_text(desc, f_sm, avail_w) if desc else []
        total_lines = max(len(trig_lines), 1) + len(desc_lines)
        if subs:
            for sub in subs:
                total_lines += len(_wrap_text(sub, f_xs, avail_w - 30)) or 1
        row_heights.append(max(row_h_min, total_lines * 16 + 14))

    if total == 0:
        row_heights = [row_h_min]

    img_h = header_h + sum(row_heights) + footer_h + pad_y * 2

    img = Image.new("RGB", (img_w, img_h), COLOR_BG)
    draw = ImageDraw.Draw(img)

    # ── header ──
    _draw_gradient_rect(draw, 0, 0, img_w, header_h, COLOR_HEADER, COLOR_HEADER_END)
    name = entry.get("plugin_name", "")
    name_wrapped = _wrap_text(name, f_title, img_w - pad_x * 2 - 30)
    for li, line in enumerate(name_wrapped[:2]):
        draw.text((pad_x, 8 + li * 20), f"{'📋' if li == 0 else ''} {line}"[:40],
                  fill=COLOR_TEXT_LIGHT, font=f_title if li == 0 else f_norm)
    desc = entry.get("description", "")
    if desc:
        desc_w = _wrap_text(desc, f_xs, img_w - pad_x * 2)
        draw.text((pad_x, 50), desc_w[0][:60], fill=(255, 220, 230), font=f_xs)
    draw.text((pad_x, 54 if desc else 50), f"共 {total} 条命令", fill=(255, 200, 215), font=f_xs)

    # ── rows ──
    cur_y = header_h + pad_y
    for i, cmd in enumerate(commands):
        row_h = row_heights[i]
        y = cur_y
        bg = COLOR_CELL_A if i % 2 == 0 else COLOR_CELL_B
        draw.rectangle([(pad_x - 4, y), (img_w - pad_x + 4, y + row_h)], fill=bg)

        trigger = cmd.get("trigger", "")
        desc_cmd = cmd.get("description", "")
        subs = cmd.get("subs", [])

        trig_lines = _wrap_text(trigger, f_norm, avail_w - 15) if trigger else ["(无触发词)"]
        desc_lines = _wrap_text(desc_cmd, f_sm, avail_w - 15) if desc_cmd else []

        trig_text_h = len(trig_lines) * 17
        total_text_h = trig_text_h + len(desc_lines) * 14
        if subs:
            for sub in subs:
                sub_lines = _wrap_text(sub, f_xs, avail_w - 30) or [sub]
                total_text_h += len(sub_lines) * 13
        content_start = y + max((row_h - total_text_h) // 2, 4)

        cur_text_y = content_start
        for line in trig_lines:
            draw.text((pad_x + 10, cur_text_y), line,
                      fill=COLOR_TEXT_DARK, font=f_norm)
            cur_text_y += 17

        for line in desc_lines:
            draw.text((pad_x + 10, cur_text_y), line,
                      fill=COLOR_TEXT_MID, font=f_xs)
            cur_text_y += 15

        # Render sub-features indented with tree-drawing chars
        if subs:
            for si, sub in enumerate(subs):
                is_last = si == len(subs) - 1
                prefix = "  └ " if is_last else "  ├ "
                sub_lines = _wrap_text(sub, f_xs, avail_w - 30) or [sub]
                for sli, sline in enumerate(sub_lines):
                    draw.text((pad_x + 30, cur_text_y), prefix + sline if sli == 0 else "  │ " + sline,
                              fill=COLOR_TEXT_MID, font=f_xs)
                    cur_text_y += 13

        cur_y += row_h

    # ── footer ──
    fy = cur_y + pad_y
    draw.line([(pad_x, fy - 4), (img_w - pad_x, fy - 4)], fill=COLOR_DIVIDER)
    draw.text((pad_x, fy + 2), "发送 /帮助 返回主菜单", fill=COLOR_TEXT_MID, font=f_xs)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════
#  Image orchestration
# ═══════════════════════════════════════════════════════════════

async def _generate_and_send_menu(
    plugin: "HelpMenuPlugin", stream_id: str,
    _type: str, data: dict[str, Any], entry: dict[str, Any] | None = None,
) -> str:
    """Generate a menu image (AI preferred, PIL fallback), send it.

    Returns a human-readable status string for the Command handler.
    """
    img_bytes: bytes | None = None
    source = ""

    # 1. Try AI layout
    ai_html = await _ai_generate_html(plugin, _type, data)
    if ai_html:
        img_bytes = await _render_html_to_image(plugin, ai_html)
        if img_bytes:
            source = "AI"

    # 2. Fallback to Pillow
    if img_bytes is None:
        if _type == "main":
            img_bytes = _build_main_menu_image([data])
        elif entry is not None:
            img_bytes = _build_plugin_menu_image(entry)
        if img_bytes:
            source = "PIL"
        else:
            await plugin.ctx.send.text("❌ 生成菜单图片失败（PIL 不可用）", stream_id)
            return "fail: no PIL"

    # 3. Send
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    await plugin.ctx.send.image(b64, stream_id)
    return f"ok ({source})"


async def _get_or_generate_cached_image(
    plugin: "HelpMenuPlugin", stream_id: str,
    _type: str, cache_key: str,
    data: dict[str, Any], entry: dict[str, Any] | None = None,
    pil_tree: list[dict[str, Any]] | None = None,
) -> str:
    """Check disk cache for image; regenerate if missing.

    Args:
        pil_tree: Full plugin tree for PIL fallback (main menu only).
                  Must include 'plugin_name', 'description', 'commands', 'index'.
    """
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    data_hash = hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    cache_path = TEMP_DIR / f"{cache_key}_{data_hash}.png"

    # Check cache
    if cache_path.exists():
        try:
            img_bytes = cache_path.read_bytes()
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            await plugin.ctx.send.image(b64, stream_id)
            return "ok (cached)"
        except Exception:
            pass

    # Generate
    img_bytes: bytes | None = None

    ai_html = await _ai_generate_html(plugin, _type, data)
    if ai_html:
        img_bytes = await _render_html_to_image(plugin, ai_html)

    if img_bytes is None:
        if _type == "main":
            img_bytes = _build_main_menu_image(pil_tree or [])
        elif entry is not None:
            img_bytes = _build_plugin_menu_image(entry)

    if img_bytes is None:
        await plugin.ctx.send.text("❌ 生成菜单图片失败", stream_id)
        return "fail"

    # Cache to disk
    try:
        cache_path.write_bytes(img_bytes)
    except Exception:
        pass

    b64 = base64.b64encode(img_bytes).decode("utf-8")
    await plugin.ctx.send.image(b64, stream_id)
    return "ok"


# ═══════════════════════════════════════════════════════════════
#  Command enhancement — English→Chinese translation
# ═══════════════════════════════════════════════════════════════
#  Plugin class
# ═══════════════════════════════════════════════════════════════


def _normalize_menu_lookup(value: Any) -> str:
    """统一菜单直达查询文本，忽略全角斜杠、大小写和多余空白。"""
    return re.sub(r"\s+", " ", str(value or "").strip().replace("／", "/")).casefold()


def _entry_search_terms(entry: dict[str, Any]) -> set[str]:
    """收集插件名称、ID、命令完整用法和命令头部作为精确匹配项。"""
    terms = {
        _normalize_menu_lookup(entry.get("plugin_id")),
        _normalize_menu_lookup(entry.get("plugin_name")),
    }
    for command in entry.get("commands", []):
        usages = command.get("usages") or [command.get("trigger", "")]
        for usage in usages:
            normalized = _normalize_menu_lookup(usage)
            if normalized:
                terms.add(normalized)
                terms.add(normalized.split(" ", 1)[0])
    return {term for term in terms if term}


def _find_menu_entry(tree: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    """优先精确匹配；只有前缀唯一时才接受缩写，避免命令误归属。"""
    normalized = _normalize_menu_lookup(query)
    query_terms = {normalized, normalized.lstrip("/")}
    candidates = [(entry, _entry_search_terms(entry)) for entry in tree]
    exact = [entry for entry, terms in candidates if query_terms & terms]
    if len(exact) == 1:
        return exact[0]

    if len(normalized.lstrip("/")) < 2:
        return None
    prefix = [
        entry
        for entry, terms in candidates
        if any(term.startswith(query_term) for term in terms for query_term in query_terms)
    ]
    unique = {str(entry.get("plugin_id")): entry for entry in prefix}
    return next(iter(unique.values())) if len(unique) == 1 else None


class HelpMenuPlugin(MaiBotPlugin):
    """🌸 指令菜单 | 自动发现插件命令并生成分级图片菜单"""

    config_model = HelpMenuConfig

    # ── lifecycle ──────────────────────────────────────────

    async def on_load(self) -> None:
        """Load cached menu and trigger initial discovery."""
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

        # Load persisted cache
        global _menu_tree, _menu_plugin_hash, _menu_last_refresh
        if CACHE_FILE.exists():
            try:
                data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    _menu_tree = data
                    ids = sorted(e.get("plugin_id", "") for e in data if e.get("plugin_id"))
                    _menu_plugin_hash = hashlib.sha256(
                        "|".join(ids).encode()
                    ).hexdigest()
                    _menu_last_refresh = time.time()
            except Exception:
                pass

        # Async refresh in background
        asyncio.create_task(self._background_refresh())

        self.ctx.logger.info(
            f"指令菜单插件 v1.0 加载 | "
            f"缓存 {len(_menu_tree)} 个插件 | "
            f"AI布局={'启用' if self.config.plugin.ai_layout_enabled else '关闭'}"
        )

    async def on_unload(self) -> None:
        pass

    async def on_config_update(
        self, scope: str, config_data: dict[str, object], version: str,
    ) -> None:
        del scope, config_data, version

    async def _background_refresh(self) -> None:
        """Refresh menu tree in background (non-blocking)."""
        try:
            await _refresh_menu_tree(self, force=True)
        except Exception as exc:
            self.ctx.logger.warning(f"后台刷新菜单失败: {exc}")

    # ── helpers ────────────────────────────────────────────

    def _get_tree(self) -> list[dict[str, Any]]:
        global _menu_tree
        return _menu_tree

    async def _ensure_tree_fresh(self) -> list[dict[str, Any]]:
        """Return fresh menu tree, triggering refresh if needed."""
        global _menu_tree, _menu_plugin_hash, _menu_last_refresh

        cfg = self.config.plugin
        ttl_seconds = cfg.cache_ttl_minutes * 60
        now = time.time()

        # Cache still valid?
        if _menu_tree and _menu_last_refresh > 0:
            if now - _menu_last_refresh < ttl_seconds:
                return _menu_tree

        return await _refresh_menu_tree(self, force=True)

    # ── main menu command ──────────────────────────────────

    @Command(
        "help_main",
        description="显示指令菜单主页面",
        pattern=r"^[/／](帮助|菜单|help|功能)\s*$",
    )
    async def handle_main_menu(
        self, stream_id: str = "", **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """Show Level 1: main menu listing all plugins with commands."""
        _ = kwargs
        if not self.config.plugin.enabled:
            return False, "插件未启用", True

        tree = await self._ensure_tree_fresh()

        if not tree:
            await self.ctx.send.text(
                "📋 暂未发现任何提供命令的插件\n"
                "请先安装并启用其他插件，然后发送 /帮助刷新",
                stream_id,
            )
            return True, "menu empty", True

        # Build main menu data for AI
        main_data = []
        for entry in tree:
            main_data.append({
                "index": entry["index"],
                "plugin_name": entry["plugin_name"],
                "description": entry.get("description", ""),
                "command_count": len(entry.get("commands", [])),
            })

        result = await _get_or_generate_cached_image(
            self, stream_id, "main", f"v{CACHE_LAYOUT_VERSION}_menu_main",
            data={"plugins": main_data, "total": len(main_data)},
            pil_tree=tree,
        )
        return True, result, True

    # ── sub-menu by number ─────────────────────────────────

    @Command(
        "help_sub",
        description="按序号查看插件命令详情",
        pattern=r"^[/／](帮助|菜单|help)\s+(?P<num>\d+)\s*$",
    )
    async def handle_sub_by_number(
        self, stream_id: str = "", **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """Show Level 2 for a plugin selected by menu index."""
        matched_groups: dict = kwargs.get("matched_groups", {})
        self.ctx.logger.info(f"[help-menu] help_sub matched_groups={matched_groups}, kwargs keys={list(kwargs.keys())}")
        if not matched_groups:
            return True, "no match", True

        num_str = matched_groups.get("num", "") or matched_groups.get(2, "")
        try:
            num = int(num_str)
        except (ValueError, TypeError):
            await self.ctx.send.text("❓ 请输入有效的菜单序号", stream_id)
            return True, "bad number", True

        self.ctx.logger.info(f"[help-menu] help_sub num={num}, refreshing tree...")
        tree = await self._ensure_tree_fresh()
        self.ctx.logger.info(f"[help-menu] help_sub tree has {len(tree)} entries, indices={[e.get('index') for e in tree]}")

        entry = None
        for e in tree:
            if e.get("index") == num:
                entry = e
                break

        if entry is None:
            await self.ctx.send.text(
                f"❓ 未找到序号 {num} 对应的插件\n"
                f"当前共 {len(tree)} 个插件，发送 /帮助 查看完整列表",
                stream_id,
            )
            return True, "not found", True

        self.ctx.logger.info(f"[help-menu] help_sub found entry={entry.get('plugin_id')}, calling _send_plugin_menu")
        return await self._send_plugin_menu(stream_id, entry)

    # ── sub-menu by plugin trigger ─────────────────────────

    @Command(
        "help_trigger",
        description="通过命令触发词查看插件详情",
        pattern=r"^[/／](?P<trigger>\S+?)(帮助|菜单)\s*$",
    )
    async def handle_trigger_menu(
        self, stream_id: str = "", **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """Show Level 2 for a plugin matched by its command trigger."""
        matched_groups: dict = kwargs.get("matched_groups", {})
        if not matched_groups:
            return True, "no match", True

        trigger = (matched_groups.get("trigger", "") or matched_groups.get(1, "") or "").strip()
        if not trigger:
            return True, "empty trigger", True

        # Normalise trigger: ensure leading /
        if not trigger.startswith("/"):
            trigger = "/" + trigger

        tree = await self._ensure_tree_fresh()

        entry = _find_menu_entry(tree, trigger)

        if entry is None:
            await self.ctx.send.text(
                f"❓ 未找到命令 `{trigger}` 对应的插件\n"
                "发送 /帮助 查看完整菜单",
                stream_id,
            )
            return True, "trigger not found", True

        return await self._send_plugin_menu(stream_id, entry)

    # ── plain-number handler REMOVED ──
    # Bare digits like "1" / "123" are too aggressive in group chats.
    # Users should use /帮助 <num> or </帮助 N> instead.

    # ── sub-menu by QQ button number (e.g. <1>, <01>) ────────

    @Command(
        "help_sub_qqnum",
        description="QQ按钮序号选择",
        pattern=r"^<(?P<num>\d+)>\s*$",
    )
    async def handle_qq_button_number(
        self, stream_id: str = "", **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """Handle QQ button click that sends a plain number like <1>."""
        matched_groups: dict = kwargs.get("matched_groups", {})
        num_str = matched_groups.get("num", "") or matched_groups.get(1, "")
        try:
            num = int(num_str)
        except (ValueError, TypeError):
            await self.ctx.send.text("❓ 请输入有效的菜单序号", stream_id)
            return True, "bad number", True

        tree = await self._ensure_tree_fresh()
        entry = None
        for e in tree:
            if e.get("index") == num:
                entry = e
                break

        if entry is None:
            await self.ctx.send.text(
                f"❓ 未找到序号 {num} 对应的插件\n"
                f"当前共 {len(tree)} 个插件，发送 /帮助 查看完整列表",
                stream_id,
            )
            return True, "not found", True

        return await self._send_plugin_menu(stream_id, entry)

    # ── sub-menu by QQ button help+number (e.g. </帮助 01>) ─────

    @Command(
        "help_sub_qqhelp",
        description="QQ按钮帮助序号",
        pattern=r"^</(帮助|菜单|help)\s+(?P<num>\d+)>\s*$",
    )
    async def handle_qq_help_number(
        self, stream_id: str = "", **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """Handle QQ button click that sends </帮助 N> format."""
        matched_groups: dict = kwargs.get("matched_groups", {})
        num_str = matched_groups.get("num", "") or matched_groups.get(2, "")
        try:
            num = int(num_str)
        except (ValueError, TypeError):
            await self.ctx.send.text("❓ 请输入有效的菜单序号", stream_id)
            return True, "bad number", True

        tree = await self._ensure_tree_fresh()
        entry = None
        for e in tree:
            if e.get("index") == num:
                entry = e
                break

        if entry is None:
            await self.ctx.send.text(
                f"❓ 未找到序号 {num} 对应的插件\n"
                f"当前共 {len(tree)} 个插件，发送 /帮助 查看完整列表",
                stream_id,
            )
            return True, "not found", True

        return await self._send_plugin_menu(stream_id, entry)

    # ── sub-menu by QQ button trigger name (e.g. </帮助 插件名>) ─

    @Command(
        "help_trigger_qq",
        description="QQ按钮插件名直达",
        pattern=r"^</(帮助|菜单|help)\s+(?![\d]+>)(?P<name>\S+)>\s*$",
    )
    async def handle_qq_help_trigger(
        self, stream_id: str = "", **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """Handle QQ button click that sends </帮助 plugin_name> format."""
        matched_groups: dict = kwargs.get("matched_groups", {})
        trigger = (matched_groups.get("name", "") or matched_groups.get(2, "") or "").strip()
        if not trigger:
            return True, "empty trigger", True

        if not trigger.startswith("/"):
            trigger = "/" + trigger

        tree = await self._ensure_tree_fresh()
        entry = _find_menu_entry(tree, trigger)

        if entry is None:
            await self.ctx.send.text(
                f"❓ 未找到命令 `{trigger}` 对应的插件\n"
                "发送 /帮助 查看完整菜单",
                stream_id,
            )
            return True, "trigger not found", True

        return await self._send_plugin_menu(stream_id, entry)

    # ═══════════════════════════════════════════════════════════════
    #  Plugin sub-menu command handler
    # ═══════════════════════════════════════════════════════════════

    async def _send_plugin_menu(
        self, stream_id: str, entry: dict[str, Any],
    ) -> tuple[bool, str, bool]:
        """Generate and send a Level-2 plugin sub-menu image."""
        plugin_id = entry.get("plugin_id", "unknown")
        raw_commands = entry.get("commands", [])
        cmd_count = len(raw_commands)
        self.ctx.logger.info(
            f"[help-menu] _send_plugin_menu plugin_id={plugin_id}, "
            f"name={entry.get('plugin_name', '?')}, commands={cmd_count}"
        )

        # Use plugin-provided descriptions as-is.
        # When description is empty, fall back to the command name
        # (no hardcoded translations — just surface what the plugin already declared).
        for cmd in raw_commands:
            if not cmd.get("description", "").strip():
                fallback = cmd.get("name", "") or cmd.get("trigger", "").lstrip("/")
                if fallback:
                    cmd["description"] = fallback
        enhanced_commands = raw_commands

        # Group commands that share the same complete usage set (e.g. avatar-meme
        # registers 6 commands all matching /表情).  Merge into grouped
        # entries so every sub-feature is visible without repeating the trigger.
        trigger_groups: OrderedDict[tuple[str, ...], list[dict[str, Any]]] = OrderedDict()
        for cmd in enhanced_commands:
            usages = tuple(cmd.get("usages") or [cmd.get("trigger", "")])
            if any(usages):
                trigger_groups.setdefault(usages, []).append(cmd)

        merged_commands: list[dict[str, Any]] = []
        for usages, cmds in trigger_groups.items():
            if len(cmds) == 1:
                command = dict(cmds[0])
                command["trigger"] = " ｜ ".join(usages)
                merged_commands.append(command)
            else:
                subs = [c.get("description", "") for c in cmds if c.get("description", "").strip()]
                if not subs:
                    subs = [c.get("name", "") for c in cmds if c.get("name", "").strip()]
                merged_commands.append({
                    "trigger": " ｜ ".join(usages),
                    "usages": list(usages),
                    "description": cmds[0].get("description", ""),
                    "name": cmds[0].get("name", ""),
                    "subs": subs,
                })

        cache_key = f"v{CACHE_LAYOUT_VERSION}_menu_plugin_{plugin_id.replace('.', '_')}"

        ai_commands: list[dict[str, Any]] = []
        for c in merged_commands:
            item: dict[str, Any] = {"trigger": c["trigger"], "description": c.get("description", "")}
            if c.get("subs"):
                item["subs"] = c["subs"]
            ai_commands.append(item)

        data = {
            "name": entry["plugin_name"],
            "description": entry.get("description", ""),
            "commands": ai_commands,
        }

        # Also patch entry so PIL fallback sees merged commands (with subs)
        enhanced_entry = dict(entry)
        enhanced_entry["commands"] = merged_commands

        result = await _get_or_generate_cached_image(
            self, stream_id, "plugin", cache_key,
            data=data, entry=enhanced_entry,
        )
        self.ctx.logger.info(f"[help-menu] _send_plugin_menu result={result}")
        return True, result, True

    # ── refresh command ────────────────────────────────────

    @Command(
        "help_refresh",
        description="强制刷新指令菜单缓存",
        pattern=r"^[/／](帮助|菜单)刷新\s*$",
    )
    async def handle_refresh(
        self, stream_id: str = "", **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """Force refresh the menu cache."""
        _ = kwargs

        await self.ctx.send.text("🔄 正在刷新指令菜单...", stream_id)

        tree = await _refresh_menu_tree(self, force=True)

        # Clear cached images
        try:
            for f in TEMP_DIR.glob("*.png"):
                f.unlink(missing_ok=True)
        except Exception:
            pass

        await self.ctx.send.text(
            f"✅ 刷新完成！共发现 **{len(tree)}** 个插件提供命令\n"
            "发送 /帮助 查看",
            stream_id,
        )
        return True, f"refreshed {len(tree)} plugins", True


# ═══════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════

def create_plugin() -> HelpMenuPlugin:
    return HelpMenuPlugin()
