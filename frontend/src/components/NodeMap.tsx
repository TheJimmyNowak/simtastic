import { useEffect, useMemo } from "react";
import { CircleMarker, MapContainer, Polyline, TileLayer, Tooltip, useMap, useMapEvents } from "react-leaflet";
import type { LatLngBoundsExpression } from "leaflet";
import type { Links, NodeConfig } from "../api";
import { INFRASTRUCTURE_ROLES } from "../api";
import { SIGNAL_COLORS, signalLevel } from "../model";

type Props = {
  nodes: NodeConfig[];
  selectedIndex?: number;
  onSelect?: (index: number) => void;
  /** Clicking the map moves the selected node there. */
  onPick?: (lat: number, lon: number) => void;
  links?: Links | null;
  sensitivityDbm?: number;
  height?: number;
};

function PickHandler({ onPick }: { onPick: (lat: number, lon: number) => void }) {
  useMapEvents({ click: (e) => onPick(e.latlng.lat, e.latlng.lng) });
  return null;
}

/** Fit the view to all nodes once, when the set of nodes changes. */
function FitToNodes({ nodes }: { nodes: NodeConfig[] }) {
  const map = useMap();
  const key = nodes.map((n) => n.id).join(",");
  useEffect(() => {
    if (nodes.length === 0) return;
    if (nodes.length === 1) {
      map.setView([nodes[0].lat, nodes[0].lon], 14);
      return;
    }
    const bounds: LatLngBoundsExpression = nodes.map((n) => [n.lat, n.lon] as [number, number]);
    map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
    // Only when nodes are added or removed, not on every move.
  }, [key, map]);
  return null;
}

type LinkLine = { from: [number, number]; to: [number, number]; color: string; label: string };

function linkLines(nodes: NodeConfig[], links: Links, sensitivity: number): LinkLine[] {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const lines: LinkLine[] = [];
  links.node_ids.forEach((a, i) => {
    links.node_ids.forEach((b, j) => {
      if (j <= i) return;
      const na = byId.get(a);
      const nb = byId.get(b);
      if (!na || !nb) return;
      // Show the better direction; the matrix is not perfectly symmetric.
      const rssi = Math.max(links.median_rssi_db[i][j], links.median_rssi_db[j][i]);
      const level = signalLevel(rssi, sensitivity);
      if (level === "none") return;
      lines.push({
        from: [na.lat, na.lon],
        to: [nb.lat, nb.lon],
        color: SIGNAL_COLORS[level],
        label: `${na.name || a} ↔ ${nb.name || b}: ${rssi.toFixed(0)} dBm (zapas ${(rssi - sensitivity).toFixed(0)} dB)`,
      });
    });
  });
  return lines;
}

export function NodeMap({ nodes, selectedIndex, onSelect, onPick, links, sensitivityDbm, height = 520 }: Props) {
  const lines = useMemo(
    () => (links && sensitivityDbm !== undefined ? linkLines(nodes, links, sensitivityDbm) : []),
    [nodes, links, sensitivityDbm]
  );
  const center: [number, number] = nodes.length ? [nodes[0].lat, nodes[0].lon] : [52.0, 21.0];

  return (
    <MapContainer center={center} zoom={13} className="map" style={{ height }}>
      <TileLayer
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        attribution="&copy; OpenStreetMap"
      />
      <FitToNodes nodes={nodes} />
      {onPick && <PickHandler onPick={onPick} />}
      {lines.map((line, i) => (
        <Polyline key={i} positions={[line.from, line.to]} pathOptions={{ color: line.color, weight: 3, opacity: 0.85 }}>
          <Tooltip sticky>{line.label}</Tooltip>
        </Polyline>
      ))}
      {nodes.map((node, index) => {
        const selected = index === selectedIndex;
        return (
          <CircleMarker
            key={node.id}
            center={[node.lat, node.lon]}
            radius={selected ? 10 : 7}
            pathOptions={{
              color: selected ? "#ffffff" : "#0b1220",
              weight: 2,
              fillColor: INFRASTRUCTURE_ROLES.includes(node.role) ? "#f4bb4f" : "#3d91f4",
              fillOpacity: 1,
            }}
            // Clicking a node selects it instead of moving the selected node there.
            bubblingMouseEvents={false}
            eventHandlers={onSelect ? { click: () => onSelect(index) } : undefined}
          >
            <Tooltip permanent direction="top" offset={[0, -8]} className="node-label">
              {node.name || node.id}
            </Tooltip>
          </CircleMarker>
        );
      })}
    </MapContainer>
  );
}
