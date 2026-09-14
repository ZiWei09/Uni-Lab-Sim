"""Shared OPC UA types used by the ICCAS simulator runtimes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opcua import ua


VTYPE_MAP: dict[str, ua.VariantType] = {
    "BOOLEAN": ua.VariantType.Boolean,
    "BYTE": ua.VariantType.Byte,
    "INT16": ua.VariantType.Int16,
    "INT32": ua.VariantType.Int32,
    "FLOAT": ua.VariantType.Float,
    "DOUBLE": ua.VariantType.Double,
    "STRING": ua.VariantType.String,
}

DEFAULT_MAP: dict[str, Any] = {
    "BOOLEAN": False,
    "BYTE": 0,
    "INT16": 0,
    "INT32": 0,
    "FLOAT": 0.0,
    "DOUBLE": 0.0,
    "STRING": "",
}


@dataclass
class NodeDef:
    """Portable definition for one ICCAS OPC UA variable."""

    name_cn: str
    name_en: str
    node_type: str
    data_type: str
    node_id: str
    array_len: int = 0
    browse_path: tuple[str, ...] = ()
    initial_value: Any = None
    write_owner: str = "shared"
