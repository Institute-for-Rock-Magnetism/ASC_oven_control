"""Reusable widgets: cards, metric cards, and button/page-title factories."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class Card(QFrame):
    """White rounded panel with an optional title and subtitle."""

    def __init__(self, title: str = "", subtitle: str = "") -> None:
        super().__init__()
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(22, 20, 22, 22)
        self.body.setSpacing(14)
        if title:
            heading = QLabel(title)
            heading.setObjectName("cardTitle")
            self.body.addWidget(heading)
        if subtitle:
            detail = QLabel(subtitle)
            detail.setObjectName("muted")
            detail.setWordWrap(True)
            self.body.addWidget(detail)


class MetricCard(QFrame):
    """Caption plus a large value with an accent bar."""

    def __init__(self, caption: str, value: str, accent: str) -> None:
        super().__init__()
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 18)
        caption_label = QLabel(caption.upper())
        caption_label.setObjectName("metricCaption")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("metricValue")
        bar = QFrame()
        bar.setFixedHeight(4)
        bar.setStyleSheet(f"background: {accent}; border-radius: 2px;")
        layout.addWidget(caption_label)
        layout.addWidget(self.value_label)
        layout.addStretch()
        layout.addWidget(bar)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)


def button(text: str, kind: str = "secondary", handler=None) -> QPushButton:
    """Factory for a styled push button."""
    widget = QPushButton(text)
    widget.setObjectName(
        {"primary": "primaryButton", "secondary": "secondaryButton",
         "danger": "dangerButton", "quiet": "quietButton"}.get(kind, "secondaryButton")
    )
    if handler is not None:
        widget.clicked.connect(handler)
    return widget


def page_title(eyebrow: str, title: str) -> QHBoxLayout:
    """Header block: small eyebrow above a large page title, with stretch."""
    layout = QVBoxLayout()
    eyebrow_label = QLabel(eyebrow)
    eyebrow_label.setObjectName("eyebrow")
    title_label = QLabel(title)
    title_label.setObjectName("pageTitle")
    layout.addWidget(eyebrow_label)
    layout.addWidget(title_label)
    return layout


def pill(text: str, object_name: str = "phaseChip") -> QLabel:
    """Small rounded status label."""
    label = QLabel(text)
    label.setObjectName(object_name)
    return label


class FieldRow(QWidget):
    """Inline labelled field used inside form rows."""

    def __init__(self, label: str, widget: QWidget) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        caption = QLabel(label)
        caption.setObjectName("muted")
        layout.addWidget(caption)
        layout.addWidget(widget, 1)
