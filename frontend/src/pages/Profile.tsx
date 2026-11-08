import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, Fact, fmtTime, label } from "../api";

const SECTION_TITLES: Record<string, string> = {
  contact: "Contact info",
  work_history: "Work history",
  education: "Education",
  certifications: "Certifications",
  authorization: "Work authorization",
  preferences: "Job preferences",
  skills: "Skills",
  answers: "Application answers",
};

const SOURCE_LABEL: Record<string, string> = {
  dashboard: "typed here",
  telegram: "Telegram",
  resume_import: "from resume",
  seed: "starter",
};

export default function ProfilePage() {
  const [facts, setFacts] = useState<Fact[]>([]);
  const [sections, setSections] = useState<string[]>([]);
  const [q, setQ] = useState("");

  const load = useCallback(() => { api.get<Fact[]>("/api/profile/facts").then(setFacts); }, []);
  useEffect(() => {
    load();
    api.get<string[]>("/api/profile/sections").then(setSections);
  }, [load]);

  const visible = facts.filter((f) =>
    !q || `${f.label} ${f.value ?? ""}`.toLowerCase().includes(q.toLowerCase()),
  );

  return (
    <>
      <h1>About Me</h1>
      <p className="muted">
        Everything JobPilot knows about you. Only <span className="badge ok">confirmed</span> facts are
        ever used on applications. Answers you give on Telegram land here too.
      </p>
      <CommonQuestions onSaved={load} />
      <input placeholder="Search your profile" value={q} onChange={(e) => setQ(e.target.value)}
        style={{ marginBottom: 16 }} />
      {sections.map((s) => (
        <Section key={s} section={s} facts={visible.filter((f) => f.section === s)} onChange={load}
          showEmpty={!q} />
      ))}
    </>
  );
}

interface CommonQ {
  id: string; section: string; label: string; group: string; options: string[];
  help: string; suggested: string; value: string | null;
}

// The questions most applications ask, answered once so applications don't stop to ask.
function CommonQuestions({ onSaved }: { onSaved: () => void }) {
  const [rows, setRows] = useState<CommonQ[]>([]);
  const [vals, setVals] = useState<Record<string, string>>({});
  const [open, setOpen] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    api.get<CommonQ[]>("/api/profile/common").then((r) => {
      setRows(r);
      setVals(Object.fromEntries(r.map((q) => [q.id, q.value ?? q.suggested ?? ""])));
      if (r.some((q) => !q.value)) setOpen(true);
    });
  }, []);
  useEffect(load, [load]);

  const missing = rows.filter((q) => !q.value).length;
  const groups = [...new Set(rows.map((q) => q.group))];

  const save = async () => {
    setErr(""); setMsg("");
    const answers: Record<string, string> = {};
    for (const q of rows) {
      const v = (vals[q.id] ?? "").trim();
      if (v && v !== (q.value ?? "")) answers[q.id] = v;
    }
    try {
      const r = await api.post<{ saved: number }>("/api/profile/common", { answers });
      setMsg(`Saved ${r.saved} answer${r.saved === 1 ? "" : "s"}.`);
      load(); onSaved();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <div className="card">
      <div className="spread">
        <h2 style={{ margin: 0 }}>Common questions</h2>
        <button className="ghost" onClick={() => setOpen(!open)}>{open ? "Hide" : "Show"}</button>
      </div>
      <p className="muted small" style={{ marginTop: 6 }}>
        Things most applications ask. Answer them once and JobPilot uses them on every site, whatever
        the site calls the field. {missing > 0 ? `${missing} not answered yet.` : "All answered."}
        {" "}You can also do this on Telegram with /setup.
      </p>
      {open && (
        <div className="stack" style={{ marginTop: 10 }}>
          {groups.map((g) => (
            <div key={g}>
              <h3 style={{ margin: "12px 0 6px" }}>{g}</h3>
              {rows.filter((q) => q.group === g).map((q) => (
                <label key={q.id} className="stack" style={{ marginBottom: 10, gap: 4 }}>
                  <span className="small">
                    {q.label}
                    {!q.value && <span className="badge warn" style={{ marginLeft: 6 }}>not saved</span>}
                  </span>
                  {q.options.length ? (
                    <select value={vals[q.id] ?? ""} onChange={(e) => setVals({ ...vals, [q.id]: e.target.value })}>
                      <option value="">Choose...</option>
                      {q.options.map((o) => <option key={o} value={o}>{o}</option>)}
                    </select>
                  ) : (
                    <input value={vals[q.id] ?? ""} placeholder={q.help}
                      onChange={(e) => setVals({ ...vals, [q.id]: e.target.value })} />
                  )}
                </label>
              ))}
            </div>
          ))}
          <div className="row">
            <button className="primary" onClick={save}>Save answers</button>
            {msg && <span className="small muted">{msg}</span>}
          </div>
          {err && <div className="error">{err}</div>}
        </div>
      )}
    </div>
  );
}

function Section({ section, facts, onChange, showEmpty }: {
  section: string; facts: Fact[]; onChange: () => void; showEmpty: boolean;
}) {
  const [adding, setAdding] = useState(false);
  if (!showEmpty && facts.length === 0) return null;
  return (
    <div className="card">
      <div className="spread">
        <h2 style={{ margin: 0 }}>{SECTION_TITLES[section] ?? label(section)}</h2>
        <button className="ghost" onClick={() => setAdding(!adding)}>{adding ? "Cancel" : "+ Add"}</button>
      </div>
      {adding && <FactForm section={section} onSaved={() => { setAdding(false); onChange(); }} />}
      <div style={{ marginTop: 10 }}>
        {facts.length === 0 && !adding && <p className="muted small">Nothing here yet.</p>}
        {facts.map((f) => <FactRow key={f.id} fact={f} onChange={onChange} />)}
      </div>
    </div>
  );
}

function FactForm({ section, onSaved }: { section: string; onSaved: () => void }) {
  const [lbl, setLbl] = useState("");
  const [val, setVal] = useState("");
  const [err, setErr] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr("");
    try {
      await api.post("/api/profile/facts", { section, label: lbl, value: val, status: "confirmed" });
      onSaved();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  return (
    <form className="stack" onSubmit={submit} style={{ marginTop: 10 }}>
      <input placeholder={section === "answers" ? "Question (e.g. Willing to relocate?)" : "Label (e.g. Phone)"}
        value={lbl} onChange={(e) => setLbl(e.target.value)} required />
      <textarea placeholder="Value" value={val} onChange={(e) => setVal(e.target.value)} />
      <div><button className="primary">Save</button></div>
      {err && <div className="error">{err}</div>}
    </form>
  );
}

function FactRow({ fact, onChange }: { fact: Fact; onChange: () => void }) {
  const [editing, setEditing] = useState(false);
  const [val, setVal] = useState(fact.value ?? "");
  const [err, setErr] = useState("");
  const [history, setHistory] = useState<{ value: string | null; source: string; changed_at: string }[] | null>(null);

  const save = async (body: Record<string, unknown>) => {
    setErr("");
    try {
      await api.patch(`/api/profile/facts/${fact.id}`, body);
      setEditing(false);
      onChange();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const remove = async () => {
    if (!confirm(`Delete "${fact.label}"?`)) return;
    await api.del(`/api/profile/facts/${fact.id}`);
    onChange();
  };

  const toggleHistory = async () => {
    setHistory(history ? null : await api.get(`/api/profile/facts/${fact.id}/history`));
  };

  return (
    <div className="list-item">
      <div className="spread">
        <div style={{ fontWeight: 600 }}>{fact.label}</div>
        <div className="row">
          <span className={`badge ${fact.status === "confirmed" ? "ok" : "warn"}`}>{fact.status}</span>
          <span className="badge">{SOURCE_LABEL[fact.source] ?? fact.source}</span>
        </div>
      </div>
      {editing ? (
        <div className="stack" style={{ marginTop: 6 }}>
          <textarea value={val} onChange={(e) => setVal(e.target.value)} autoFocus />
          <div className="row">
            <button className="primary" onClick={() => save({ value: val, status: "confirmed" })}>Save</button>
            <button onClick={() => { setEditing(false); setVal(fact.value ?? ""); }}>Cancel</button>
          </div>
        </div>
      ) : (
        <div className="fact-value" style={{ marginTop: 4 }}>
          {fact.value || <span className="muted">(empty)</span>}
        </div>
      )}
      <div className="row small muted" style={{ marginTop: 4 }}>
        <span>
          {fact.confirmed_at ? `Confirmed ${fmtTime(fact.confirmed_at)}` : "Not confirmed"}
          {` · used ${fact.used_count}×`}
        </span>
        {!editing && <button className="ghost" onClick={() => setEditing(true)}>Edit</button>}
        {fact.status === "unconfirmed" && !editing && (
          <button className="ghost" onClick={() => save({ status: "confirmed" })}>Confirm</button>
        )}
        <button className="ghost" onClick={toggleHistory}>{history ? "Hide history" : "History"}</button>
        <button className="ghost" onClick={remove}>Delete</button>
      </div>
      {history && (
        <div className="small" style={{ marginTop: 6 }}>
          {history.map((h, i) => (
            <div key={i} className="spread">
              <span className="fact-value">{h.value ?? "(empty)"}</span>
              <span className="muted">{SOURCE_LABEL[h.source] ?? h.source} · {fmtTime(h.changed_at)}</span>
            </div>
          ))}
        </div>
      )}
      {err && <div className="error">{err}</div>}
    </div>
  );
}
