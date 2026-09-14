"""Minimal OPC UA server construction helpers required by ICCAS."""

from __future__ import annotations

import logging
from typing import Any

from opcua import Server, ua

try:
    from .common import DEFAULT_MAP, VTYPE_MAP, NodeDef
except ImportError:  # Direct source execution compatibility.
    from common import DEFAULT_MAP, VTYPE_MAP, NodeDef


log = logging.getLogger("ICCAS-Server")


def build_server(endpoint: str) -> Server:
    server = Server()
    server.set_endpoint(endpoint)
    server.set_server_name("ICCAS Simulation OPC UA Server")
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
    return server


def register_ns_padding(server: Server, target_index: int, namespace_uri: str) -> int:
    """Register placeholders so the ICCAS namespace has a stable index."""

    while len(server.get_namespace_array()) < target_index:
        placeholder = f"urn:iccas:sim:placeholder:{len(server.get_namespace_array())}"
        server.register_namespace(placeholder)
    index = server.register_namespace(namespace_uri)
    if index != target_index:
        raise RuntimeError(
            f"Namespace index mismatch: expected {target_index}, got {index}"
        )
    return index


def add_nodes(server: Server, namespace_index: int, definitions: list[NodeDef]) -> dict[str, Any]:
    """Create ICCAS scalar or fixed-length array variables."""

    objects = server.get_objects_node()
    parents: dict[tuple[str, ...], Any] = {(): objects}
    result: dict[str, Any] = {}
    for definition in definitions:
        variant_type = VTYPE_MAP[definition.data_type]
        scalar_default = DEFAULT_MAP[definition.data_type]
        default = definition.initial_value
        if default is None:
            default = (
                [scalar_default] * definition.array_len
                if definition.array_len
                else scalar_default
            )

        parent = objects
        partial: tuple[str, ...] = ()
        for part in definition.browse_path:
            partial += (part,)
            if partial not in parents:
                parents[partial] = parent.add_object(namespace_index, part)
            parent = parents[partial]

        try:
            node_id = ua.NodeId.from_string(definition.node_id)
        except Exception:
            identifier = definition.node_id.split("s=", 1)[-1]
            node_id = ua.NodeId(identifier, namespace_index, ua.NodeIdType.String)
        if node_id.NamespaceIndex != namespace_index:
            node_id = ua.NodeId(
                node_id.Identifier,
                namespace_index,
                node_id.NodeIdType,
            )

        variable = parent.add_variable(
            node_id,
            definition.name_cn,
            default,
            varianttype=variant_type,
        )
        if definition.array_len:
            variable.set_value_rank(1)
            variable.set_array_dimensions([definition.array_len])
        variable.set_writable()
        if definition.name_en:
            try:
                variable.set_attribute(
                    ua.AttributeIds.Description,
                    ua.DataValue(
                        ua.Variant(
                            ua.LocalizedText(definition.name_en),
                            ua.VariantType.LocalizedText,
                        )
                    ),
                )
            except Exception:
                log.debug("Could not set description for %s", definition.name_cn)
        result[definition.name_cn] = variable
    return result
