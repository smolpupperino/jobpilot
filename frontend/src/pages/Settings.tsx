import { FormEvent, useEffect, useState } from "react";
import { api, AutomationSettings, fmtTime, TaskRoute } from "../api";

interface AIResponse {
  settings: { tasks: Record<string, TaskRoute> };
  tasks: Record<string, string>;
  model_suggestions: Record<string, string[]>;
  effort_choices: Record<string, Record<string, string[]>>;
  providers?: string[];
}

const DEFAULT_PROVIDERS = ["claude", "codex", "none"];

export default function SettingsPage({ onLogout }: { onLogout?: () => void }) {
  return (
    <>
      <h1>Settings</h1>
      <Automation />
      <AIRouting />
      <GeminiConnect />
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
      <label className="row"><input type="checkbox" style={{ width: "auto", minHeight: 0 }} checked={a.browser_agent}
        onChange={(e) => save({ ...a, browser_agent: e.target.checked })} /> Smart browser agent (works out unusual dropdowns, popups and stuck pages step by step before asking me)</label>
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
      <label className="field" style={{ maxWidth: 320 }}>
        Batch size (jobs per batch; Telegram: /batch or /batch 8)
        <input type="number" min={1} max={50} value={a.batch_size}
          onChange={(e) => setA({ ...a, batch_size: Math.max(1, Math.min(50, Number(e.target.value) || 1)) })}
          onBlur={() => save(a)} />
      </label>
      <label className="row"><input type="checkbox" style={{ width: "auto", minHeight: 0 }} checked={a.discovery}
        onChange={(e) => save({ ...a, discovery: e.target.checked })} /> Find jobs in job-alert emails (and track employer replies)</label>
      {a.discovery && (
        <label className="field" style={{ maxWidth: 320 }}>
          Check the job Gmail every
          <select value={a.discovery_interval_minutes}
            onChange={(e) => save({ ...a, discovery_interval_minutes: Number(e.target.value) })}>
            {[[15, "15 minutes"], [30, "30 minutes"], [60, "hour"], [180, "3 hours"], [360, "6 hours"], [1440, "day"]].map(([v, l]) => (
              <option key={v} value={v}>{l}</option>))}
          </select>
        </label>
      )}
      {a.discovery && <Discovery />}
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

interface DiscoveryStatus {
  last_scan: string | null;
  last_result: { alerts?: number; added?: number; replies?: number; error?: string | null } | null;
}

function Discovery() {
  const [d, setD] = useState<DiscoveryStatus | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.get<DiscoveryStatus>("/api/discovery/status").then(setD); }, []);
  const scan = async () => {
    setBusy(true);
    try { setD(await api.post<DiscoveryStatus>("/api/discovery/scan")); } finally { setBusy(false); }
  };
  const r = d?.last_result;
  return (
    <div className="row small">
      <span className="muted">
        {!d?.last_scan ? "Not checked yet." : r?.error ? `Last check ${fmtTime(d.last_scan)}: ${r.error}`
          : `Last check ${fmtTime(d.last_scan)}: ${r?.added ?? 0} new jobs, ${r?.replies ?? 0} employer emails.`}
      </span>
      <button className="ghost" disabled={busy} onClick={scan}>{busy ? "Checking..." : "Check now"}</button>
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
  const PROVIDERS = data.providers ?? DEFAULT_PROVIDERS;

  const effortsFor = (provider: string, model: string): string[] => {
    const per = data.effort_choices[provider] ?? {};
    return (model && per[model]) || per["*"] || [];
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

interface GeminiStatus { installed: boolean; signed_in: boolean; runner_ok: boolean; models: string[] }

/** One-time Google sign-in for Gemini: JobPilot gets the link, you sign in, paste the code. */
function GeminiConnect() {
  const [st, setSt] = useState<GeminiStatus | null>(null);
  const [url, setUrl] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const load = () => { api.get<GeminiStatus>("/api/settings/gemini").then(setSt).catch(() => setSt(null)); };
  useEffect(load, []);
  const start = async () => {
    setBusy(true); setMsg("");
    try {
      const r = await api.post<{ url: string }>("/api/settings/gemini/start");
      setUrl(r.url);
      window.open(r.url, "_blank", "noopener");
    } catch (e) { setMsg((e as Error).message); } finally { setBusy(false); }
  };
  const finish = async () => {
    setBusy(true); setMsg("");
    try {
      setSt(await api.post<GeminiStatus>("/api/settings/gemini/finish", { code }));
      setUrl(""); setCode(""); setMsg("Gemini is connected. Pick it as a provider or fallback above, then Save.");
    } catch (e) { setMsg((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <div className="card stack">
      <h2>Gemini</h2>
      {!st ? <p className="muted small">Checking...</p> : !st.installed ? (
        <p className="small">The Gemini CLI isn't installed on the mini PC.</p>
      ) : st.signed_in ? (
        <p className="small"><span className="badge ok">Connected</span>{" "}
          {st.models.length ? `Models: ${st.models.join(", ")}` : "Uses Gemini's default model."}</p>
      ) : (
        <p className="small"><span className="badge">Not connected</span> Sign in once with your Google account
          to use Gemini (for example as a fallback). Gemini has no effort setting.</p>
      )}
      {st?.installed && !url && (
        <div><button className={st.signed_in ? "ghost" : "primary"} disabled={busy} onClick={start}>
          {busy ? "Starting..." : st.signed_in ? "Sign in again" : "Connect Gemini"}</button></div>
      )}
      {url && (
        <div className="stack">
          <p className="small">1. <a href={url} target="_blank" rel="noopener noreferrer">Open Google sign-in</a>{" "}
            (opened in a new tab) and sign in. 2. Google shows a code: paste it here.</p>
          <div className="row">
            <input placeholder="Paste the code from Google" value={code} onChange={(e) => setCode(e.target.value)}
              autoComplete="off" />
            <button className="primary" disabled={busy || code.trim().length < 4} onClick={finish}>
              {busy ? "Checking..." : "Finish"}</button>
          </div>
        </div>
      )}
      {msg && <span className="small">{msg}</span>}
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
