import { FormEvent, useEffect, useState } from "react";
import { api, AutomationSettings, fmtTime, TaskRoute } from "../api";

interface AIResponse {
  settings: { tasks: Record<string, TaskRoute> };
  tasks: Record<string, string>;
  model_suggestions: Record<string, string[]>;
  effort_choices: Record<string, Record<string, string[]>>;
}

const PROVIDERS = ["claude", "codex", "gemini", "none"];

export default function SettingsPage({ onLogout }: { onLogout?: () => void }) {
  return (
    <>
      <h1>Settings</h1>
      <Automation />
      <AIRouting />
      <JobAccounts />
      <Telegram />
      {onLogout ? (
        <>
          <Password />
          <div className="card"><button onClick={onLogout}>Log out</button></div>
        </>
      ) : (
        <div className="card">
          <h2>Sign-in</h2>
          <p>You're signed in automatically through your Tailscale account. No password needed.</p>
        </div>
      )}
    </>
  );
}

function Automation() {
  const [a, setA] = useState<AutomationSettings | null>(null);
  useEffect(() => { api.get<AutomationSettings>("/api/settings/automation").then(setA); }, []);
  if (!a) return null;
  const save = async (next: AutomationSettings) => setA(await api.put("/api/settings/automation", next));
  return (
    <div className="card stack">
      <h2>Automation</h2>
      <label className="row"><input type="checkbox" style={{ width: "auto", minHeight: 0 }} checked={a.paused}
        onChange={(e) => save({ ...a, paused: e.target.checked })} /> Paused (emergency stop)</label>
      <label className="row"><input type="checkbox" style={{ width: "auto", minHeight: 0 }} checked={a.dry_run}
        onChange={(e) => save({ ...a, dry_run: e.target.checked })} /> Dry run (fill forms but never submit)</label>
      <label className="row"><input type="checkbox" style={{ width: "auto", minHeight: 0 }} checked={a.auto_submit}
        onChange={(e) => save({ ...a, auto_submit: e.target.checked })} /> Submit automatically (don't wait for my tap on Telegram)</label>
      <label className="row"><input type="checkbox" style={{ width: "auto", minHeight: 0 }} checked={a.fit_check}
        onChange={(e) => save({ ...a, fit_check: e.target.checked })} /> Skip jobs that don't fit my resume</label>
      {a.fit_check && (
        <label className="field" style={{ maxWidth: 320 }}>
          Minimum fit score to apply
          <select value={a.min_fit_score} onChange={(e) => save({ ...a, min_fit_score: Number(e.target.value) })}>
            {[[30, "30: only skip wildly different jobs"], [40, "40: skip poor fits"], [50, "50: skip poor fits and big stretches (recommended)"],
              [60, "60: only reasonable or better"], [70, "70: only strong-ish fits"], [80, "80: only strong fits"]].map(([v, l]) => (
              <option key={v} value={v}>{l}</option>))}
          </select>
        </label>
      )}
      <label className="field" style={{ maxWidth: 260 }}>
        Daily Telegram summary at
        <select value={a.daily_summary_hour} onChange={(e) => save({ ...a, daily_summary_hour: Number(e.target.value) })}>
          {Array.from({ length: 24 }, (_, h) => (
            <option key={h} value={h}>{new Date(2000, 0, 1, h).toLocaleTimeString([], { hour: "numeric" })}</option>
          ))}
        </select>
      </label>
    </div>
  );
}

function ModelSelect({ choices, value, disabled, onChange }: {
  choices: string[]; value: string; disabled?: boolean; onChange: (m: string) => void;
}) {
  const opts = choices.includes("") ? choices : ["", ...choices];
  // Keep an old saved value visible (flagged) instead of silently changing it.
  const unknown = value && !opts.includes(value) ? [value] : [];
  return (
    <select value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
      {opts.map((m) => <option key={m} value={m}>{m || "Default"}</option>)}
      {unknown.map((m) => <option key={m} value={m}>{m} (not available)</option>)}
    </select>
  );
}

interface GmailStatus { mode: string; address: string | null; signed_in: boolean | null; error?: string }
interface SiteAccount { id: number; site: string; login_email: string; status: string; created_at: string }

function JobAccounts() {
  const [g, setG] = useState<GmailStatus | null>(null);
  const [accts, setAccts] = useState<SiteAccount[]>([]);
  const [pw, setPw] = useState("");
  const [msg, setMsg] = useState("");
  const load = () => {
    api.get<GmailStatus>("/api/gmail/status").then(setG).catch(() => setG(null));
    api.get<SiteAccount[]>("/api/accounts").then(setAccts);
  };
  useEffect(load, []);
  const openGmail = async () => {
    setMsg("");
    try {
      const r = await api.post<{ view_url: string | null }>("/api/gmail/open");
      setMsg("Gmail is open in JobPilot's browser. Sign in there, then tap Check again.");
      if (r.view_url) window.open(r.view_url, "_blank", "noopener");
    } catch (e) { setMsg((e as Error).message); }
  };
  return (
    <div className="card stack">
      <h2>Job-site accounts &amp; Gmail</h2>
      <p className="muted small">
        JobPilot creates accounts on job sites (Workday, iCIMS, ...) with your job Gmail and one
        shared password, stored encrypted. It reads verification emails from that Gmail.
      </p>
      <div className="list-item">
        <div style={{ fontWeight: 600 }}>Gmail {g?.address ? `(${g.address})` : ""}</div>
        {!g ? <div className="muted small">Checking...</div> : g.mode === "browser" ? (
          <div className="small">
            {g.signed_in ? <span className="badge ok">Signed in inside JobPilot's browser</span>
              : <span className="badge">Not signed in</span>}
            {g.error && <span className="muted"> {g.error}</span>}
            {!g.address && <div className="muted">Set JOBPILOT_GMAIL_ADDRESS in .env.</div>}
          </div>
        ) : <div className="small"><span className="badge ok">{g.mode}</span></div>}
        <div className="row" style={{ marginTop: 8 }}>
          {g?.mode === "browser" && !g.signed_in && (
            <button className="primary" onClick={openGmail}>Sign in to Gmail in JobPilot's browser</button>
          )}
          <button className="ghost" onClick={load}>Check again</button>
        </div>
        {msg && <div className="small" style={{ marginTop: 6 }}>{msg}</div>}
      </div>
      <div className="list-item">
        <div className="spread">
          <div style={{ fontWeight: 600 }}>Job-site password</div>
          {pw ? <code style={{ userSelect: "all" }}>{pw}</code>
            : <button onClick={() => api.post<{ password: string }>("/api/accounts/password").then((r) => setPw(r.password))}>
                Show</button>}
        </div>
        <div className="muted small">The same password on every job site, so you can log in yourself too.</div>
      </div>
      {accts.length > 0 && <h3>Accounts created ({accts.length})</h3>}
      {accts.map((a) => (
        <div key={a.id} className="small spread">
          <span>{a.site}</span>
          <span className="muted">{a.status} · {fmtTime(a.created_at)}</span>
        </div>
      ))}
    </div>
  );
}

function AIRouting() {
  const [data, setData] = useState<AIResponse | null>(null);
  const [routes, setRoutes] = useState<Record<string, TaskRoute>>({});
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    api.get<AIResponse>("/api/settings/ai").then((d) => { setData(d); setRoutes(d.settings.tasks); });
  }, []);
  if (!data) return null;

  const effortsFor = (provider: string, model: string): string[] => {
    const per = data.effort_choices[provider] ?? {};
    return per[model] ?? per["*"] ?? [];
  };
  // Changing the model drops an effort level the new model doesn't support.
  const pickModel = (task: string, model: string, fallback = false) => {
    const r = routes[task];
    const provider = fallback ? r.fallback_provider : r.provider;
    const effort = fallback ? r.fallback_effort : r.effort;
    const keep = !effort || effortsFor(provider, model).includes(effort) ? effort : "";
    set(task, fallback ? { fallback_model: model, fallback_effort: keep } : { model, effort: keep });
  };

  const set = (task: string, patch: Partial<TaskRoute>) => {
    setSaved(false);
    setRoutes({ ...routes, [task]: { ...routes[task], ...patch } });
  };
  const save = async () => {
    setErr("");
    try {
      await api.put("/api/settings/ai", { tasks: routes });
      setSaved(true);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <div className="card stack">
      <h2>AI models</h2>
      <p className="muted small">
        Pick a provider, model, and effort level for each job JobPilot uses AI for. Higher effort
        thinks longer: better on tricky forms, slower, and uses more quota. Changes apply to the very next AI
        call, even in the middle of an application, because nothing about an application lives inside
        the AI. "Default" uses whatever that tool is set to use.
      </p>
      {Object.entries(data.tasks).map(([task, desc]) => {
        const r = routes[task];
        return (
          <div key={task} className="list-item">
            <div style={{ fontWeight: 600 }}>{desc}</div>
            <div className="form-grid" style={{ marginTop: 6 }}>
              <label className="field">Provider
                <select value={r.provider} onChange={(e) => set(task, { provider: e.target.value, model: "", effort: "" })}>
                  {PROVIDERS.map((p) => <option key={p}>{p}</option>)}
                </select>
              </label>
              <label className="field">Model
                <ModelSelect choices={data.model_suggestions[r.provider] ?? []} value={r.model}
                  disabled={r.provider === "none"} onChange={(m) => pickModel(task, m)} />
              </label>
              <label className="field">Effort
                <ModelSelect choices={effortsFor(r.provider, r.model)} value={r.effort}
                  disabled={r.provider === "none" || effortsFor(r.provider, r.model).length === 0}
                  onChange={(v) => set(task, { effort: v })} />
              </label>
              <label className="field">Fallback
                <select value={r.fallback_provider}
                  onChange={(e) => set(task, { fallback_provider: e.target.value, fallback_model: "", fallback_effort: "" })}>
                  {PROVIDERS.map((p) => <option key={p}>{p}</option>)}
                </select>
              </label>
              <label className="field">Fallback model
                <ModelSelect choices={data.model_suggestions[r.fallback_provider] ?? []} value={r.fallback_model}
                  disabled={r.fallback_provider === "none"} onChange={(m) => pickModel(task, m, true)} />
              </label>
              <label className="field">Fallback effort
                <ModelSelect choices={effortsFor(r.fallback_provider, r.fallback_model)} value={r.fallback_effort}
                  disabled={r.fallback_provider === "none" || effortsFor(r.fallback_provider, r.fallback_model).length === 0}
                  onChange={(v) => set(task, { fallback_effort: v })} />
              </label>
            </div>
          </div>
        );
      })}
      <div className="row">
        <button className="primary" onClick={save}>Save AI settings</button>
        {saved && <span className="badge ok">Saved</span>}
      </div>
      {err && <div className="error">{err}</div>}
    </div>
  );
}

function Telegram() {
  const [t, setT] = useState<Record<string, unknown> | null>(null);
  const [testSent, setTestSent] = useState(false);
  useEffect(() => { api.get<Record<string, unknown>>("/api/settings/telegram").then(setT); }, []);
  if (!t) return null;
  const sendTest = async () => {
    await api.post("/api/questions", {
      question: "Test from JobPilot: are you open to remote jobs?",
      options: ["Yes", "No", "Hybrid only"],
    });
    setTestSent(true);
  };
  const ready = Boolean(t.token_configured && t.owner_id_configured);
  return (
    <div className="card stack">
      <h2>Telegram</h2>
      <div className="row">
        <span className={`badge ${t.token_configured ? "ok" : "warn"}`}>Bot token {t.token_configured ? "set" : "missing"}</span>
        <span className={`badge ${t.owner_id_configured ? "ok" : "warn"}`}>Owner ID {t.owner_id_configured ? "set" : "missing"}</span>
        <span className={`badge ${t.running ? "ok" : ""}`}>{t.running ? "Connected" : "Not running"}</span>
      </div>
      {t.last_error ? <div className="error">Last error: {String(t.last_error)}</div> : null}
      {ready && (
        <div className="row">
          <button onClick={sendTest} disabled={testSent}>Send a test question</button>
          {testSent && <span className="small muted">Sent. Check Telegram within ~30 seconds. Your answer will show up on About Me.</span>}
        </div>
      )}
      {!ready && (
        <p className="muted small">
          Setup steps are in docs/SETUP.md. The token goes in the .env file on the mini PC, never in this
          dashboard or in chat.
        </p>
      )}
    </div>
  );
}

function Password() {
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [msg, setMsg] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await api.post("/api/auth/change-password", { current_password: cur, new_password: next });
      setMsg("Password changed. Other devices are logged out.");
      setCur(""); setNext("");
    } catch (e) {
      setMsg((e as Error).message);
    }
  };
  return (
    <form className="card stack" onSubmit={submit}>
      <h2>Change password</h2>
      <div className="form-grid">
        <input type="password" placeholder="Current password" autoComplete="current-password" value={cur}
          onChange={(e) => setCur(e.target.value)} required />
        <input type="password" placeholder="New password (12+ chars)" autoComplete="new-password" value={next}
          onChange={(e) => setNext(e.target.value)} required />
      </div>
      <div><button>Change password</button></div>
      {msg && <div className="small">{msg}</div>}
    </form>
  );
}
