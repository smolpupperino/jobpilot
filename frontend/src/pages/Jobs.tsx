import { ChangeEvent, FormEvent, useCallback, useEffect, useState } from "react";
import { api, ApiError, EventRow, fmtTime, Job, label } from "../api";

export default function JobsPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [statuses, setStatuses] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState<number | null>(null);

  const load = useCallback(() => {
    api.get<Job[]>(`/api/jobs${filter ? `?status=${filter}` : ""}`).then(setJobs);
  }, [filter]);

  useEffect(() => { api.get<string[]>("/api/jobs/statuses").then(setStatuses); }, []);
  useEffect(load, [load]);

  return (
    <>
      <h1>Jobs</h1>
      <AddJob onAdded={load} />
      <div className="spread" style={{ marginBottom: 12 }}>
        <span className="muted small">{jobs.length} shown</span>
        <select value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: "auto" }}
          aria-label="Filter by status">
          <option value="">All statuses</option>
          {statuses.map((s) => <option key={s} value={s}>{label(s)}</option>)}
        </select>
      </div>
      {jobs.length === 0 && <div className="card muted">No jobs yet. Paste a job link above.</div>}
      {jobs.map((j) => (
        <JobCard key={j.id} job={j} statuses={statuses} expanded={open === j.id}
          onToggle={() => setOpen(open === j.id ? null : j.id)} onChange={load} />
      ))}
    </>
  );
}

function AddJob({ onAdded }: { onAdded: () => void }) {
  const [f, setF] = useState({ url: "", employer: "", title: "", location: "" });
  const [err, setErr] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr("");
    try {
      await api.post("/api/jobs", f);
      setF({ url: "", employer: "", title: "", location: "" });
      onAdded();
    } catch (e) {
      const d = (e as ApiError).detail as { message?: string } | string;
      setErr(typeof d === "string" ? d : d?.message ?? "Failed");
    }
  };
  const set = (k: keyof typeof f) => (e: ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value });
  return (
    <form className="card stack" onSubmit={submit}>
      <h2>Add a job</h2>
      <input placeholder="Paste the job link (https://…)" value={f.url} onChange={set("url")} required
        inputMode="url" />
      <div className="form-grid">
        <input placeholder="Employer" value={f.employer} onChange={set("employer")} />
        <input placeholder="Job title" value={f.title} onChange={set("title")} />
        <input placeholder="Location" value={f.location} onChange={set("location")} />
      </div>
      <div><button className="primary">Add job</button></div>
      {err && <div className="error">{err}</div>}
    </form>
  );
}

function JobCard({ job, statuses, expanded, onToggle, onChange }: {
  job: Job; statuses: string[]; expanded: boolean; onToggle: () => void; onChange: () => void;
}) {
  const [events, setEvents] = useState<EventRow[]>([]);
  const [notes, setNotes] = useState(job.notes);
  const [applyMsg, setApplyMsg] = useState("");

  const startApply = async (force = false) => {
    setApplyMsg("");
    try {
      await api.post(`/api/jobs/${job.id}/apply${force ? "?force=true" : ""}`);
      setApplyMsg("Started. Follow along on Overview or in Telegram.");
      onChange();
    } catch (e) {
      setApplyMsg((e as Error).message);
    }
  };

  useEffect(() => {
    if (expanded) api.get<{ events: EventRow[] }>(`/api/jobs/${job.id}`).then((r) => setEvents(r.events));
  }, [expanded, job.id, job.status]);

  const patch = async (body: Partial<Job>) => {
    await api.patch(`/api/jobs/${job.id}`, body);
    onChange();
  };

  const remove = async () => {
    if (!confirm("Delete this job and its history?")) return;
    await api.del(`/api/jobs/${job.id}`);
    onChange();
  };

  return (
    <div className="card">
      <div className="spread">
        <div style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 600 }}>{job.title || "Untitled job"}</div>
          <div className="muted small">
            {[job.employer, job.location].filter(Boolean).join(" · ") || "No employer set"}
          </div>
        </div>
        <div className="row">
          <span className="badge">{job.ats}</span>
          <select value={job.status} onChange={(e) => patch({ status: e.target.value })}
            style={{ width: "auto" }} aria-label="Status">
            {statuses.map((s) => <option key={s} value={s}>{label(s)}</option>)}
          </select>
        </div>
      </div>
      <div className="row small" style={{ marginTop: 8 }}>
        <a href={job.url} target="_blank" rel="noopener noreferrer">Open posting ↗</a>
        <button className="ghost" onClick={onToggle}>{expanded ? "Hide details" : "Details"}</button>
        {job.status === "skipped" ? (
          <button onClick={() => startApply(true)}>Apply anyway</button>
        ) : !["applying", "waiting_for_owner", "submitted_confirmed", "submitted_uncertain"].includes(job.status) && (
          <button className="primary" onClick={() => startApply()}>Apply with JobPilot</button>
        )}
      </div>
      {applyMsg && <div className="small" style={{ marginTop: 6 }}>{applyMsg}</div>}
      {expanded && (
        <div className="stack" style={{ marginTop: 12 }}>
          <label className="field">
            Notes
            <textarea value={notes} onChange={(e) => setNotes(e.target.value)} />
          </label>
          <div className="row">
            <button disabled={notes === job.notes} onClick={() => patch({ notes })}>Save notes</button>
            <button className="ghost" onClick={remove}>Delete job</button>
          </div>
          <h3 style={{ marginTop: 12 }}>Timeline</h3>
          {events.map((e) => (
            <div key={e.id} className="small spread">
              <span>
                {label(e.type)}{e.data.after ? `: ${label(String(e.data.after))}` : ""}
                {e.data.page_kind ? `: ${label(String(e.data.page_kind))}` : ""}
                {e.data.outcome ? `: ${label(String(e.data.outcome))}` : ""}
                {e.type === "apply.fit" ? `: ${String(e.data.score)}/100 (${String(e.data.verdict)}). ${String(e.data.summary)}` : ""}
                {Boolean(e.data.shot || e.data.evidence) && (
                  <> · <a href={`/api/shots/${String(e.data.shot || e.data.evidence)}`} target="_blank" rel="noopener noreferrer">screenshot</a></>
                )}
              </span>
              <span className="muted">{fmtTime(e.ts)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
