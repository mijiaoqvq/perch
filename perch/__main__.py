import argparse
import json
from dataclasses import replace
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys

from .config import load, state_path
from .library import Library


def main():
    parser = argparse.ArgumentParser(description="栖景 · Perch — Wallhaven 壁纸管理")
    parser.add_argument("command", nargs="?", choices=("gui", "update", "replace", "cleanup", "schedule", "sync-tags", "sync-collections"), default="gui")
    parser.add_argument("--wallpaper-id", help="不喜欢并替换指定壁纸（用于 replace）")
    parser.add_argument("--config", type=Path, help="使用独立设置文件")
    parser.add_argument("--state-directory", type=Path, default=state_path())
    parser.add_argument("--directory", type=Path, help="本次运行使用的壁纸目录（兼容旧脚本）")
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--apply", action="store_true", help="实际清理；否则仅预览")
    parser.add_argument("--demo", action="store_true", help="独立预览模式，不操作桌面和系统定时器")
    args = parser.parse_args()
    if args.command == "replace" and not args.wallpaper_id:
        parser.error("replace 需要 --wallpaper-id")
    try:
        config = load(args.config)
        if args.directory:
            config = replace(config, directory=str(args.directory))
        if args.max_pages is not None:
            config = replace(config, max_pages=args.max_pages)
        config.validate()
        if args.command == "schedule":
            from .scheduler import timer_text
            print(timer_text(config), end="")
            return 0
        if args.demo and (not args.config or args.state_directory == state_path()):
            parser.error("预览模式需要独立的 --config 和 --state-directory")
        library = Library(config.directory, args.state_directory)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                            handlers=[logging.StreamHandler(), RotatingFileHandler(
                                library.state / "perch.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")])
        if args.command == "gui":
            from .gui import Application
            return Application(config, library, args.config, args.demo).run([])
        if args.command == "update":
            from .downloader import update
            from .accounts import load_account, account_path, sync_collections, friendly_error
            try:
                try:
                    account = load_account(account_path(args.config))
                    if account.automatic and account.username:
                        sync_collections(config, library, account)
                except BlockingIOError:
                    raise
                except Exception as exc:
                    logging.getLogger('perch').warning('收藏夹同步暂未完成，继续普通更新：%s', friendly_error(exc))
                return update(config, library)
            except BlockingIOError:
                logging.getLogger("perch").info("已有更新任务正在执行")
                return 0
        if args.command == 'sync-collections':
            from .accounts import load_account, account_path, sync_collections, friendly_error
            try:
                result = sync_collections(config, library, load_account(account_path(args.config)))
            except Exception as exc:
                logging.getLogger('perch').warning('收藏夹同步尚未完成：%s；已导入内容已保留',
                                                  '已有下载或同步任务，请稍后重试' if isinstance(exc, BlockingIOError) else friendly_error(exc))
                return 1
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "replace":
            from .downloader import replace_wallpaper
            path = replace_wallpaper(config, library, args.wallpaper_id)
            print(json.dumps({"path": str(path)}, ensure_ascii=False))
            return 0
        if args.command == "sync-tags":
            from .downloader import Client
            from . import colors
            from .recommendation import TagFetcher, sync_feedback
            fetcher = TagFetcher(library, Client())
            synced = sync_feedback(config, library, fetcher)
            pending = sum(library.tags_for(wid) is None or colors.palette(library, wid) is None for wid in library.feedback())
            print(f"已同步 {synced} 张反馈壁纸的标签与色调；{pending} 张待后续同步")
            return 1 if fetcher.unavailable else 0
        if args.command == "cleanup":
            candidates = library.cleanup_candidates(config.keep, config.wallpaper_backend)
            for item in candidates:
                print(item.path.name)
            if args.apply:
                print(f"已清理 {len(library.prune(config.keep, {p.path.name for p in candidates}, config.wallpaper_backend))} 张；收藏和当前桌面壁纸已保护")
            else:
                print(f"将清理 {len(candidates)} 张普通壁纸；添加 --apply 执行")
            return 0
    except Exception as exc:
        if logging.getLogger().handlers:
            logging.getLogger("perch").exception("操作失败；现有文件及历史已保留")
        else:
            print(f"栖景：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
