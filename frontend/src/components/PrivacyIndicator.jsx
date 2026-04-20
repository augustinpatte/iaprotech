/**
 * PrivacyIndicator — badge "Données protégées" avec tooltip RGPD.
 *
 * Props:
 *   redacted   : number — total d'entités PII masquées dans la session
 *   sessionId  : string | null — session vault active
 */

export default function PrivacyIndicator({ redacted = 0, sessionId = null }) {
  const active = redacted > 0 || !!sessionId;

  return (
    <div className="relative group">
      {/* Badge */}
      <div
        className={`
          flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-medium
          border cursor-default select-none transition-all duration-200
          ${active
            ? "bg-emerald-900/40 border-emerald-600/60 text-emerald-300"
            : "bg-proxy-card/60 border-proxy-card text-slate-400"
          }
        `}
      >
        {/* Shield icon */}
        <svg
          className={`w-3.5 h-3.5 shrink-0 ${active ? "text-emerald-400" : "text-slate-500"}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round"
            d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944
               a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591
               3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622
               0-1.042-.133-2.052-.382-3.016z"
          />
        </svg>

        {active ? (
          <span>
            <strong>{redacted}</strong>
            {" "}entité{redacted !== 1 ? "s" : ""} masquée{redacted !== 1 ? "s" : ""}
          </span>
        ) : (
          <span>Données protégées</span>
        )}
      </div>

      {/* Tooltip — affiché au hover, positionné à droite */}
      <div
        className="
          absolute right-0 top-full mt-2 z-50
          w-72 p-4 rounded-xl shadow-2xl
          bg-proxy-card border border-proxy-surface
          text-xs text-slate-300 leading-relaxed
          opacity-0 pointer-events-none
          group-hover:opacity-100 group-hover:pointer-events-auto
          transition-opacity duration-150
          animate-fade-in
        "
      >
        {/* Arrow */}
        <div className="absolute -top-1.5 right-4 w-3 h-3 rotate-45 bg-proxy-card border-l border-t border-proxy-surface" />

        <p className="font-semibold text-emerald-400 mb-2 flex items-center gap-1.5">
          <span>🛡️</span> Pseudonymisation RGPD active
        </p>
        <p className="mb-2">
          Avant chaque envoi au modèle IA, vos données personnelles sont
          remplacées par des codes neutres&nbsp;:
        </p>
        <ul className="space-y-1 mb-2 font-mono text-[11px]">
          <li><span className="text-proxy-accent">Jean Dupont</span> → <span className="text-emerald-400">[PERSONNE_1]</span></li>
          <li><span className="text-proxy-accent">jean@acme.fr</span> → <span className="text-emerald-400">[EMAIL_1]</span></li>
          <li><span className="text-proxy-accent">+33 6 12 34 56</span> → <span className="text-emerald-400">[TELEPHONE_1]</span></li>
        </ul>
        <p>
          La ré-identification s'effectue côté serveur après la réponse&nbsp;—
          le fournisseur IA ne voit jamais vos vraies données.
        </p>
        {sessionId && (
          <p className="mt-2 text-slate-500 font-mono text-[10px] truncate">
            Session : {sessionId}
          </p>
        )}
      </div>
    </div>
  );
}
