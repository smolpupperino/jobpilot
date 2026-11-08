import { useCallback, useEffect, useRef, useState } from "react";
import { api, fmtTime, Resume } from "../api";

export default function ResumePage() {
  const [list, setList] = useState<Resume[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(() => { api.get<Resume[]>("/api/resumes").then(setList); }, []);
  useEffect(load, [load]);

  const upload = async () => {
    const f = fileRef.current?.files?.[0];
    if (!f) return;
    setErr("");
    setBusy(true);
    const fd = new FormData();
    fd.append("file", f);
    try {
      await api.post("/api/resumes", fd);
      if (fileRef.current) fileRef.current.value = "";
      load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const showText = async (id: number) => {
    const r = await api.get<{ pages: string[] }>(`/api/resumes/${id}/text`);
    setText(r.pages.join("\n\n— page break —\n\n"));
  };

  return (
    <>
      <h1>Resume</h1>
      <p className="muted">
        Your PDF is uploaded to applications exactly as-is. JobPilot never edits it. Each upload becomes a
        new version, and every application records which version (by fingerprint) it used.
      </p>
      <div className="card stack">
        <h2>Upload a new version</h2>
        <input ref={fileRef} type="file" accept="application/pdf" />
        <div><button className="primary" onClick={upload} disabled={busy}>Upload PDF</button></div>
        {err && <div className="error">{err}</div>}
      </div>
      {list.map((r) => (
        <div className="card" key={r.id}>
          <div className="spread">
            <div>
              <div style={{ fontWeight: 600 }}>
                Version {r.version} {r.is_active && <span className="badge ok">in use</span>}
              </div>
              <div className="muted small">{r.filename} · {(r.size_bytes / 1024).toFixed(0)} KB · {fmtTime(r.uploaded_at)}</div>
            </div>
            <div className="row">
              <a className="btn" href={`/api/resumes/${r.id}/file`} target="_blank" rel="noopener">View</a>
              <button onClick={() => showText(r.id)}>Text</button>
              {!r.is_active && (
                <button onClick={() => api.post(`/api/resumes/${r.id}/activate`).then(load)}>Use this one</button>
              )}
            </div>
          </div>
          <div className="mono muted" style={{ marginTop: 8 }}>SHA-256 {r.sha256}</div>
        </div>
      ))}
      {text !== null && (
        <div className="card">
          <div className="spread"><h2>Extracted text</h2><button className="ghost" onClick={() => setText(null)}>Close</button></div>
          <p className="muted small">This is what application systems "see" when they read your PDF.</p>
          <pre className="fact-value small" style={{ margin: 0 }}>{text}</pre>
        </div>
      )}
    </>
  );
}
