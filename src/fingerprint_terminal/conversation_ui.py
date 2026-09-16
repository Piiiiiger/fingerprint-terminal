"""GTK conversation manager for Claude Code and Codex local histories."""

from __future__ import annotations

import calendar
import math
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .conversations import (
    RETENTION_DAYS,
    Conversation,
    ConversationError,
    ConversationManager,
    TrashEntry,
)


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _format_time(value: float) -> str:
    if value <= 0:
        return "时间未知"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


def _date_bound(value: str, *, end: bool = False) -> float | None:
    text = value.strip()
    if not text:
        return None
    moment = datetime.strptime(text, "%Y-%m-%d")
    if end:
        moment += timedelta(days=1)
    return moment.timestamp()


class ConversationWindow(Adw.Window):
    def __init__(self, parent: Gtk.Window, profile_id: str = "strict-auto-ip") -> None:
        super().__init__(transient_for=parent, title="AI 对话管理")
        self.set_default_size(980, 760)
        self.set_size_request(760, 560)
        self.manager = ConversationManager(profile_id)
        self.manager.cleanup_expired()
        self.selected_provider = "claude"
        self.selected_category = "全部"
        self.start_date_value: date | None = None
        self.end_date_value: date | None = None
        self._date_popover: Gtk.Popover | None = None
        self.category_rows: dict[Gtk.ListBoxRow, str] = {}
        self.active_rows: list[Gtk.Widget] = []
        self.trash_rows: list[Gtk.Widget] = []

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        toolbar = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar)
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        toolbar.add_top_bar(header)

        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.stack)

        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        provider_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        provider_box.add_css_class("linked")
        self.claude_provider_button = Gtk.ToggleButton(label="Claude Code")
        self.codex_provider_button = Gtk.ToggleButton(label="Codex")
        self.codex_provider_button.set_group(self.claude_provider_button)
        self.claude_provider_button.set_active(True)
        self.claude_provider_button.connect(
            "toggled", self._provider_toggled, "claude"
        )
        self.codex_provider_button.connect(
            "toggled", self._provider_toggled, "codex"
        )
        provider_box.append(self.claude_provider_button)
        provider_box.append(self.codex_provider_button)
        title_box.append(provider_box)
        separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        title_box.append(separator)
        title_box.append(switcher)
        header.set_title_widget(title_box)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("刷新")
        refresh.connect("clicked", lambda _button: self.refresh())
        header.pack_end(refresh)

        close_button = Gtk.Button(icon_name="window-close-symbolic")
        close_button.set_tooltip_text("关闭")
        close_button.connect("clicked", self._close_window)
        header.pack_end(close_button)

        self.active_page = self._build_active_page()
        self.trash_page = self._build_trash_page()
        self.stack.add_titled(self.active_page, "active", "对话")
        self.stack.add_titled(self.trash_page, "trash", "回收站")
        toolbar.set_content(self.stack)

        self.refresh()
        self._cleanup_timer = GLib.timeout_add_seconds(3600, self._hourly_cleanup)
        self.connect("close-request", self._on_close_request)

    def _toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast.new(message))

    def _close_window(self, _button: Gtk.Widget | None = None) -> None:
        if self._cleanup_timer:
            GLib.source_remove(self._cleanup_timer)
            self._cleanup_timer = 0
        parent = self.get_transient_for()
        if parent is not None and getattr(parent, "_conversation_window", None) is self:
            setattr(parent, "_conversation_window", None)
        self.set_visible(False)
        self.set_transient_for(None)

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        self._close_window()
        return True

    def _provider_toggled(self, button: Gtk.ToggleButton, provider: str) -> None:
        if not button.get_active() or provider == self.selected_provider:
            return
        self.selected_provider = provider
        self.selected_category = "全部"
        if hasattr(self, "conversations"):
            provider_label = "Claude Code" if provider == "claude" else "Codex"
            self.categories_title.set_label(f"{provider_label} 分类")
            self._refresh_categories()
            self._refresh_active_rows()
            self._refresh_trash_rows()

    def _hourly_cleanup(self) -> bool:
        removed = self.manager.cleanup_expired()
        if removed:
            self._toast(f"已自动永久删除 {removed} 条过期对话")
            self.refresh()
        return True

    def _build_active_page(self) -> Gtk.Widget:
        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        sidebar.set_size_request(220, -1)
        sidebar.set_margin_top(20)
        sidebar.set_margin_bottom(20)
        sidebar.set_margin_start(18)
        sidebar.set_margin_end(18)
        root.append(sidebar)

        self.categories_title = Gtk.Label(label="Claude Code 分类", xalign=0)
        self.categories_title.add_css_class("title-3")
        sidebar.append(self.categories_title)

        self.category_list = Gtk.ListBox()
        self.category_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.category_list.add_css_class("boxed-list")
        self.category_list.connect("row-selected", self._category_selected)
        sidebar.append(self.category_list)

        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.category_entry = Gtk.Entry()
        self.category_entry.set_placeholder_text("新分类")
        self.category_entry.set_hexpand(True)
        self.category_entry.connect("activate", self._add_category)
        add_box.append(self.category_entry)
        add_button = Gtk.Button(icon_name="list-add-symbolic")
        add_button.set_tooltip_text("添加分类")
        add_button.connect("clicked", self._add_category)
        add_box.append(add_button)
        sidebar.append(add_box)

        separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        root.append(separator)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        right.set_hexpand(True)
        right.set_margin_top(20)
        right.set_margin_bottom(20)
        right.set_margin_start(20)
        right.set_margin_end(20)
        root.append(right)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("搜索标题")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", lambda _entry: self._refresh_active_rows())
        controls.append(self.search)

        right.append(controls)

        date_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        date_label = Gtk.Label(label="日期范围")
        date_label.add_css_class("dim-label")
        date_controls.append(date_label)

        self.start_date_button = Gtk.Button(label="开始日期")
        self.start_date_button.add_css_class("pill")
        self.start_date_button.set_tooltip_text("选择开始日期")
        self.start_date_button.connect("clicked", self._open_date_picker, "start")
        date_controls.append(self.start_date_button)

        dash = Gtk.Label(label="—")
        dash.add_css_class("dim-label")
        date_controls.append(dash)

        self.end_date_button = Gtk.Button(label="结束日期")
        self.end_date_button.add_css_class("pill")
        self.end_date_button.set_tooltip_text("选择结束日期")
        self.end_date_button.connect("clicked", self._open_date_picker, "end")
        date_controls.append(self.end_date_button)

        clear_dates = Gtk.Button(label="清除日期")
        clear_dates.add_css_class("flat")
        clear_dates.connect("clicked", self._clear_date_filter)
        date_controls.append(clear_dates)
        right.append(date_controls)

        category_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        category_header.set_margin_top(2)
        self.category_heading = Gtk.Label(label="全部对话", xalign=0)
        self.category_heading.add_css_class("title-2")
        self.category_heading.set_hexpand(True)
        category_header.append(self.category_heading)
        self.clear_category_button = Gtk.Button(label="本类移入回收站")
        self.clear_category_button.add_css_class("destructive-action")
        self.clear_category_button.set_sensitive(False)
        self.clear_category_button.connect("clicked", self._clear_selected_category)
        category_header.append(self.clear_category_button)
        right.append(category_header)

        self.active_empty = Gtk.Label(
            label="没有符合条件的对话",
            xalign=0.5,
            yalign=0.5,
        )
        self.active_empty.add_css_class("dim-label")

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        right.append(scroller)
        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        scroller.set_child(list_box)
        self.active_group = Adw.PreferencesGroup()
        list_box.append(self.active_group)
        list_box.append(self.active_empty)

        return root

    def _build_trash_page(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        clamp = Adw.Clamp()
        clamp.set_maximum_size(850)
        clamp.set_tightening_threshold(700)
        scroller.set_child(clamp)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        page.set_margin_top(26)
        page.set_margin_bottom(30)
        page.set_margin_start(24)
        page.set_margin_end(24)
        clamp.set_child(page)

        self.trash_heading = Gtk.Label(label="Claude Code 回收站", xalign=0)
        self.trash_heading.add_css_class("title-1")
        page.append(self.trash_heading)
        self.trash_hint = Gtk.Label(
            label=f"移入这里的 Claude Code 对话不会再被 Claude Code 识别，保留 {RETENTION_DAYS} 天后自动永久删除。",
            xalign=0,
            wrap=True,
        )
        self.trash_hint.add_css_class("dim-label")
        page.append(self.trash_hint)

        self.trash_group = Adw.PreferencesGroup()
        page.append(self.trash_group)
        self.trash_empty = Gtk.Label(label="回收站为空", xalign=0.5)
        self.trash_empty.add_css_class("dim-label")
        page.append(self.trash_empty)
        return scroller

    def refresh(self) -> None:
        removed = self.manager.cleanup_expired()
        if removed:
            self._toast(f"已自动永久删除 {removed} 条过期对话")
        self.conversations = self.manager.discover()
        self.trash = self.manager.trash_entries()
        self._refresh_categories()
        self._refresh_active_rows()
        self._refresh_trash_rows()

    def _clear_listbox(self, box: Gtk.ListBox) -> None:
        while row := box.get_row_at_index(0):
            box.remove(row)

    def _refresh_categories(self) -> None:
        previous = self.selected_category
        self._clear_listbox(self.category_list)
        self.category_rows.clear()
        categories = ["全部", *self.manager.categories()]
        counts = {category: 0 for category in categories}
        provider_conversations = [
            conversation
            for conversation in self.conversations
            if conversation.provider == self.selected_provider
        ]
        counts["全部"] = len(provider_conversations)
        for conversation in provider_conversations:
            counts[conversation.category] = counts.get(conversation.category, 0) + 1

        selected_row: Gtk.ListBoxRow | None = None
        for category in categories:
            row = Gtk.ListBoxRow()
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            content.set_margin_top(8)
            content.set_margin_bottom(8)
            content.set_margin_start(10)
            content.set_margin_end(10)
            label = Gtk.Label(label=category, xalign=0)
            label.set_hexpand(True)
            content.append(label)
            count = Gtk.Label(label=str(counts.get(category, 0)))
            count.add_css_class("dim-label")
            content.append(count)
            if category not in {"全部", "未分类"}:
                remove = Gtk.Button(icon_name="window-close-symbolic")
                remove.add_css_class("flat")
                remove.set_tooltip_text("删除分类；其中的对话会回到“未分类”")
                remove.connect("clicked", self._remove_category, category)
                content.append(remove)
            row.set_child(content)
            self.category_list.append(row)
            self.category_rows[row] = category
            if category == previous:
                selected_row = row
        if selected_row is None:
            selected_row = self.category_list.get_row_at_index(0)
            self.selected_category = "全部"
        if selected_row is not None:
            self.category_list.select_row(selected_row)

    def _category_selected(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        self.selected_category = self.category_rows.get(row, "全部")
        self._refresh_active_rows()

    def _add_category(self, _widget: Gtk.Widget) -> None:
        try:
            name = self.manager.add_category(self.category_entry.get_text())
        except ConversationError as exc:
            self._toast(str(exc))
            return
        self.category_entry.set_text("")
        self.selected_category = name
        self.refresh()
        self._toast(f"已添加分类“{name}”")

    def _remove_category(self, _button: Gtk.Button, category: str) -> None:
        try:
            self.manager.remove_category(category)
        except ConversationError as exc:
            self._toast(str(exc))
            return
        if self.selected_category == category:
            self.selected_category = "未分类"
        self.refresh()
        self._toast(f"已删除分类“{category}”，其中对话已归入“未分类”")

    def _clear_active_group(self) -> None:
        for row in self.active_rows:
            self.active_group.remove(row)
        self.active_rows.clear()

    def _available_date_counts(self) -> dict[date, int]:
        counts: dict[date, int] = {}
        if not hasattr(self, "conversations"):
            return counts
        for conversation in self.conversations:
            if conversation.provider != self.selected_provider:
                continue
            if self.selected_category != "全部" and conversation.category != self.selected_category:
                continue
            if conversation.updated_at <= 0:
                continue
            day = datetime.fromtimestamp(conversation.updated_at).date()
            counts[day] = counts.get(day, 0) + 1
        return counts

    def _sync_date_controls(self) -> None:
        available = set(self._available_date_counts())
        if self.start_date_value is not None and self.start_date_value not in available:
            self.start_date_value = None
        if self.end_date_value is not None and self.end_date_value not in available:
            self.end_date_value = None
        if (
            self.start_date_value is not None
            and self.end_date_value is not None
            and self.start_date_value > self.end_date_value
        ):
            self.end_date_value = None

        enabled = bool(available)
        self.start_date_button.set_sensitive(enabled)
        self.end_date_button.set_sensitive(enabled)
        self.start_date_button.set_label(
            self.start_date_value.isoformat() if self.start_date_value else "开始日期"
        )
        self.end_date_button.set_label(
            self.end_date_value.isoformat() if self.end_date_value else "结束日期"
        )

    def _close_date_popover(self) -> None:
        popover = self._date_popover
        self._date_popover = None
        if popover is None:
            return
        popover.popdown()
        if popover.get_parent() is not None:
            popover.unparent()

    def _open_date_picker(self, button: Gtk.Button, target: str) -> None:
        counts = self._available_date_counts()
        if not counts:
            self._toast("当前分类没有可选的聊天日期")
            return

        self._close_date_popover()
        popover = Gtk.Popover()
        popover.set_autohide(True)
        popover.set_has_arrow(True)
        popover.set_position(Gtk.PositionType.BOTTOM)
        popover.set_parent(button)
        self._date_popover = popover

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.set_margin_top(12)
        body.set_margin_bottom(12)
        body.set_margin_start(12)
        body.set_margin_end(12)
        popover.set_child(body)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        previous = Gtk.Button(icon_name="go-previous-symbolic")
        previous.add_css_class("flat")
        month_label = Gtk.Label()
        month_label.set_hexpand(True)
        month_label.add_css_class("heading")
        month_label.set_xalign(0.5)
        following = Gtk.Button(icon_name="go-next-symbolic")
        following.add_css_class("flat")
        header.append(previous)
        header.append(month_label)
        header.append(following)
        body.append(header)

        grid = Gtk.Grid(column_spacing=4, row_spacing=4)
        body.append(grid)

        hint = Gtk.Label(label="带圆点的日期有聊天记录；灰色日期不可选", xalign=0)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        body.append(hint)

        months = sorted({(day.year, day.month) for day in counts})
        selected = self.start_date_value if target == "start" else self.end_date_value
        if selected is None:
            selected = self.end_date_value if target == "start" else self.start_date_value
        current_month = (selected.year, selected.month) if selected else months[-1]
        if current_month not in months:
            current_month = months[-1]
        state = {"month": current_month}

        weekday_names = ("一", "二", "三", "四", "五", "六", "日")

        def clear_grid() -> None:
            child = grid.get_first_child()
            while child is not None:
                next_child = child.get_next_sibling()
                grid.remove(child)
                child = next_child

        def choose_day(_button: Gtk.Button, chosen: date) -> None:
            if target == "start":
                self.start_date_value = chosen
                if self.end_date_value is not None and chosen > self.end_date_value:
                    self.end_date_value = None
            else:
                self.end_date_value = chosen
                if self.start_date_value is not None and chosen < self.start_date_value:
                    self.start_date_value = None
            self._sync_date_controls()
            self._close_date_popover()
            self._refresh_active_rows()

        def render_month() -> None:
            clear_grid()
            year, month = state["month"]
            month_label.set_label(f"{year}年{month}月")

            month_index = months.index((year, month))
            previous.set_sensitive(month_index > 0)
            following.set_sensitive(month_index + 1 < len(months))

            for column, name in enumerate(weekday_names):
                label = Gtk.Label(label=name)
                label.add_css_class("dim-label")
                label.add_css_class("caption")
                grid.attach(label, column, 0, 1, 1)

            weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
            for row_index, week in enumerate(weeks, start=1):
                for column, day_number in enumerate(week):
                    if day_number == 0:
                        spacer = Gtk.Label(label="")
                        spacer.set_size_request(42, 44)
                        grid.attach(spacer, column, row_index, 1, 1)
                        continue

                    chosen = date(year, month, day_number)
                    count = counts.get(chosen, 0)
                    day_button = Gtk.Button()
                    day_button.set_size_request(42, 44)
                    day_button.add_css_class("flat")
                    day_button.set_sensitive(count > 0)

                    day_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
                    day_label = Gtk.Label(label=str(day_number))
                    day_box.append(day_label)
                    marker = Gtk.Label(label="•" if count else "")
                    marker.add_css_class("caption")
                    if count:
                        marker.add_css_class("accent")
                        day_button.add_css_class("accent")
                        day_button.set_tooltip_text(f"{count} 条聊天记录")
                        day_button.connect("clicked", choose_day, chosen)
                    else:
                        marker.add_css_class("dim-label")
                    day_box.append(marker)
                    day_button.set_child(day_box)

                    if chosen in {self.start_date_value, self.end_date_value}:
                        day_button.remove_css_class("flat")
                        day_button.add_css_class("suggested-action")
                    grid.attach(day_button, column, row_index, 1, 1)

        def move_month(_button: Gtk.Button, delta: int) -> None:
            index = months.index(state["month"])
            target_index = index + delta
            if 0 <= target_index < len(months):
                state["month"] = months[target_index]
                render_month()

        previous.connect("clicked", move_month, -1)
        following.connect("clicked", move_month, 1)
        def on_closed(closed: Gtk.Popover) -> None:
            if self._date_popover is closed:
                self._date_popover = None
            if closed.get_parent() is not None:
                closed.unparent()

        popover.connect("closed", on_closed)
        render_month()
        popover.popup()

    def _clear_date_filter(self, _button: Gtk.Button | None = None) -> None:
        self.start_date_value = None
        self.end_date_value = None
        self._close_date_popover()
        self._sync_date_controls()
        self._refresh_active_rows()

    def _filtered_conversations(self) -> list[Conversation]:
        query = self.search.get_text().strip().casefold()
        result: list[Conversation] = []
        for conversation in self.conversations:
            if conversation.provider != self.selected_provider:
                continue
            if self.selected_category != "全部" and conversation.category != self.selected_category:
                continue
            if query and query not in conversation.title.casefold():
                continue
            day = datetime.fromtimestamp(conversation.updated_at).date()
            if self.start_date_value is not None and day < self.start_date_value:
                continue
            if self.end_date_value is not None and day > self.end_date_value:
                continue
            result.append(conversation)
        return result

    def _refresh_active_rows(self) -> None:
        if not hasattr(self, "conversations"):
            return
        self._sync_date_controls()
        self._clear_active_group()
        records = self._filtered_conversations()
        self.active_empty.set_visible(not records)
        provider_label = "Claude Code" if self.selected_provider == "claude" else "Codex"
        if self.selected_category == "全部":
            self.category_heading.set_label(f"{provider_label} · 全部对话 · {len(records)}")
            self.clear_category_button.set_sensitive(False)
        else:
            self.category_heading.set_label(
                f"{provider_label} · {self.selected_category} · {len(records)}"
            )
            category_count = sum(
                1
                for item in self.conversations
                if item.provider == self.selected_provider
                and item.category == self.selected_category
            )
            self.clear_category_button.set_sensitive(category_count > 0)

        categories = self.manager.categories()
        for conversation in records:
            row = Adw.ActionRow(title=conversation.title)
            project = Path(conversation.cwd).name if conversation.cwd else "目录未知"
            row.set_subtitle(
                f"{project} · {_format_time(conversation.updated_at)} · {_format_bytes(conversation.size_bytes)}"
            )

            combo = Gtk.ComboBoxText()
            for category in categories:
                combo.append_text(category)
            try:
                combo.set_active(categories.index(conversation.category))
            except ValueError:
                combo.set_active(0)
            combo.set_tooltip_text("分类")
            combo.connect("changed", self._category_changed, conversation.key)
            row.add_suffix(combo)

            edit = Gtk.Button(icon_name="document-edit-symbolic")
            edit.add_css_class("flat")
            edit.set_tooltip_text("修改标题")
            edit.set_valign(Gtk.Align.CENTER)
            edit.connect(
                "clicked",
                self._edit_title,
                conversation.key,
                conversation.title,
            )
            row.add_suffix(edit)

            trash = Gtk.Button(icon_name="user-trash-symbolic")
            trash.add_css_class("flat")
            trash.set_tooltip_text("移入回收站")
            trash.set_valign(Gtk.Align.CENTER)
            trash.connect("clicked", self._trash_one, conversation.key)
            row.add_suffix(trash)
            self.active_group.add(row)
            self.active_rows.append(row)

    def _edit_title(
        self,
        _button: Gtk.Button,
        key: str,
        current_title: str,
    ) -> None:
        dialog = Adw.MessageDialog.new(self, "修改对话标题", "")
        dialog.add_response("cancel", "取消")
        dialog.add_response("save", "保存")
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        entry = Gtk.Entry()
        entry.set_text(current_title)
        entry.set_max_length(200)
        entry.set_hexpand(True)
        entry.set_activates_default(True)
        entry.set_margin_top(8)
        entry.set_margin_bottom(4)
        dialog.set_extra_child(entry)
        dialog.connect("response", self._edit_title_response, key, entry)
        dialog.present()
        entry.grab_focus()
        entry.select_region(0, -1)

    def _edit_title_response(
        self,
        dialog: Adw.MessageDialog,
        response: str,
        key: str,
        entry: Gtk.Entry,
    ) -> None:
        if response != "save":
            return
        try:
            self.manager.rename_conversation(key, entry.get_text())
        except ConversationError as exc:
            self._toast(str(exc))
            return
        self._toast("标题已更新")
        self.refresh()

    def _category_changed(self, combo: Gtk.ComboBoxText, key: str) -> None:
        category = combo.get_active_text()
        if not category:
            return
        try:
            self.manager.assign_category(key, category)
        except ConversationError as exc:
            self._toast(str(exc))
            return
        self.conversations = self.manager.discover()
        self._refresh_categories()
        self._refresh_active_rows()

    def _trash_one(self, _button: Gtk.Button, key: str) -> None:
        try:
            self.manager.move_to_trash(key)
        except ConversationError as exc:
            self._toast(str(exc))
            return
        self._toast("已移入回收站，14 天内可恢复")
        self.refresh()

    def _clear_selected_category(self, _button: Gtk.Button) -> None:
        if self.selected_category == "全部":
            return
        moved, errors = self.manager.move_category_to_trash(
            self.selected_category, provider=self.selected_provider
        )
        if moved:
            self._toast(f"已将“{self.selected_category}”中的 {moved} 条对话移入回收站")
        if errors:
            self._toast(errors[0] if len(errors) == 1 else f"{len(errors)} 条对话未能移动，请先退出对应 CLI")
        self.refresh()

    def _clear_trash_group(self) -> None:
        for row in self.trash_rows:
            self.trash_group.remove(row)
        self.trash_rows.clear()

    def _refresh_trash_rows(self) -> None:
        if not hasattr(self, "trash"):
            return
        self._clear_trash_group()
        entries = [
            entry for entry in self.trash if entry.provider == self.selected_provider
        ]
        provider_label = "Claude Code" if self.selected_provider == "claude" else "Codex"
        self.trash_heading.set_label(f"{provider_label} 回收站 · {len(entries)}")
        self.trash_hint.set_label(
            f"移入这里的 {provider_label} 对话不会再被 {provider_label} 识别，"
            f"保留 {RETENTION_DAYS} 天后自动永久删除。"
        )
        self.trash_empty.set_label(f"{provider_label} 回收站为空")
        self.trash_empty.set_visible(not entries)
        now = time.time()
        for entry in entries:
            days = max(0, math.ceil((entry.expires_at - now) / 86400))
            row = Adw.ActionRow(title=entry.title)
            project = Path(entry.cwd).name if entry.cwd else "目录未知"
            row.set_subtitle(f"{entry.category} · {project} · {days} 天后永久删除")

            restore = Gtk.Button(label="恢复")
            restore.set_valign(Gtk.Align.CENTER)
            restore.connect("clicked", self._restore_one, entry.key)
            row.add_suffix(restore)

            delete = Gtk.Button(icon_name="edit-delete-symbolic")
            delete.add_css_class("flat")
            delete.set_tooltip_text("永久删除")
            delete.set_valign(Gtk.Align.CENTER)
            delete.connect("clicked", self._confirm_delete, entry.key, entry.title)
            row.add_suffix(delete)
            self.trash_group.add(row)
            self.trash_rows.append(row)

    def _restore_one(self, _button: Gtk.Button, key: str) -> None:
        try:
            self.manager.restore(key)
        except ConversationError as exc:
            self._toast(str(exc))
            return
        self._toast("对话已恢复，重新打开对应 CLI 后可继续使用")
        self.refresh()

    def _confirm_delete(self, _button: Gtk.Button, key: str, title: str) -> None:
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="永久删除这条对话？",
            body=f"“{title}”将无法恢复。",
        )
        dialog.add_response("cancel", "取消")
        dialog.add_response("delete", "永久删除")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._delete_response, key)
        dialog.present()

    def _delete_response(self, dialog: Adw.MessageDialog, response: str, key: str) -> None:
        if response == "delete":
            try:
                self.manager.delete_permanently(key)
            except ConversationError as exc:
                self._toast(str(exc))
            else:
                self._toast("已永久删除")
                self.refresh()
        dialog.close()
