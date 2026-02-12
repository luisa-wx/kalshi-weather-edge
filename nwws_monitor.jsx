import { useState, useEffect, useRef, useCallback } from "react";

const STATIONS = {
  KNYC: { city: "NYC", tz: "ET" },
  KPHL: { city: "Philadelphia", tz: "ET" },
  KMDW: { city: "Chicago", tz: "CT" },
  KLAX: { city: "Los Angeles", tz: "PT" },
  KMIA: { city: "Miami", tz: "ET" },
  KAUS: { city: "Austin", tz: "CT" },
  KDEN: { city: "Denver", tz: "MT" },
  KSFO: { city: "San Francisco", tz: "PT" },
  KSEA: { city: "Seattle", tz: "PT" },
  KDCA: { city: "Washington DC", tz: "ET" },
  KMSY: { city: "New Orleans", tz: "CT" },
  KLAS: { city: "Las Vegas", tz: "PT" },
  KDFW: { city: "Dallas", tz: "CT" },
  KHOU: { city: "Houston", tz: "CT" },
  KBOS: { city: "Boston", tz: "ET" },
  KMSP: { city: "Minneapolis", tz: "CT" },
  KSAT: { city: "San Antonio", tz: "CT" },
  KOKC: { city: "Oklahoma City", tz: "CT" },
  KPHX: { city: "Phoenix", tz: "MST" },
};

function fmt(isoStr) {
  if (!isoStr) return "";
  const d = new Date(isoStr);
  return d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true });
}

function timeSince(isoStr) {
  if (!isoStr) return "";
  const s = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

export default function NWWSMonitor() {
  const [products, setProducts] = useState([]);
  const [nwwsConnected, setNwwsConnected] = useState(false);
  const [wsStatus, setWsStatus] = useState("disconnected");
  const [filter, setFilter] = useState("all");
  const [wsUrl, setWsUrl] = useState("ws://54.91.7.11:8765");
  const [showConfig, setShowConfig] = useState(false);
  const [now, setNow] = useState(Date.now());
  const wsRef = useRef(null);
  const reconnectRef = useRef(null);

  // Tick every 10s to update "time since"
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 10000);
    return () => clearInterval(t);
  }, []);

  const connect = useCallback((url) => {
    if (wsRef.current) wsRef.current.close();
    setWsStatus("connecting");
    try {
      const ws = new WebSocket(url);
      wsRef.current = ws;
      ws.onopen = () => setWsStatus("connected");
      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "history") {
            setProducts(msg.products.reverse());
          } else if (msg.type === "product") {
            setProducts((prev) => [msg.product, ...prev].slice(0, 300));
          } else if (msg.type === "status") {
            setNwwsConnected(msg.connected);
          }
        } catch (e) {}
      };
      ws.onclose = () => {
        setWsStatus("disconnected");
        reconnectRef.current = setTimeout(() => connect(url), 5000);
      };
      ws.onerror = () => setWsStatus("error");
    } catch (e) {
      setWsStatus("error");
    }
  }, []);

  useEffect(() => {
    connect(wsUrl);
    return () => {
      if (wsRef.current) wsRef.current.close();
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
    };
  }, []);

  const filtered = products.filter((p) => {
    if (filter === "cli") return p.type === "CLI";
    if (filter === "dsm") return p.type === "DSM";
    if (filter === "hasdata") return p.high !== null || p.low !== null;
    return true;
  });

  const latestByStation = {};
  for (const p of products) {
    if (p.station && !latestByStation[p.station]) {
      latestByStation[p.station] = p;
    }
  }

  return (
    <div style={{
      minHeight: "100vh",
      backgroundColor: "#08080c",
      color: "#d0d0d0",
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
    }}>
      {/* Header */}
      <div style={{
        borderBottom: "1px solid #16161e",
        padding: "14px 20px",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        background: "#0b0b12",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 15, fontWeight: 800, color: "#f0f0f0", letterSpacing: "1px" }}>
            WX SNIPER
          </span>
          <span style={{
            fontSize: 10, color: "#444", padding: "2px 8px",
            border: "1px solid #222", borderRadius: 3,
          }}>NWWS-OI LIVE</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{
              width: 7, height: 7, borderRadius: "50%", display: "inline-block",
              backgroundColor: nwwsConnected ? "#22c55e" : wsStatus === "connecting" ? "#eab308" : "#ef4444",
              boxShadow: nwwsConnected ? "0 0 8px #22c55e44" : "none",
            }} />
            <span style={{ fontSize: 10, color: nwwsConnected ? "#22c55e" : "#666" }}>
              {nwwsConnected ? "NWWS LIVE" : wsStatus === "connected" ? "WS OK · NWWS..." : wsStatus === "connecting" ? "CONNECTING..." : "OFFLINE"}
            </span>
          </div>
          <button
            onClick={() => setShowConfig(!showConfig)}
            style={{
              fontSize: 10, color: "#555", background: "none", border: "1px solid #222",
              borderRadius: 3, padding: "3px 8px", cursor: "pointer", fontFamily: "inherit",
            }}
          >⚙</button>
        </div>
      </div>

      {showConfig && (
        <div style={{
          padding: "12px 20px", borderBottom: "1px solid #16161e",
          background: "#0a0a10", display: "flex", gap: 8, alignItems: "center",
        }}>
          <span style={{ fontSize: 10, color: "#555" }}>WS:</span>
          <input
            value={wsUrl}
            onChange={(e) => setWsUrl(e.target.value)}
            style={{
              flex: 1, fontSize: 11, fontFamily: "inherit", background: "#111",
              border: "1px solid #222", borderRadius: 3, padding: "4px 8px", color: "#ccc",
            }}
          />
          <button
            onClick={() => connect(wsUrl)}
            style={{
              fontSize: 10, color: "#22c55e", background: "#0a1a0a",
              border: "1px solid #1a3a1a", borderRadius: 3, padding: "4px 12px",
              cursor: "pointer", fontFamily: "inherit",
            }}
          >Connect</button>
        </div>
      )}

      {/* Station Grid */}
      <div style={{ padding: "16px 20px 8px" }}>
        <div style={{ fontSize: 10, color: "#444", marginBottom: 10, textTransform: "uppercase", letterSpacing: "1.5px" }}>
          Station Overview — {Object.keys(STATIONS).length} Markets
        </div>
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(155px, 1fr))",
          gap: 3,
        }}>
          {Object.entries(STATIONS).map(([code, info]) => {
            const latest = latestByStation[code];
            const hasHigh = latest?.high != null;
            return (
              <div key={code} style={{
                padding: "7px 10px",
                borderRadius: 4,
                background: hasHigh ? "#0d1117" : "#0a0a0e",
                border: hasHigh ? "1px solid #1a2a3a" : "1px solid #111",
              }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span style={{ fontSize: 10, fontWeight: 700, color: hasHigh ? "#e0e0e0" : "#2a2a2a" }}>
                    {code}
                  </span>
                  <span style={{ fontSize: 8, color: "#333" }}>{info.tz}</span>
                </div>
                <div style={{ fontSize: 9, color: hasHigh ? "#555" : "#1a1a1a", marginBottom: 3 }}>{info.city}</div>
                {hasHigh ? (
                  <div style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
                    <span>
                      <span style={{ fontSize: 8, color: "#666" }}>H </span>
                      <span style={{ fontSize: 15, fontWeight: 800, color: "#f87171" }}>{latest.high}°</span>
                    </span>
                    {latest.low != null && (
                      <span>
                        <span style={{ fontSize: 8, color: "#666" }}>L </span>
                        <span style={{ fontSize: 15, fontWeight: 800, color: "#60a5fa" }}>{latest.low}°</span>
                      </span>
                    )}
                  </div>
                ) : (
                  <div style={{ fontSize: 10, color: "#1a1a1a" }}>—</div>
                )}
                {latest && (
                  <div style={{ fontSize: 7, color: "#2a2a2a", marginTop: 1 }}>
                    {latest.type} · {timeSince(latest.received_at)}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Filters */}
      <div style={{ padding: "12px 20px 4px", display: "flex", gap: 6 }}>
        {[["all", "All"], ["cli", "CLI"], ["dsm", "DSM"], ["hasdata", "With Temps"]].map(([key, label]) => (
          <button
            key={key}
            onClick={() => setFilter(key)}
            style={{
              padding: "3px 12px", fontSize: 10, fontFamily: "inherit",
              border: filter === key ? "1px solid #2a2a3a" : "1px solid #151518",
              borderRadius: 3, background: filter === key ? "#14141e" : "transparent",
              color: filter === key ? "#ccc" : "#444", cursor: "pointer",
            }}
          >{label}</button>
        ))}
        <span style={{ marginLeft: "auto", fontSize: 10, color: "#333" }}>
          {filtered.length} products
        </span>
      </div>

      {/* Product Feed */}
      <div style={{ padding: "8px 20px 20px" }}>
        {filtered.length === 0 ? (
          <div style={{ padding: "48px 0", textAlign: "center", color: "#222", fontSize: 12 }}>
            {wsStatus === "connected" ? "Waiting for CLI/DSM products..." : "Connect to see live products"}
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {filtered.map((p, i) => (
              <ProductRow key={`${p.awipsid}-${p.received_at}-${i}`} product={p} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function ProductRow({ product: p }) {
  const [expanded, setExpanded] = useState(false);
  const hasTemp = p.high != null || p.low != null;

  return (
    <div
      onClick={() => setExpanded(!expanded)}
      style={{
        padding: "8px 12px", borderRadius: 4, cursor: "pointer",
        background: hasTemp ? "#0c0c18" : "#09090c",
        border: hasTemp ? "1px solid #1c1c30" : "1px solid #0f0f12",
      }}
    >
      <div style={{
        display: "grid",
        gridTemplateColumns: "78px 44px 60px 1fr 76px",
        alignItems: "center",
        gap: 8,
        fontSize: 11,
      }}>
        <span style={{ color: "#3a3a4a", fontSize: 10 }}>{fmt(p.received_at)}</span>
        <span style={{
          fontSize: 9, padding: "1px 6px", borderRadius: 2, fontWeight: 700, textAlign: "center",
          background: p.type === "CLI" ? "#12122a" : "#1a1500",
          color: p.type === "CLI" ? "#818cf8" : "#fbbf24",
        }}>{p.type}</span>
        <span style={{
          color: p.station ? "#e0e0e0" : "#444",
          fontWeight: p.station ? 700 : 400,
        }}>{p.station || p.awipsid}</span>
        <div style={{ display: "flex", gap: 14, alignItems: "center" }}>
          {p.high != null && (
            <span>
              <span style={{ color: "#555", fontSize: 9 }}>HIGH </span>
              <span style={{ color: "#f87171", fontWeight: 800, fontSize: 15 }}>{p.high}°F</span>
              {p.high_time && <span style={{ color: "#444", fontSize: 9, marginLeft: 4 }}>@ {p.high_time}</span>}
            </span>
          )}
          {p.low != null && (
            <span>
              <span style={{ color: "#555", fontSize: 9 }}>LOW </span>
              <span style={{ color: "#60a5fa", fontWeight: 800, fontSize: 15 }}>{p.low}°F</span>
              {p.low_time && <span style={{ color: "#444", fontSize: 9, marginLeft: 4 }}>@ {p.low_time}</span>}
            </span>
          )}
          {!hasTemp && <span style={{ color: "#222", fontSize: 10 }}>{p.city || "—"}</span>}
        </div>
        <span style={{ color: "#333", fontSize: 9, textAlign: "right" }}>
          {p.valid_as ? `${p.valid_as}` : ""}
        </span>
      </div>
      {expanded && (
        <div style={{
          marginTop: 8, padding: "8px 10px", background: "#060609",
          borderRadius: 3, fontSize: 10, color: "#444", lineHeight: 1.5,
        }}>
          <div><span style={{ color: "#555" }}>AWIPS:</span> {p.awipsid}</div>
          <div><span style={{ color: "#555" }}>WFO:</span> {p.cccc}</div>
          <div><span style={{ color: "#555" }}>Issue:</span> {p.issue}</div>
          {p.city && <div><span style={{ color: "#555" }}>Report:</span> {p.city}</div>}
        </div>
      )}
    </div>
  );
}
