import socket

import pytest
from opcua import Client, Server, ua

from iccas_prosys import FREE_NAMESPACE, NAMESPACES, attach, export_nodesets, refresh
from iccas_sim import Station, definitions
from common import DEFAULT_MAP, VTYPE_MAP


@pytest.mark.parametrize("free_layout", [False, True])
def test_nodeset_and_external_agent_roundtrip(tmp_path, free_layout):
    export_nodesets(tmp_path)
    server = Server()
    # Deliberately change namespace indices, as Prosys already has several.
    server.register_namespace("urn:other:existing-model")
    if free_layout:
        ns = server.register_namespace(FREE_NAMESPACE)
        for kind in NAMESPACES:
            for definition in definitions(kind, filter_extensions=True):
                value = definition.initial_value
                if value is None:
                    value = DEFAULT_MAP[definition.data_type]
                node = server.get_objects_node().add_variable(
                    ua.NodeId(f"ICCAS.{kind}|{definition.name_cn}", ns), definition.name_cn,
                    value, varianttype=VTYPE_MAP[definition.data_type])
                node.set_writable()
    else:
        for kind in NAMESPACES:
            server.import_xml(str(tmp_path / f"iccas-{kind}.NodeSet2.xml"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint = f"opc.tcp://127.0.0.1:{port}/test/"
    server.set_endpoint(endpoint)
    server.start()
    client = Client(endpoint)
    try:
        client.connect()
        node_maps, bindings, config = attach(client, free_layout=free_layout)
        assert sum(len(nodes) for nodes in node_maps.values()) == 338
        assert config["filter"]["simulation_extensions"] is True
        for kind, nodes in node_maps.items():
            station = Station(kind, nodes, delay=0.1)
            for base in ("烧杯仓放料", "真空箱取料"):
                name = base + "号码PCtoPLC"
                if name in nodes:
                    nodes[name].set_value(ua.Variant(1, ua.VariantType.Int16))
            for ch in station.channels:
                trigger = nodes[ch.trigger].node
                trigger.set_value(True)
                refresh(client, bindings)
                station.tick(0)
                station.tick(1)
                assert nodes[ch.complete].node.get_value(), ch.trigger
                trigger.set_value(False)
                refresh(client, bindings)
                station.tick(2)
                assert not nodes[ch.complete].node.get_value()
    finally:
        client.disconnect()
        server.stop()
