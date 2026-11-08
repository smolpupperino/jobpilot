import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, AutomationSettings, fmtTime, label, Overview, Question } from "../api";
import ApplyStatus from "../ApplyStatus";

function describe(e: Overview["recent_events"][number]): string {
  const d = e.data as Record<string, string>;
  switch (e.type) {
    case "job.added": return `Added a job (${d.ats ?? "unknown site"})`;
    case "job.status_changed": return `Job moved ${label(d.before)} → ${label(d.after)}`;
    case "profile.fact_created": return `Saved "${d.label}" to profile`;
    case "profile.fact_updated": return `Updated "${d.label}"`;
    case "profile.fact_deleted": return `Deleted "${d.label}"`;
    case "question.answered": return "Answered a question";
    case "question.created": return `Asked: "${d.question}"`;
    case "resume.uploaded": return `Uploaded resume v${d.version}`;
    case "settings.paused": return "Paused automation";
    case "settings.unpaused": return "Unpaused automation";
    case "apply.fit": return `Fit check: ${d.score}/100 (${d.verdict})`;
    case "apply.resume_uploaded": return `Attached your resume (${d.file})`;
    case "apply.wrote": return `Wrote answers for: ${Object.keys((d.answers ?? {}) as unknown as Record<string, string>).join("; ")}`;
    case "apply.resume_upload_failed": return "Couldn't attach your resume";
    case "apply.started": return `Started an application${d.dry_run ? " (dry run)" : ""}`;
    case "apply.page": return `Application step ${d.step}: ${label(d.page_kind)}`;
    case "apply.finished": return `Application finished: ${label(d.outcome)}`;
    case "apply.stopped": return `Application stopped: ${d.reason}`;
    case "apply.error": return `Application error: ${d.error}`;
    case "ai.call": return `AI ${d.provider}/${d.model} (${d.ms} ms)${d.ok ? "" : " failed"}`;
    case "question.decided": return `You chose: ${d.answer}`;
    default: return label(e.type.replace(".", ": "));
  }
}

export default function OverviewPage() {
  const [ov, setOv] = useState<Overview | null>(null);
  const [hours, setHours] = useState(24);
  const [questions, setQuestions] = useState<Question[]>([]);

  const load = useCallback(() => {
    api.get<Overview>(`/api/overview?hours=${hours}`).then(setOv);
    api.get<Question[]>("/api/questions").then((qs) => setQuestions(qs.filter((q) => q.status === "open")));
  }, [hours]);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  const togglePause = async () => {
    if (!ov) return;
    const cur = await api.get<AutomationSettings>("/api/settings/automation");
    await api.put("/api/settings/automation", { ...cur, paused: !cur.paused });
    load();
  };

  if (!ov) return null;
  const s = ov.jobs_by_status;
  const stats: [string, number][] = [
    [`New jobs (${hours}h)`, ov.new_jobs_in_window],
    ["Tracked", ov.jobs_total],
    ["Submitted ✓", s.submitted_confirmed],
    ["Submitted ?", s.submitted_uncertain],
    ["Interviews", s.interview],
    ["Waiting on you", ov.waiting_for_owner + questions.length],
  ];

  return (
    <>
      <div className="spread" style={{ marginBottom: 16 }}>
        <h1 style={{ margin: 0 }}>Overview</h1>
        <select value={hours} onChange={(e) => setHours(Number(e.target.value))} style={{ width: "auto" }}
          aria-label="Time window">
          <option value={24}>Last 24 hours</option>
          <option value={168}>Last 7 days</option>
          <option value={720}>Last 30 days</option>
        </select>
      </div>

      <ApplyStatus />
      <div className={`banner spread ${ov.automation_paused ? "" : "ok"}`}>
        <span>
          {ov.automation_paused ? "Automation is paused. Nothing runs on its own." : "Automation is on."}
          {ov.dry_run ? " Dry run is on: JobPilot fills forms but stops before submitting."
            : " Submitting needs your OK on Telegram every time."}
        </span>
        <button className={ov.automation_paused ? "primary" : "danger"} onClick={togglePause}>
          {ov.automation_paused ? "Unpause" : "Pause everything"}
        </button>
      </div>

      {(ov.active_resume_version === null || ov.unconfirmed_facts > 0) && (
        <div className="card">
          <h2>Setup checklist</h2>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {ov.active_resume_version === null && <li><Link to="/resume">Upload your resume PDF</Link></li>}
            {ov.unconfirmed_facts > 0 && (
              <li><Link to="/profile">Confirm {ov.unconfirmed_facts} profile facts</Link></li>
            )}
          </ul>
        </div>
      )}

      <div className="grid">
        {stats.map(([l, n]) => (
          <div className="stat" key={l}><div className="n">{n}</div><div className="l">{l}</div></div>
        ))}
      </div>

      {questions.length > 0 && (
        <div className="card">
          <h2>Questions for you</h2>
          {questions.map((q) => <QuestionRow key={q.id} q={q} onDone={load} />)}
        </div>
      )}

      <div className="card">
        <h2>Recent activity</h2>
        {ov.recent_events.length === 0 && <p className="muted">Nothing yet.</p>}
        {ov.recent_events.map((e) => (
          <div key={e.id} className="list-item spread small">
            <span>{describe(e)}</span>
            <span className="muted">{fmtTime(e.ts)} · {e.actor}</span>
          </div>
        ))}
      </div>
    </>
  );
}

function QuestionRow({ q, onDone }: { q: Question; onDone: () => void }) {
  const [text, setText] = useState("");
  const [err, setErr] = useState("");
  const send = async (answer: string) => {
    setErr("");
    try {
      await api.post(`/api/questions/${q.id}/answer`, { answer });
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  return (
    <div className="list-item stack">
      <div style={{ fontWeight: 600 }}>{q.question}</div>
      <div className="row">
        {q.options.map((o) => <button key={o} onClick={() => send(o)}>{o}</button>)}
      </div>
      <div className="row" style={{ flexWrap: "nowrap" }}>
        <input placeholder="Type an answer" value={text} onChange={(e) => setText(e.target.value)} />
        <button className="primary" disabled={!text.trim()} onClick={() => send(text)}>Save</button>
      </div>
      {err && <div className="error">{err}</div>}
    </div>
  );
}
