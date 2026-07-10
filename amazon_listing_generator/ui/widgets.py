"""Reusable CustomTkinter widgets."""

from __future__ import annotations

from typing import Callable

import customtkinter as ctk

from ui import styles


class FilePickerRow(ctk.CTkFrame):
    """A labelled row with a read-only path entry and a Browse button."""

    def __init__(
        self,
        master: ctk.CTkBaseClass,
        label: str,
        browse_command: Callable[[], str | None],
        placeholder: str = "No file selected",
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._browse_command = browse_command

        self.grid_columnconfigure(1, weight=1)

        self._label = ctk.CTkLabel(self, text=label, font=styles.FONT_LABEL, anchor="w")
        self._label.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 2))

        self._path_var = ctk.StringVar(value="")
        self._entry = ctk.CTkEntry(
            self,
            textvariable=self._path_var,
            placeholder_text=placeholder,
            font=styles.FONT_BODY,
            state="readonly",
        )
        self._entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=(0, 8))

        self._button = ctk.CTkButton(
            self,
            text="Browse…",
            width=110,
            height=styles.BUTTON_HEIGHT,
            command=self._on_browse,
        )
        self._button.grid(row=1, column=2, sticky="e")

    def _on_browse(self) -> None:
        path = self._browse_command()
        if path:
            self.set_path(path)

    def set_path(self, path: str) -> None:
        """Display the chosen path."""
        self._entry.configure(state="normal")
        self._path_var.set(path)
        self._entry.configure(state="readonly")

    def get_path(self) -> str:
        """Return the currently selected path ('' if none)."""
        return self._path_var.get().strip()

    def set_enabled(self, enabled: bool) -> None:
        """Enable/disable the row's button."""
        self._button.configure(state="normal" if enabled else "disabled")


class StatusBar(ctk.CTkFrame):
    """Progress bar plus a live status line."""

    def __init__(self, master: ctk.CTkBaseClass) -> None:
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)

        self.progress = ctk.CTkProgressBar(self, height=14)
        self.progress.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.progress.set(0)

        self._status_var = ctk.StringVar(value="Ready.")
        self._status = ctk.CTkLabel(
            self,
            textvariable=self._status_var,
            font=styles.FONT_STATUS,
            anchor="w",
            text_color=styles.COLOR_MUTED,
        )
        self._status.grid(row=1, column=0, sticky="ew")

    def update_status(self, fraction: float, message: str, color: str | None = None) -> None:
        """Thread-safe-friendly setter (call via ``widget.after``)."""
        self.progress.set(max(0.0, min(1.0, fraction)))
        self._status_var.set(message)
        self._status.configure(text_color=color or styles.COLOR_MUTED)
