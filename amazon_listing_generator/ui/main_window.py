"""Main application window.

The window collects the three inputs (Shopify CSV, Amazon template, output
folder), runs the pipeline on a worker thread and streams progress back to
the UI. All marketplace work goes through the generator registry, so this
window never imports Amazon-specific code directly.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from core.marketplace import get_generator
from core.models import GenerationRequest, GenerationResult
from core.utils import ListingGeneratorError, get_logger
from ui import styles
from ui.widgets import FilePickerRow, StatusBar

logger = get_logger("ui")


class MainWindow(ctk.CTk):
    """Top-level window of the listing generator."""

    def __init__(self, marketplace_id: str = "amazon_in") -> None:
        ctk.set_appearance_mode(styles.APPEARANCE_MODE)
        ctk.set_default_color_theme(styles.COLOR_THEME)
        super().__init__()

        self._marketplace_id = marketplace_id
        self._worker: threading.Thread | None = None
        self._last_output_dir: Path | None = None

        self.title(styles.APP_TITLE)
        self.geometry(styles.WINDOW_SIZE)
        self.minsize(*styles.MIN_SIZE)
        self.grid_columnconfigure(0, weight=1)

        self._build_layout()

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def _build_layout(self) -> None:
        header = ctk.CTkLabel(
            self, text="Amazon India Listing Generator", font=styles.FONT_HEADING
        )
        header.grid(row=0, column=0, sticky="w", **styles.SECTION_PADDING)

        subtitle = ctk.CTkLabel(
            self,
            text="Convert a Shopify product export into an Amazon-ready bulk listing file.",
            font=styles.FONT_SUBHEADING,
            text_color=styles.COLOR_MUTED,
        )
        subtitle.grid(row=1, column=0, sticky="w", padx=20)

        self._csv_row = FilePickerRow(
            self, "Shopify CSV Export", self._pick_csv, "Select the Shopify products CSV"
        )
        self._csv_row.grid(row=2, column=0, sticky="ew", **styles.PADDING)

        self._template_row = FilePickerRow(
            self,
            "Amazon Template (.xlsm)",
            self._pick_template,
            "Select the Amazon bulk listing template",
        )
        self._template_row.grid(row=3, column=0, sticky="ew", **styles.PADDING)

        self._output_row = FilePickerRow(
            self, "Output Folder", self._pick_output, "Choose where files are written"
        )
        self._output_row.grid(row=4, column=0, sticky="ew", **styles.PADDING)

        self._generate_button = ctk.CTkButton(
            self,
            text="Generate Listings",
            height=styles.GENERATE_BUTTON_HEIGHT,
            font=styles.FONT_LABEL,
            command=self._on_generate,
        )
        self._generate_button.grid(row=5, column=0, sticky="ew", padx=20, pady=(18, 6))

        self._status_bar = StatusBar(self)
        self._status_bar.grid(row=6, column=0, sticky="ew", padx=20, pady=(6, 4))

        self._open_button = ctk.CTkButton(
            self,
            text="Open Output Folder",
            height=styles.BUTTON_HEIGHT,
            fg_color="transparent",
            border_width=1,
            command=self._open_output_folder,
            state="disabled",
        )
        self._open_button.grid(row=7, column=0, sticky="ew", padx=20, pady=(4, 8))

        appearance = ctk.CTkSegmentedButton(
            self,
            values=["Dark", "Light", "System"],
            command=lambda mode: ctk.set_appearance_mode(mode.lower()),
        )
        appearance.set(styles.APPEARANCE_MODE.capitalize())
        appearance.grid(row=8, column=0, sticky="e", padx=20, pady=(4, 16))

    # ------------------------------------------------------------------ #
    # File pickers
    # ------------------------------------------------------------------ #

    def _pick_csv(self) -> str | None:
        return filedialog.askopenfilename(
            title="Select Shopify CSV export",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        ) or None

    def _pick_template(self) -> str | None:
        return filedialog.askopenfilename(
            title="Select Amazon template",
            filetypes=[
                ("Excel macro-enabled", "*.xlsm"),
                ("Excel workbook", "*.xlsx"),
                ("All files", "*.*"),
            ],
        ) or None

    def _pick_output(self) -> str | None:
        return filedialog.askdirectory(title="Choose output folder") or None

    # ------------------------------------------------------------------ #
    # Generation
    # ------------------------------------------------------------------ #

    def _on_generate(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        try:
            request = GenerationRequest(
                shopify_csv=Path(self._csv_row.get_path() or ""),
                amazon_template=Path(self._template_row.get_path() or ""),
                output_dir=Path(self._output_row.get_path() or ""),
            )
        except Exception as exc:
            messagebox.showwarning(
                "Missing input",
                "Please check your selections:\n\n"
                + "\n".join(str(exc).splitlines()[-3:]),
            )
            return

        self._set_busy(True)
        self._worker = threading.Thread(
            target=self._run_pipeline, args=(request,), daemon=True
        )
        self._worker.start()

    def _run_pipeline(self, request: GenerationRequest) -> None:
        """Worker thread body - must never raise."""
        try:
            generator = get_generator(self._marketplace_id)
            result = generator.generate(request, progress=self._post_progress)
            self.after(0, self._on_finished, result, request.output_dir)
        except ListingGeneratorError as exc:
            logger.error("Generation failed: %s", exc, exc_info=True)
            self.after(0, self._on_failed, str(exc))
        except Exception as exc:  # noqa: BLE001 - top-level guard
            logger.critical("Unexpected failure:\n%s", traceback.format_exc())
            self.after(
                0,
                self._on_failed,
                f"An unexpected error occurred ({exc.__class__.__name__}). "
                "Full details were written to the logs folder.",
            )

    def _post_progress(self, fraction: float, message: str) -> None:
        self.after(0, self._status_bar.update_status, fraction, message)

    def _on_finished(self, result: GenerationResult, output_dir: Path) -> None:
        self._set_busy(False)
        self._last_output_dir = output_dir
        self._open_button.configure(state="normal")
        stats = result.stats
        if result.status == "success":
            self._status_bar.update_status(1.0, result.message, styles.COLOR_SUCCESS)
            messagebox.showinfo(
                "Generation complete",
                f"{result.message}\n\nUpload file:\n{result.output_file}",
            )
        else:
            self._status_bar.update_status(1.0, result.message, styles.COLOR_WARNING)
            messagebox.showwarning(
                "Completed with errors",
                f"{result.message}\n\n"
                f"{stats.errors} error(s) need attention — see:\n"
                f"{result.error_report}",
            )

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self._status_bar.update_status(0.0, "Failed — see logs.", styles.COLOR_ERROR)
        messagebox.showerror("Generation failed", message)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self._generate_button.configure(
            state=state, text="Generating…" if busy else "Generate Listings"
        )
        for row in (self._csv_row, self._template_row, self._output_row):
            row.set_enabled(not busy)
        if busy:
            self._status_bar.update_status(0.01, "Starting…")

    # ------------------------------------------------------------------ #

    def _open_output_folder(self) -> None:
        if not self._last_output_dir:
            return
        path = str(self._last_output_dir)
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", path])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError as exc:
            logger.warning("Could not open output folder: %s", exc)


def run(marketplace_id: str = "amazon_in") -> None:
    """Create and run the main window (blocking)."""
    window = MainWindow(marketplace_id)
    window.mainloop()
