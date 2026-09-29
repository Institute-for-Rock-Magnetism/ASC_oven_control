"""Application stylesheet: one QSS string keyed on object names.

Palette: warm canvas, dark teal sidebar, orange accent (from the first
prototype). Shapes follow macOS conventions: every control is rounded —
pill-shaped status chips and buttons, rounded fields with chevron
steppers, rounded check boxes, cards and chart frames, slim rounded
scroll bars and a pill splitter grip. ``build_style`` substitutes the
absolute path of the SVG icons in ``assets/ui`` (Qt style sheets need a
file path for images).
"""

from __future__ import annotations

from pathlib import Path

_STYLE = """
* { font-family: "SF Pro Text", "Segoe UI Variable Text", "Segoe UI", "Helvetica Neue";
    font-size: 13px; color: #17313B; }
QMainWindow, #content, QScrollArea, QScrollArea > QWidget > QWidget, QDialog, QMessageBox {
    background: #F3F0E8; }
QToolTip { background: #102A36; color: #F3F0E8; border: 0; border-radius: 8px; padding: 6px 9px; }

/* ---- sidebar ---- */
#sidebar { background: #102A36; }
#logo { color: #F4A261; font-size: 34px; font-weight: 800; letter-spacing: 3px; }
#product { color: #AFC2C8; font-size: 10px; font-weight: 700; letter-spacing: 3px; }
#navButton { background: transparent; color: #AFC2C8; border: 0; border-radius: 12px;
             text-align: left; padding: 11px 14px; font-weight: 600; }
#navButton:hover { background: #1B3B47; color: white; }
#navButton:checked { background: #F4A261; color: #102A36; }
#simSafeFooter { color: #7FA3B0; background: #17394A; border-radius: 12px; padding: 10px 12px;
                 font-size: 10px; font-weight: 700; letter-spacing: 1px; }

/* ---- header ---- */
#eyebrow { color: #C16C37; font-size: 10px; font-weight: 800; letter-spacing: 2px; }
#pageTitle { color: #102A36; font-size: 24px; font-weight: 700; }
#statusDot { color: #BE3A34; font-size: 16px; }
#statusText { color: #52666D; font-weight: 600; background: #E9E5DA; border-radius: 13px; padding: 5px 12px; }
#iconButton { background: #FFFEFA; border: 1px solid #DDD9CE; border-radius: 17px;
              min-width: 34px; max-width: 34px; min-height: 34px; max-height: 34px;
              padding: 0; font-size: 15px; }
#iconButton:hover { background: #ECE8DF; }

/* ---- cards ---- */
#card, #metricCard { background: #FFFEFA; border: 1px solid #E3DFD4; border-radius: 18px; }
#cardTitle { color: #102A36; font-size: 17px; font-weight: 700; }
#muted { color: #6D7D82; }
#metricCaption { color: #78898E; font-size: 10px; font-weight: 800; letter-spacing: 2px; }
#metricValue { color: #102A36; font-size: 30px; font-weight: 700; }
#chartFrame { background: #102A36; border-radius: 14px; }

/* ---- pills ---- */
#phaseChip, #fieldBadgeOff { background: #EDE9DE; color: #53666C; border-radius: 14px; padding: 6px 14px;
                             font-weight: 700; font-size: 12px; }
#fieldBadge { background: #FDEBDD; color: #A8501F; border-radius: 14px; padding: 6px 14px;
              font-weight: 700; font-size: 12px; }
#alarmClear { background: #E5F3EC; color: #267150; border-radius: 14px; padding: 6px 14px; font-weight: 700; }
#alarmActive { background: #FCE8E5; color: #A9322B; border-radius: 14px; padding: 6px 14px; font-weight: 700; }
#recoveredNote { background: #F7F3E9; border: 1px solid #E8D9AE; border-radius: 12px;
                 padding: 10px 12px; color: #6D5A2E; font-size: 12px; }

/* ---- buttons (pill shaped) ---- */
QPushButton { background: #FFFEFA; color: #17313B; border: 1px solid #CFCABF; border-radius: 16px;
              padding: 7px 18px; min-height: 18px; font-weight: 600; }
QPushButton:hover { background: #ECE8DF; }
QPushButton:pressed { background: #E2DDD2; }
QPushButton:disabled { color: #A8B2B5; background: #F4F1EA; border-color: #E2DED4; }
#primaryButton { background: #C76532; color: white; border: 1px solid #C76532; }
#primaryButton:hover { background: #AE5226; border-color: #AE5226; }
#primaryButton:disabled { background: #E3B79D; border-color: #E3B79D; color: #FFF7F1; }
#secondaryButton, #quietButton { background: #FFFEFA; color: #17313B; border: 1px solid #CFCABF; }
#quietButton { border-color: transparent; background: #EFEBE2; }
#secondaryButton:hover, #quietButton:hover { background: #E7E2D8; }
#dangerButton { background: #FFF3F0; color: #A9322B; border: 1px solid #E4AAA5; }
#dangerButton:hover { background: #FBE2DD; }
#dangerButton:disabled { color: #D9AFAB; background: #FBF4F2; border-color: #F0D6D3; }
#navButton, #iconButton { min-height: 0; }

/* ---- fields ---- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit {
    background: #FFFEFA; border: 1px solid #D5D0C4; border-radius: 10px; padding: 6px 10px;
    min-height: 20px; selection-background-color: #F4A261; selection-color: #102A36; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus {
    border: 2px solid #E9A06E; padding: 5px 9px; }
QSpinBox, QDoubleSpinBox { padding-right: 26px; }
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border; subcontrol-position: top right; width: 22px; margin: 3px 3px 0 0;
    border: 0; border-top-right-radius: 8px; background: #EFEBE2; }
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border; subcontrol-position: bottom right; width: 22px; margin: 0 3px 3px 0;
    border: 0; border-bottom-right-radius: 8px; background: #EFEBE2; }
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover { background: #E2DDD2; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url(@ICONS@/chevron-up.svg); width: 10px; height: 10px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url(@ICONS@/chevron-down.svg); width: 10px; height: 10px; }
QComboBox { padding-right: 30px; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right; width: 24px;
                       margin-right: 4px; border: 0; border-radius: 8px; background: #EFEBE2; height: 22px; }
QComboBox::down-arrow { image: url(@ICONS@/chevron-down.svg); width: 10px; height: 10px; }
QComboBox QAbstractItemView { background: #FFFEFA; border: 1px solid #D5D0C4; border-radius: 10px;
                              padding: 4px; outline: 0; selection-background-color: #F6DCC8;
                              selection-color: #102A36; }

/* ---- check boxes ---- */
QCheckBox { spacing: 9px; }
QCheckBox::indicator { width: 18px; height: 18px; border-radius: 6px; border: 1px solid #C2BCB0;
                       background: #FFFEFA; }
QCheckBox::indicator:hover { border-color: #C76532; }
QCheckBox::indicator:checked { background: #C76532; border-color: #C76532; image: url(@ICONS@/check.svg); }

/* ---- tables ---- */
QTableWidget { background: #FFFEFA; alternate-background-color: #F7F4ED; border: 1px solid #E3DFD4;
               border-radius: 12px; gridline-color: #EEEAE1; selection-background-color: #F3D5BF;
               selection-color: #102A36; }
QHeaderView { background: transparent; }
QHeaderView::section { background: #EFEBE2; color: #53666C; border: 0; padding: 9px;
                       font-size: 10px; font-weight: 800; }
QHeaderView::section:first { border-top-left-radius: 11px; }
QHeaderView::section:last { border-top-right-radius: 11px; }
QTableCornerButton::section { background: transparent; border: 0; }

/* ---- scroll bars (slim, rounded, no arrows) ---- */
QScrollBar:vertical { background: transparent; width: 10px; margin: 3px; }
QScrollBar::handle:vertical { background: #C9C4B9; border-radius: 4px; min-height: 36px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 3px; }
QScrollBar::handle:horizontal { background: #C9C4B9; border-radius: 4px; min-width: 36px; }
QScrollBar::handle:hover { background: #A9A397; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: 0; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

/* ---- splitter grip (pill) ---- */
QSplitter::handle:vertical { image: none; background: transparent; }
QSplitter::handle:vertical:hover { background: #E4DFD4; border-radius: 6px; }
"""

ICON_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "ui"


def build_style(icon_dir: Path | None = None) -> str:
    directory = Path(icon_dir) if icon_dir is not None else ICON_DIR
    return _STYLE.replace("@ICONS@", directory.as_posix())


APP_STYLE = build_style()
