/* Portal - đọc REST /api/* và WebSocket /ws của backend (cùng origin qua nginx proxy). */
"use strict";

const API_BASE = ""; // same origin
const WS_URL = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;

const state = {
  slots: new Map(),
  sessions: [],
  filter: "",
  ws: null,
  reconnectDelay: 1000,
  refreshTimer: null,
};

const el = {
  slotGrid: document.getElementById("slotGrid"),
  slotsEmpty: document.getElementById("slotsEmpty"),
  sessionRows: document.getElementById("sessionRows"),
  sessionsEmpty: document.getElementById("sessionsEmpty"),
  plateSearch: document.getElementById("plateSearch"),
  connDot: document.getElementById("connDot"),
  connText: document.getElementById("connText"),
  statFree: document.getElementById("statFree"),
  statTotal: document.getElementById("statTotal"),
  statInside: document.getElementById("statInside"),
  statSessions: document.getElementById("statSessions"),
  lastUpdate: document.getElementById("lastUpdate"),
};

// ---- tiện ích ----
function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("vi-VN", { hour12: false });
}

function fmtDistance(value) {
  return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(1)} cm` : "—";
}

async function apiGet(path) {
  const res = await fetch(API_BASE + path, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

// ---- render ô đỗ ----
function renderSlots() {
  const slots = [...state.slots.values()].sort((a, b) => a.id - b.id);
  el.slotGrid.replaceChildren();
  el.slotsEmpty.hidden = slots.length > 0;

  for (const slot of slots) {
    const card = document.createElement("div");
    card.className = `slot ${slot.occupied ? "occupied" : "free"}`;

    const top = document.createElement("div");
    top.className = "slot-top";
    const name = document.createElement("span");
    name.className = "slot-name";
    name.textContent = `Ô ${slot.id}`;
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = slot.occupied ? "Có xe" : "Trống";
    top.append(name, badge);

    const distance = document.createElement("span");
    distance.className = "slot-distance";
    distance.textContent = `Khoảng cách: ${fmtDistance(slot.distance_cm)}`;

    card.append(top, distance);
    el.slotGrid.append(card);
  }

  const free = slots.filter((s) => !s.occupied).length;
  el.statFree.textContent = String(free);
  el.statTotal.textContent = String(slots.length || 4);
}

// ---- render phiên ----
function renderSessions() {
  const filter = state.filter.trim().toLowerCase();
  const rows = filter
    ? state.sessions.filter((s) => (s.plate || "").toLowerCase().includes(filter))
    : state.sessions;

  el.sessionRows.replaceChildren();
  el.sessionsEmpty.hidden = rows.length > 0;
  el.sessionsEmpty.textContent = state.sessions.length === 0
    ? "Chưa có phiên nào."
    : "Không tìm thấy biển số phù hợp.";

  for (const s of rows) {
    const tr = document.createElement("tr");

    const cells = [
      String(s.id ?? ""),
      s.plate ?? "—",
      fmtTime(s.entry_time),
      fmtTime(s.exit_time),
    ];
    for (const text of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.append(td);
    }

    const statusTd = document.createElement("td");
    const status = document.createElement("span");
    status.className = `status ${s.status === "inside" ? "inside" : "exited"}`;
    status.textContent = s.status === "inside" ? "Đang gửi" : "Đã ra";
    statusTd.append(status);
    tr.append(statusTd);

    const confTd = document.createElement("td");
    confTd.textContent =
      typeof s.confidence === "number" ? `${Math.round(s.confidence * 100)}%` : "—";
    tr.append(confTd);

    el.sessionRows.append(tr);
  }

  const inside = state.sessions.filter((s) => s.status === "inside").length;
  el.statInside.textContent = String(inside);
  el.statSessions.textContent = String(state.sessions.length);
}

function markUpdated() {
  el.lastUpdate.textContent = `Cập nhật: ${new Date().toLocaleTimeString("vi-VN", { hour12: false })}`;
}

// ---- tải dữ liệu ----
async function loadSlots() {
  try {
    const slots = await apiGet("/api/slots");
    state.slots.clear();
    for (const slot of slots) state.slots.set(slot.id, slot);
    renderSlots();
  } catch (err) {
    console.error("Không tải được /api/slots", err);
  }
}

async function loadSessions() {
  try {
    state.sessions = await apiGet("/api/sessions?limit=50");
    renderSessions();
    markUpdated();
  } catch (err) {
    console.error("Không tải được /api/sessions", err);
  }
}

function scheduleSessionRefresh() {
  if (state.refreshTimer) return;
  state.refreshTimer = setTimeout(() => {
    state.refreshTimer = null;
    loadSessions();
  }, 600);
}

// ---- WebSocket ----
function setConn(status, text) {
  el.connDot.className = `dot ${status}`;
  el.connText.textContent = text;
}

function handleMessage(message) {
  if (!message || typeof message !== "object") return;

  if (message.type === "slot_update" && message.slot) {
    state.slots.set(message.slot.id, message.slot);
    renderSlots();
    markUpdated();
    return;
  }

  if (message.type === "event") {
    const topic = message.topic || "";
    if (/\/gate\/(in|out)\/(event|passed)$/.test(topic)) {
      scheduleSessionRefresh();
    }
  }
}

function connect() {
  setConn("", "Đang kết nối…");
  let ws;
  try {
    ws = new WebSocket(WS_URL);
  } catch (err) {
    console.error("Không mở được WebSocket", err);
    setTimeout(connect, state.reconnectDelay);
    return;
  }
  state.ws = ws;

  ws.addEventListener("open", () => {
    state.reconnectDelay = 1000;
    setConn("online", "Đã kết nối");
    loadSlots();
    loadSessions();
  });

  ws.addEventListener("message", (event) => {
    try {
      handleMessage(JSON.parse(event.data));
    } catch (err) {
      console.warn("Bản tin WebSocket không hợp lệ", err);
    }
  });

  ws.addEventListener("close", () => {
    setConn("offline", "Mất kết nối, đang thử lại…");
    state.ws = null;
    setTimeout(connect, state.reconnectDelay);
    state.reconnectDelay = Math.min(state.reconnectDelay * 2, 15000);
  });

  ws.addEventListener("error", () => {
    // đóng kết nối sẽ kích hoạt reconnect ở sự kiện close
    ws.close();
  });
}

// ---- khởi động ----
el.plateSearch.addEventListener("input", (event) => {
  state.filter = event.target.value;
  renderSessions();
});

loadSlots();
loadSessions();
connect();
