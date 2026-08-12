"""Infrastructure for the ASC oven control application.

Importing this package has no side effects: no logging, no config reading,
and no serial ports are opened by importing it. The transport factory keeps
hardware access behind a simulation default (see ``serial_transport.py``).
"""

from asc_oven_control.infrastructure.config import (
    ApplicationConfig,
    ConfigValidationError,
    SerialProfile,
)
from asc_oven_control.infrastructure.persistence import (
    RunLogger,
    atomic_write_json,
    export_samples_csv,
)
from asc_oven_control.infrastructure.serial_transport import (
    BaseTransport,
    CommunicationError,
    create_transport,
)
from asc_oven_control.infrastructure.watlow_protocol import (
    ProtocolValidationError,
    WatlowFrame,
    WatlowCommands,
    crc16,
    verify_frame,
)
from asc_oven_control.infrastructure.legacy_table import (
    LegacyTable,
    LegacyTableError,
    parse_legacy_table,
    render_legacy_table,
)

__all__ = [
    "ApplicationConfig",
    "BaseTransport",
    "CommunicationError",
    "ConfigValidationError",
    "LegacyTable",
    "LegacyTableError",
    "ProtocolValidationError",
    "RunLogger",
    "SerialProfile",
    "WatlowCommands",
    "WatlowFrame",
    "atomic_write_json",
    "create_transport",
    "crc16",
    "export_samples_csv",
    "parse_legacy_table",
    "render_legacy_table",
    "verify_frame",
]
