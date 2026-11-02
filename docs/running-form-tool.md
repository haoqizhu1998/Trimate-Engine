# 跑姿视频执行工具

跑姿分析包含姿态、质量、指标、评分、安全模式、标注关键帧与中文报告。视频只在当前用户的私有目录暂存，任务结束后删除暂存副本；用户原文件不变。MCP 只接收随机任务 ID，不接收任意本机文件路径。

## 一次性安装

当前产品主环境可能使用 Python 3.14，而 MediaPipe 跑姿后端需要 Python 3.12。安装命令在私有数据目录创建独立环境，不改动主环境：

```bash
athlete-growth running-form-setup
athlete-growth running-form-install-model
athlete-growth running-form-runtime
```

第二条命令下载 [Google 官方 Pose Landmarker Full 模型](https://developers.google.com/edge/mediapipe/solutions/vision/pose_landmarker/index#models) 到私有目录。网络不通时，可从该页面下载兼容的 `.task` 文件，再运行 `athlete-growth running-form-install-model --from-file <文件路径>`。`running-form-runtime` 必须显示 `available: true` 才能启动分析。

## 分析视频

```bash
athlete-growth running-form-stage <跑姿视频路径> --note "本次拍摄的自述信息"
```

命令返回 `job_id`。在 Codex 对话中提供该 ID，Trimate调用 `start_running_form_analysis` 启动后台任务，再用 `get_running_form_job` 查询状态和结果；有可靠标注时用 `get_running_form_contact_sheet` 读取关键帧拼图。支持 MP4、MOV、WebM，单个文件不超过 500 MiB、10 分钟。建议侧面和正面或后方各拍一段 10–20 秒固定机位视频。当前一次任务只接收一个视频，因此缺少另一视角的维度会显示“未评估”。

没有真实视频、视觉依赖或模型时，工具只返回状态，不生成跑姿评分。当前尚未把聊天附件直接转成视频任务，也未用真实跑姿视频完成端到端验收。
