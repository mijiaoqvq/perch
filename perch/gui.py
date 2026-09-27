"""Native GTK4 interface. Network and thumbnail work never runs on the UI thread."""
from dataclasses import replace
import hashlib
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk
from PIL import Image, ImageOps

from . import __version__
from .config import save
from . import scheduler, colors
from .preference_ui import PreferencePages
from .desktop import set_wallpaper
from .downloader import Client
from .recommendation import CALIBRATION_SAMPLES, TagFetcher, learning_model, sync_feedback
from .widgets import SystemFont, TagChip, tag_cloud

CSS = """
window { background: @window_bg_color; color: @window_fg_color; }
headerbar { background: @headerbar_bg_color; color: @headerbar_fg_color; border: none; box-shadow: none; }
.sidebar { background: @sidebar_bg_color; color: @sidebar_fg_color; border-right: 1px solid alpha(@window_fg_color, 0.08); padding: 28px 18px 18px; }
.brand { font-size: 1.800em; font-weight: 800; letter-spacing: 3px; }
.brand-en { opacity: 0.65; font-size: 0.733em; letter-spacing: 4px; }
.brand-mark { color: mix(@window_fg_color, @accent_bg_color, 0.6); font-size: 2.133em; }
.nav { margin-top: 38px; }
.nav row { padding: 13px 15px; border-radius: 10px; margin-bottom: 6px; }
.nav row:selected { background: alpha(@accent_bg_color, 0.18); color: @window_fg_color; }
.nav row:hover { background: alpha(@window_fg_color, 0.07); }
.nav label { font-weight: 600; }
.muted { opacity: 0.65; }
.caption { font-size: 0.800em; opacity: 0.70; }
.page { padding: 22px 30px 26px; }
.eyebrow { color: mix(@window_fg_color, @accent_bg_color, 0.5); font-size: 0.733em; letter-spacing: 2px; font-weight: 700; }
.page-title { font-size: 2.067em; font-weight: 800; }
.page-subtitle { opacity: 0.70; margin-top: 7px; }
.hero { background: linear-gradient(120deg, mix(@window_bg_color, @accent_bg_color, 0.16), mix(@window_bg_color, @accent_bg_color, 0.05)); border: 1px solid alpha(@accent_bg_color, 0.25); border-radius: 16px; padding: 20px 24px; margin: 22px 0 24px; }
.hero-title { font-size: 1.133em; font-weight: 700; }
.hero-sub { opacity: 0.72; font-size: 0.800em; margin-top: 6px; }
.stat-value { font-size: 1.800em; font-weight: 700; }
.stat-label { font-size: 0.733em; opacity: 0.70; }
button.suggested-action { background: @accent_bg_color; color: @accent_fg_color; font-weight: 700; border-radius: 9px; }
button { border-radius: 8px; }
.gallery { background: transparent; }
.gallery > flowboxchild { padding: 0; margin: 0; border-radius: 13px; }
.card { background: @card_bg_color; color: @card_fg_color; border: 1px solid alpha(@window_fg_color, 0.10); border-radius: 12px; }
.card:hover { border-color: alpha(@accent_bg_color, 0.65); }
.thumbnail { padding: 0; border-radius: 11px 11px 0 0; background: @view_bg_color; }
.card-footer { padding: 12px 13px; }
.card-id { font-weight: 700; font-size: 0.867em; }
.favorite { color: mix(@window_fg_color, @accent_bg_color, 0.5); background: alpha(@accent_bg_color, 0.16); }
.liked { color: mix(@window_fg_color, @error_bg_color, 0.4); background: alpha(@error_bg_color, 0.12); }
.pill { background: alpha(@accent_bg_color, 0.12); color: @window_fg_color; border-radius: 14px; padding: 6px 12px; font-size: 0.733em; }
.footer { padding-top: 16px; }
.log { font-size: 0.800em; padding: 18px; }
.log, .log text { background: @view_bg_color; color: @view_fg_color; }
.tag-chip > button { background: transparent; border: none; box-shadow: none; padding: 6px 9px; border-radius: 8px; font-weight: 600; }
.tag-positive > button { color: mix(@window_fg_color, @accent_bg_color, 0.5); }
.tag-negative > button { color: mix(@window_fg_color, @error_bg_color, 0.4); }
.tag-neutral > button { color: @window_fg_color; }
.tag-muted > button { color: alpha(@window_fg_color, 0.65); }
.tag-chip > button:hover, .tag-chip > button:checked { background: alpha(@accent_bg_color, 0.14); }
.tag-heading { font-size: 0.867em; font-weight: 600; opacity: 0.75; margin-bottom: 5px; }
popover > contents, popover > arrow { background: @popover_bg_color; color: @popover_fg_color; }
.settings-group { margin-bottom: 20px; }
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
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.DEFAULT)
            css = Gtk.CssProvider()
            css.load_from_data(CSS.encode())
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.system_font = SystemFont()
            self.window = PerchWindow(self)
        self.window.present()


class PerchWindow(PreferencePages, Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="栖景 · Perch", default_width=1180, default_height=820)
        self.app = app
        self.config, self.library = app.config, app.library
        self.items = []
        self.thumbnails = {}
        self.page_name = "library"
        self.likes_view = "tags"
        self.profile_generation = 0
        self.learning_samples = 0
        self.tag_edit_busy = False
        self.active_tag_chip = None
        self.spec_window = None
        self.spec_busy = False
        self.spec_generation = 0
        self.process = None
        self.job_kind = None
        self.job_output = None
        self.replacement_jobs = {}
        self.tag_sync_busy = False
        self.tag_sync_requested = False
        self.tag_sync_pending = set()
        self.tag_sync_callbacks = []
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
                                 ("likes", "我喜欢的", "emblem-favorite-symbolic"),
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
        if self.active_tag_chip:
            self.active_tag_chip.close_details()
        self.page_name = row.page
        self.gallery_hero.set_visible(row.page == "library")
        if row.page in ("library", "favorites", "likes"):
            self.stack.set_visible_child_name("gallery")
            self.gallery_title.set_text({"favorites": "我的收藏", "likes": "我喜欢的"}.get(row.page, "让桌面，常有新风景。"))
            self.gallery_sub.set_text({"favorites": "收藏让喜欢的壁纸一直保留。取消喜欢，也会取消收藏。",
                                       "likes": "从喜欢中学习，也听你的调整。喜欢表达偏好，收藏保留图片。"}.get(
                                           row.page, "喜欢让推荐更懂你，收藏让风景留下来。"))
            self.render_gallery()
        else:
            self.stack.set_visible_child_name(row.page)
        if row.page == "activity":
            self.read_logs()
        elif row.page in ("settings", "likes"):
            self.refresh_profile()

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
        self.gallery_sub.set_wrap(True)
        titles.append(self.gallery_sub)
        heading.append(titles)
        self.update_button = button("立即更新", "view-refresh-symbolic", self.start_update, "suggested-action")
        self.update_button.set_valign(Gtk.Align.CENTER)
        heading.append(self.update_button)
        page.append(heading)
        hero = box(Gtk.Orientation.HORIZONTAL, 30, "hero")
        self.gallery_hero = hero
        hero_text = box()
        hero_text.set_hexpand(True)
        hero_text.append(label("让风景常新，让收藏留下。", "hero-title"))
        self.rule_label = label("", "hero-sub")
        hero_text.append(self.rule_label)
        self.schedule_label = label("正在读取更新计划…", "hero-sub")
        hero_text.append(self.schedule_label)
        hero.append(hero_text)
        self.total_stat = self.stat(hero, "壁纸")
        self.fav_stat = self.stat(hero, "已收藏")
        self.size_stat = self.stat(hero, "占用 MB")
        page.append(hero)
        self.likes_tabs = box(Gtk.Orientation.HORIZONTAL, 6)
        self.likes_tabs.set_margin_top(22)
        self.likes_tabs.set_margin_bottom(18)
        self.likes_tab_buttons = {}
        for key, title in (("tags", "推荐标签"), ("colors", "色调偏好"), ("images", "喜欢的壁纸")):
            tab = Gtk.ToggleButton(label=title)
            if self.likes_tab_buttons:
                tab.set_group(self.likes_tab_buttons["tags"])
            tab.set_active(key == self.likes_view)
            tab.connect("toggled", lambda widget, view=key: self.set_likes_view(view) if widget.get_active() else None)
            self.likes_tab_buttons[key] = tab
            self.likes_tabs.append(tab)
        page.append(self.likes_tabs)
        toolbar = box(Gtk.Orientation.HORIZONTAL, 10)
        self.library_toolbar = toolbar
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
        self.build_tag_manager()
        self.build_color_manager()
        footer = box(Gtk.Orientation.HORIZONTAL, 12, "footer")
        self.count_label = label("正在整理图库…", "caption")
        self.count_label.set_hexpand(True)
        footer.append(self.count_label)
        footer.append(label("ANIME  ·  SFW", "pill"))
        page.append(footer)

    def set_likes_view(self, view):
        if self.active_tag_chip:
            self.active_tag_chip.close_details()
        self.likes_view = view
        tab = self.likes_tab_buttons[view]
        if not tab.get_active():
            tab.set_active(True)
        self.render_gallery()
        if view in ("tags", "colors"):
            self.refresh_profile()

    def show_tag_manager(self):
        self.set_likes_view("tags")
        row = self.nav.get_first_child()
        while row:
            if row.page == "likes":
                self.nav.select_row(row)
                break
            row = row.get_next_sibling()

    def build_tag_manager(self):
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        page = box(spacing=14)
        page.set_margin_end(8)
        scroll.set_child(page)
        self.gallery_stack.add_named(scroll, "tags")
        self.profile_status = label("", "caption", wrap=True)
        page.append(self.profile_status)
        self.profile_label = label("正在读取偏好记录…", "caption", wrap=True)
        page.append(self.profile_label)
        self.tag_editor = box(spacing=14)
        page.append(self.tag_editor)
        add = box(Gtk.Orientation.HORIZONTAL, 10)
        self.tag_entry = Gtk.Entry(placeholder_text="添加 Wallhaven 标签，例如 sky 或 cherry blossoms")
        self.tag_entry.set_hexpand(True)
        self.tag_entry.set_max_length(200)
        self.tag_entry.connect("activate", lambda *_: self.add_tag_override())
        add.append(self.tag_entry)
        self.tag_direction = Gtk.DropDown.new_from_strings(["更多推荐", "减少推荐"])
        add.append(self.tag_direction)
        self.tag_add_button = button("添加标签", "list-add-symbolic", self.add_tag_override, "suggested-action")
        add.append(self.tag_add_button)
        self.tag_editor.append(add)
        self.tag_editor.append(label("悬停或点击标签，查看反馈和调整偏好。手动调整立即保存。", "caption", wrap=True))
        self.tag_rows = box(spacing=16)
        self.tag_editor.append(self.tag_rows)
        neutral = box(Gtk.Orientation.HORIZONTAL, 12)
        self.neutral_label = label("", "caption", wrap=True)
        self.neutral_label.set_hexpand(True)
        neutral.append(self.neutral_label)
        neutral.append(button("设置规格名单", "emblem-system-symbolic", self.show_spec_settings))
        page.append(neutral)
        sync = box(Gtk.Orientation.HORIZONTAL, 12)
        sync.append(button("同步已有反馈的标签与色调", "view-refresh-symbolic", self.sync_tags))
        self.sync_label = label("旧图和已删除图片的标签、色调会在同步或下次下载时补全", "caption", wrap=True)
        self.sync_label.set_hexpand(True)
        sync.append(self.sync_label)
        page.append(sync)

    def add_tag_override(self):
        name = self.tag_entry.get_text()
        mode = "prefer" if self.tag_direction.get_selected() == 0 else "avoid"
        self.change_tag_override(name, mode, clear_entry=True)

    def change_tag_override(self, name, mode, clear_entry=False):
        if self.tag_edit_busy:
            return
        self.tag_edit_busy = True
        self.profile_generation += 1
        self.set_focus(None)
        self.tag_editor.set_sensitive(False)
        library = self.library
        def done(_, error):
            self.tag_edit_busy = False
            self.tag_editor.set_sensitive(True)
            if not error:
                if clear_entry and self.tag_entry.get_text() == name:
                    self.tag_entry.set_text("")
                    self.tag_entry.grab_focus()
                self.toast({None: "已恢复自动学习", "prefer": "已设为更多推荐", "avoid": "已设为减少推荐",
                            "ignore": "已移除标签；后续学习不会自动加回，可随时恢复"}[mode])
            self.refresh_profile()
        self.task(lambda: library.set_tag_override(name, mode), done)

    def render_tag_rows(self, profile):
        if self.active_tag_chip:
            self.active_tag_chip.close_details()
        child = self.tag_rows.get_first_child()
        while child:
            self.tag_rows.remove(child)
            child = self.tag_rows.get_first_child()
        self.tag_widgets = {}
        groups = (("更多推荐", "positive", lambda t: not t['ignored'] and t['weight'] > 0),
                  ("减少推荐", "negative", lambda t: not t['ignored'] and t['weight'] < 0),
                  ("暂时中立", "neutral", lambda t: not t['ignored'] and t['weight'] == 0),
                  ("已移除", "muted", lambda t: t['mode'] == 'ignore' and not t['technical']))
        for title, tone, condition in groups:
            tags = [tag for tag in profile if condition(tag)]
            if not tags:
                continue
            group = box(spacing=4)
            group.append(label(f"{title} · {len(tags)}", "tag-heading"))
            cloud = tag_cloud()
            group.append(cloud)
            self.tag_rows.append(group)
            for tag in tags:
                chip = TagChip(self, tag['name'], tone)
                chip.panel.append(label(tag['name'], "heading", wrap=True))
                origin = "自动学习" if tag['mode'] == 'auto' else "手动设置"
                chip.panel.append(label(f"{title} · {origin}\n{tag['positive']} 次正面反馈 · {tag['negative']} 次不喜欢", "caption"))
                widgets = dict(chip=chip)
                if tone == "muted":
                    chip.panel.append(label("不参与评分，也不会被自动加回。", "caption", wrap=True))
                    restore = button("恢复自动学习", action=lambda name=tag['name']: self.change_tag_override(name, None))
                    chip.panel.append(restore)
                    widgets['restore'] = restore
                else:
                    for mode, caption in (("prefer", "更多推荐"), ("avoid", "减少推荐"), (None, "自动学习")):
                        action = button(caption, action=lambda name=tag['name'], mode=mode: self.change_tag_override(name, mode),
                                        css="suggested-action" if mode == (None if tag['mode'] == 'auto' else tag['mode']) else None)
                        chip.panel.append(action)
                        widgets[mode or 'auto'] = action
                    remove = button("移除标签", "list-remove-symbolic", lambda name=tag['name']: self.change_tag_override(name, "ignore"))
                    chip.panel.append(remove)
                    chip.panel.append(label("减少推荐会降低优先级；移除则保持中立。", "caption", wrap=True))
                    widgets['remove'] = remove
                cloud.append(chip)
                self.tag_widgets[tag['key']] = widgets
        if not self.tag_widgets and self.learning_samples >= CALIBRATION_SAMPLES:
            self.tag_rows.append(label("还没有推荐标签。可以手动添加，或喜欢几张壁纸后同步标签。", "caption", wrap=True))
        specs = [tag['name'] for tag in profile if tag['technical']]
        self.neutral_label.set_text("规格名单中的标签不参与推荐评分。" + ("\n当前反馈中：" + "、".join(specs) if specs else ""))

    def show_spec_settings(self):
        if self.spec_window:
            self.spec_window.present()
            return
        win = Gtk.Window(title="规格标签名单 · 栖景", transient_for=self, modal=True,
                         default_width=700, default_height=560)
        win.set_titlebar(Gtk.HeaderBar())
        self.spec_window = win
        def closed(*_):
            if self.active_tag_chip:
                self.active_tag_chip.close_details()
            self.spec_window = None
            self.spec_generation += 1
            return False
        win.connect("close-request", closed)
        page = box(spacing=16, css="page")
        page.append(label("规格标签中立名单", "title-2"))
        page.append(label("名单内的标签不加分、不减分，也不用于偏好搜索。增删立即保存，尺寸筛选仍单独生效。", "caption", wrap=True))
        self.spec_editor = box(spacing=14)
        page.append(self.spec_editor)
        add = box(Gtk.Orientation.HORIZONTAL, 10)
        self.spec_entry = Gtk.Entry(placeholder_text="加入中立名单，例如 HDR", hexpand=True, max_length=200)
        self.spec_entry.connect("activate", lambda *_: self.change_spec_tag(self.spec_entry.get_text(), True))
        self.spec_add_button = button("加入名单", "list-add-symbolic",
                                      lambda: self.change_spec_tag(self.spec_entry.get_text(), True), "suggested-action")
        add.append(self.spec_entry)
        add.append(self.spec_add_button)
        self.spec_editor.append(add)
        self.spec_message = label("", "caption", wrap=True)
        self.spec_editor.append(self.spec_message)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.spec_list = box(spacing=12)
        scroll.set_child(self.spec_list)
        self.spec_editor.append(scroll)
        self.spec_editor.set_vexpand(True)
        page.append(label("悬停或点击标签可移出名单。默认识别常见规格、像素尺寸和比例；移出后，这个名称不会被自动加回。", "caption", wrap=True))
        win.set_child(page)
        win.present()
        self.refresh_spec_list()

    def refresh_spec_list(self):
        if not self.spec_window or self.spec_busy:
            return
        self.spec_generation += 1
        generation = self.spec_generation
        def done(entries, error):
            if error or not self.spec_window or generation != self.spec_generation:
                return
            if self.active_tag_chip:
                self.active_tag_chip.close_details()
            child = self.spec_list.get_first_child()
            while child:
                self.spec_list.remove(child)
                child = self.spec_list.get_first_child()
            self.spec_widgets = {}
            self.spec_list.append(label(f"{len(entries)} 个中立标签", "tag-heading"))
            cloud = tag_cloud()
            self.spec_list.append(cloud)
            for entry in entries:
                chip = TagChip(self, entry['name'])
                chip.panel.append(label(entry['name'], "heading", wrap=True))
                chip.panel.append(label(f"{entry['source']} · 保持中立", "caption"))
                chip.panel.append(label("移出后将恢复已有的自动学习或手动偏好。", "caption", wrap=True))
                remove = button("移出中立名单", "list-remove-symbolic",
                                lambda name=entry['name']: self.change_spec_tag(name, False))
                chip.panel.append(remove)
                cloud.append(chip)
                self.spec_widgets[entry['key']] = dict(chip=chip, remove=remove)
        self.task(self.library.spec_entries, done)

    def change_spec_tag(self, name, enabled):
        if self.spec_busy:
            return
        self.spec_busy = True
        self.spec_generation += 1
        window = self.spec_window
        if window:
            window.set_focus(None)
            self.spec_editor.set_sensitive(False)
        def done(_, error):
            self.spec_busy = False
            if window is self.spec_window and window:
                self.spec_editor.set_sensitive(True)
                self.spec_message.set_text(error or ("已加入中立名单" if enabled else "已移出名单，恢复原有偏好"))
                if not error and enabled and self.spec_entry.get_text() == name:
                    self.spec_entry.set_text("")
                    self.spec_entry.grab_focus()
            self.refresh_spec_list()
            self.refresh_profile()
        self.task(lambda: self.library.set_spec_tag(name, enabled), done)

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
        return tuple((str(i.path), i.modified, i.size, i.favorite, i.disliked, i.liked) for i in items)

    def render_gallery(self):
        if not hasattr(self, "flow"):
            return
        self.flow.remove_all()
        items = [i for i in self.items if (self.page_name != "favorites" or i.favorite)
                 and (self.page_name != "likes" or i.liked)
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
        liked = sum(item.liked for item in self.items)
        self.count_label.set_text(f"{len(items)} 张壁纸  /  {liked} 张喜欢  /  {favorites} 张收藏受保护")
        tags_view = self.page_name == "likes" and self.likes_view in ("tags", "colors")
        self.likes_tabs.set_visible(self.page_name == "likes")
        self.library_toolbar.set_visible(not tags_view)
        self.gallery_hero.set_visible(self.page_name == "library")
        self.library_toolbar.set_margin_top(22 if self.page_name == "favorites" else 0)
        if tags_view:
            self.count_label.set_text("偏好保存在本机 · 标签和色调调整不会更改喜欢或收藏状态")
            self.gallery_stack.set_visible_child_name(self.likes_view)
            return
        self.gallery_stack.set_visible_child_name("images" if items else "empty")
        if self.search.get_text():
            self.empty.set_title("没有找到这张壁纸")
            self.empty.set_description("试试另一段壁纸 ID，或清空搜索框。")
        elif self.page_name == "favorites":
            self.empty.set_title("把喜欢的风景留下来")
            self.empty.set_description("先点击爱心喜欢，再点击星标收藏；收藏额外保存，不计入普通保留数量。")
        elif self.page_name == "likes":
            self.empty.set_title("告诉栖景，你喜欢什么")
            self.empty.set_description("点击爱心表示喜欢；喜欢仍可能被清理，清理后偏好记录会保留。")
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
            footer = box(spacing=6, css="card-footer")
            actions = box(Gtk.Orientation.HORIZONTAL, 5)
            title = label(item.wid.upper(), "card-id")
            title.set_hexpand(True)
            actions.append(title)
            metadata = (f"{width} × {height}  ·  {item.size / 1024**2:.1f} MB" if width else "无法读取图片")
            actions.append(self.dislike_button(item))
            actions.append(self.like_button(item))
            star = button(icon="starred-symbolic" if item.favorite else "non-starred-symbolic",
                          action=lambda i=item: self.toggle_favorite(i),
                          css="favorite" if item.favorite else "flat",
                          tooltip="取消收藏（喜欢状态不变）" if item.favorite else "收藏，保留图片不被清理")
            star.set_valign(Gtk.Align.CENTER)
            if item.liked:
                actions.append(star)
            footer.append(actions)
            status = "已提交 · 等待换新" if item.wid in self.replacement_jobs else ("不喜欢 · 等待替换" if item.disliked else metadata)
            footer.append(label(status, "caption"))
            card.append(footer)
            self.flow.append(card)

    def like_button(self, item, preview=None):
        widget = button(text=("取消喜欢" if item.liked else "喜欢") if preview else None,
                        icon="emblem-favorite-symbolic", css="liked" if item.liked else "flat",
                        tooltip="取消喜欢，同时取消收藏" if item.liked else "喜欢，影响推荐但仍可能被清理")
        def clicked():
            def refreshed():
                if preview and preview.get_visible():
                    updated = next((row for row in self.library.items() if row.wid == item.wid), None)
                    preview.close()
                    if updated:
                        self.preview(updated)
            self.toggle_like(item, refreshed)
        widget.connect("clicked", lambda *_: clicked())
        widget.set_valign(Gtk.Align.CENTER)
        return widget

    def toggle_like(self, item, callback=None):
        library = self.library
        def done(_, error):
            if not error:
                self.toast("已取消喜欢和收藏，图片仍保留至正常清理" if item.liked else "已喜欢；可点击星标收藏，永久保留图片")
                self.refresh_library()
                self.refresh_profile()
                if not item.liked:
                    self.sync_tags(item.wid)
                if callback:
                    callback()
        self.task(lambda: library.set_liked(item.wid, not item.liked), done)

    def dislike_button(self, item, preview=None):
        pending = item.wid in self.replacement_jobs
        tooltip = "先取消收藏，再点击不喜欢" if item.favorite else "不喜欢，换一张新壁纸"
        if item.disliked:
            tooltip = "原图已保留，点击重试换一张"
        if pending:
            tooltip = "这张已提交换图；可以继续标记其他壁纸"
        widget = button(text=("等待换新" if pending else "重试换一张" if item.disliked else "不喜欢") if preview else None,
                        icon="view-refresh-symbolic" if pending or item.disliked else "action-unavailable-symbolic",
                        action=lambda: self.dislike(item, preview), css="flat", tooltip=tooltip)
        widget.set_valign(Gtk.Align.CENTER)
        widget.set_sensitive(not item.favorite and not pending)
        return widget

    def dislike(self, item, preview=None):
        if item.favorite:
            self.toast("请先取消收藏，再点击不喜欢")
            return
        if self.start_job("replace", ["--wallpaper-id", item.wid]):
            self.replacement_jobs[item.wid]['preview'] = preview
            self.render_gallery()
            if preview:
                preview.get_child().set_sensitive(False)
                preview.set_title(f"等待换新 · {item.wid.upper()} · 栖景")
            self.toast(f"已提交换图 · {len(self.replacement_jobs)} 张处理中，可继续标记其他壁纸")

    def toggle_favorite(self, item):
        library = self.library
        def done(_, error):
            if not error:
                self.toast("已取消收藏，将按保留数量参与清理" if item.favorite else "已收藏，自动清理会跳过这张壁纸")
                self.refresh_library()
                self.refresh_profile()
                if not item.favorite:
                    self.sync_tags(item.wid)
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
        if item.liked:
            actions.append(star)
        actions.append(self.like_button(item, win))
        win.dislike_action = self.dislike_button(item, win)
        actions.append(win.dislike_action)
        spacer = box()
        spacer.set_hexpand(True)
        actions.append(spacer)
        actions.append(button("设为桌面壁纸", "preferences-desktop-wallpaper-symbolic",
                              lambda: self.apply_wallpaper(item), "suggested-action"))
        tags_row = box(Gtk.Orientation.HORIZONTAL, 12)
        tags_row.set_margin_start(20)
        tags_row.set_margin_end(20)
        tag_label = label(self.tag_text(item.wid), "caption", wrap=True)
        tag_label.set_hexpand(True)
        tags_row.append(tag_label)
        def update_tags():
            if win.get_visible():
                tag_label.set_text(self.tag_text(item.wid))
        tags_row.append(button("同步标签", action=lambda: self.sync_tags(item.wid, update_tags)))
        root.append(tags_row)
        root.append(actions)
        win.set_child(root)
        if item.wid in self.replacement_jobs:
            self.replacement_jobs[item.wid]['preview'] = win
            root.set_sensitive(False)
            win.set_title(f"等待换新 · {item.wid.upper()} · 栖景")
        win.present()
        return win

    def apply_wallpaper(self, item):
        if self.app.demo:
            self.toast("预览模式不会更改桌面壁纸")
            return
        self.task(lambda: set_wallpaper(item.path, self.config.wallpaper_backend),
                  lambda _, error: self.toast("已设为桌面壁纸") if not error else None)

    def tag_text(self, wid):
        tags = self.library.tags_for(wid)
        if tags is None:
            return "标签尚未同步；同步后用于个性化推荐。"
        return "标签：" + " · ".join(tag['name'] for tag in tags) if tags else "Wallhaven 暂无标签"

    def refresh_profile(self):
        if self.tag_edit_busy:
            return
        self.profile_generation += 1
        generation = self.profile_generation
        library, config = self.library, self.config
        def work():
            feedback = library.feedback()
            pending = sum(library.tags_for(wid) is None for wid in feedback)
            samples, profile = learning_model(library, include_specs=True)
            color_samples, color_profile = colors.learning_model(library)
            return feedback, pending, samples, profile, color_samples, color_profile
        def done(result, error):
            if error or library is not self.library or generation != self.profile_generation:
                return
            feedback, pending, samples, profile, color_samples, color_profile = result
            self.learning_samples = samples
            if not self.config.personalized:
                status = "个性化已关闭 · 标签调整仍会保存，在「偏好设置」开启后生效"
            elif samples < CALIBRATION_SAMPLES:
                status = "自动校准中"
            else:
                status = "个性化已开启 · 手动调整与自动学习共同影响推荐"
            self.profile_status.set_text(status)
            if samples < CALIBRATION_SAMPLES:
                manual = "手动标签照常生效" if self.config.personalized else "手动标签已保存，开启个性化后生效"
                summary = (f"{samples} / {CALIBRATION_SAMPLES} 张有效反馈 · 继续标记喜欢或不喜欢，自动偏好在校准后启用。\n"
                           f"{manual} · {pending} 张待同步标签")
            else:
                summary = (f"已记录 {sum(v > 0 for v in feedback.values())} 张正面反馈、"
                           f"{sum(v < 0 for v in feedback.values())} 张不喜欢 · {pending} 张待同步标签")
            self.profile_label.set_text(summary)
            self.render_tag_rows(profile)
            self.render_color_rows(color_samples, color_profile)
        self.task(work, done)

    def sync_tags(self, wid=None, callback=None):
        if self.app.demo:
            if callback:
                callback()
            self.refresh_profile()
            return
        self.tag_sync_requested = True
        if wid:
            self.tag_sync_pending.add(wid)
        if callback:
            self.tag_sync_callbacks.append(callback)
        if self.tag_sync_busy:
            return
        self.tag_sync_busy = True
        self.tag_sync_requested = False
        pending, self.tag_sync_pending = self.tag_sync_pending, set()
        callbacks, self.tag_sync_callbacks = self.tag_sync_callbacks, []
        library, config = self.library, self.config
        self.sync_label.set_text("正在后台同步标签与色调，仍可继续浏览…")
        def work():
            fetcher = TagFetcher(library, Client())
            for target in pending:
                fetcher.get(target)
            sync_feedback(config, library, fetcher)
            targets = set(library.feedback()) | pending
            return fetcher.unavailable or any(library.tags_for(target) is None for target in targets)
        def done(unavailable, error):
            self.tag_sync_busy = False
            self.sync_label.set_text("部分标签尚待同步，反馈已保存，下次更新会继续补全" if unavailable or error else "标签与色调已同步；偏好只保存在本机")
            self.refresh_profile()
            for callback in callbacks:
                callback()
            if self.tag_sync_requested:
                self.sync_tags()
        self.task(work, done)

    def preview_cleanup(self):
        if getattr(self, 'cleanup_preview_busy', False):
            return
        self.cleanup_preview_busy = True
        library, keep, backend = self.library, self.config.keep, self.config.wallpaper_backend
        def done(candidates, error):
            self.cleanup_preview_busy = False
            if not error and library is self.library:
                self.show_cleanup_preview(candidates, library, keep, backend)
        self.task(lambda: library.cleanup_candidates(keep, backend), done)

    def show_cleanup_preview(self, candidates, library, keep, backend):
        if not candidates:
            self.toast("没有可清理的超额壁纸；收藏和当前桌面壁纸已排除")
            return
        names = {item.path.name for item in candidates}
        size = sum(item.size for item in candidates) / 1024**2
        listing = "\n".join(item.path.name for item in candidates[:8])
        if len(candidates) > 8:
            listing += f"\n…以及另外 {len(candidates) - 8} 张"
        dialog = Adw.MessageDialog(transient_for=self, heading=f"清理 {len(candidates)} 张旧壁纸？",
                                  body=f"将释放约 {size:.1f} MB，保留最新 {keep} 张普通壁纸。收藏和当前桌面壁纸全部保留；删除前会再次核对。删除无法撤销。\n\n{listing}")
        dialog.add_response("cancel", "取消")
        dialog.add_response("clean", "清理这些壁纸")
        dialog.set_response_appearance("clean", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        def response(_, answer):
            if answer == "clean":
                def done(result, error):
                    if not error:
                        self.toast(f"已清理 {len(result)} 张壁纸，收藏和当前桌面壁纸已保护")
                        logging.getLogger("perch").info("手动清理 %s 张普通壁纸", len(result))
                        self.refresh_library()
                self.task(lambda: library.prune(keep, names, backend), done)
        dialog.connect("response", response)
        dialog.present()

    def start_update(self):
        if self.start_job("update"):
            self.render_gallery()
            self.toast("开始更新，进度可在「更新记录」查看")

    def start_job(self, kind, extra=()):
        if self.app.demo:
            self.toast("预览模式不会下载真实壁纸")
            return False
        wid = extra[extra.index('--wallpaper-id') + 1] if kind == 'replace' else None
        if wid in self.replacement_jobs:
            self.toast("这张壁纸已提交换图，可以继续标记其他壁纸")
            return False
        if kind != 'replace' and self.jobs_busy():
            self.toast("正在下载，请等待本次任务完成")
            return False
        command = [sys.executable, "-m", "perch", kind, "--state-directory", str(self.library.state),
                   "--directory", str(self.library.directory), *extra]
        if self.app.config_file:
            command.extend(["--config", str(self.app.config_file)])
        try:
            # An anonymous file allows the worker to finish even after GUI exit.
            output = tempfile.TemporaryFile()
            process = subprocess.Popen(command, cwd=Path(__file__).resolve().parent.parent,
                                       stdout=output, stderr=subprocess.DEVNULL,
                                       start_new_session=True)
        except OSError as exc:
            if 'output' in locals():
                output.close()
            self.toast(str(exc))
            return False
        if kind == 'replace':
            # Submit every image now. Workers wait on run.lock, so the entire
            # queue survives closing the GUI without concurrent downloads.
            self.replacement_jobs[wid] = dict(process=process, output=output, preview=None)
        else:
            self.process = process
            self.job_output, self.job_kind = output, kind
        self.refresh_job_controls()
        return True

    def jobs_busy(self):
        return self.process is not None or bool(self.replacement_jobs)

    def refresh_job_controls(self):
        self.update_button.set_sensitive(not self.jobs_busy())
        self.update_button.set_tooltip_text(
            f"{len(self.replacement_jobs)} 张壁纸等待换新；关闭窗口后仍会继续" if self.replacement_jobs
            else "正在下载；关闭窗口后仍会继续" if self.process else "下载新壁纸")

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
        group = self.group(page, "个性化推荐", "喜欢影响推荐但不防清理；收藏保护图片；不喜欢会降低相关标签优先级。")
        self.switch(group, "personalized", "根据标签与色调个性化推荐", self.config.personalized,
                    f"标签与色调各校准 {CALIBRATION_SAMPLES} 张有效反馈；手动设置可提前生效")
        group.add(button("在「我喜欢的」管理推荐标签", "emblem-favorite-symbolic", self.show_tag_manager))
        group.add(button("在「我喜欢的」选择色调偏好", "applications-graphics-symbolic", self.show_color_manager))
        group.add(button("管理规格标签中立名单", "emblem-system-symbolic", self.show_spec_settings))
        group = self.group(page, "Wallhaven 账户同步", "导入网站收藏夹为喜欢、收藏，或仅用于学习标签和色调。")
        group.add(button("管理账户与收藏夹同步", "folder-remote-symbolic", self.show_account_settings))
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
        if self.jobs_busy():
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
                self.refresh_profile()
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
        completed = False
        for wid, job in list(self.replacement_jobs.items()):
            if job['process'].poll() is None:
                continue
            del self.replacement_jobs[wid]
            output = job['output']
            output.seek(0)
            result = output.read()
            output.close()
            self.finish_replacement_job(wid, job['process'].returncode, result, job['preview'])
            completed = True
        if self.process and self.process.poll() is not None:
            code = self.process.returncode
            self.process = None
            kind, self.job_kind = self.job_kind, None
            self.job_output.seek(0)
            result = self.job_output.read()
            self.job_output.close()
            self.job_output = None
            if kind == 'sync-collections':
                if code == 0:
                    try:
                        stats = json.loads(result)
                        message = f"同步完成 · 新导入 {stats['imported']} 张 · 下载 {stats['downloaded']} 张 · 跳过 {stats['skipped']} 张"
                    except (ValueError, KeyError):
                        message = '收藏夹同步完成'
                else:
                    message = '同步尚未完成，已导入内容已保留；详情见更新记录'
                self.toast(message)
                if getattr(self, 'account_window', None):
                    self.account_status.set_text(message)
            else:
                self.toast("更新完成" if code == 0 else "本次更新未完全完成，请查看更新记录")
            completed = True
        if completed:
            self.refresh_job_controls()
            self.render_gallery()
            self.refresh_library()
            self.refresh_profile()
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

    def finish_replacement_job(self, wid, code, result, preview):
        if preview and preview.get_visible():
            preview.get_child().set_sensitive(True)
            preview.set_title(f"{wid.upper()} · 栖景")
        if code != 0:
            if preview and preview.get_visible():
                item = next((item for item in self.library.items() if item.wid == wid), None)
                if item:
                    old = preview.dislike_action
                    parent = old.get_parent()
                    preview.dislike_action = self.dislike_button(item, preview)
                    parent.insert_child_after(preview.dislike_action, old)
                    parent.remove(old)
            self.toast(f"{wid.upper()} 暂未换新，原图已保留；其他换图任务继续，可稍后重试")
            return
        self.toast(f"{wid.upper()} 已换成新壁纸，原图不再推荐")
        if preview and preview.get_visible():
            try:
                path = Path(json.loads(result)["path"])
                item = next(i for i in self.library.items() if i.path == path)
                preview.close()
                self.preview(item)
            except (ValueError, KeyError, StopIteration):
                self.toast("替换已完成，请在图库查看新壁纸")
