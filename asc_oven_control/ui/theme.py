"""Application stylesheet: one QSS string keyed on object names and
dynamic properties. The palette carries over from the first prototype and
the Long Core Control project: warm canvas, dark teal sidebar, orange
accent.
"""

APP_STYLE = """
* { font-family: "Avenir Next", "Segoe UI"; font-size: 13px; color: #17313B; }
QMainWindow, #content, QScrollArea, QScrollArea > QWidget > QWidget { background: #F3F0E8; }
#sidebar { background: #102A36; }
#logo { color: #F4A261; font-size: 34px; font-weight: 800; letter-spacing: 3px; }
#product { color: #AFC2C8; font-size: 10px; font-weight: 700; letter-spacing: 3px; }
#navButton { background: transparent; color: #AFC2C8; border: 0; border-radius: 9px;
             text-align: left; padding: 13px 12px; font-weight: 600; }
#navButton:hover { background: #1B3B47; color: white; }
#navButton:checked { background: #F4A261; color: #102A36; }
#simSafeFooter { color: #5F8796; border-top: 1px solid #294753; padding-top: 14px;
                 font-size: 9px; font-weight: 700; letter-spacing: 1px; }
#eyebrow { color: #C16C37; font-size: 10px; font-weight: 800; letter-spacing: 2px; }
#pageTitle { color: #102A36; font-size: 27px; font-weight: 700; }
#statusDot { color: #BE3A34; font-size: 18px; }
#statusText { color: #52666D; font-weight: 600; }
#card, #metricCard { background: #FFFEFA; border: 1px solid #DDD9CE; border-radius: 14px; }
#cardTitle { color: #102A36; font-size: 18px; font-weight: 700; }
#muted { color: #6D7D82; }
#metricCaption { color: #78898E; font-size: 9px; font-weight: 800; letter-spacing: 2px; }
#metricValue { color: #102A36; font-size: 25px; font-weight: 700; }
#phaseChip { background: #EDE9DE; color: #53666C; border-radius: 12px; padding: 7px 12px;
             font-weight: 700; font-size: 12px; }
#fieldBadge { background: #FDEBDD; color: #A8501F; border-radius: 12px; padding: 7px 12px;
              font-weight: 700; font-size: 12px; }
#fieldBadgeOff { background: #EDE9DE; color: #53666C; border-radius: 12px; padding: 7px 12px;
                 font-weight: 700; font-size: 12px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit { background: #F8F6F0;
    border: 1px solid #D5D0C4; border-radius: 7px; padding: 8px; selection-background-color: #F4A261; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus {
    border: 1px solid #C16C37; }
QCheckBox { spacing: 9px; }
QCheckBox::indicator { width: 18px; height: 18px; }
QPushButton { border-radius: 8px; padding: 10px 16px; font-weight: 700; }
#primaryButton { background: #C76532; color: white; border: 1px solid #C76532; }
#primaryButton:hover { background: #AE5226; }
#secondaryButton, #quietButton { background: #FFFEFA; color: #17313B; border: 1px solid #C9C5BA; }
#secondaryButton:hover, #quietButton:hover { background: #ECE8DF; }
#dangerButton { background: #FFF3F0; color: #A9322B; border: 1px solid #E4AAA5; }
#alarmClear { background: #E5F3EC; color: #267150; border-radius: 12px; padding: 7px 12px; font-weight: 700; }
#alarmActive { background: #FCE8E5; color: #A9322B; border-radius: 12px; padding: 7px 12px; font-weight: 700; }
#recoveredNote { background: #F7F3E9; border: 1px dashed #D5B45C; border-radius: 10px;
                 padding: 10px 12px; color: #6D5A2E; font-size: 12px; }
QTableWidget { background: #FFFEFA; alternate-background-color: #F5F2EA; border: 0;
               gridline-color: #E5E0D5; selection-background-color: #F3D5BF; }
QHeaderView::section { background: #E9E4DA; color: #53666C; border: 0; border-bottom: 1px solid #D2CCC0;
                       padding: 10px; font-size: 10px; font-weight: 800; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #C9C4B9; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""
