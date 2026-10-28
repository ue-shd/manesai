// 画面。API（/api/...）を呼ぶだけで、計算はしない。数値の整形は表示のときだけ行う。
const { createApp } = Vue;

const MODE_COLORS = { 徒歩: "#2a9d55", 自転車: "#e67e22", バス: "#8e44ad", 電車: "#2980b9", タクシー: "#c0392b" };

async function api(path, options) {
  const res = await fetch(path, options);
  return res.json();
}

createApp({
  data() {
    return {
      dataset: null, settings: null, pick: {}, dataTab: "shops", dataNote: "", loadError: "",
      result: null, selected: 0, busy: false,
    };
  },
  computed: {
    plan() { return this.result && this.result.plans ? this.result.plans[this.selected] : null; },
    places() {
      const m = {};
      for (const s of this.dataset.shops) m[s.id] = s;
      for (const s of this.dataset.stations) m[s.id] = s;
      return m;
    },
    chart() {
      const p = this.plan;
      if (!p) return {};
      const t0 = p.totals.start_time, t1 = Math.max(p.totals.end_time, t0 + 60);
      const top = Math.max(this.settings.fullness_max, this.settings.fatigue_max, ...p.trace.map((r) => Math.max(r.fullness, r.fatigue))) * 1.1;
      const x = (t) => 40 + ((t - t0) / (t1 - t0)) * 550;
      const y = (v) => 240 - (v / top) * 230;
      const pts = (key) => p.trace.map((r) => `${x(r.time)},${y(r[key])}`).join(" ");
      const step = top > 150 ? 50 : 25;
      const grid = [];
      for (let v = 0; v <= top; v += step) grid.push({ v, y: y(v) });
      const hours = [];
      for (let h = Math.ceil(t0 / 60); h * 60 <= t1; h++) hours.push({ x: x(h * 60), label: `${h}時` });
      return { full: pts("fullness"), fat: pts("fatigue"), grid, hours, y };
    },
  },
  watch: {
    plan() { this.$nextTick(this.drawMap); },
  },
  async mounted() {
    try {
      this.settings = await api("/api/settings/default");
      await this.loadSample();
    } catch (e) {
      this.loadError = `読み込みに失敗しました：${e.message}`;
    }
  },
  methods: {
    async loadSample() {
      this.setDataset(await api("/api/sample"), "サンプル（大阪キタ・ミナミ）");
    },
    setDataset(data, note) {
      this.dataset = data;
      this.pick = Object.fromEntries(data.shops.map((s) => [s.id, ""]));
      this.dataNote = note;
      this.result = null;
      const ids = data.stations.map((s) => s.id);
      if (!ids.includes(this.settings.start_station)) this.settings.start_station = ids[0];
      if (!ids.includes(this.settings.end_station)) this.settings.end_station = ids[0];
    },
    async upload(e) {
      const file = e.target.files[0];
      if (!file) return;
      const form = new FormData();
      form.append("file", file);
      const data = await api("/api/upload", { method: "POST", body: form });
      e.target.value = "";
      if (data.status) { // 入力の誤り
        this.dataNote = `${data.status}：${data.message}`;
        return;
      }
      this.setDataset(data, file.name);
    },
    async solve() {
      this.busy = true;
      const settings = {
        ...this.settings,
        must: Object.keys(this.pick).filter((k) => this.pick[k] === "must"),
        exclude: Object.keys(this.pick).filter((k) => this.pick[k] === "exclude"),
      };
      try {
        this.result = await api("/api/plan", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ dataset: this.dataset, settings }),
        });
        this.selected = 0;
      } catch (e) {
        this.result = { status: "通信に失敗しました", message: e.message, plans: null };
      } finally {
        this.busy = false;
      }
    },
    drawMap() {
      const p = this.plan;
      if (!p) return;
      if (!this.map) {
        this.map = L.map("map");
        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
          maxZoom: 19, attribution: "&copy; OpenStreetMap contributors",
        }).addTo(this.map);
        this.layer = L.layerGroup().addTo(this.map);
      }
      this.layer.clearLayers();
      const bounds = [];
      let n = 0;
      for (const s of p.steps) {
        if (s.kind === "移動") {
          const a = this.places[s.from_id], b = this.places[s.to_id];
          L.polyline([[a.lat, a.lon], [b.lat, b.lon]], { color: this.color(s.mode), weight: 4, opacity: 0.8 })
            .bindTooltip(`${s.mode} ${s.minutes}分 ${this.yen(s.cost)}`).addTo(this.layer);
        } else {
          const place = this.places[s.id];
          const label = s.kind === "食事" ? String(++n) : s.kind[0];
          L.marker([place.lat, place.lon], {
            icon: L.divIcon({ className: `pin ${s.kind}`, html: label, iconSize: [24, 24] }),
          }).bindTooltip(s.kind === "食事" ? `${n}. ${s.shop}（${this.hm(s.start)}）` : `${s.kind}：${s.place}`)
            .addTo(this.layer);
          bounds.push([place.lat, place.lon]);
        }
      }
      this.map.invalidateSize();
      this.map.fitBounds(bounds, { padding: [30, 30] });
    },
    shopName(id) { return this.places[id] ? this.places[id].name : id; },
    color(mode) { return MODE_COLORS[mode] || "#666"; },
    hm(min) { return `${Math.floor(min / 60)}:${String(min % 60).padStart(2, "0")}`; },
    yen(v) { return `${Number(v).toLocaleString()}円`; },
    fmt(v) { return Number.isInteger(v) ? String(v) : v.toFixed(1); },
    statusClass(s) { return s === "最適" ? "ok" : s === "最適とは限らない解" ? "warn" : "ng"; },
  },
}).mount("#app");
