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

import asyncio
import base64
import hashlib
import io
import json
import math
import os
import re
import textwrap
import time
from pathlib import Path
from typing import Any

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
    config_version: str = Field(default="1.0.0", description="配置版本")
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


def _extract_command_trigger(pattern: str) -> str:
    """Extract a human-readable command trigger from a regex pattern.

    Strategy: strip anchors and [/／] prefix, then collect literal characters
    until we hit a regex construct or parameter boundary (@).

    Examples:
        r"^[/／]表情\\s+(?P<target>.+)$"        → "/表情"
        r"禁言\\s*@(?P<target>\\S+)"          → "/禁言"
        r"^/amind_create\\s*$"              → "/amind_create"
        r"^[/／](帮助|菜单|help)\\s+(\\d+)"    → "/帮助"
        r"^\\s*(?:\\[)*/dr(?:\\s+...)?$"  → "/dr"
        r"^[/／]表情(?:包)?列表\\s*$"           → "/表情列表"
    """
    p = pattern.strip()

    # ── 1. strip anchors ──
    p = p.lstrip("^").rstrip("$")

    # ── 2. strip [/／] or / or ／ leader ──
    if p.startswith("[/") or p.startswith("[/"):
        # handle character class like [/／]
        p = re.sub(r"^\[[/／]\]\\?s?\\*?", "", p)
    elif p.startswith("/"):
        p = p[1:]
    elif p.startswith("／"):
        p = p[1:]

    # ── 3. strip leading \\s* (whitespace noise) ──
    p = re.sub(r"^\\[sS][*+?]?", "", p)
    p = re.sub(r"^\\(?:\\\[\.\^\\[\]]*\\)[*+?]?$", "", p)  # complex prefix patterns

    # ── 4. collect literal prefix ──
    # Stop at: \ (regex escape), @ (parameter), ( (group), [ (char class),
    #          *+?{} (quantifiers), .^$| (metachars)
    result = ""
    i = 0
    n = len(p)
    while i < n:
        ch = p[i]

        if ch == "\\":
            # regex escape — we've entered regex territory, stop
            # unless this is the very start of a complex pattern
            if not result:
                # No command found yet, this is likely a complex prefix
                # Try to find a /command pattern
                m = re.search(r"/([\w\u4e00-\u9fff-]+)", p)
                if m:
                    return "/" + m.group(1)[:30]
            break

        if ch == "@":
            # Parameter boundary — stop
            break

        if ch == "(":
            # Group — check if optional suffix like (?:包)?
            # If we already have result and group is optional, skip it
            check = p[i:]
            if re.match(r"^\\(\?:\w+\\)[?]", check):
                # Optional suffix after command — skip it, continue
                j = p.find(")", i)
                if j > i:
                    i = j + 1
                    while i < n and p[i] in "?*+":
                        i += 1
                    continue
            break

        if ch in "[{":
            # Character class or quantifier — stop
            break

        if ch in "*+?" and result:
            # Quantifier after command chars — skip but continue
            i += 1
            continue

        if ch in ".^$|#~":
            break

        # Collect literal character
        result += ch
        i += 1

    if not result:
        # Strip named-group prefixes like (?P<name> to avoid matching "P"
        clean = re.sub(r"\(\?P<[^>]+>", "", p)
        # Fallback 1: /word pattern (handles char-class e.g. /[Jj][Mm])
        m = re.search(r"/((?:\[[^\]]+\]|[\w\u4e00-\u9fff-])+)", clean)
        if m:
            raw = m.group(1)
            # Simplify character classes: pick first lowercase char from each
            simplified = re.sub(
                r"\[([^\]]+)\]",
                lambda m2: m2.group(1)[0].lower(),
                raw,
            )
            if simplified:
                return "/" + simplified[:30]
        # Fallback 2: prefer CJK (min 2 chars)
        m = re.search(r"([\u4e00-\u9fff][\u4e00-\u9fff-]*)", clean)
        if m and len(m.group(1)) >= 2:
            return "/" + m.group(1)[:30]
        # Fallback 3: English/word (min 2 chars)
        m = re.search(r"([a-zA-Z_][\w-]*)", clean)
        if m and len(m.group(1)) >= 2:
            return "/" + m.group(1)[:30]
        return "/?"

    result = result.strip()
    if not result.startswith("/"):
        result = "/" + result
    return result[:30]


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

    # ── enrich: scan all plugin directories for _manifest.json ──
    # (get_plugin_info API doesn't return manifest display name)
    manifest_map: dict[str, dict] = {}
    plugins_root = PLUGIN_DIR.parent
    if plugins_root.exists():
        for entry in plugins_root.iterdir():
            if entry.is_dir():
                mf_path = entry / "_manifest.json"
                if mf_path.exists():
                    try:
                        with open(mf_path, "r", encoding="utf-8") as f:
                            mf = json.load(f)
                        if isinstance(mf, dict):
                            pid_in_manifest = mf.get("id", "")
                            if pid_in_manifest:
                                manifest_map[pid_in_manifest] = mf
                    except Exception:
                        pass
    logger.info(f"[help-menu] scanned {len(manifest_map)} plugin manifests")

    for pid, p in plugin_details.items():
        if pid in manifest_map:
            p["_manifest"] = manifest_map[pid]

    # Log first plugin keys for debugging
    if plugin_details:
        first = next(iter(plugin_details.values()))
        logger.info(f"[help-menu debug] first plugin raw keys: {sorted(first.keys())}")
        mf = first.get("_manifest", {})
        if mf:
            logger.info(f"[help-menu debug] first plugin _manifest.name={mf.get('name', '?')}")

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
        for comp in components:
            if not isinstance(comp, dict):
                continue
            if comp.get("type", "").upper() != "COMMAND":
                continue
            meta = comp.get("metadata", {})
            if not isinstance(meta, dict):
                continue
            pattern = meta.get("command_pattern", "")
            desc = meta.get("description", "")
            trig = _extract_command_trigger(pattern) if pattern else f"/{comp.get('name', '?')}"
            commands.append({
                "name": comp.get("name", "?"),
                "trigger": trig,
                "description": desc or "",
            })

        if not commands:
            continue  # skip plugins with no Command components

        # ── get display name ──
        # _manifest is injected by the fetch_detail pass above.
        mf = plugin.get("_manifest", {})
        pname = ""
        if isinstance(mf, dict):
            pname = mf.get("name", "").strip()
        if not pname:
            pname = plugin.get("plugin_name", "") or plugin.get("display_name", "") or plugin.get("title", "")
        if not pname:
            pname = pid  # fallback to plugin id

        # ── get description ──
        pdesc = ""
        if isinstance(mf, dict):
            pdesc = mf.get("description", "").strip()
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

_AI_MAIN_PROMPT = textwrap.dedent("""\
你是一个优秀的 UI 设计师。请根据下面的菜单数据生成一个美观的 **HTML 页面代码**。

## 硬性约束
1. **只使用内联 CSS**，禁止任何外部资源引用（不要 link、不要 @import、不要 CDN）
2. 粉色系主题，主色 #ff7eb3，辅色 #ffb6c1
3. 响应式设计：在手机（360-768px）和桌面（>768px）上都好看
4. 使用 Emoji 作为装饰图标
5. 字体族: "Microsoft YaHei", "PingFang SC", sans-serif
6. body 宽度固定为 480px，方便截图

## 菜单数据（JSON）
{data}

## 数据结构说明
每个插件项包含：
- index: 菜单序号
- plugin_name: 插件名称
- description: 插件简介

## 输出要求
**只输出完整的 HTML 代码**（从 <!DOCTYPE html> 到 </html>），
**不要**包含任何解释、说明、markdown 代码块标记。""")

_AI_PLUGIN_PROMPT = textwrap.dedent("""\
你是一个优秀的 UI 设计师。请根据下面的插件命令数据生成一个美观的 **HTML 页面代码**。

## 硬性约束
1. **只使用内联 CSS**，禁止任何外部资源引用（不要 link、不要 @import、不要 CDN）
2. 粉色系主题，主色 #ff7eb3，辅色 #ffb6c1
3. 响应式设计：在手机（360-768px）和桌面（>768px）上都好看
4. 使用 Emoji 作为装饰图标
5. 字体族: "Microsoft YaHei", "PingFang SC", sans-serif
6. body 宽度固定为 480px，方便截图

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
**不要**包含任何解释、说明、markdown 代码块标记。""")


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
        "temperature": 0.7,
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

    return _extract_html(str(content))


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
        for li, line in enumerate(trig_lines):
            draw.text((pad_x + 10, cur_text_y), line,
                      fill=COLOR_TEXT_DARK, font=f_norm)
            cur_text_y += 17

        for li, line in enumerate(desc_lines):
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
    cache_path = TEMP_DIR / f"{cache_key}.png"

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

_CN_MAP: dict[str, str] = {
    "create": "创建", "add": "添加", "list": "列表", "show": "查看",
    "get": "获取", "set": "设置", "update": "更新", "delete": "删除",
    "remove": "移除", "search": "搜索", "find": "查找", "query": "查询",
    "config": "配置", "help": "帮助", "info": "信息", "status": "状态",
    "start": "启动", "stop": "停止", "restart": "重启", "reset": "重置",
    "on": "开启", "off": "关闭", "enable": "启用", "disable": "禁用",
    "style": "风格", "model": "模型", "image": "图片", "generate": "生成",
    "styles": "风格列表", "selfie": "自拍", "recall": "撤回",
    "default": "默认", "standard": "标准", "mirror": "镜像", "photo": "拍照",
}


def _enhance_commands(commands: list[dict[str, str]]) -> list[dict[str, str]]:
    """Add Chinese labels to commands where descriptions are English-only."""
    enhanced: list[dict[str, str]] = []
    for cmd in commands:
        trigger = cmd.get("trigger", "")
        desc = cmd.get("description", "")
        name = cmd.get("name", "")
        raw = trigger.lstrip("/")
        cn_parts: list[str] = []
        for word in raw.replace("_", " ").replace("-", " ").split():
            low = word.lower()
            if low in _CN_MAP:
                cn_parts.append(_CN_MAP[low])
            elif word.isascii() and not word.isdigit() and len(word) > 1:
                cn_parts.append(word)
        cn_label = " ".join(cn_parts) if cn_parts else ""
        if not desc or desc.strip() == "":
            enriched_desc = cn_label if cn_label else trigger
        elif _is_english_only(desc) and cn_label:
            enriched_desc = f"{cn_label} — {desc}"
        else:
            enriched_desc = desc
        enhanced.append({
            "trigger": trigger,
            "description": enriched_desc,
            "name": name,
        })
    return enhanced


def _is_english_only(text: str) -> bool:
    """Check if text contains only ASCII characters (no CJK)."""
    return all(ord(ch) < 128 for ch in text if not ch.isspace())


# ═══════════════════════════════════════════════════════════════
#  Plugin class
# ═══════════════════════════════════════════════════════════════

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
            self, stream_id, "main", "menu_main",
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

        # Try exact trigger match first, then prefix match
        entry = None
        for e in tree:
            for cmd in e.get("commands", []):
                cmd_trigger = cmd.get("trigger", "")
                if cmd_trigger == trigger or cmd_trigger.startswith(trigger):
                    entry = e
                    break
            if entry:
                break

        if entry is None:
            # Try partial match
            for e in tree:
                for cmd in e.get("commands", []):
                    cmd_trigger = cmd.get("trigger", "")
                    if trigger in cmd_trigger or cmd_trigger.lstrip("/") in trigger:
                        entry = e
                        break
                if entry:
                    break

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
        entry = None
        for e in tree:
            for cmd in e.get("commands", []):
                cmd_trigger = cmd.get("trigger", "")
                if cmd_trigger == trigger or cmd_trigger.startswith(trigger):
                    entry = e
                    break
            if entry:
                break

        if entry is None:
            for e in tree:
                for cmd in e.get("commands", []):
                    cmd_trigger = cmd.get("trigger", "")
                    if trigger in cmd_trigger or cmd_trigger.lstrip("/") in trigger:
                        entry = e
                        break
                if entry:
                    break

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

        # Pre-process: add Chinese labels for English-only commands
        enhanced_commands = _enhance_commands(raw_commands)

        # Group commands that share the same trigger (e.g. avatar-meme
        # registers 6 commands all matching /表情).  Merge into grouped
        # entries so every sub-feature is visible without repeating the trigger.
        from collections import OrderedDict

        trigger_groups: OrderedDict[str, list[dict]] = OrderedDict()
        for cmd in enhanced_commands:
            trig = cmd.get("trigger", "")
            if trig:
                trigger_groups.setdefault(trig, []).append(cmd)

        merged_commands: list[dict[str, Any]] = []
        for trig, cmds in trigger_groups.items():
            if len(cmds) == 1:
                merged_commands.append(cmds[0])
            else:
                subs = [c.get("description", "") for c in cmds if c.get("description", "").strip()]
                if not subs:
                    subs = [c.get("name", "") for c in cmds if c.get("name", "").strip()]
                merged_commands.append({
                    "trigger": trig,
                    "description": cmds[0].get("description", ""),
                    "name": cmds[0].get("name", ""),
                    "subs": subs,
                })

        cache_key = f"menu_plugin_{plugin_id.replace('.', '_')}"

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
