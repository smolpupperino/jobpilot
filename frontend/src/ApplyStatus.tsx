import { useEffect, useState } from "react";
import { api, ApiError, AutomationSettings } from "./api";

export interface BatchState {
  running: boolean;
  target: number;
  submitted: number;
  tried: number;
  current: number | null;
}

export interface ApplyState {
  running: boolean;
  job_id: number | null;
  step: number;
  message: string;
  view_url: string | null;
  batch?: BatchState;
}

/** Live status of the application worker (and batch), with Watch and Stop. Polls every 3s. */
export default function ApplyStatus() {
  const [st, setSt] = useState<ApplyState | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => api.get<ApplyState>("/api/apply/status").then((s) => alive && setSt(s)).catch(() => {});
    load();
    const t = setInterval(load, 3000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  const b = st?.batch;
  if (!st || (!st.running && !b?.running)) return null;
  return (
    <div className="banner spread ok" role="status">
      <span>
        {b?.running && <strong>Batch: {b.submitted} of {b.target} applied ({b.tried} tried). </strong>}
        {st.running ? <><strong>Applying (job #{st.job_id}).</strong> {st.message}</> : "Picking the next job..."}
      </span>
      <span className="row">
        {st.view_url && <a className="btn" style={{ textDecoration: "none", display: "inline-flex", alignItems: "center" }} href={st.view_url} target="_blank" rel="noopener noreferrer">Watch</a>}
        <button className="danger" onClick={() => api.post<ApplyState>("/api/apply/stop").then(setSt)}>Stop</button>
      </span>
    </div>
  );
}

/** "Apply to the next N jobs" with N from Settings (editable here too). */
export function BatchButton({ onStarted }: { onStarted?: () => void }) {
  const [a, setA] = useState<AutomationSettings | null>(null);
  const [n, setN] = useState(5);
  const [msg, setMsg] = useState("");
  useEffect(() => {
    api.get<AutomationSettings>("/api/settings/automation").then((x) => { setA(x); setN(x.batch_size); });
  }, []);
  if (!a) return null;
  const start = async () => {
    setMsg("");
    try {
      if (n !== a.batch_size) setA(await api.put<AutomationSettings>("/api/settings/automation", { ...a, batch_size: n }));
      await api.post(`/api/apply/batch?n=${n}`);
      setMsg(`Started. Applying to the next ${n} jobs; the summary goes to Telegram.`);
      onStarted?.();
    } catch (e) {
      const d = (e as ApiError).detail;
      setMsg(typeof d === "string" ? d : (e as Error).message);
    }
  };
  return (
    <div className="card stack">
      <div className="row">
        <span>Apply to the next</span>
        <input type="number" min={1} max={50} value={n} style={{ width: 80 }} aria-label="Number of jobs"
          onChange={(e) => setN(Math.max(1, Math.min(50, Number(e.target.value) || 1)))} />
        <span>jobs</span>
        <button className="primary" onClick={start}>Start batch</button>
      </div>
      <span className="muted small">Picks Interested jobs first, then New. Poor fits are skipped and don't count.</span>
      {msg && <span className="small">{msg}</span>}
    </div>
  );
}
