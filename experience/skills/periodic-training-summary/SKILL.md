---
name: periodic-training-summary
description: Use when the user asks for 周期总结, 训练总结, 总结上周, 总结本周, 总结六月, 总结 Q1, 年度总结, or a 训练分享图 for a specific week, 4-week block, month, quarter, or year.
---

# 周期训练总结

根据后台传入的周期 JSON，生成中文 Markdown 训练总结，并在可用时直接生成一张适合朋友圈分享的英文运动海报。

## 与 weekly-review 的区别

- `weekly-review`：用于日常周复盘、月趋势、下周调整建议。
- `periodic-training-summary`：用于正式周期成果总结、阶段表达、训练分享图。

## 支持周期

只处理用户指定的周期，不要求用户手动输入训练数据：

- `week`：单周，默认周一至周日。
- `4weeks`：连续 4 个完整周，默认从周一至周日滚动计算。
- `month`：月度，默认月初至月末。
- `quarter`：季度，默认季度首日至末日。
- `year`：年度，默认 1 月 1 日至 12 月 31 日。

当用户要求尚未结束的周期，例如“总结本周”“总结六月”且当前仍在周期内，允许生成阶段总结，并在标题或总览中说明“截至今天”。

## 输入数据

使用系统后台提供的 JSON。不要要求用户补充训练、身体或计划数据；只在周期意图无法判断时最多问一个问题。

```json
{
  "cycle_type": "week|4weeks|month|quarter|year",
  "start_date": "...",
  "end_date": "...",
  "user_profile": { "nickname": "...", "gender": "...", "age": 0, "level": "..." },
  "training_data": {
    "total_sessions": 0,
    "planned_sessions": 0,
    "completion_rate": 0,
    "total_duration_minutes": 0,
    "avg_duration_minutes": 0,
    "total_volume": "...",
    "avg_intensity": "...",
    "discipline_breakdown": {},
    "highlights": [],
    "personal_records": [],
    "notes": ""
  },
  "body_data": {
    "weight_start": 0,
    "weight_end": 0,
    "weight_change": 0,
    "body_fat_start": 0,
    "body_fat_end": 0,
    "body_fat_change": 0,
    "muscle_mass_change": 0,
    "resting_hr_change": 0
  },
  "plan_data": {
    "plan_name": "...",
    "phase": "...",
    "focus": "...",
    "next_week_goal": "..."
  },
  "mood_or_feeling": "",
  "custom_requirements": ""
}
```

## 数据规则

- 只使用当前用户自己的后台数据，不跨用户读取或引用。
- 没有训练数据时，说明该周期没有可总结数据，提示检查同步或换一个周期；不生成图片。
- 字段缺失时省略对应内容，不编造数字、亮点、身体变化或计划执行结果。
- 不使用真实姓名。报告用“你”；图片默认不显示昵称。
- 遵循 `custom_requirements`，例如“幽默一点”“多强调跑步”，但不能违背数据和隐私规则。

## 输出顺序

先输出中文 Markdown 报告，再用本地脚本直接生成图片并通过 OpenClaw 富输出投递。若当前环境没有图片生成能力或微信通道不能发送图片，必须输出 `---IMAGE_PROMPT---` 作为降级结果，不能静默省略图片部分。

### 中文 Markdown 报告

默认优先使用用户可读的详细周报结构，尤其是 `week` 和 `4weeks`：

```markdown
总结上周训练（6月22日周一至6月28日周日）：

**📊 总量概览**
- 跑步：X次，约 X km
- 骑行：X次，约 X km
- 游泳：X次，约 X km
- 力量：X次，X分钟

**📅 逐日详情**

**周一 6/22** — 休息日
- 睡眠 / 静息心率 / 压力 / 训练准备度等可用身体数据

**周二 6/23** — 路跑 / 骑行 / 游泳 / 力量
- 距离、配速或速度、时长、均心/最高心率、爬升、天气、备注

**📈 趋势与建议**
- 本周期负荷高峰、恢复风险、睡眠或静息心率变化、下阶段重点
```

通用周期也可以使用模块化结构：

```markdown
### 📊 周期总览 / 总量概览

### 🔥 训练亮点

### ❤️ 身体变化

### 🧭 计划执行复盘

### ✅ 下阶段提醒

### ✨ 结束语
```

写作要求：

- 语气积极鼓励，像朋友交谈，称呼“你”。
- 使用适量 emoji，不要堆砌。
- 周总结默认必须包含“总量概览”“逐日详情”“趋势与建议”；不要只输出按项目分组的简单列表。
- 逐日详情中，有训练的日期列出当天训练；无训练但有恢复数据的日期写为休息日。
- 优先写具体事实：训练次数、计划完成率、总时长、总量、项目分布、亮点、PR、身体变化和下阶段目标。
- 有日级身体数据时，写入睡眠、静息心率、压力、HRV、训练准备度、体电量或疲劳感；没有就省略。
- `body_data` 缺失时省略“身体变化”。
- `plan_data` 缺失时省略“计划执行复盘”。
- `highlights` 和 `personal_records` 都为空时，可以省略“训练亮点”或只写基于真实汇总的温和亮点。

## 图片生成规则

可生成图片时，直接生成图片，不只给提示词。图片不可用时必须输出 `IMAGE_PROMPT`，不能静默省略图片部分。

图片要求：

- 比例：`4:5`，分辨率 `1024×1536`，适合手机预览、手机全屏阅读、朋友圈、小红书和 Instagram。
- 风格：Nike / 专业训练仪表盘 / 赛博霓虹数据看板，Modern Sports Dashboard，premium / clean / athletic / infographic / glassmorphism。
- 视觉：深海军蓝到黑蓝渐变，主色接近 `#05192B`、`#071C30`、`#0B2740`；少量 neon green、cyan、yellow、orange、red 作为强调，白字、高对比度、数据点阵、细线轨迹图、半透明运动路线曲线、边缘蓝绿色微光；不要纯黑、白底、卡通或花哨插画。
- 版式：两栏构图。左栏约 52%，从 x=22 到 x=508；右栏约 44%，从 x=570 到 x=1000；中间时间线约 x=555。
- Header：`MY WEEK`、`WEEK XX · 2026` 绿色描边胶囊、中文大标题“训练总结”、中文日期范围、手写斜体英文标语，例如 `Train Hard, Recover Smart.`。
- Header 数据必须按海报语言写，不要把 `title` 写成 `WEEK 26` 或把 `period` 写成 `TRAINING SUMMARY`。正确写法是 `title: "MY WEEK"`，`period: "WEEK 26 · 2026"`；中文周期信息放在 `date_range`。
- `headline` 默认使用英文运动海报短句，例如 `Train Hard, Recover Smart.`；中文阶段亮点放在 `highlight` 或 `insights`，不要放进手写英文标语位置。
- 左侧卡片：`总量概览`、`关键数据`、`本周亮点`、`趋势与建议`。圆角 10-14px，半透明蓝灰边框，数字大、单位小，信息密集但整齐。
- `weekly_totals` 的 `value` 必须优先写训练次数，例如 `5次`、`1次`；距离或时长放在 `subvalue`，例如 `约 45.2 km`、`72.3 km`、`30 分钟`。不要把 `45.2` 这类距离放成主数字。
- 右侧时间线：每天用 rounded glass cards 展示中文星期胶囊、日期、矢量运动图标、训练标题、训练细节、睡眠、恢复、心率、备注。
- 活动类型必须使用高相似矢量运动图标，支持 `run`、`bike`、`swim`、`strength`、`rest`、`tri`，不要用字母或 emoji 代替主要运动图标。
- 状态色：一个周期只选一个真正最重训练日使用 `tone: "hardest"` 和 gold/yellow accents；恢复日用 cool blue；高温、心率异常、疲劳风险等用 `tone: "warning"` 和 subtle red accents；恢复跑、开放水域放松等不要标成 `hardest`。
- 字体：modern sans-serif / 无衬线字体；标题 Extra Bold，数字 Black，正文 Regular，英文偏 condensed；大号数字优先。
- 隐私：不显示昵称，不使用真实姓名；默认使用 `MY WEEK`、`MY MONTH`、`MY YEAR`、`MY BLOCK` 等第一人称标题。
- 必须包含可用的关键数字：训练总量、`SESSIONS`、`TOTAL TIME`、总时长、总消耗、总爬升、平均心率、`DURATION`、`ELEVATION`、`CALORIES`、`AVG HR`、`TRAINING LOAD` 中有数据的部分。
- 整体感觉：premium、energetic、athletic、data-driven、magazine-quality infographic。

本地投递流程：

1. 从真实周期数据整理海报 JSON，只放可公开展示的汇总指标，不放真实姓名。
2. 将中文 Markdown 报告写入临时文件。
3. 运行 `python3 scripts/training_summary_poster.py --input /tmp/training-summary-poster.json --message-file /tmp/training-summary-report.md`。
4. 脚本成功时会返回包含 `mediaUrls` 和 `richPayload` 的 JSON。最终回复应使用 `richPayload`，也就是 `message` 加 `mediaUrls`，让 OpenClaw 微信链路发送文字和本地 PNG 图片。
5. 脚本失败、没有图片路径、或媒体发送不可用时，保留中文报告，并输出 `---IMAGE_PROMPT---`。

海报 JSON 示例：

```json
{
  "title": "MY WEEK",
  "period": "WEEK 26 · 2026",
  "main_title": "训练总结",
  "date_range": "6月22日 周一 — 6月28日 周日",
  "headline": "Train Hard, Recover Smart.",
  "weekly_totals": [
    { "label": "跑步", "value": "5次", "subvalue": "约 45.2 km", "icon": "run", "color": "#8cff3f" },
    { "label": "骑行", "value": "1次", "subvalue": "72.3 km", "icon": "bike", "color": "#ffd21f" },
    { "label": "游泳", "value": "4次", "subvalue": "约 5.2 km", "icon": "swim", "color": "#35c8ff" },
    { "label": "力量", "value": "1次", "subvalue": "30 分钟", "icon": "strength", "color": "#a477ff" }
  ],
  "key_metrics": [
    { "label": "总时长", "value": "10h 16m", "icon": "clock", "color": "#9cff3f" },
    { "label": "总消耗", "value": "6,210 kcal", "icon": "fire", "color": "#ff9f2e" },
    { "label": "总爬升", "value": "429 m", "icon": "mountain", "color": "#55e6bf" },
    { "label": "平均心率", "value": "148 bpm", "icon": "heart", "color": "#ff5e66" }
  ],
  "highlight": "铁三高强度日！",
  "insights": "周跑量45km属于中高水平，周六铁三强度日是本周期最大负荷高峰。",
  "recommendations": ["周末训练很猛，今天恢复要到位。", "下周适当减量控强度，优先保障睡眠。"],
  "quote": "SMALL CONSISTENT STEPS, BIG CHANGES.",
  "timeline": [
    { "day": "周一", "date": "6/22", "title": "休息日", "tone": "recovery", "details": ["睡眠 77 / 静息心率 51"], "icon": "rest" },
    { "day": "周三", "date": "6/24", "title": "高温节奏跑 + 力量", "tone": "warning", "details": ["均心165 / 峰值200", "30°C 夜跑", "等效跑量18.12km"], "icon": "run" },
    { "day": "周六", "date": "6/27", "title": "铁三高强度日", "tone": "hardest", "details": ["跑步 3 km @ 5:28/km", "骑行 72.3 km，154 分钟", "游泳 2.35 km + 400m + 1 km"], "icon": "tri" },
    { "day": "周日", "date": "6/28", "title": "恢复跑 + 公开水域", "tone": "normal", "details": ["跑步 10 km @ 7:02/km", "公开水域游泳 1.48 km"], "icon": "swim" }
  ]
}
```

降级 `IMAGE_PROMPT` 示例结构：

```text
---IMAGE_PROMPT---
Design a premium 4:5 Chinese weekly training summary poster matching the provided reference style: dark navy sports poster, MY WEEK header, WEEK XX · 2026 capsule, huge Chinese title 训练总结, Chinese date range, handwritten neon line "Train Hard, Recover Smart.", GPS route lines, glassmorphism cards. Left column contains 总量概览, 关键数据, 本周亮点, 趋势与建议. Right column contains Chinese Monday-to-Sunday timeline cards with date capsules, workout details, sleep, recovery, heart rate and notes. Use gold/yellow for exactly one hardest training day, cool blue for rest/recovery, subtle red for warnings such as high HR or heat stress. Include SMALL CONSISTENT STEPS, BIG CHANGES. and YOU VS. YOU. EVERY DAY. Do not include a real name or nickname.
```
