# 本地 Vlog 自动化工作流

把每一期素材放入 `D:\vlog\inbox\项目名`。文件稳定5分钟后自动开始；如果想立即开始，可以在项目目录建立空文件 `READY.txt`。

浏览器审核地址：<http://127.0.0.1:4380>

## 能力

- 素材入库、ffprobe 技术分析、代理视频、音频抽取和缩略图
- 中文/日文语音识别与时间轴接口（Qwen3-ASR）
- 镜头和故事结构分析接口（Qwen3-VL）
- 长篇 16:9 与短篇 9:16 的独立版本、审核意见、锁定和重新渲染
- 四个平台上传完成确认
- 上传确认后原片/中间视频保留14天，成片保留90天
- SQLite 元数据、字幕、审核意见和日志长期保留

## 启动

```powershell
cd D:\vlog\_automation\repository
docker compose up -d --build
```

浏览器打开 `http://127.0.0.1:4380`。代码位于本目录；视频和模型只保存在 `D:\vlog\_automation`。系统不会扫描 `D:\vlog` 下已有的其他文件夹。

## 按需启动和停止

Vlog服务不会随Docker自动运行。需要使用时执行：

```powershell
& "D:\vlog\_automation\repository\scripts\start.ps1"
```

结束后执行：

```powershell
& "D:\vlog\_automation\repository\scripts\stop.ps1"
```

停止脚本只关闭Vlog服务，不影响其他Docker项目。
