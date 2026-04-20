import { useRef, useState } from "react";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export default function ExportButton({ token, projectId, onImport }) {
  const [exportLoading, setExportLoading] = useState(false);
  const [importLoading, setImportLoading] = useState(false);
  const [error, setError] = useState(null);
  const fileRef = useRef(null);

  const handleExport = async () => {
    if (!projectId) return;
    setExportLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/projects/${projectId}/export`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? `HTTP ${res.status}`);
      const blob = await res.blob();
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement("a");
      a.href     = url;
      const cd   = res.headers.get("Content-Disposition") ?? "";
      const m    = cd.match(/filename="([^"]+)"/);
      a.download = m ? m[1] : "conversation.pdf";
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err.message);
    } finally {
      setExportLoading(false);
    }
  };

  const handleImportFile = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (!confirm("Ce PDF contient vos donnees reelles. Restaurer la session ?")) {
      e.target.value = ""; return;
    }
    setImportLoading(true);
    setError(null);
    const form = new FormData();
    form.append("file", file);
    try {
      const res = await fetch(`${API_URL}/api/projects/import`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: form,
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? `HTTP ${res.status}`);
      if (onImport) onImport(await res.json());
    } catch (err) {
      setError(err.message);
    } finally {
      setImportLoading(false);
      e.target.value = "";
    }
  };

  return (
    <div className="space-y-1.5">
      <button
        onClick={handleExport}
        disabled={!projectId || exportLoading}
        className="w-full flex items-center gap-2 px-3 py-2 rounded-xl text-xs font-medium
                   border border-proxy-card text-proxy-text/50
                   hover:text-proxy-accent hover:border-proxy-accent/30 hover:bg-proxy-accent/5
                   disabled:opacity-25 disabled:cursor-not-allowed transition-all"
      >
        {exportLoading
          ? <svg className="animate-spin w-3.5 h-3.5 shrink-0" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" strokeDasharray="30 10"/></svg>
          : <svg className="w-3.5 h-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/></svg>
        }
        Telecharger PDF
      </button>

      <button
        onClick={() => fileRef.current?.click()}
        disabled={importLoading}
        className="w-full flex items-center gap-2 px-3 py-2 rounded-xl text-xs font-medium
                   border border-proxy-card text-proxy-text/50
                   hover:text-proxy-accent hover:border-proxy-accent/30 hover:bg-proxy-accent/5
                   disabled:opacity-25 disabled:cursor-not-allowed transition-all"
      >
        {importLoading
          ? <svg className="animate-spin w-3.5 h-3.5 shrink-0" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" strokeDasharray="30 10"/></svg>
          : <svg className="w-3.5 h-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"/></svg>
        }
        Reprendre conversation
      </button>

      <input ref={fileRef} type="file" accept=".pdf" className="hidden" onChange={handleImportFile} />
      {error && (
        <p className="text-[10px] text-red-400 px-1 truncate" title={error}>{error}</p>
      )}
    </div>
  );
}
