"""Application entry point: wire model, views and controller together."""

from __future__ import annotations

import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from os_builder import APP_ID, APP_NAME, __version__
from os_builder.controllers.main_controller import MainController
from os_builder.views import resources
from os_builder.views.main_window import MainWindow


def main(argv: list[str] | None = None) -> int:
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName(APP_ID)  # lets Wayland compositors match the .desktop file
    app.setWindowIcon(resources.app_icon())
    window = MainWindow()
    controller = MainController(window)
    window.show()
    QTimer.singleShot(0, controller.start)  # after the first paint: never block startup
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
