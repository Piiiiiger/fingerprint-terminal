"""GTK conversation manager for Claude Code and Codex local histories."""

from __future__ import annotations

import math
import time
from datetime import datetime
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


class ConversationWindow(Adw.Window):
    def __init__(self, parent: Gtk.Window, profile_id: str = "strict-auto-ip") -> None:
        super().__init__(transient_for=parent, title="AI 对话管理")
        self.set_default_size(980, 760)
        self.set_size_request(760, 560)
        self.manager = ConversationManager(profile_id)
        self.manager.cleanup_expired()
        self.selected_category = "全部"
        self.category_rows: dict[Gtk.ListBoxRow, str] = {}
        self.active_rows: list[Gtk.Widget] = []
        self.trash_rows: list[Gtk.Widget] = []

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        toolbar = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar)
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.stack)
        header.set_title_widget(switcher)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("刷新")
        refresh.connect("clicked", lambda _button: self.refresh())
        header.pack_end(refresh)

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

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        if self._cleanup_timer:
            GLib.source_remove(self._cleanup_timer)
            self._cleanup_timer = 0
        return False

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

        categories_title = Gtk.Label(label="分类", xalign=0)
        categories_title.add_css_class("title-3")
        sidebar.append(categories_title)

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
        self.search.set_placeholder_text("搜索标题、项目或会话 ID")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", lambda _entry: self._refresh_active_rows())
        controls.append(self.search)

        self.provider_filter = Gtk.ComboBoxText()
        self.provider_filter.append("all", "全部来源")
        self.provider_filter.append("claude", "Claude Code")
        self.provider_filter.append("codex", "Codex")
        self.provider_filter.set_active_id("all")
        self.provider_filter.connect("changed", lambda _combo: self._refresh_active_rows())
        controls.append(self.provider_filter)
        right.append(controls)

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

        title = Gtk.Label(label="回收站", xalign=0)
        title.add_css_class("title-1")
        page.append(title)
        hint = Gtk.Label(
            label=f"移入这里的 Claude Code / Codex 对话不会再被对应 CLI 识别，保留 {RETENTION_DAYS} 天后自动永久删除。",
            xalign=0,
            wrap=True,
        )
        hint.add_css_class("dim-label")
        page.append(hint)

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
        counts["全部"] = len(self.conversations)
        for conversation in self.conversations:
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

    def _filtered_conversations(self) -> list[Conversation]:
        query = self.search.get_text().strip().casefold()
        provider = self.provider_filter.get_active_id() or "all"
        result: list[Conversation] = []
        for conversation in self.conversations:
            if self.selected_category != "全部" and conversation.category != self.selected_category:
                continue
            if provider != "all" and conversation.provider != provider:
                continue
            if query:
                haystack = " ".join(
                    (
                        conversation.title,
                        conversation.cwd,
                        conversation.session_id,
                        conversation.category,
                    )
                ).casefold()
                if query not in haystack:
                    continue
            result.append(conversation)
        return result

    def _refresh_active_rows(self) -> None:
        if not hasattr(self, "conversations"):
            return
        self._clear_active_group()
        records = self._filtered_conversations()
        self.active_empty.set_visible(not records)
        if self.selected_category == "全部":
            self.category_heading.set_label(f"全部对话 · {len(records)}")
            self.clear_category_button.set_sensitive(False)
        else:
            self.category_heading.set_label(f"{self.selected_category} · {len(records)}")
            category_count = sum(
                1 for item in self.conversations if item.category == self.selected_category
            )
            self.clear_category_button.set_sensitive(category_count > 0)

        categories = self.manager.categories()
        for conversation in records:
            row = Adw.ActionRow(title=conversation.title)
            provider = "Claude Code" if conversation.provider == "claude" else "Codex"
            project = Path(conversation.cwd).name if conversation.cwd else "目录未知"
            row.set_subtitle(
                f"{provider} · {project} · {_format_time(conversation.updated_at)} · {_format_bytes(conversation.size_bytes)}"
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

            trash = Gtk.Button(icon_name="user-trash-symbolic")
            trash.add_css_class("flat")
            trash.set_tooltip_text("移入回收站")
            trash.set_valign(Gtk.Align.CENTER)
            trash.connect("clicked", self._trash_one, conversation.key)
            row.add_suffix(trash)
            self.active_group.add(row)
            self.active_rows.append(row)

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
        moved, errors = self.manager.move_category_to_trash(self.selected_category)
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
        self.trash_empty.set_visible(not self.trash)
        now = time.time()
        for entry in self.trash:
            provider = "Claude Code" if entry.provider == "claude" else "Codex"
            days = max(0, math.ceil((entry.expires_at - now) / 86400))
            row = Adw.ActionRow(title=entry.title)
            project = Path(entry.cwd).name if entry.cwd else "目录未知"
            row.set_subtitle(f"{provider} · {entry.category} · {project} · {days} 天后永久删除")

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
