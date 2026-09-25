"""Account connection and compact colour preference pages for the native GUI."""
from gi.repository import Gtk
from . import accounts, colors
from .recommendation import CALIBRATION_SAMPLES
from .widgets import TagChip, tag_cloud


class PreferencePages:
    def build_color_manager(self):
        from .gui import box, label, button
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        page = box(spacing=18)
        scroll.set_child(page)
        self.gallery_stack.add_named(scroll, 'colors')
        self.color_status = label('自动校准中', 'caption', wrap=True)
        page.append(self.color_status)
        page.append(label('选择喜欢的主色调，让下一张风景更合心意。', 'title-3'))
        page.append(label('悬停或点击色块：更多推荐、减少推荐、自动学习或保持中立。手动设置立即保存。', 'caption', wrap=True))
        self.color_rows = box(spacing=18)
        page.append(self.color_rows)
        page.append(label('根据图片前三个主要颜色学习，主色权重更高。色调影响推荐顺序，仍保留探索机会。', 'caption', wrap=True))
        page.append(button('同步已有反馈的标签与色调', 'view-refresh-symbolic', self.sync_tags))
        self.color_widgets = {}
        self.color_edit_busy = False

    def render_color_rows(self, samples, profile):
        from .gui import box, label, button
        if self.color_edit_busy:
            return
        if self.active_tag_chip and self.active_tag_chip.get_ancestor(Gtk.ScrolledWindow) == self.color_rows.get_ancestor(Gtk.ScrolledWindow):
            self.active_tag_chip.close_details()
        child = self.color_rows.get_first_child()
        while child:
            self.color_rows.remove(child)
            child = self.color_rows.get_first_child()
        if not self.config.personalized:
            status = '个性化已关闭 · 色调设置已保存，开启后生效'
        elif samples < CALIBRATION_SAMPLES:
            status = f'自动校准中 · {samples} / {CALIBRATION_SAMPLES} 张有效色调反馈 · 手动偏好照常生效'
        else:
            status = f'色调个性化已开启 · 已学习 {samples} 张反馈壁纸'
        self.color_status.set_text(status)
        self.color_widgets = {}
        for title, tone, condition in (
                ('更多推荐', 'positive', lambda row: row['weight'] > 0),
                ('减少推荐', 'negative', lambda row: row['weight'] < 0),
                ('可选色调', 'neutral', lambda row: row['weight'] == 0 and row['mode'] != 'ignore'),
                ('保持中立', 'muted', lambda row: row['mode'] == 'ignore')):
            rows = [row for row in profile if condition(row)]
            if not rows:
                continue
            group = box(spacing=8)
            group.append(label(title, 'tag-heading'))
            cloud = tag_cloud()
            group.append(cloud)
            self.color_rows.append(group)
            for row in rows:
                chip = TagChip(self, row['name'], tone)
                content = box(Gtk.Orientation.HORIZONTAL, 9)
                swatch = Gtk.Box(width_request=25, height_request=25, valign=Gtk.Align.CENTER)
                css = Gtk.CssProvider()
                css.load_from_data(f'box {{ background: #{row["swatch"]}; border-radius: 50%; border: 1px solid alpha(@window_fg_color, 0.25); }}'.encode())
                swatch.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
                content.append(swatch)
                content.append(label(row['name']))
                chip.set_child(content)
                chip.panel.append(label(row['name'], 'heading'))
                origin = '自动学习' if row['calibrated'] else '自动校准中'
                if row['mode'] != 'auto':
                    origin = '手动设置 · ' + {'prefer': '更多推荐', 'avoid': '减少推荐', 'ignore': '保持中立'}[row['mode']]
                chip.panel.append(label(origin, 'caption'))
                chip.panel.append(label(f"{row['positive']} 次正面反馈 · {row['negative']} 次不喜欢", 'caption'))
                widgets = dict(chip=chip)
                for mode, caption in (('prefer', '更多推荐'), ('avoid', '减少推荐'), (None, '自动学习'), ('ignore', '保持中立')):
                    control = button(caption, action=lambda key=row['key'], mode=mode: self.change_color_override(key, mode),
                                     css='suggested-action' if (mode or 'auto') == row['mode'] else None)
                    chip.panel.append(control)
                    widgets[mode or 'auto'] = control
                chip.panel.append(label('保持中立后不参与评分，直到手动恢复。', 'caption', wrap=True))
                cloud.append(chip)
                self.color_widgets[row['key']] = widgets

    def change_color_override(self, key, mode):
        if self.color_edit_busy:
            return
        if self.active_tag_chip:
            self.active_tag_chip.close_details()
        self.color_edit_busy = True
        self.profile_generation += 1
        self.color_rows.set_sensitive(False)
        library = self.library
        def done(_, error):
            self.color_edit_busy = False
            self.color_rows.set_sensitive(True)
            if not error:
                self.toast('色调偏好已保存')
            self.refresh_profile()
        self.task(lambda: colors.set_override(library, key, mode), done)

    def show_color_manager(self):
        self.show_tag_manager()
        self.set_likes_view('colors')

    def show_account_settings(self):
        from .gui import box, label, button
        if getattr(self, 'account_window', None):
            self.account_window.present()
            return
        path = accounts.account_path(self.app.config_file)
        try:
            self.account = accounts.load_account(path)
        except ValueError as exc:
            self.toast(str(exc))
            self.account = accounts.Account()
        win = Gtk.Window(title='Wallhaven 账户同步 · 栖景', transient_for=self, modal=True,
                         default_width=760, default_height=730)
        win.set_titlebar(Gtk.HeaderBar())
        self.account_window = win
        win.connect('close-request', lambda *_: setattr(self, 'account_window', None) or False)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        win.set_child(scroll)
        page = box(spacing=14, css='page')
        scroll.set_child(page)
        page.append(label('把网站上的喜欢，带回栖景。', 'title-2'))
        page.append(label('从 Wallhaven 单向导入；新增内容会同步，网站移除不会删除本地图片。', 'caption', wrap=True))
        self.account_editor = box(spacing=12)
        page.append(self.account_editor)
        self.account_username = Gtk.Entry(text=self.account.username, placeholder_text='Wallhaven 用户名', max_length=64)
        self.account_editor.append(label('用户名', 'heading'))
        self.account_editor.append(self.account_username)
        self.account_editor.append(label('API Key（公开收藏夹可留空）', 'heading'))
        self.account_key = Gtk.PasswordEntry(show_peek_icon=True)
        self.account_key.set_text(self.account.api_key)
        self.account_editor.append(self.account_key)
        self.account_editor.append(label('密钥仅保存在本机，仅当前用户可读；只发送给 Wallhaven 官方接口。', 'caption', wrap=True))
        self.account_editor.append(button('在 Wallhaven 获取 API Key', 'web-browser-symbolic',
                                          lambda: self.open_uri('https://wallhaven.cc/settings/account')))
        automatic = box(Gtk.Orientation.HORIZONTAL, 12)
        automatic.append(label('随壁纸更新同步所选收藏夹', wrap=True))
        self.account_automatic = Gtk.Switch(active=self.account.automatic, valign=Gtk.Align.CENTER)
        automatic.append(self.account_automatic)
        self.account_editor.append(automatic)
        actions = box(Gtk.Orientation.HORIZONTAL, 10)
        self.account_load_button = button('保存并读取收藏夹', 'view-refresh-symbolic', self.fetch_collections)
        self.account_sync_button = button('保存并立即同步', 'folder-download-symbolic', self.start_collection_sync, 'suggested-action')
        actions.append(self.account_load_button)
        actions.append(self.account_sync_button)
        self.account_editor.append(actions)
        self.account_editor.append(button('仅保存设置', 'emblem-ok-symbolic', self.save_account_settings))
        self.collection_rows = box(spacing=10)
        self.account_editor.append(self.collection_rows)
        self.account_status = label('选择每个收藏夹的导入方式。仅学习偏好不会下载原图。', 'caption', wrap=True)
        page.append(self.account_status)
        page.append(label('沿用当前 Anime / SFW、最低尺寸及宽高比筛选。喜欢可清理，收藏永久保留。\n'
                          '在本机取消喜欢或收藏会优先保留；切换导入方式只补充，不撤回已有导入。', 'caption', wrap=True))
        self.render_collections()
        win.present()

    def render_collections(self):
        from .gui import box, label
        child = self.collection_rows.get_first_child()
        while child:
            self.collection_rows.remove(child)
            child = self.collection_rows.get_first_child()
        self.collection_controls = {}
        for cid, value in self.account.collections.items():
            row = box(Gtk.Orientation.HORIZONTAL, 12)
            title = label(value['label'], wrap=True)
            title.set_hexpand(True)
            row.append(title)
            select = Gtk.DropDown.new_from_strings(list(accounts.MODE_LABELS.values()))
            select.set_selected(list(accounts.MODE_LABELS).index(value['mode']))
            row.append(select)
            self.collection_rows.append(row)
            self.collection_controls[cid] = select

    def read_account_settings(self):
        username = self.account_username.get_text().strip()
        collections = {cid: dict(label=self.account.collections[cid]['label'],
                                mode=list(accounts.MODE_LABELS)[control.get_selected()])
                       for cid, control in self.collection_controls.items()}
        if username.casefold() != self.account.username.casefold():
            collections = {}
        return accounts.Account(username, self.account_key.get_text().strip(), self.account_automatic.get_active(),
                                collections).validate(required=True)

    def save_account_settings(self):
        try:
            account = self.read_account_settings()
            accounts.save_account(account, accounts.account_path(self.app.config_file))
            self.account = account
            self.render_collections()
            self.account_status.set_text('账户同步设置已保存。')
            return True
        except (OSError, ValueError) as exc:
            self.account_status.set_text(str(exc))
            return False

    def fetch_collections(self):
        if not self.save_account_settings():
            return
        account, win = self.account, self.account_window
        win.set_focus(None)
        self.account_editor.set_sensitive(False)
        self.account_status.set_text('正在读取收藏夹…')
        def work():
            try:
                return accounts.list_collections(account)
            except Exception as exc:
                raise ValueError(accounts.friendly_error(exc)) from None
        def done(result, error):
            if self.account_window is not win:
                return
            self.account_editor.set_sensitive(True)
            if error:
                self.account_status.set_text(error)
                return
            account.collections = result
            self.render_collections()
            self.account_status.set_text(f'已读取 {len(result)} 个收藏夹。选择导入方式后保存或同步。')
        self.task(work, done)

    def start_collection_sync(self):
        if not self.save_account_settings():
            return
        if not any(value['mode'] in accounts.MODES for value in self.account.collections.values()):
            self.account_status.set_text('请先读取收藏夹，并为至少一个收藏夹选择导入方式。')
            return
        if self.start_job('sync-collections'):
            self.account_status.set_text('正在后台同步，关闭此窗口也会继续；进度见「更新记录」。')
            self.render_gallery()
