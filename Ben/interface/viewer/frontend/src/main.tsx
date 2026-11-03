import { StrictMode, Suspense, lazy, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
const App = lazy(() => import("./App"));
import ScanConsole from "./ScanConsole";
import "./index.css";

function Entry() {
  const [launcher, setLauncher] = useState<boolean | null>(null);
  useEffect(() => {
    fetch("/api/scans")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setLauncher(d != null && d.available !== false))
      .catch(() => setLauncher(false));
  }, []);
  if (launcher === null)
    return <div style={{ padding: 32 }}>正在连接本地服务…</div>;
  return launcher ? <ScanConsole /> : <App />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Suspense fallback={<div style={{ padding: 32 }}>正在加载工作空间…</div>}>
      <Entry />
    </Suspense>
  </StrictMode>,
);
