"""Small GTK widgets shared by the tag editor and its settings list."""
import json
import os
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from .appearance import desktop_font_family


class ScrollRestore:
    """Restore a position after GTK allocates changed content, coalescing refreshes."""
    def __init__(self, scroll):
        self.scroll = scroll
        self.target = None
        self.tick = 0
        self.idle = 0

    def schedule(self, target):
        self.target = target
        if self.tick:
            self.scroll.remove_tick_callback(self.tick)
        if self.idle:
            GLib.source_remove(self.idle)
            self.idle = 0
        self.tick = self.scroll.add_tick_callback(self.after_frame)

    def after_frame(self, *_):
        self.tick = 0
        # Tick callbacks run before layout. An idle after that frame sees the
        # new child allocations and adjustment bounds, including wrapped text.
        self.idle = GLib.idle_add(self.restore, priority=GLib.PRIORITY_LOW)
        return GLib.SOURCE_REMOVE

    def restore(self):
        self.idle = 0
        target, self.target = self.target, None
        adjustment = self.scroll.get_vadjustment()
        value = target()
        adjustment.set_value(max(adjustment.get_lower(), min(value,
                             adjustment.get_upper() - adjustment.get_page_size())))
        return GLib.SOURCE_REMOVE


def text_overlap(previous, current):
    """Length of the old suffix retained by a bounded, append-only log tail."""
    if current.startswith(previous):
        return len(previous)
    # KMP avoids quadratic work when a log contains many repeated lines.
    prefix = [0] * len(current)
    matched = 0
    for index in range(1, len(current)):
        while matched and current[index] != current[matched]:
            matched = prefix[matched - 1]
        if current[index] == current[matched]:
            matched += 1
        prefix[index] = matched
    matched = 0
    for char in previous:
        while matched and (matched == len(current) or char != current[matched]):
            matched = prefix[matched - 1]
        if current and char == current[matched]:
            matched += 1
    return matched


def tag_cloud():
    if hasattr(Adw, "WrapBox"):
        return Adw.WrapBox(child_spacing=6, line_spacing=8, natural_line_length=600)
    # GTK 4.12 / libadwaita 1.4 compatibility; current versions wrap by width.
    return Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                       column_spacing=6, row_spacing=8, min_children_per_line=1,
                       max_children_per_line=30, valign=Gtk.Align.START)


class SystemFont:
    """Follow DMS's desktop font on DMS sessions, otherwise the GTK font."""
    def __init__(self, config_dir=None, desktop=None):
        self.config_dir = Path(config_dir or os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        self.desktop = desktop
        self.update_timer = 0
        self.monitors = []
        self.settings = Gtk.Settings.get_default()
        self.provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.settings_handler = self.settings.connect("notify::gtk-font-name", self.update)
        for directory in (self.config_dir, self.config_dir / "DankMaterialShell"):
            if directory.is_dir():
                try:
                    monitor = Gio.File.new_for_path(str(directory)).monitor_directory(Gio.FileMonitorFlags.NONE, None)
                    monitor.connect("changed", self.desktop_changed)
                    self.monitors.append(monitor)
                except GLib.Error:
                    pass
        self.update()

    def desktop_changed(self, *_):
        if self.update_timer:
            GLib.source_remove(self.update_timer)
        self.update_timer = GLib.timeout_add(150, self.update)

    def update(self, *_):
        if self.update_timer:
            GLib.source_remove(self.update_timer)
        self.update_timer = 0
        description = Pango.FontDescription.from_string(self.settings.get_property("gtk-font-name"))
        desktop_family = desktop_font_family(self.config_dir, self.desktop)
        if desktop_family:
            description.set_family(desktop_family)
        self.family = description.get_family()
        self.source = "DMS 桌面字体" if desktop_family else "GTK 系统字体"
        families = ", ".join(json.dumps(name.strip(), ensure_ascii=False)
                             for name in description.get_family().split(","))
        size = description.get_size() / Pango.SCALE
        unit = "px" if description.get_size_is_absolute() else "pt"
        self.provider.load_from_data((f"* {{ font-family: {families}; }}\n"
                                      f"window, popover {{ font-size: {size:g}{unit}; }}").encode())
        return GLib.SOURCE_REMOVE

    def close(self):
        if self.update_timer:
            GLib.source_remove(self.update_timer)
            self.update_timer = 0
        for monitor in self.monitors:
            monitor.cancel()
        self.settings.disconnect(self.settings_handler)
        Gtk.StyleContext.remove_provider_for_display(Gdk.Display.get_default(), self.provider)


class TagChip(Gtk.MenuButton):
    """A hashtag with a hover card; click/keyboard activation also works."""
    def __init__(self, owner, name, tone="neutral"):
        super().__init__()
        self.owner = owner
        self.open_timer = self.close_timer = 0
        self.over_chip = self.over_panel = False
        self.add_css_class("tag-chip")
        self.add_css_class("tag-" + tone)
        self.set_has_frame(False)
        title = Gtk.Label(label="#" + "_".join(name.split()), ellipsize=Pango.EllipsizeMode.END,
                          max_width_chars=32)
        self.set_child(title)
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"标签 {name}，查看详情和操作"])
        self.panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for edge in ("start", "end", "top", "bottom"):
            getattr(self.panel, f"set_margin_{edge}")(10)
        self.panel.set_size_request(275, -1)
        self.popover = Gtk.Popover(autohide=False, has_arrow=True)
        self.popover.set_child(self.panel)
        self.set_popover(self.popover)
        self.motion = Gtk.EventControllerMotion()
        self.motion.connect("enter", self.enter_chip)
        self.motion.connect("leave", self.leave_chip)
        self.add_controller(self.motion)
        self.panel_motion = Gtk.EventControllerMotion()
        self.panel_motion.connect("enter", self.enter_panel)
        self.panel_motion.connect("leave", self.leave_panel)
        self.popover.add_controller(self.panel_motion)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.key_pressed)
        self.popover.add_controller(keys)
        self.popover.connect("show", self.shown)
        self.popover.connect("hide", self.hidden)
        self.connect("unmap", lambda *_: self.close_details())

    def cancel(self, which):
        timer = getattr(self, which)
        if timer:
            GLib.source_remove(timer)
            setattr(self, which, 0)

    def enter_chip(self, *_):
        self.over_chip = True
        self.cancel("close_timer")
        if not self.popover.get_visible() and not self.open_timer:
            self.open_timer = GLib.timeout_add(180, self.open_details)

    def leave_chip(self, *_):
        self.over_chip = False
        self.cancel("open_timer")
        self.schedule_close()

    def enter_panel(self, *_):
        self.over_panel = True
        self.cancel("close_timer")

    def leave_panel(self, *_):
        self.over_panel = False
        self.schedule_close()

    def schedule_close(self):
        self.cancel("close_timer")
        self.close_timer = GLib.timeout_add(350, self.close_if_outside)

    def close_if_outside(self):
        self.close_timer = 0
        if not self.over_chip and not self.over_panel:
            self.close_details()
        return GLib.SOURCE_REMOVE

    def open_details(self):
        self.cancel("open_timer")
        if self.get_mapped():
            self.popup()
        return GLib.SOURCE_REMOVE

    def shown(self, *_):
        previous = self.owner.active_tag_chip
        if previous is not None and previous is not self:
            previous.close_details()
        self.owner.active_tag_chip = self

    def hidden(self, *_):
        if self.owner.active_tag_chip is self:
            self.owner.active_tag_chip = None

    def close_details(self):
        self.cancel("open_timer")
        self.cancel("close_timer")
        self.popdown()

    def key_pressed(self, _, key, *__):
        if key == Gdk.KEY_Escape:
            self.close_details()
            self.grab_focus()
            return True
        return False
