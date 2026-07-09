# 🌸 指令菜单 | Help Menu

自动发现所有已启用插件的指令并生成分级图片菜单，支持 **AI 辅助 HTML 排版**（Bailian 通义千问），AI 不可用时自动回退到 Pillow 粉色主题菜单。

## ✨ 功能

| 命令 | 说明 |
|------|------|
| `/帮助` `/菜单` `/help` `/功能` | 显示主菜单（所有提供命令的插件列表） |
| `/帮助 <序号>` | 按菜单序号查看插件命令详情 |
| `/<命令>帮助` `/<命令>菜单` | 通过插件触发词直达命令详情 |
| `/帮助刷新` `/菜单刷新` | 强制刷新菜单缓存 |

## 🎨 AI 布局 vs PIL 回退

### AI 布局（默认启用）
- 使用 Bailian 通义千问 (`qwen-plus`) 生成精美 HTML
- 通过 MaiBot Runner 的浏览器渲染引擎输出为 PNG
- 支持响应式设计、渐变背景、圆角卡片等现代 UI

### PIL 回退
- 当 AI API 不可用或超时时自动切换
- 粉色系主题，与 `ling_avatar-meme` 风格一致
- 纯本地生成，无需网络

## ⚙️ 配置

编辑 `config.toml`：

```toml
[plugin]
enabled = true
ai_layout_enabled = true          # 启用 AI 布局
ai_model = "qwen-plus"            # 模型名称
ai_api_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
ai_api_key = "sk-your-key"        # 你的 Bailian API Key
cache_ttl_minutes = 60            # 缓存有效期
```

## 🧩 工作原理

1. **启动时**：通过 `self.ctx.component.get_all_plugins()` 获取所有已注册插件
2. **命令发现**：遍历每个插件的组件列表，筛选 `Command` 类型
3. **菜单缓存**：将菜单树保存到 `menu_cache.json`，通过插件列表哈希检测变化
4. **图片生成**：
   - 优先调用 Bailian AI 生成 HTML → Runner 浏览器渲染为 PNG
   - AI 失败时回退到 Pillow 绘制菜单图片
5. **图片缓存**：渲染结果缓存到 `temp/` 目录，定期刷新

## 📂 文件结构

```
ling_help-menu/
├── plugin.py              # 插件主代码
├── config.toml            # 当前配置
├── config.example.toml    # 配置示例
├── _manifest.json         # 插件声明
├── README.md              # 本文件
├── LICENSE                # MIT 许可证
├── menu_cache.json        # 运行时菜单缓存（自动生成）
└── temp/                  # 图片缓存目录
    ├── menu_main.png
    └── menu_plugin_*.png
```

## 📋 依赖

- `maibot_sdk` ≥ 2.0.0
- `Pillow`（PIL 回退用）
- `aiohttp`（Bailian API 调用）

## 📜 许可证

MIT License - 详见 [LICENSE](./LICENSE)
