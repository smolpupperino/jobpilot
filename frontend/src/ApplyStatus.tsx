import { useEffect, useState } from "react";
import { api } from "./api";

export interface ApplyState {
  running: boolean;
  job_id: number | null;
  step: number;
  message: string;
  view_url: string | null;
}

/** Live status of the application worker, with Watch and Stop. Polls every 3s. */
export default function ApplyStatus() {
  const [st, setSt] = useState<ApplyState | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => api.get<ApplyState>("/api/apply/status").then((s) => alive && setSt(s)).catch(() => {});
    load();
    const t = setInterval(load, 3000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  if (!st || !st.running) return null;
  return (
    <div className="banner spread ok" role="status">
      <span><strong>Applying (job #{st.job_id}).</strong> {st.message}</span>
      <span className="row">
        {st.view_url && <a className="btn" style={{ textDecoration: "none", display: "inline-flex", alignItems: "center" }} href={st.view_url} target="_blank" rel="noopener noreferrer">Watch</a>}
        <button className="danger" onClick={() => api.post<ApplyState>("/api/apply/stop").then(setSt)}>Stop</button>
      </span>
    </div>
  );
}
