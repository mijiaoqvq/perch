"""Native GTK4 interface. Network and thumbnail work never runs on the UI thread."""
from dataclasses import replace
import hashlib
import logging
from pathlib import Path
import subprocess
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk
from PIL import Image, ImageOps

from . import __version__
from .config import save
from . import scheduler
from .desktop import set_wallpaper

CSS = """
window { background: #141a1b; color: #e5eae8; }
headerbar { background: #141a1b; border: none; box-shadow: none; }
.sidebar { background: #192122; border-right: 1px solid #2a3435; padding: 28px 18px 18px; }
.brand { font-size: 27px; font-weight: 800; letter-spacing: 3px; }
.brand-en { color: #8daba6; font-size: 11px; letter-spacing: 4px; }
.brand-mark { color: #9ad4be; font-size: 32px; }
.nav { margin-top: 38px; }
.nav row { padding: 13px 15px; border-radius: 10px; margin-bottom: 6px; }
.nav row:selected { background: #29413c; color: #b8e4cf; }
.nav row:hover { background: #263334; }
.nav label { font-weight: 600; }
.muted { color: #8eaaa5; }
.caption { font-size: 12px; color: #98aaa6; }
.page { padding: 22px 30px 26px; }
.eyebrow { color: #9cccb7; font-size: 11px; letter-spacing: 2px; font-weight: 700; }
.page-title { font-size: 31px; font-weight: 800; }
.page-subtitle { color: #94aaa5; margin-top: 7px; }
.hero { background: linear-gradient(120deg, #263d37, #20302f); border: 1px solid #354b43; border-radius: 16px; padding: 20px 24px; margin: 22px 0 24px; }
.hero-title { color: #d9eadf; font-size: 17px; font-weight: 700; }
.hero-sub { color: #a1bcb0; font-size: 12px; margin-top: 6px; }
.stat-value { font-size: 27px; font-weight: 700; color: #d9eadf; }
.stat-label { font-size: 11px; color: #a1bcb0; }
button.suggested-action { background: #a2d6be; color: #18392b; font-weight: 700; border-radius: 9px; }
button { border-radius: 8px; }
.gallery { background: transparent; }
.gallery > flowboxchild { padding: 0; margin: 0; border-radius: 13px; }
.card { background: #202a2b; border: 1px solid #303c3d; border-radius: 12px; }
.card:hover { border-color: #638e7b; }
.thumbnail { padding: 0; border-radius: 11px 11px 0 0; background: #263232; }
.card-footer { padding: 12px 13px; }
.card-id { font-weight: 700; font-size: 13px; }
.favorite { color: #b5dec8; background: #2a4239; }
.pill { background: #263c34; color: #acd2bc; border-radius: 14px; padding: 6px 12px; font-size: 11px; }
.footer { padding-top: 16px; }
.log { font-family: monospace; font-size: 12px; padding: 18px; background: #192122; color: #b6cec4; }
.settings-group { margin-bottom: 20px; }
preferencesgroup > box > label { color: #bbd5c7; }
.empty { padding: 70px 20px; }
"""


def label(text, css=None, xalign=0, wrap=False):
    widget = Gtk.Label(label=text, xalign=xalign, wrap=wrap)
    if css:
        widget.add_css_class(css)
    return widget


def box(orientation=Gtk.Orientation.VERTICAL, spacing=0, css=None):
    widget = Gtk.Box(orientation=orientation, spacing=spacing)
    if css:
        widget.add_css_class(css)
    return widget


def button(text=None, icon=None, action=None, css=None, tooltip=None):
    widget = Gtk.Button()
    if text and icon:
        child = box(Gtk.Orientation.HORIZONTAL, 7)
        child.append(Gtk.Image.new_from_icon_name(icon))
        child.append(label(text))
        widget.set_child(child)
    elif icon:
        widget.set_icon_name(icon)
    elif text is not None:
        widget.set_label(text)
    if css:
        widget.add_css_class(css)
    if tooltip:
        widget.set_tooltip_text(tooltip)
    if action:
        widget.connect("clicked", lambda *_: action())
    return widget


class Thumbnail(Gtk.Picture):
    # The original texture stays sharp while natural image width does not force
    # the responsive gallery into a single column.
    def do_measure(self, orientation, for_size):
        return (0, 230, -1, -1) if orientation == Gtk.Orientation.HORIZONTAL else (145, 145, -1, -1)


class Application(Adw.Application):
    def __init__(self, config, library, config_file=None, demo=False):
        super().__init__(application_id="io.github.mijiaoqvq.Perch",
                         flags=Gio.ApplicationFlags.NON_UNIQUE if demo else Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.config, self.library = config, library
        self.config_file, self.demo = config_file, demo
        self.window = None

    def do_activate(self):
        if not self.window:
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            css = Gtk.CssProvider()
            css.load_from_data(CSS.encode())
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.window = PerchWindow(self)
        self.window.present()


class PerchWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="栖景 · Perch", default_width=1180, default_height=820)
        self.app = app
        self.config, self.library = app.config, app.library
        self.items = []
        self.thumbnails = {}
        self.page_name = "library"
        self.process = None
        self.status_busy = False
        self.refresh_busy = False
        self.library_signature = None
        self.closed = False
        self.connect("close-request", self.on_close)
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)
        root = box()
        self.toast_overlay.set_child(root)
        header = Adw.HeaderBar()
        header.set_title_widget(label("栖景 · Perch", "caption"))
        root.append(header)
        body = box(Gtk.Orientation.HORIZONTAL)
        body.set_vexpand(True)
        root.append(body)
        sidebar = box(css="sidebar")
        sidebar.set_size_request(184, -1)
        body.append(sidebar)
        brand = box(Gtk.Orientation.HORIZONTAL, 12)
        brand.append(label("◒", "brand-mark"))
        names = box(spacing=3)
        names.append(label("栖景", "brand"))
        names.append(label("P E R C H", "brand-en"))
        brand.append(names)
        sidebar.append(brand)
        self.nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.nav.add_css_class("nav")
        self.nav.add_css_class("navigation-sidebar")
        for key, title, icon in (("library", "全部壁纸", "view-grid-symbolic"),
                                 ("favorites", "我的收藏", "starred-symbolic"),
                                 ("settings", "偏好设置", "emblem-system-symbolic"),
                                 ("activity", "更新记录", "document-open-recent-symbolic")):
            row = Gtk.ListBoxRow()
            row.page = key
            content = box(Gtk.Orientation.HORIZONTAL, 12)
            content.append(Gtk.Image.new_from_icon_name(icon))
            content.append(label(title))
            row.set_child(content)
            self.nav.append(row)
        self.nav.connect("row-selected", self.navigate)
        sidebar.append(self.nav)
        spacer = box()
        spacer.set_vexpand(True)
        sidebar.append(spacer)
        sidebar.append(label("给喜欢的风景，\n留一个位置。", "muted", wrap=True))
        sidebar.append(label(f"\nv{__version__}  /  WALLHAVEN", "caption"))
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_hexpand(True)
        body.append(self.stack)
        self.build_gallery()
        self.build_settings()
        self.build_activity()
        self.nav.select_row(self.nav.get_row_at_index(0))
        self.refresh_library()
        self.poll()
        GLib.timeout_add_seconds(4, self.poll)

    def on_close(self, *_):
        self.closed = True
        return False

    def toast(self, text):
        if not self.closed:
            self.toast_overlay.add_toast(Adw.Toast.new(text))

    def task(self, work, done=None):
        def run():
            try:
                result = work()
            except Exception as exc:
                logging.getLogger("perch").warning("%s", exc)
                GLib.idle_add(self.task_result, None, str(exc), done)
            else:
                GLib.idle_add(self.task_result, result, None, done)
        threading.Thread(target=run, daemon=True).start()

    def task_result(self, result, error, done):
        if self.closed:
            return GLib.SOURCE_REMOVE
        if error:
            self.toast(error)
        if done:
            done(result, error)
        return GLib.SOURCE_REMOVE

    def navigate(self, _, row):
        if row is None:
            return
        self.page_name = row.page
        if row.page in ("library", "favorites"):
            self.stack.set_visible_child_name("gallery")
            self.gallery_title.set_text("我的收藏" if row.page == "favorites" else "让桌面，常有新风景。")
            self.gallery_sub.set_text("喜欢就留下。这里的壁纸不会被自动清理。" if row.page == "favorites" else
                                      "发现、收藏，让每一次打开桌面都有好心情。")
            self.render_gallery()
        else:
            self.stack.set_visible_child_name(row.page)
        if row.page == "activity":
            self.read_logs()

    def build_gallery(self):
        page = box(css="page")
        self.stack.add_named(page, "gallery")
        heading = box(Gtk.Orientation.HORIZONTAL, 12)
        titles = box()
        titles.set_hexpand(True)
        titles.append(label("YOUR DAILY SCENERY", "eyebrow"))
        self.gallery_title = label("让桌面，常有新风景。", "page-title")
        titles.append(self.gallery_title)
        self.gallery_sub = label("", "page-subtitle")
        titles.append(self.gallery_sub)
        heading.append(titles)
        self.update_button = button("立即更新", "view-refresh-symbolic", self.start_update, "suggested-action")
        self.update_button.set_valign(Gtk.Align.CENTER)
        heading.append(self.update_button)
        page.append(heading)
        hero = box(Gtk.Orientation.HORIZONTAL, 30, "hero")
        hero_text = box()
        hero_text.set_hexpand(True)
        hero_text.append(label("风景会更新，喜欢的会留下。", "hero-title"))
        self.rule_label = label("", "hero-sub")
        hero_text.append(self.rule_label)
        self.schedule_label = label("正在读取更新计划…", "hero-sub")
        hero_text.append(self.schedule_label)
        hero.append(hero_text)
        self.total_stat = self.stat(hero, "壁纸")
        self.fav_stat = self.stat(hero, "已收藏")
        self.size_stat = self.stat(hero, "占用 MB")
        page.append(hero)
        toolbar = box(Gtk.Orientation.HORIZONTAL, 10)
        self.search = Gtk.SearchEntry(placeholder_text="搜索本地壁纸 ID…")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", lambda *_: self.render_gallery())
        toolbar.append(self.search)
        self.order = Gtk.DropDown.new_from_strings(["最新下载", "最早下载", "文件大小"])
        self.order.connect("notify::selected", lambda *_: self.render_gallery())
        toolbar.append(self.order)
        toolbar.append(button(icon="folder-open-symbolic", action=self.open_folder, tooltip="打开壁纸文件夹"))
        toolbar.append(button(icon="user-trash-symbolic", action=self.preview_cleanup, tooltip="预览并清理超额普通壁纸"))
        toolbar.set_margin_bottom(18)
        page.append(toolbar)
        self.gallery_stack = Gtk.Stack()
        self.gallery_stack.set_vexpand(True)
        page.append(self.gallery_stack)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                               column_spacing=16, row_spacing=16, min_children_per_line=1,
                               max_children_per_line=4, valign=Gtk.Align.START)
        self.flow.add_css_class("gallery")
        scroll.set_child(self.flow)
        self.gallery_stack.add_named(scroll, "images")
        empty = Adw.StatusPage(icon_name="image-x-generic-symbolic", title="这里，等待一片风景。",
                               description="点击「立即更新」下载壁纸，或在偏好设置中选择已有目录。")
        self.empty = empty
        self.gallery_stack.add_named(empty, "empty")
        footer = box(Gtk.Orientation.HORIZONTAL, 12, "footer")
        self.count_label = label("正在整理图库…", "caption")
        self.count_label.set_hexpand(True)
        footer.append(self.count_label)
        footer.append(label("ANIME  ·  SFW", "pill"))
        page.append(footer)

    def stat(self, parent, caption):
        group = box(spacing=3)
        value = label("—", "stat-value", 0.5)
        group.append(value)
        group.append(label(caption, "stat-label", 0.5))
        parent.append(group)
        return value

    def refresh_library(self):
        if self.refresh_busy:
            return
        self.refresh_busy = True
        library = self.library
        def work():
            items = library.items()
            cache = library.state / "thumbnails"
            cache.mkdir(exist_ok=True)
            thumbs = {}
            for item in items:
                key = hashlib.sha256(f"{item.path}:{item.modified}:{item.size}".encode()).hexdigest()
                target = cache / f"{key}.jpg"
                try:
                    with Image.open(item.path) as im:
                        width, height = im.size
                        if not target.exists():
                            im.thumbnail((640, 400))
                            thumb = ImageOps.fit(im.convert("RGB"), (560, 330))
                            thumb.save(target, quality=85)
                    thumbs[item.wid] = (target, width, height)
                except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                    thumbs[item.wid] = (None, 0, 0)
            # Cached thumbnails have no user content of their own; bound cache growth.
            valid = {info[0].name for info in thumbs.values() if info[0]}
            for old in cache.glob("*.jpg"):
                if old.name not in valid:
                    old.unlink(missing_ok=True)
            return library, items, thumbs
        def done(result, error):
            self.refresh_busy = False
            if not error:
                source, items, thumbs = result
                if source is self.library:
                    self.items, self.thumbnails = items, thumbs
                    self.library_signature = self.signature(items)
                    self.render_gallery()
                else:
                    self.refresh_library()
        self.task(work, done)

    @staticmethod
    def signature(items):
        return tuple((str(i.path), i.modified, i.size, i.favorite) for i in items)

    def render_gallery(self):
        if not hasattr(self, "flow"):
            return
        self.flow.remove_all()
        items = [i for i in self.items if (self.page_name != "favorites" or i.favorite)
                 and self.search.get_text().lower() in i.path.name.lower()]
        if self.order.get_selected() == 1:
            items.reverse()
        elif self.order.get_selected() == 2:
            items.sort(key=lambda i: i.size, reverse=True)
        favorites = sum(item.favorite for item in self.items)
        self.total_stat.set_text(str(len(self.items)))
        self.fav_stat.set_text(str(favorites))
        self.size_stat.set_text(f"{sum(i.size for i in self.items) / 1024**2:.0f}")
        self.rule_label.set_text(f"保留 {self.config.keep} 张普通壁纸  ·  每次新增 {self.config.batch} 张  ·  收藏额外保存")
        self.count_label.set_text(f"{len(items)} 张壁纸  /  {favorites} 张收藏受到保护")
        self.gallery_stack.set_visible_child_name("images" if items else "empty")
        if self.search.get_text():
            self.empty.set_title("没有找到这张壁纸")
            self.empty.set_description("试试另一段壁纸 ID，或清空搜索框。")
        elif self.page_name == "favorites":
            self.empty.set_title("把喜欢的风景留下来")
            self.empty.set_description("点击壁纸上的星标即可收藏，收藏不会计入普通壁纸保留数量。")
        else:
            self.empty.set_title("这里，等待一片风景。")
            self.empty.set_description("点击「立即更新」，或在偏好设置中选择已有壁纸目录。")
        for item in items:
            card = box(css="card")
            card.set_size_request(230, -1)
            card.set_overflow(Gtk.Overflow.HIDDEN)
            thumb, width, height = self.thumbnails.get(item.wid, (None, 0, 0))
            picture = Thumbnail() if thumb else Gtk.Image.new_from_icon_name("image-missing-symbolic")
            if thumb:
                picture.set_filename(str(thumb))
            picture.set_size_request(-1, 145)
            if thumb:
                picture.set_content_fit(Gtk.ContentFit.COVER)
                picture.set_can_shrink(True)
            preview = button(action=lambda i=item: self.preview(i), css="thumbnail", tooltip="预览壁纸")
            preview.set_child(picture)
            card.append(preview)
            footer = box(Gtk.Orientation.HORIZONTAL, 8, "card-footer")
            texts = box(spacing=5)
            texts.set_hexpand(True)
            texts.append(label(item.wid.upper(), "card-id"))
            texts.append(label(f"{width} × {height}  ·  {item.size / 1024**2:.1f} MB" if width else "无法读取图片", "caption"))
            footer.append(texts)
            star = button(icon="starred-symbolic" if item.favorite else "non-starred-symbolic",
                          action=lambda i=item: self.toggle_favorite(i),
                          css="favorite" if item.favorite else "flat",
                          tooltip="取消收藏" if item.favorite else "收藏，保护此壁纸")
            star.set_valign(Gtk.Align.CENTER)
            footer.append(star)
            card.append(footer)
            self.flow.append(card)

    def toggle_favorite(self, item):
        library = self.library
        def done(_, error):
            if not error:
                self.toast("已取消收藏，将按保留数量参与清理" if item.favorite else "已收藏，自动清理会跳过这张壁纸")
                self.refresh_library()
        self.task(lambda: library.set_favorite(item.wid, not item.favorite), done)

    def open_uri(self, uri):
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as exc:
            self.toast(str(exc))

    def open_folder(self):
        self.open_uri(self.library.directory.as_uri())

    def preview(self, item):
        win = Gtk.Window(title=f"{item.wid.upper()} · 栖景", transient_for=self, modal=True,
                         default_width=980, default_height=650)
        root = box(spacing=14)
        head = Gtk.HeaderBar()
        win.set_titlebar(head)
        image = Gtk.Picture.new_for_filename(str(item.path))
        image.set_content_fit(Gtk.ContentFit.CONTAIN)
        image.set_can_shrink(True)
        image.set_vexpand(True)
        root.append(image)
        actions = box(Gtk.Orientation.HORIZONTAL, 10)
        actions.set_margin_start(20)
        actions.set_margin_end(20)
        actions.set_margin_bottom(20)
        actions.append(button("打开原图", "image-x-generic-symbolic", lambda: self.open_uri(item.path.as_uri())))
        actions.append(button("Wallhaven 来源", "web-browser-symbolic", lambda: self.open_uri(f"https://wallhaven.cc/w/{item.wid}")))
        star = button("取消收藏" if item.favorite else "收藏壁纸", "starred-symbolic")
        def favorite():
            self.toggle_favorite(item)
            win.close()
        star.connect("clicked", lambda *_: favorite())
        actions.append(star)
        spacer = box()
        spacer.set_hexpand(True)
        actions.append(spacer)
        actions.append(button("设为桌面壁纸", "preferences-desktop-wallpaper-symbolic",
                              lambda: self.apply_wallpaper(item), "suggested-action"))
        root.append(actions)
        win.set_child(root)
        win.present()

    def apply_wallpaper(self, item):
        if self.app.demo:
            self.toast("预览模式不会更改桌面壁纸")
            return
        self.task(lambda: set_wallpaper(item.path, self.config.wallpaper_backend),
                  lambda _, error: self.toast("已设为桌面壁纸") if not error else None)

    def preview_cleanup(self):
        candidates = self.library.cleanup_candidates(self.config.keep)
        if not candidates:
            self.toast("目前没有超额的普通壁纸，无需清理")
            return
        names = {item.path.name for item in candidates}
        size = sum(item.size for item in candidates) / 1024**2
        listing = "\n".join(item.path.name for item in candidates[:8])
        if len(candidates) > 8:
            listing += f"\n…以及另外 {len(candidates) - 8} 张"
        dialog = Adw.MessageDialog(transient_for=self, heading=f"清理 {len(candidates)} 张旧壁纸？",
                                  body=f"将释放约 {size:.1f} MB，保留最新 {self.config.keep} 张普通壁纸。收藏全部保留。删除无法撤销。\n\n{listing}")
        dialog.add_response("cancel", "取消")
        dialog.add_response("clean", "清理这些壁纸")
        dialog.set_response_appearance("clean", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        library, keep = self.library, self.config.keep
        def response(_, answer):
            if answer == "clean":
                def done(result, error):
                    if not error:
                        self.toast(f"已清理 {len(result)} 张壁纸，收藏已保护")
                        logging.getLogger("perch").info("手动清理 %s 张普通壁纸", len(result))
                        self.refresh_library()
                self.task(lambda: library.prune(keep, names), done)
        dialog.connect("response", response)
        dialog.present()

    def start_update(self):
        if self.app.demo:
            self.toast("预览模式不会下载真实壁纸")
            return
        if self.process and self.process.poll() is None:
            return
        command = [sys.executable, "-m", "perch", "update", "--state-directory", str(self.library.state),
                   "--directory", str(self.library.directory)]
        if self.app.config_file:
            command.extend(["--config", str(self.app.config_file)])
        try:
            self.process = subprocess.Popen(command, cwd=Path(__file__).resolve().parent.parent,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            start_new_session=True)
        except OSError as exc:
            self.toast(str(exc))
            return
        self.update_button.set_sensitive(False)
        self.update_button.set_tooltip_text("正在下载；关闭窗口后仍会继续")
        self.toast("开始更新，进度可在「更新记录」查看")

    def build_settings(self):
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        page = box(spacing=12, css="page")
        page.append(label("MAKE IT YOURS", "eyebrow"))
        page.append(label("按你的节奏，换风景。", "page-title"))
        page.append(label("保存后应用到后台计划，关闭窗口也会继续执行。", "page-subtitle"))
        self.controls = {}
        group = self.group(page, "壁纸库", "保留数量只计算普通壁纸，收藏始终额外保存。")
        self.entry(group, "directory", "保存位置", self.config.directory)
        choose = button(icon="folder-open-symbolic", action=self.choose_directory, tooltip="选择壁纸文件夹")
        choose.set_valign(Gtk.Align.CENTER)
        self.controls["directory"].row.add_suffix(choose)
        self.spin(group, "keep", "保留普通壁纸", self.config.keep, 1, 10000)
        self.spin(group, "batch", "每次新增", self.config.batch, 1, 100)
        group = self.group(page, "自动更新", "使用本地时间；时间窗口允许跨越午夜。")
        self.switch(group, "enabled", "启用自动更新", self.config.enabled)
        self.dropdown(group, "schedule_mode", "更新方式", ["interval", "daily"], ["时段内按间隔更新", "每天固定时间点"], self.config.schedule_mode)
        self.spin(group, "interval_hours", "间隔（小时）", self.config.interval_hours, 1, 24)
        self.entry(group, "active_start", "时段开始", self.config.active_start)
        self.entry(group, "active_end", "时段结束", self.config.active_end)
        self.entry(group, "daily_times", "固定时间点（逗号分隔）", ", ".join(self.config.daily_times))
        self.switch(group, "catch_up", "错过计划后补执行一次", self.config.catch_up,
                    "开启后，登录或恢复服务时可能在所选时段外下载")
        self.slots_label = label("", "caption", wrap=True)
        group.add(self.slots_label)
        self.controls["schedule_mode"].connect("notify::selected", lambda *_: self.schedule_changed())
        for key in ("active_start", "active_end", "daily_times"):
            self.controls[key].connect("changed", lambda *_: self.schedule_changed())
        self.controls["interval_hours"].connect("value-changed", lambda *_: self.schedule_changed())
        self.schedule_changed()
        group = self.group(page, "发现偏好", "仅下载 Wallhaven Anime / SFW 静态原图；更改筛选不会删除已有壁纸。")
        self.entry(group, "query", "关键词 / 标签", self.config.query, "例如 landscape、+sky -city")
        self.dropdown(group, "sorting", "发现方式", ["toplist", "date_added", "random", "favorites"],
                      ["热门榜 · 月榜不足时查年榜", "最新上传", "随机发现", "收藏最多"], self.config.sorting)
        self.spin(group, "min_width", "最低宽度（像素）", self.config.min_width, 1, 16384)
        self.spin(group, "min_height", "最低高度（像素）", self.config.min_height, 1, 16384)
        self.dropdown(group, "ratio", "严格宽高比", ["16x9", "16x10", "21x9", "any"],
                      ["16:9", "16:10", "21:9", "不限比例"], self.config.ratio)
        self.spin(group, "max_pages", "每个榜单最多扫描页数", self.config.max_pages, 1, 200)
        group = self.group(page, "桌面集成", "仅在点击「设为桌面壁纸」时更换桌面背景。")
        self.dropdown(group, "wallpaper_backend", "壁纸后端", ["auto", "dms", "awww", "swww", "gnome", "none"],
                      ["自动检测", "DankMaterialShell", "awww", "swww", "GNOME", "不设置桌面背景"], self.config.wallpaper_backend)
        actions = box(Gtk.Orientation.HORIZONTAL, 12)
        self.save_button = button("保存并应用", "emblem-ok-symbolic", self.save_settings, "suggested-action")
        actions.append(self.save_button)
        actions.append(label("目录更改不会搬移或删除原目录文件。", "caption", wrap=True))
        page.append(actions)
        scroll.set_child(page)
        self.stack.add_named(scroll, "settings")

    def group(self, page, title, description):
        group = Adw.PreferencesGroup(title=title, description=description)
        group.add_css_class("settings-group")
        page.append(group)
        return group

    def choose_directory(self):
        dialog = Gtk.FileDialog(title="选择壁纸文件夹")
        def selected(source, result):
            try:
                folder = source.select_folder_finish(result)
                if folder and folder.get_path():
                    self.controls["directory"].set_text(folder.get_path())
            except GLib.Error:
                pass  # Closing the folder chooser is not an application error.
        dialog.select_folder(self, None, selected)

    def row(self, group, key, title, widget, subtitle=None):
        row = Adw.ActionRow(title=title)
        if subtitle:
            row.set_subtitle(subtitle)
        widget.set_valign(Gtk.Align.CENTER)
        row.add_suffix(widget)
        row.set_activatable_widget(widget)
        group.add(row)
        self.controls[key] = widget
        widget.row = row

    def entry(self, group, key, title, value, placeholder=None):
        widget = Gtk.Entry(text=value, width_chars=28)
        if placeholder:
            widget.set_placeholder_text(placeholder)
        self.row(group, key, title, widget)

    def spin(self, group, key, title, value, low, high):
        widget = Gtk.SpinButton.new_with_range(low, high, 1)
        widget.set_value(value)
        self.row(group, key, title, widget)

    def switch(self, group, key, title, value, subtitle=None):
        self.row(group, key, title, Gtk.Switch(active=value), subtitle)

    def dropdown(self, group, key, title, values, titles, value):
        widget = Gtk.DropDown.new_from_strings(titles)
        widget.values = values
        widget.set_selected(values.index(value))
        self.row(group, key, title, widget)

    def read_settings(self):
        data = {}
        for key, widget in self.controls.items():
            if isinstance(widget, Gtk.Switch):
                data[key] = widget.get_active()
            elif isinstance(widget, Gtk.SpinButton):
                data[key] = widget.get_value_as_int()
            elif isinstance(widget, Gtk.DropDown):
                data[key] = widget.values[widget.get_selected()]
            else:
                data[key] = widget.get_text().strip()
        data["daily_times"] = [value.strip() for value in data["daily_times"].replace("，", ",").split(",") if value.strip()]
        return replace(self.config, **data).validate()

    def schedule_changed(self):
        interval = self.controls["schedule_mode"].get_selected() == 0
        for key in ("interval_hours", "active_start", "active_end"):
            self.controls[key].row.set_visible(interval)
        self.controls["daily_times"].row.set_visible(not interval)
        try:
            # Only schedule fields are present while constructing the settings page.
            config = replace(self.config, schedule_mode="interval" if interval else "daily",
                             interval_hours=self.controls["interval_hours"].get_value_as_int(),
                             active_start=self.controls["active_start"].get_text().strip(),
                             active_end=self.controls["active_end"].get_text().strip(),
                             daily_times=[s.strip() for s in self.controls["daily_times"].get_text().replace("，", ",").split(",")])
            config.validate()
            self.slots_label.set_text("每天执行：" + "  ·  ".join(config.slots()))
        except ValueError as exc:
            self.slots_label.set_text(str(exc))

    def save_settings(self):
        try:
            config = self.read_settings()
        except ValueError as exc:
            self.toast(str(exc))
            return
        if self.process and self.process.poll() is None:
            self.toast("请等待本次更新结束后再保存设置")
            return
        self.save_button.set_sensitive(False)
        def work():
            from .library import Library
            # Verify the new location is accessible before applying the timer.
            library = Library(config.directory, self.library.state)
            if self.app.demo or self.app.config_file:
                save(config, self.app.config_file)
            else:
                scheduler.apply(config)
            return library
        def done(library, error):
            self.save_button.set_sensitive(True)
            if not error:
                self.config = config
                self.library = library
                self.toast("设置已保存" if self.app.demo or self.app.config_file else "设置已保存，后台更新计划已生效")
                self.refresh_library()
                self.poll()
        self.task(work, done)

    def build_activity(self):
        page = box(spacing=12, css="page")
        page.append(label("A LITTLE DIARY", "eyebrow"))
        page.append(label("每一次更新，都有记录。", "page-title"))
        page.append(label("下载进度、网络错误和清理结果会显示在这里。", "page-subtitle"))
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_view.add_css_class("log")
        scroll = Gtk.ScrolledWindow(vexpand=True, margin_top=14)
        scroll.set_child(self.log_view)
        page.append(scroll)
        page.append(button("刷新记录", "view-refresh-symbolic", self.read_logs))
        self.stack.add_named(page, "activity")

    def read_logs(self):
        path = self.library.state / "perch.log"
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 50000))
                text = stream.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            text = "还没有更新记录。下一次风景，正在路上。"
        self.log_view.get_buffer().set_text(text or "还没有更新记录。下一次风景，正在路上。")

    def poll(self):
        if self.closed:
            return GLib.SOURCE_REMOVE
        if self.process and self.process.poll() is not None:
            code = self.process.returncode
            self.process = None
            self.update_button.set_sensitive(True)
            self.update_button.set_tooltip_text("下载新壁纸")
            self.toast("更新完成" if code == 0 else "本次更新未完全完成，请查看更新记录")
            self.refresh_library()
        if self.page_name == "activity":
            self.read_logs()
        if not self.status_busy:
            self.status_busy = True
            library = self.library
            def work():
                try:
                    state = "预览模式 · 独立测试图库" if self.app.demo else scheduler.status()
                except Exception:
                    state = "定时服务不可用 · 安装后可启用自动更新"
                return state, self.signature(library.items())
            def done(result, error):
                self.status_busy = False
                if not error:
                    state, signature = result
                    self.schedule_label.set_text(state)
                    if library is self.library and signature != self.library_signature:
                        self.refresh_library()
            self.task(work, done)
        return GLib.SOURCE_CONTINUE
