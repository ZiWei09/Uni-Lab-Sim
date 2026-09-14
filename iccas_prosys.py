"""Export ICCAS NodeSets and run the handshake agent against a Prosys server."""
from __future__ import annotations

import argparse
import json
import math
import signal
import threading
from pathlib import Path

from opcua import Client, Server, ua

try:
    from .common import DEFAULT_MAP, VTYPE_MAP
    from .iccas_sim import FILTER_EXTENSION_MARKER, FILTER_EXTENSION_VERSION, Station, definitions, load_faults
except ImportError:
    from common import DEFAULT_MAP, VTYPE_MAP
    from iccas_sim import FILTER_EXTENSION_MARKER, FILTER_EXTENSION_VERSION, Station, definitions, load_faults


NAMESPACES = {"spray": "urn:unilab:iccas:prosys:spray:v1",
              "filter": "urn:unilab:iccas:prosys:filter-extended:v1"}
FREE_NAMESPACE = "http://www.prosysopc.com/OPCUA/SimulationNodes/"


def export_nodesets(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    for kind, uri in NAMESPACES.items():
        server = Server()
        ns = server.register_namespace(uri)
        title = "ICCAS 喷雾析出" if kind == "spray" else "ICCAS 过滤除泡（仿真扩展）"
        parent = server.get_objects_node().add_object(ua.NodeId(f"ICCAS.{kind}", ns), title)
        nodes = [parent]
        for definition in definitions(kind, filter_extensions=True):
            value = definition.initial_value
            if value is None:
                value = DEFAULT_MAP[definition.data_type]
            node = parent.add_variable(
                ua.NodeId("上位机通讯|" + definition.name_cn, ns), definition.name_cn,
                value, varianttype=VTYPE_MAP[definition.data_type])
            if definition.name_cn != FILTER_EXTENSION_MARKER:
                node.set_writable()
            nodes.append(node)
        path = directory / f"iccas-{kind}.NodeSet2.xml"
        server.export_xml(nodes, str(path))
        print(f"{path}: {len(nodes) - 1} variables; {uri}")
        groups = {}
        initial_values = {}
        for definition in definitions(kind, filter_extensions=True):
            identifier = f"ICCAS.{kind}|{definition.name_cn}"
            groups.setdefault(definition.data_type, []).append(identifier)
            if definition.initial_value is not None:
                initial_values[identifier] = definition.initial_value
        for dtype, names in groups.items():
            (directory / f"free-{kind}-{dtype}.txt").write_text(";".join(names), encoding="utf-8")
        (directory / f"free-{kind}-initial-values.json").write_text(
            json.dumps(initial_values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class CachedNode:
    """One batch read per scan; typed OPC UA writes still go to Prosys."""
    def __init__(self, node, variant_type):
        self.node, self.variant_type, self.value = node, variant_type, None

    def get_value(self):
        return self.value

    def get_data_type_as_variant_type(self):
        return self.variant_type

    def set_value(self, variant):
        # Send Value only: source/server timestamps need not be writable.
        value = ua.DataValue(variant)
        value.SourceTimestamp = None
        value.ServerTimestamp = None
        self.node.set_value(value)
        self.value = variant.Value


def attach(client, *, free_layout=False):
    stations, bindings, config = {}, [], {}
    namespaces = client.get_namespace_array()
    for kind, uri in NAMESPACES.items():
        if free_layout:
            uri = FREE_NAMESPACE
        if uri not in namespaces:
            raise RuntimeError(f"Missing NodeSet in target server: {uri}")
        ns = namespaces.index(uri)
        prefix = f"ICCAS.{kind}|" if free_layout else "上位机通讯|"
        nodes = {}
        for definition in definitions(kind, filter_extensions=True):
            node = client.get_node(f"ns={ns};s={prefix}{definition.name_cn}")
            expected = VTYPE_MAP[definition.data_type]
            if node.get_data_type_as_variant_type() != expected:
                raise ValueError(f"Wrong data type: {kind}/{definition.name_cn}")
            if definition.name_cn != FILTER_EXTENSION_MARKER:
                if ua.AccessLevel.CurrentWrite not in node.get_user_access_level():
                    raise ValueError(f"Node must be writable and have no signal simulation: {kind}/{definition.name_cn}")
            nodes[definition.name_cn] = CachedNode(node, expected)
        bindings.extend(nodes.values())
        config[kind] = {"node_prefix": f"ns={ns};s={prefix}"}
        if kind == "filter":
            config[kind]["simulation_extensions"] = True
        stations[kind] = nodes
    refresh(client, bindings)
    if stations["filter"][FILTER_EXTENSION_MARKER].get_value() != FILTER_EXTENSION_VERSION:
        raise ValueError("Filter extension marker must equal filter-return-v1")
    return stations, bindings, config


def refresh(client, bindings):
    values = client.get_values([binding.node for binding in bindings])
    for binding, value in zip(bindings, values):
        binding.value = value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--directory", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--url", default="opc.tcp://127.0.0.1:53530/OPCUA/SimulationServer")
    run.add_argument("--delay", type=float, default=0.5)
    run.add_argument("--time-scale", type=float, default=1.0)
    run.add_argument("--timing-mode", choices=["handshake", "process"], default="handshake")
    run.add_argument("--timing-profile", type=Path)
    run.add_argument("--state-file", type=Path)
    run.add_argument("--fault-file", type=Path)
    run.add_argument("--free-layout", action="store_true",
                     help="use manually created nodes in Prosys SimulationNodes namespace")
    args = parser.parse_args()
    if args.command == "export":
        export_nodesets(args.directory)
        return 0
    if any(not math.isfinite(v) or v <= 0 for v in (args.delay, args.time_scale)):
        parser.error("delay and time-scale must be finite and positive")
    stop = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    client = Client(args.url)
    connected = False
    try:
        client.connect()
        connected = True
        node_maps, bindings, config = attach(client, free_layout=args.free_layout)
        stations = {kind: Station(kind, nodes, delay=args.delay, time_scale=args.time_scale,
                                  timing_mode=args.timing_mode, timing_profile=args.timing_profile)
                    for kind, nodes in node_maps.items()}
        for item in config.values():
            item["endpoint"] = args.url
        print(json.dumps({"client_config": config}, ensure_ascii=False, indent=2), flush=True)
        while not stop.is_set():
            refresh(client, bindings)
            faults = load_faults(args.fault_file, stations)
            for kind, station in stations.items():
                station.tick(faults=faults.get(kind))
            if args.state_file:
                args.state_file.parent.mkdir(parents=True, exist_ok=True)
                temporary = args.state_file.with_suffix(args.state_file.suffix + ".tmp")
                temporary.write_text(json.dumps({"endpoint": args.url, "client_config": config,
                                                 "stations": {k: s.snapshot() for k, s in stations.items()}},
                                                ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(args.state_file)
            stop.wait(0.05)
    finally:
        if connected:
            client.disconnect()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
