import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def app(tmp_path_factory):
    application = QApplication.instance() or QApplication([])
    application.setStyle("Fusion")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path_factory.mktemp("settings")))
    return application


@pytest.fixture
def window(app):
    from weverse_chat_ui import WeverseChatStudio
    QSettings(QSettings.IniFormat, QSettings.UserScope, "weverse-dlt", "ReplayStudio").clear()
    studio = WeverseChatStudio()
    studio.show()
    app.processEvents()
    yield studio
    studio.close()
    app.processEvents()
