import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
const ACCEPTED_EXTS = [
  "pdf", "docx", "doc", "odt", "xlsx", "xls", "ods", "pptx", "ppt", "odp",
  "txt", "csv", "tsv", "rtf", "md",
  "jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif",
  "eml", "msg",
  "vtt", "srt",
  "html", "htm",
  "json", "xml", "yaml", "yml",
];
const FILE_ACCEPT = ".pdf,.docx,.doc,.odt,.xlsx,.xls,.ods,.pptx,.ppt,.odp,.txt,.csv,.tsv,.rtf,.md,.jpg,.jpeg,.png,.gif,.webp,.bmp,.tiff,.tif,.eml,.msg,.vtt,.srt,.html,.htm,.json,.xml,.yaml,.yml";
const OCR_IMAGE_EXTS = ["jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif"];
const PROTECTION_STORAGE_KEY = "privacy-proxy:upload-protection-mode";
const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;
const DIRECT_UPLOAD_EXTS = new Set([
  "xlsx", "xls", "ods",
  "pptx", "ppt", "odp",
  "vtt", "srt",
  "eml", "msg",
])
const PROTECTION_OPTIONS = [
  { value: "fast", labelKey: "protection.fast", tone: "text-sky-300", dot: "bg-sky-400", tooltipKey: "protection.fast_tooltip" },
  { value: "privacy", labelKey: "protection.privacy", tone: "text-emerald-300", dot: "bg-emerald-400", tooltipKey: "protection.privacy_tooltip" },
  { value: "strict", labelKey: "protection.strict", tone: "text-rose-300", dot: "bg-rose-400", tooltipKey: "protection.strict_tooltip" },
];
const LABEL_KEYS = {
  PERSON: "fileUpload.entity_person", PERSONNE: "fileUpload.entity_person", EMAIL: "fileUpload.entity_email", EMAIL_ADDRESS: "fileUpload.entity_email",
  IBAN: "fileUpload.entity_iban", IBAN_CODE: "fileUpload.entity_iban", TELEPHONE: "fileUpload.entity_phone", PHONE_NUMBER: "fileUpload.entity_phone",
  ADRESSE: "fileUpload.entity_address", LOCATION: "fileUpload.entity_address", DATE: "fileUpload.entity_date", DATE_TIME: "fileUpload.entity_date",
  CARTE: "fileUpload.entity_card", CREDIT_CARD: "fileUpload.entity_card", IP: "fileUpload.entity_ip", IP_ADDRESS: "fileUpload.entity_ip",
  NIR: "fileUpload.entity_nir", FR_NIR: "fileUpload.entity_nir", SALAIRE: "fileUpload.entity_salary", SALARY: "fileUpload.entity_salary",
  CONTRAT: "fileUpload.entity_contract", CONTRACT: "fileUpload.entity_contract", SIRET: "fileUpload.entity_siret", URL: "fileUpload.entity_url",
};
const BUSINESS_LABEL_KEYS = {
  REVENUE: "fileUpload.business_revenue", MARGIN: "fileUpload.business_margin", PRICING: "fileUpload.business_pricing",
  FORECAST: "fileUpload.business_forecast", CLIENT_LIST: "fileUpload.business_client_list", DEAL: "fileUpload.business_deal",
};

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} o`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} Ko`;
  return `${(bytes / 1048576).toFixed(1)} Mo`;
}

function buildSummary(breakdown, t) {
  if (!breakdown || !Object.keys(breakdown).length) return t("fileUpload.no_sensitive_entity");
  return Object.entries(breakdown)
    .sort(([, a], [, b]) => b - a)
    .map(([label, count]) => `${count} ${readableEntity(label, t)}`)
    .join(", ");
}

function readableEntity(type, t) {
  return LABEL_KEYS[type] ? t(LABEL_KEYS[type]) : type;
}

function readableBusinessEntity(type, t) {
  return BUSINESS_LABEL_KEYS[type] ? t(BUSINESS_LABEL_KEYS[type]) : type;
}

function createEmptyExclusions() {
  return { excludedSheets: [], excludedColumns: [], excludedRanges: [] };
}

export default function FileUpload({ token, onResult, onClose }) {
  const { t } = useTranslation();
  const protectionOptions = PROTECTION_OPTIONS.map((option) => ({
    ...option,
    label: t(option.labelKey),
    tooltip: t(option.tooltipKey),
  }));
  const inputRef = useRef(null);
  const [isDragging, setIsDragging] = useState(false);
  const [phase, setPhase] = useState("idle");
  const [preview, setPreview] = useState(null);
  const [pendingFile, setPendingFile] = useState(null);
  const [progress, setProgress] = useState(0);
  const [jobId, setJobId] = useState(null);
  const [processingMessage, setProcessingMessage] = useState(null);
  const [summary, setSummary] = useState(null);
  const [error, setError] = useState(null);
  const [downloading, setDownloading] = useState(false);
  const [showDetails, setShowDetails] = useState(false);
  const [protectionMode, setProtectionMode] = useState("privacy");
  const [excelExclusions, setExcelExclusions] = useState(createEmptyExclusions);
  const [showGranularControls, setShowGranularControls] = useState(false);

  const excelSheets = preview?.excel_controls?.sheets ?? [];
  const isExcelPreview = preview?.file_type === "xlsx" && excelSheets.length > 0;
  const hasSensitiveExcel = isExcelPreview && excelSheets.some((s) => (s.sensitive_columns ?? []).length > 0);

  const allExcelSheetNames = excelSheets.map((s) => s.name);
  const allExcelColumnIds = excelSheets.flatMap((s) => (s.sensitive_columns ?? []).map((c) => c.id));

  // Labels des colonnes sensibles (dedupes) pour le panel de decision
  const sensitiveColLabels = [...new Set(
    excelSheets.flatMap((s) => (s.sensitive_columns ?? []).map((c) => c.label || c.header || c.letter))
  )];
  // Resume par feuille : "Feuil1 (3 col.)"
  const sheetSensitiveSummary = excelSheets
    .filter((s) => (s.sensitive_columns ?? []).length > 0)
    .map((s) => `${s.name} (${s.sensitive_columns.length} col.)`);

  useEffect(() => {
    const saved = localStorage.getItem(PROTECTION_STORAGE_KEY);
    if (saved && protectionOptions.some((o) => o.value === saved)) setProtectionMode(saved);
  }, []);

  useEffect(() => {
    localStorage.setItem(PROTECTION_STORAGE_KEY, protectionMode);
  }, [protectionMode]);

  useEffect(() => {
    setExcelExclusions(createEmptyExclusions());
    setShowGranularControls(false);
  }, [preview?.filename, preview?.file_type]);

  useEffect(() => {
    if (phase !== "processing" || !jobId) return undefined;

    let cancelled = false;
    const pollStatus = async () => {
      try {
        const res = await fetch(`${API_URL}/api/upload/status/${jobId}`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail ?? `HTTP ${res.status}`);
        if (cancelled) return;

        if (data.status === "done" && data.result) {
          setProgress(100);
          setSummary(data.result);
          setJobId(null);
          setProcessingMessage(null);
          setPhase("done");
          onResult(data.result);
          return;
        }

        if (data.status === "error") {
          setJobId(null);
          setProcessingMessage(null);
          setError(data.error ?? "Le traitement du fichier a echoue.");
          setPhase("preview_ok");
          return;
        }

        setProcessingMessage(
          data.message
            ?? (data.status === "processing"
              ? "Traitement du fichier en cours..."
              : "Fichier en attente de traitement...")
        );
        setProgress((current) => Math.min(current < 15 ? 15 : current + 10, 90));
      } catch (e) {
        if (cancelled) return;
        setJobId(null);
        setProcessingMessage(null);
        setError(e.message);
        setPhase("preview_ok");
      }
    };

    pollStatus();
    const intervalId = window.setInterval(pollStatus, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [jobId, onResult, phase, token]);

  function validate(file) {
    const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
    if (!ACCEPTED_EXTS.includes(ext)) return t("fileUpload.unsupported_type", { ext });
    if (file.size > MAX_UPLOAD_BYTES) return t("fileUpload.file_too_large", { size: formatSize(file.size) });
    if (!file.size) return t("fileUpload.empty_file");
    return null;
  }

  function shouldSkipPreview(file) {
    const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
    return DIRECT_UPLOAD_EXTS.has(ext) || file.size > 1024 * 1024;
  }

  function startUpload(file, modeOverride, failurePhase = "preview_ok") {
    const activeMode = modeOverride !== undefined ? modeOverride : protectionMode;
    setPendingFile(file);
    setPhase("uploading");
    setProgress(0);
    setJobId(null);
    setProcessingMessage(null);

    const formData = new FormData();
    formData.append("file", file);
    formData.append("protection_mode", activeMode);
    if (isExcelPreview) {
      formData.append("excluded_sheets", JSON.stringify(excelExclusions.excludedSheets));
      formData.append("excluded_columns", JSON.stringify(excelExclusions.excludedColumns));
      formData.append("excluded_ranges", JSON.stringify(excelExclusions.excludedRanges));
    }

    const xhr = new XMLHttpRequest();
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) setProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const data = JSON.parse(xhr.responseText);
          if (data.job_id && data.status === "processing") {
            setJobId(data.job_id);
            setProcessingMessage(data.message ?? "Traitement du fichier en cours...");
            setProgress((current) => Math.max(current, 15));
            setPhase("processing");
            return;
          }
          setSummary(data);
          setPhase("done");
          onResult(data);
        } catch {
          setError(t("fileUpload.invalid_response"));
          setPhase(failurePhase);
        }
      } else {
        try { setError(JSON.parse(xhr.responseText).detail ?? `HTTP ${xhr.status}`); }
        catch { setError(`HTTP ${xhr.status}`); }
        setPhase(failurePhase);
      }
    };
    xhr.onerror = () => { setError(t("fileUpload.network_error")); setPhase(failurePhase); };
    xhr.open("POST", `${API_URL}/api/upload`);
    xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.send(formData);
  }

  async function runPreview(file) {
    const err = validate(file);
    if (err) { setError(err); return; }
    if (shouldSkipPreview(file)) {
      setError(null);
      setPreview(null);
      setSummary(null);
      setShowDetails(false);
      setShowGranularControls(false);
      setExcelExclusions(createEmptyExclusions());
      startUpload(file, undefined, "idle");
      return;
    }
    setError(null);
    setPhase("previewing");
    setPendingFile(file);
    setSummary(null);
    setShowDetails(false);
    const formData = new FormData();
    formData.append("file", file);
    formData.append("protection_mode", protectionMode);
    try {
      const res = await fetch(`${API_URL}/api/upload/preview`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: formData,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? `HTTP ${res.status}`);
      setPreview(data);
      setPhase("preview_ok");
    } catch (e) {
      setError(e.message);
      setPhase("idle");
    }
  }

  function toggleExcludedSheet(name) {
    setExcelExclusions((cur) => ({
      ...cur,
      excludedSheets: cur.excludedSheets.includes(name)
        ? cur.excludedSheets.filter((x) => x !== name)
        : [...cur.excludedSheets, name],
    }));
  }

  function toggleExcludedColumn(id) {
    setExcelExclusions((cur) => ({
      ...cur,
      excludedColumns: cur.excludedColumns.includes(id)
        ? cur.excludedColumns.filter((x) => x !== id)
        : [...cur.excludedColumns, id],
    }));
  }

  function includeAllExcel() { setExcelExclusions(createEmptyExclusions()); }
  function excludeAllExcel() {
    setExcelExclusions({ excludedSheets: allExcelSheetNames, excludedColumns: allExcelColumnIds, excludedRanges: [] });
  }

  // modeOverride : permet aux boutons du panel de decision de declencher directement
  // l'upload avec un mode specifique sans passer par le setter React (mise a jour async).
  function confirmUpload(modeOverride) {
    if (!pendingFile) return;
    startUpload(pendingFile, modeOverride, "preview_ok");
  }

  function reset() {
    setPhase("idle");
    setPreview(null);
    setPendingFile(null);
    setProgress(0);
    setJobId(null);
    setProcessingMessage(null);
    setSummary(null);
    setError(null);
    setDownloading(false);
    setShowDetails(false);
    setShowGranularControls(false);
    setExcelExclusions(createEmptyExclusions());
  }

  async function downloadReidentified(sessionId, projectId) {
    setDownloading(true);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/upload/excel-export`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, project_id: projectId ?? null }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail ?? `HTTP ${res.status}`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `export_${sessionId.slice(0, 8)}.xlsx`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(e.message);
    } finally {
      setDownloading(false);
    }
  }

  const onDragOver = useCallback((e) => { e.preventDefault(); setIsDragging(true); }, []);
  const onDragLeave = useCallback((e) => {
    if (!e.currentTarget.contains(e.relatedTarget)) setIsDragging(false);
  }, []);
  const onDrop = useCallback((e) => {
    e.preventDefault();
    setIsDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (file) runPreview(file);
  }, [protectionMode, token]);
  const onInput = (e) => { const file = e.target.files?.[0]; if (file) runPreview(file); e.target.value = ""; };

  // Selecteur de mode de protection (reutilise dans plusieurs phases)
  const modeSelector = (
    <div className="mb-4 rounded-xl border border-proxy-card/60 bg-proxy-bg/40 px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-[11px] uppercase tracking-wider text-proxy-text/30">{t("fileUpload.mode_title")}</p>
          <p className="text-xs text-proxy-text/45">{t("fileUpload.mode_help")}</p>
        </div>
        <select
          value={protectionMode}
          onChange={(e) => setProtectionMode(e.target.value)}
          title={protectionOptions.find((o) => o.value === protectionMode)?.tooltip}
          className="rounded-lg border border-proxy-card bg-proxy-surface px-3 py-2 text-xs text-proxy-text outline-none"
        >
          {protectionOptions.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </div>
      <div className="mt-2 flex flex-wrap gap-2">
        {protectionOptions.map((o) => (
          <button
            key={o.value}
            type="button"
            title={o.tooltip}
            onClick={() => setProtectionMode(o.value)}
            className={[
              "inline-flex items-center gap-2 rounded-full border px-3 py-1 text-[11px] transition-all",
              protectionMode === o.value
                ? "border-proxy-accent/40 bg-proxy-accent/10 text-proxy-text"
                : "border-proxy-card/60 bg-proxy-bg/30 text-proxy-text/45 hover:text-proxy-text/70",
            ].join(" ")}
          >
            <span className={`h-2 w-2 rounded-full ${o.dot}`} />
            <span className={o.tone}>{o.label}</span>
          </button>
        ))}
      </div>
    </div>
  );

  // Controles granulaires inline (Step 3) — JSX inlined pour eviter le re-mount
  const granularControlsJsx = (
    <div className="space-y-3">
      <div className="flex items-center gap-3 mb-2">
        <button
          type="button"
          onClick={() => setShowGranularControls(false)}
          className="text-xs text-proxy-text/40 hover:text-proxy-text/70 transition-colors"
        >
          &lt;- {t("common.back")}
        </button>
        <p className="text-sm font-medium text-proxy-text">{preview?.filename}</p>
      </div>
      <div className="rounded-xl bg-proxy-bg/60 border border-proxy-card/50 px-4 py-3 space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-[11px] uppercase tracking-wider text-proxy-text/30">{t("fileUpload.granular_controls")}</p>
            <p className="text-xs text-proxy-text/45">{t("fileUpload.granular_help")}</p>
          </div>
          <div className="flex gap-2">
            <button type="button" onClick={includeAllExcel} className="rounded-lg border border-proxy-card px-3 py-1.5 text-[11px] text-proxy-text/70 hover:text-proxy-text">
              {t("fileUpload.include_all")}
            </button>
            <button type="button" onClick={excludeAllExcel} className="rounded-lg border border-amber-700/30 bg-amber-950/20 px-3 py-1.5 text-[11px] text-amber-200">
              {t("fileUpload.exclude_all")}
            </button>
          </div>
        </div>
        <div className="space-y-3">
          {excelSheets.map((sheet) => {
            const sheetExcluded = excelExclusions.excludedSheets.includes(sheet.name);
            return (
              <div key={sheet.name} className="rounded-lg border border-proxy-card/50 bg-proxy-surface/30 px-3 py-3">
                <label className="flex items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium text-proxy-text">{sheet.name}</p>
                    <p className="text-xs text-proxy-text/45">{t("fileUpload.sensitive_columns_count", { count: (sheet.sensitive_columns ?? []).length })}</p>
                  </div>
                  <span className="inline-flex items-center gap-2 text-xs text-proxy-text/70">
                    <input type="checkbox" checked={sheetExcluded} onChange={() => toggleExcludedSheet(sheet.name)} />
                    {t("fileUpload.exclude")}
                  </span>
                </label>
                <div className={`mt-3 grid gap-2 ${sheetExcluded ? "opacity-40" : ""}`}>
                  {(sheet.sensitive_columns ?? []).length > 0 ? sheet.sensitive_columns.map((col) => (
                    <label key={col.id} className="flex items-center justify-between gap-3 rounded-md border border-proxy-card/40 px-3 py-2 text-xs text-proxy-text/75">
                      <span>{col.letter}{col.label ? ` · ${col.label}` : ""}</span>
                      <span className="inline-flex items-center gap-2">
                        <input type="checkbox" checked={excelExclusions.excludedColumns.includes(col.id)} disabled={sheetExcluded} onChange={() => toggleExcludedColumn(col.id)} />
                        {t("fileUpload.exclude")}
                      </span>
                    </label>
                  )) : (
                    <p className="text-xs text-proxy-text/35">{t("fileUpload.no_sensitive_columns")}</p>
                  )}
                </div>
              </div>
            );
          })}
        </div>
        {/* Mini selecteur de mode + bouton Appliquer */}
        <div className="flex items-center justify-between gap-3 pt-1 border-t border-proxy-card/30">
          <div className="flex gap-1.5">
            {protectionOptions.map((o) => (
              <button
                key={o.value}
                type="button"
                title={o.tooltip}
                onClick={() => setProtectionMode(o.value)}
                className={[
                  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] transition-all",
                  protectionMode === o.value
                    ? "border-proxy-accent/40 bg-proxy-accent/10 text-proxy-text"
                    : "border-proxy-card/60 bg-proxy-bg/30 text-proxy-text/40 hover:text-proxy-text/70",
                ].join(" ")}
              >
                <span className={`h-1.5 w-1.5 rounded-full ${o.dot}`} />
                {o.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={() => confirmUpload()}
            className="px-4 py-2 rounded-xl text-xs font-medium bg-proxy-accent/20 border border-proxy-accent/40 text-proxy-accent hover:bg-proxy-accent/30 transition-all"
          >
            {t("fileUpload.apply_and_send")}
          </button>
        </div>
      </div>
    </div>
  );

  return (
    <div className="px-4 pb-3 max-w-3xl mx-auto w-full animate-slide-up">
      <div
        onDragOver={phase === "idle" ? onDragOver : undefined}
        onDragLeave={phase === "idle" ? onDragLeave : undefined}
        onDrop={phase === "idle" ? onDrop : undefined}
        className={[
          "relative rounded-2xl border-2 border-dashed p-5 transition-all duration-200",
          isDragging ? "border-proxy-accent bg-proxy-accent/10" : "border-proxy-card bg-proxy-surface/50",
        ].join(" ")}
      >
        <button
          onClick={onClose}
          className="absolute top-3 right-3 w-6 h-6 flex items-center justify-center rounded-full text-proxy-text/30 hover:text-proxy-text/70 hover:bg-proxy-card transition-all text-sm"
        >
          x
        </button>

        {/* Selecteur de mode — masque pour le panel de decision Excel (mode choisi via boutons) */}
        {(phase === "idle" || phase === "previewing" || (phase === "preview_ok" && !isExcelPreview)) && modeSelector}

        {/* ETAPE 1 — Analyse en cours */}
        {phase === "previewing" && (
          <div className="flex flex-col items-center gap-3 py-2">
            <div className="w-8 h-8 rounded-full border-2 border-proxy-accent border-t-transparent animate-spin" />
            <p className="text-sm text-proxy-text/50">Analyse en cours...</p>
          </div>
        )}

        {/* ETAPE 2A/2B — Panel de decision Excel */}
        {phase === "preview_ok" && isExcelPreview && !showGranularControls && (
          <div className="space-y-4">
            {hasSensitiveExcel ? (
              /* 2A — Donnees sensibles detectees */
              <div className="rounded-xl border border-amber-700/30 bg-amber-950/20 px-4 py-4 space-y-3">
                <div className="flex items-center gap-2">
                  <span className="text-xl" role="img" aria-label="loupe">&#128269;</span>
                  <p className="text-sm font-semibold text-amber-100">{t("fileUpload.sensitive_data_detected")}</p>
                </div>
                {sensitiveColLabels.length > 0 && (
                  <div>
                    <p className="text-[11px] uppercase tracking-wider text-amber-300/60 mb-1">{t("fileUpload.columns")}</p>
                    <div className="flex flex-wrap gap-1.5">
                      {sensitiveColLabels.map((label) => (
                        <span key={label} className="px-2 py-0.5 rounded-full bg-amber-900/40 border border-amber-700/30 text-[10px] text-amber-200">
                          {label}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                {sheetSensitiveSummary.length > 0 && (
                  <div>
                    <p className="text-[11px] uppercase tracking-wider text-amber-300/60 mb-1">{t("fileUpload.sheets")}</p>
                    <p className="text-xs text-amber-100/70">{sheetSensitiveSummary.join(", ")}</p>
                  </div>
                )}
                <div className="rounded-lg border border-emerald-700/30 bg-emerald-950/20 px-3 py-2.5">
                  <p className="text-[11px] uppercase tracking-wider text-emerald-300/70 mb-0.5">{t("fileUpload.recommended_mode")}</p>
                  <p className="text-xs text-emerald-100/70">{t("fileUpload.recommended_help")}</p>
                </div>
                <div className="flex flex-wrap gap-2 pt-1">
                  <button
                    type="button"
                    onClick={() => confirmUpload("privacy")}
                    className="flex-1 min-w-[200px] px-4 py-2.5 rounded-xl text-xs font-medium bg-emerald-900/60 border border-emerald-700/40 text-emerald-300 hover:bg-emerald-800/60 transition-all"
                  >
                    {t("fileUpload.continue_privacy")}
                  </button>
                  <button
                    type="button"
                    onClick={() => confirmUpload("fast")}
                    className="px-4 py-2.5 rounded-xl text-xs border border-proxy-card text-proxy-text/60 hover:text-proxy-text transition-all"
                  >
                    Mode Rapide
                  </button>
                  <button
                    type="button"
                    onClick={() => setShowGranularControls(true)}
                    className="px-4 py-2.5 rounded-xl text-xs border border-proxy-accent/30 text-proxy-accent/70 hover:text-proxy-accent transition-all"
                  >
                    Personnaliser
                  </button>
                  <button
                    type="button"
                    onClick={reset}
                    className="px-4 py-2.5 rounded-xl text-xs border border-proxy-card text-proxy-text/35 hover:text-proxy-text/60 transition-all"
                  >
                    {t("common.cancel")}
                  </button>
                </div>
              </div>
            ) : (
              /* 2B — Aucune donnee sensible detectee */
              <div className="rounded-xl border border-emerald-700/30 bg-emerald-950/20 px-4 py-4 space-y-3">
                <div className="flex items-center gap-2">
                  <span className="text-xl" role="img" aria-label="ok">&#9989;</span>
                  <p className="text-sm font-semibold text-emerald-100">{t("fileUpload.no_sensitive_data")}</p>
                </div>
                <div className="space-y-2">
                  <div className="rounded-lg border border-proxy-card/40 bg-proxy-bg/30 px-3 py-2">
                    <p className="text-xs font-medium text-sky-300 mb-0.5">Mode Rapide</p>
                    <p className="text-[11px] text-proxy-text/50">{t("fileUpload.fast_mode_help")}</p>
                  </div>
                  <div className="rounded-lg border border-proxy-card/40 bg-proxy-bg/30 px-3 py-2">
                    <p className="text-xs font-medium text-emerald-300 mb-0.5">{t("fileUpload.privacy_mode")}</p>
                    <p className="text-[11px] text-proxy-text/50">{t("fileUpload.privacy_mode_help")}</p>
                  </div>
                </div>
                <div className="flex flex-wrap gap-2 pt-1">
                  <button
                    type="button"
                    onClick={() => confirmUpload("fast")}
                    className="px-4 py-2.5 rounded-xl text-xs font-medium bg-sky-900/40 border border-sky-700/30 text-sky-300 hover:bg-sky-800/40 transition-all"
                  >
                    Mode Rapide
                  </button>
                  <button
                    type="button"
                    onClick={() => confirmUpload("privacy")}
                    className="px-4 py-2.5 rounded-xl text-xs font-medium bg-emerald-900/40 border border-emerald-700/30 text-emerald-300 hover:bg-emerald-800/40 transition-all"
                  >
                    {t("fileUpload.privacy_mode")}
                  </button>
                  <button
                    type="button"
                    onClick={() => setShowGranularControls(true)}
                    className="px-4 py-2.5 rounded-xl text-xs border border-proxy-accent/30 text-proxy-accent/70 hover:text-proxy-accent transition-all"
                  >
                    Personnaliser
                  </button>
                  <button
                    type="button"
                    onClick={reset}
                    className="px-4 py-2.5 rounded-xl text-xs border border-proxy-card text-proxy-text/35 hover:text-proxy-text/60 transition-all"
                  >
                    {t("common.cancel")}
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        {/* ETAPE 3 — Controles granulaires */}
        {phase === "preview_ok" && isExcelPreview && showGranularControls && granularControlsJsx}

        {/* preview_ok — flux non-Excel */}
        {phase === "preview_ok" && !isExcelPreview && preview && (
          <div className="space-y-4">
            <div className="flex items-start gap-3">
              <span className="text-amber-400 text-lg">!</span>
              <div>
                <p className="text-sm font-medium text-proxy-text">{preview.filename}</p>
                <p className="text-xs text-proxy-text/40">
                  {preview.file_type?.toUpperCase()} · {preview.preview_chars?.toLocaleString("fr-FR")} cars
                </p>
              </div>
            </div>
            {OCR_IMAGE_EXTS.includes(preview.file_type) && !preview.ocr_available && (
              <div className="rounded-xl border border-amber-700/30 bg-amber-950/20 px-4 py-3">
                <p className="text-sm font-medium text-amber-100">{t("fileUpload.ocr_unavailable_title")}</p>
                <p className="mt-1 text-xs text-amber-100/70">{t("fileUpload.ocr_unavailable_help")}</p>
              </div>
            )}
            {preview.ocr_used && (
              <div className="inline-flex items-center gap-2 rounded-full border border-sky-700/30 bg-sky-950/20 px-3 py-1.5 text-xs text-sky-300">
                <span role="img" aria-label="ocr">&#128269;</span>
                <span>{t("fileUpload.ocr_used_badge")}</span>
              </div>
            )}
            <div className="rounded-xl bg-proxy-bg/60 border border-proxy-card/50 px-4 py-3">
              <p className="text-[11px] uppercase tracking-wider text-proxy-text/30 mb-2">{t("fileUpload.detected_entities")}</p>
              {preview.total_entities > 0 ? (
                <>
                  <p className="text-sm font-bold text-proxy-accent mb-1">
                    {preview.total_entities} entite{preview.total_entities > 1 ? "s" : ""}
                  </p>
                  <p className="text-xs text-proxy-text/50">{buildSummary(preview.breakdown, t)}</p>
                  <div className="mt-2.5 flex flex-wrap gap-1.5">
                    {Object.entries(preview.breakdown).map(([label, count]) => (
                      <span key={label} className="px-2 py-0.5 rounded-full bg-proxy-accent/15 border border-proxy-accent/25 text-[10px] text-proxy-accent font-medium">
                        {count} {readableEntity(label, t)}
                      </span>
                    ))}
                  </div>
                </>
              ) : (
                <p className="text-sm text-proxy-text/30">{t("fileUpload.no_sensitive_entity")}</p>
              )}
            </div>
            {preview.explanation?.length > 0 && (
              <div className="rounded-xl bg-proxy-bg/60 border border-proxy-card/50 px-4 py-3">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <p className="text-[11px] uppercase tracking-wider text-proxy-text/30">{t("fileUpload.masking_explanations")}</p>
                    <p className="text-xs text-proxy-text/45">Resume des decisions prises avant pseudonymisation.</p>
                  </div>
                  <button type="button" onClick={() => setShowDetails(true)} className="rounded-lg border border-proxy-card px-3 py-1.5 text-[11px] text-proxy-text/70 hover:text-proxy-text transition-colors">
                    {t("fileUpload.view_details")}
                  </button>
                </div>
                <div className="mt-3 space-y-2">
                  {preview.explanation.slice(0, 4).map((item, idx) => (
                    <div key={`${item.placeholder}-${idx}`} className="rounded-lg border border-proxy-card/50 bg-proxy-surface/40 px-3 py-2">
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-xs font-medium text-proxy-text">{readableEntity(item.entity_type, t)}</span>
                        <code className="text-[10px] text-proxy-accent">{item.placeholder}</code>
                      </div>
                      <p className="mt-1 text-[11px] text-proxy-text/45">{item.reason}</p>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {preview.business_findings?.length > 0 && (
              <div className="rounded-xl border border-amber-700/30 bg-amber-950/20 px-4 py-3">
                <p className="text-[11px] uppercase tracking-wider text-amber-300/70 mb-2">{t("fileUpload.business_sensitive")}</p>
                <div className="space-y-2">
                  {preview.business_findings.map((finding, idx) => (
                    <div key={`${finding.category}-${idx}`} className="rounded-lg border border-amber-700/20 bg-black/10 px-3 py-2">
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-xs font-medium text-amber-100">{readableBusinessEntity(finding.category, t)}</span>
                        <span className="text-[10px] text-amber-300/70">{finding.category}</span>
                      </div>
                      <p className="mt-1 text-[11px] text-amber-100/70">{finding.recommendation}</p>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {preview.strict_warning && (
              <div className="rounded-xl border border-rose-700/30 bg-rose-950/20 px-4 py-3">
                <p className="text-sm font-medium text-rose-200">Des donnees business-sensibles ont ete detectees hors mode Strict.</p>
                <p className="mt-1 text-xs text-rose-100/70">Passez en mode Strict pour renforcer le controle avant envoi.</p>
              </div>
            )}
            <div className="flex gap-2 justify-end">
              <button onClick={reset} className="px-4 py-2 rounded-xl text-xs border border-proxy-card text-proxy-text/40 hover:text-proxy-text/70 transition-all">
                {t("common.cancel")}
              </button>
              <button onClick={() => confirmUpload()} className="px-4 py-2 rounded-xl text-xs font-medium bg-proxy-accent/20 border border-proxy-accent/40 text-proxy-accent hover:bg-proxy-accent/30 transition-all">
                Confirmer et envoyer
              </button>
            </div>
          </div>
        )}

        {/* ETAPE 1 upload — barre de progression */}
        {phase === "uploading" && (
          <div className="space-y-3">
            <p className="text-sm text-proxy-text/50 text-center">{t("fileUpload.uploading", { progress })}</p>
            <div className="h-2 rounded-full bg-proxy-card overflow-hidden">
              <div className="h-full bg-proxy-accent rounded-full transition-all duration-150" style={{ width: `${progress}%` }} />
            </div>
          </div>
        )}

        {phase === "processing" && (
          <div className="space-y-3">
            <p className="text-sm text-proxy-text/50 text-center">
              {processingMessage ?? "Traitement du fichier en cours..."}
            </p>
            <div className="h-2 rounded-full bg-proxy-card overflow-hidden">
              <div className="h-full bg-proxy-accent rounded-full transition-all duration-300" style={{ width: `${progress}%` }} />
            </div>
            <p className="text-center text-xs text-proxy-text/35">
              Vérification du statut toutes les 2 secondes...
            </p>
          </div>
        )}

        {/* ETAPE 4 — Resultat + bouton telechargement Excel reidentifie */}
        {phase === "done" && summary && (
          <div className="space-y-3">
            <div className="flex items-center gap-3">
              <span className="text-emerald-400 text-lg">ok</span>
              <div>
                <p className="text-sm font-medium text-proxy-text">{summary.filename}</p>
                <p className="text-xs text-proxy-text/40">
                  {summary.chunks_count} chunks · {summary.total_entities} entites masquees
                </p>
              </div>
            </div>
            {summary.ocr_used && (
              <div className="inline-flex items-center gap-2 rounded-full border border-sky-700/30 bg-sky-950/20 px-3 py-1.5 text-xs text-sky-300">
                <span role="img" aria-label="ocr">&#128269;</span>
                <span>{t("fileUpload.ocr_used_badge")}</span>
              </div>
            )}
            {summary.excel_report && (
              <div className="rounded-xl border border-proxy-card/50 bg-proxy-bg/40 px-4 py-3 text-xs text-proxy-text/60 space-y-1">
                <p>Feuilles traitees : {(summary.excel_report.sheets_processed ?? []).length}</p>
                <p>Feuilles exclues : {(summary.excel_report.sheets_excluded ?? []).length}</p>
                <p>Colonnes masquees : {(summary.excel_report.columns_redacted ?? []).join(", ") || "—"}</p>
                <p>Cellules masquees : {summary.excel_report.cells_redacted ?? 0}</p>
                {summary.excel_report.entities_by_type && Object.keys(summary.excel_report.entities_by_type).length > 0 && (
                  <div className="flex flex-wrap gap-1.5 pt-1">
                    {Object.entries(summary.excel_report.entities_by_type).map(([key, count]) => (
                      <span key={key} className="px-2 py-0.5 rounded-full bg-proxy-accent/10 border border-proxy-accent/20 text-[10px] text-proxy-accent">
                        {count} {key}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}
            {summary.is_excel && (
              <button
                onClick={() => downloadReidentified(summary.session_id, summary.project_id)}
                disabled={downloading}
                className="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl text-xs font-medium bg-emerald-950/60 border border-emerald-700/40 text-emerald-400 hover:bg-emerald-900/60 disabled:opacity-40 transition-all"
              >
                {downloading
                  ? <span className="w-3 h-3 rounded-full border border-emerald-400 border-t-transparent animate-spin" />
                  : <span>&#8595;</span>}
                {downloading ? t("export.exporting_excel") : t("export.download_excel")}
              </button>
            )}
            <div className="flex gap-2">
              <button onClick={reset} className="text-xs text-proxy-text/40 hover:text-proxy-text/70 transition-colors">
                {t("fileUpload.new_file")}
              </button>
              <span className="text-proxy-card">·</span>
              <button onClick={onClose} className="text-xs text-proxy-accent hover:opacity-80 transition-opacity">
                {t("common.close")}
              </button>
            </div>
          </div>
        )}

        {/* IDLE — zone de depot */}
        {phase === "idle" && (
          <div className="flex flex-col items-center gap-3 py-2">
            <div className={[
              "w-12 h-12 rounded-xl flex items-center justify-center border-2 transition-colors",
              isDragging ? "border-proxy-accent bg-proxy-accent/20 text-proxy-accent" : "border-proxy-card bg-proxy-bg text-proxy-text/30",
            ].join(" ")}>
              <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" />
              </svg>
            </div>
            <div className="text-center">
              <p className="text-sm text-proxy-text/60">
                {isDragging ? t("fileUpload.release_to_analyze") : t("fileUpload.drag_drop")}
              </p>
              <p className="text-xs text-proxy-text/25 mt-0.5">
                {t("fileUpload.supported_formats_help")}
              </p>
            </div>
            <div className="w-full max-w-xl rounded-xl border border-proxy-card/50 bg-proxy-bg/40 px-4 py-3 text-left text-xs text-proxy-text/45">
              <div className="grid gap-1.5 sm:grid-cols-2">
                <p>📄 {t("fileUpload.category_documents")}</p>
                <p>🖼️ {t("fileUpload.category_images")}</p>
                <p>📧 {t("fileUpload.category_emails")}</p>
                <p>🎙️ {t("fileUpload.category_transcripts")}</p>
                <p className="sm:col-span-2">📊 {t("fileUpload.category_data")}</p>
              </div>
            </div>
            <button type="button" onClick={() => inputRef.current?.click()} className="px-4 py-2 rounded-xl text-xs font-medium bg-proxy-card hover:bg-proxy-panel border border-proxy-surface hover:border-proxy-card text-proxy-text/60 transition-all">
              {t("common.browse")}
            </button>
            <input ref={inputRef} type="file" accept={FILE_ACCEPT} className="hidden" onChange={onInput} />
          </div>
        )}

        {error && (
          <div className="mt-3 flex items-start gap-2 bg-red-950/60 border border-red-800/50 rounded-xl px-3 py-2.5 text-xs text-red-300">
            <span className="shrink-0">!</span>
            <span>{error}</span>
          </div>
        )}
      </div>

      {/* Modal details masquage (flux non-Excel uniquement) */}
      {showDetails && preview && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4">
          <div className="max-h-[80vh] w-full max-w-2xl overflow-hidden rounded-2xl border border-proxy-card bg-proxy-bg shadow-2xl">
            <div className="flex items-center justify-between border-b border-proxy-card px-5 py-4">
              <div>
                <p className="text-sm font-medium text-proxy-text">{t("fileUpload.masking_details_title")}</p>
                <p className="text-xs text-proxy-text/45">{preview.filename}</p>
              </div>
              <button type="button" onClick={() => setShowDetails(false)} className="rounded-full border border-proxy-card px-3 py-1 text-xs text-proxy-text/55 hover:text-proxy-text">
                {t("common.close")}
              </button>
            </div>
            <div className="max-h-[60vh] space-y-3 overflow-y-auto px-5 py-4">
              {preview.explanation?.length ? preview.explanation.map((item, idx) => (
                <div key={`${item.placeholder}-${idx}`} className="rounded-xl border border-proxy-card/50 bg-proxy-surface/40 px-4 py-3">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <span className="text-sm font-medium text-proxy-text">{readableEntity(item.entity_type, t)}</span>
                    <code className="text-[11px] text-proxy-accent">{item.placeholder}</code>
                  </div>
                  <div className="mt-2 text-xs text-proxy-text/55">
                    <p>Apercu source: {item.original_preview}</p>
                    <p className="mt-1">{item.reason}</p>
                  </div>
                </div>
              )) : (
                <p className="text-sm text-proxy-text/45">{t("fileUpload.no_explanation")}</p>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
