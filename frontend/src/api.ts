// Small fetch wrapper. Every request sends the CSRF header the backend requires.

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : (detail as { message?: string })?.message ?? `Error ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = {
    method,
    credentials: "same-origin",
    headers: { "X-JobPilot": "1" },
  };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const r = await fetch(path, init);
  if (r.status === 401 && !path.startsWith("/api/auth/")) {
    window.dispatchEvent(new Event("jp-logged-out"));
  }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) throw new ApiError(r.status, data?.detail ?? r.statusText);
  return data as T;
}

export const api = {
  get: <T>(p: string) => request<T>("GET", p),
  post: <T>(p: string, b?: unknown) => request<T>("POST", p, b),
  put: <T>(p: string, b?: unknown) => request<T>("PUT", p, b),
  patch: <T>(p: string, b?: unknown) => request<T>("PATCH", p, b),
  del: <T>(p: string) => request<T>("DELETE", p),
};

// ---- Types mirrored from the backend ----

export interface Fact {
  id: number;
  section: string;
  key: string;
  label: string;
  value: string | null;
  source: string;
  status: "confirmed" | "unconfirmed";
  sensitivity: string;
  confirmed_at: string | null;
  used_count: number;
  updated_at: string;
}

export interface Job {
  id: number;
  url: string;
  employer: string;
  title: string;
  location: string;
  ats: string;
  source: string;
  status: string;
  notes: string;
  created_at: string;
  updated_at: string;
}

export interface EventRow {
  id: number;
  ts: string;
  actor: string;
  type: string;
  job_id?: number | null;
  data: Record<string, unknown>;
}

export interface Resume {
  id: number;
  version: number;
  filename: string;
  sha256: string;
  size_bytes: number;
  is_active: boolean;
  uploaded_at: string;
}

export interface Question {
  id: number;
  question: string;
  options: string[];
  status: string;
  answer: string | null;
  job_id: number | null;
  created_at: string;
}

export interface Overview {
  window_hours: number;
  jobs_by_status: Record<string, number>;
  jobs_total: number;
  new_jobs_in_window: number;
  status_changes_in_window: Record<string, number>;
  waiting_for_owner: number;
  unconfirmed_facts: number;
  active_resume_version: number | null;
  automation_paused: boolean;
  dry_run: boolean;
  recent_events: EventRow[];
}

export interface TaskRoute {
  provider: string;
  model: string;
  fallback_provider: string;
  fallback_model: string;
  effort: string;
  fallback_effort: string;
}

export interface AutomationSettings {
  paused: boolean;
  dry_run: boolean;
  auto_submit: boolean;
  daily_summary_hour: number;
  fit_check: boolean;
  min_fit_score: number;
}

// Backend stores naive UTC timestamps; show them in the viewer's local time.
export function fmtTime(ts: string): string {
  const d = new Date(ts.endsWith("Z") ? ts : ts + "Z");
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export const label = (s: string) => s.replace(/_/g, " ");
