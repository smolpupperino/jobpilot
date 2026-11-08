import { useCallback, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api } from "./api";
import Login from "./pages/Login";
import OverviewPage from "./pages/Overview";
import JobsPage from "./pages/Jobs";
import ProfilePage from "./pages/Profile";
import ResumePage from "./pages/Resume";
import SettingsPage from "./pages/Settings";

type AuthState = { setup_done: boolean; logged_in: boolean; via_tailscale?: boolean };

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/jobs", label: "Jobs" },
  { to: "/profile", label: "About Me" },
  { to: "/resume", label: "Resume" },
  { to: "/settings", label: "Settings" },
];

export default function App() {
  const [auth, setAuth] = useState<AuthState | null>(null);

  const refresh = useCallback(() => {
    api.get<AuthState>("/api/auth/state").then(setAuth).catch(() =>
      setAuth({ setup_done: true, logged_in: false }),
    );
  }, []);

  useEffect(() => {
    refresh();
    const onOut = () => setAuth((a) => (a ? { ...a, logged_in: false } : a));
    window.addEventListener("jp-logged-out", onOut);
    return () => window.removeEventListener("jp-logged-out", onOut);
  }, [refresh]);

  if (!auth) return null;
  if (!auth.logged_in) return <Login setupDone={auth.setup_done} onDone={refresh} />;

  const logout = async () => {
    await api.post("/api/auth/logout");
    refresh();
  };

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">JobPilot</div>
        <nav className="nav">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end}>
              {n.label}
            </NavLink>
          ))}
        </nav>
        {!auth.via_tailscale && (
          <div style={{ position: "absolute", bottom: 16, left: 12 }}>
            <button className="ghost" onClick={logout}>Log out</button>
          </div>
        )}
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/profile" element={<ProfilePage />} />
          <Route path="/resume" element={<ResumePage />} />
          <Route path="/settings" element={<SettingsPage onLogout={auth.via_tailscale ? undefined : logout} />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <nav className="tabbar" aria-label="Main">
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end}>
            {n.label}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
