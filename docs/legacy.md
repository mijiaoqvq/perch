# Arch：SFW 二次元 4K 壁纸自动下载

此套件使用 Wallhaven API、Python/Pillow 和 systemd 用户定时器，无须 API Key。

## 默认行为

- 按本地时间每天 00:00、06:00、12:00、18:00 运行，定时精度为 1 分钟。
- 筛选 Anime 分类、SFW、至少 3840×2160、严格 16:9 的静态原图。
- 优先月度热门榜，候选不足时查年度热门榜；各榜最多扫描 100 页。
- 首次从空目录下载 20 张。正常每次新增 3 张，并淘汰最早下载的图片，保留 20 张。
- 不足 20 张时，本次下载数量为 `max(3, 20 - 当前有效张数)`。例如有 12 张时下载 8 张，有 19 张时下载 3 张并淘汰 2 张。
- “新”指本机历史中没下载过，不要求是最近 6 小时上传的图片。
- 用 Wallhaven ID 和原文件 SHA-256 去重，淘汰图片后继续保留历史。不同压缩、裁剪或水印版本可能仍算不同文件；没有进行视觉相似度去重。
- 下载后验证真实格式、完整解码、尺寸和比例。API 的 16:9 过滤可能返回近似比例，脚本会再次严格检查。
- 新图下载并校验成功后才淘汰旧图。下载失败或候选不足时保留已有有效图片，服务返回失败并记录日志，下个计划时间再尝试。
- 临时文件不计入 20 张；新图完成替换到正式文件名和清理旧图之间，会短暂存在第 21 张。中断后下次运行会修复数量并清理残留临时文件。

默认壁纸目录：`~/Pictures/Wallpapers/`

默认历史目录：`${XDG_STATE_HOME:-$HOME/.local/state}/wallhaven-anime/`

只管理目标目录中符合 `wallhaven-六位ID.jpg/png/webp` 命名的文件。请将它作为专用目录；这些文件中的损坏文件、重复文件和超额旧图会被删除。历史数据库不随图片淘汰，不要删除历史目录，否则已淘汰图片可能再次被下载。

SFW 依据 Wallhaven 的分类标签，脚本无法保证社区没有误标。至少 4K 表示也接受 5120×2880、7680×4320 等严格 16:9 原图，不做放大或缩放。热门榜和网络供应有限，无法保证任意时刻都能找到足够的新图；脚本不会放宽 SFW、分类、分辨率或比例条件凑数。

## 安装

在 Arch 目标电脑上执行。只对安装系统依赖的命令使用 sudo；安装用户服务时使用普通用户。

```bash
sudo pacman -Syu --needed python python-pillow unzip
unzip arch-anime-wallpapers.zip
cd arch-anime-wallpapers
bash install.sh
```

安装脚本复制下载程序和两个 unit 文件，启用定时器，并异步启动首次补全。

若要退出登录后继续运行，以及开机未登录时启动用户管理器：

```bash
sudo loginctl enable-linger "$USER"
```

这不会让关机的电脑下载，也不会主动唤醒休眠中的电脑。`Persistent=true` 会在定时器重新激活时，为错过的一个或多个计划时间补执行一次，不会逐次追补所有错过的任务。依赖用户登录才能解密/挂载的 home 目录还需等目录可用。

## 用户服务配置

`~/.config/systemd/user/wallhaven-anime.service`（设置 XDG_CONFIG_HOME 时使用其下的 systemd/user）：

```ini
[Unit]
Description=Download SFW anime 4K wallpapers and keep 20

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 %h/.local/bin/wallhaven-anime.py
Environment=PYTHONUNBUFFERED=1
TimeoutStartSec=45min
UMask=0077
Nice=10
NoNewPrivileges=yes
```

`~/.config/systemd/user/wallhaven-anime.timer`：

```ini
[Unit]
Description=Refresh anime wallpapers every six hours

[Timer]
OnCalendar=*-*-* 00,06,12,18:00:00
Persistent=true
AccuracySec=1min
Unit=wallhaven-anime.service

[Install]
WantedBy=timers.target
```

下载程序安装在 `~/.local/bin/wallhaven-anime.py`。不设置 `RemainAfterExit=yes`，保证后续定时事件可以再次运行服务。没有给用户服务加 `network-online.target`：用户管理器里的同名 target 并不能保证系统网络已经可用，脚本在请求层重试网络错误。

## 查看状态与手动执行

```bash
# 首次填充进度；Ctrl+C 仅退出日志查看
journalctl --user -u wallhaven-anime.service -f

# 下次触发时间
systemctl --user list-timers --all wallhaven-anime.timer

# 最近执行结果
systemctl --user status wallhaven-anime.service
journalctl --user -u wallhaven-anime.service -n 80 --no-pager

# 手动更新；已在运行时不会再启动一份
systemctl --user start wallhaven-anime.service
```

oneshot 服务成功结束后显示 `inactive (dead)` 是正常的，以 `status=0/SUCCESS` 和日志为准。失败会显示 `failed`，定时器仍会在下个计划时间触发。HTTP 请求有间隔和有限重试，429 会退避；整个服务最多运行 45 分钟。

## 修改保存位置或扫描范围

```bash
systemctl --user edit wallhaven-anime.service
```

填入以下覆盖配置，例如保持保存到 `~/Pictures/Wallpapers`，将每个榜单的扫描上限改为 200 页：

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/python3 %h/.local/bin/wallhaven-anime.py --directory %h/Pictures/Wallpapers --max-pages 200
```

路径包含空格时，将完整的路径参数用双引号括起来。更新后执行：

```bash
systemctl --user daemon-reload
```

如果目标电脑需要 HTTP 代理，在同一个覆盖文件的 `[Service]` 下添加实际代理地址，例如 `Environment="HTTPS_PROXY=http://127.0.0.1:7890"`。该地址只作格式示例；代理需在服务运行时可用。脚本不会自动读取交互式终端的所有环境配置。

要限制为恰好 3840×2160，可将脚本 `valid_size` 中的判断改成 `return (w, h) == (3840, 2160)`，并将请求中的 `atleast="3840x2160"` 改成 `resolutions="3840x2160"`。

## 停用

```bash
systemctl --user disable --now wallhaven-anime.timer
systemctl --user stop wallhaven-anime.service
```

以上保留图片和历史数据。

## 核验记录与文档

已验证 systemd unit 语法和每天四个触发时间，并用模拟数据验证首次填充、每次新增 3 张、删除后补全、历史 ID/SHA-256 去重、实际尺寸校验、损坏图片处理、断网时保留旧图和中断恢复。另对真实 API 查询和一张原图进行了下载校验。没有在你的系统中安装或启用这些服务。

- Wallhaven API 文档：https://www.whvn.cc/help/api （说明中的接口地址为 wallhaven.cc；正确热门时间参数是 `topRange`）
- systemd timer 文档：https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml
- loginctl linger 文档：https://github.com/systemd/systemd/blob/main/man/loginctl.xml
