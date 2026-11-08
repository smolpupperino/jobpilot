import { FormEvent, useState } from "react";
import { api } from "../api";

export default function Login({ setupDone, onDone }: { setupDone: boolean; onDone: () => void }) {
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr("");
    if (!setupDone && pw !== pw2) return setErr("Passwords don't match");
    setBusy(true);
    try {
      await api.post(setupDone ? "/api/auth/login" : "/api/auth/setup", { password: pw });
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="center-page">
      <form className="card stack" onSubmit={submit}>
        <h1 style={{ color: "var(--forest)" }}>JobPilot</h1>
        {!setupDone && (
          <p className="muted small">
            First run. Create the owner password (12+ characters). This is the only account.
          </p>
        )}
        <label className="field">
          Password
          <input type="password" autoComplete={setupDone ? "current-password" : "new-password"}
            value={pw} onChange={(e) => setPw(e.target.value)} autoFocus required />
        </label>
        {!setupDone && (
          <label className="field">
            Confirm password
            <input type="password" autoComplete="new-password" value={pw2}
              onChange={(e) => setPw2(e.target.value)} required />
          </label>
        )}
        <button className="primary" disabled={busy} style={{ width: "100%" }}>
          {setupDone ? "Log in" : "Create password"}
        </button>
        {err && <div className="error">{err}</div>}
      </form>
    </div>
  );
}
