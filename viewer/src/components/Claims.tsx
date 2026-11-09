import { citationHref, citationLabel, parseCitation } from "../citations";
import type { Claim, RejectedClaim } from "../types";

export function ClaimList({ claims }: { claims: Claim[] }) {
  if (!claims.length) return <p className="muted">None.</p>;
  return (
    <ul className="claims">
      {claims.map((c, i) => (
        <li className="claim" key={i}>
          {c.text}
          {c.origin === "ir" && <span className="from-ir" title="Listed from the IR because the model did not describe it">from IR</span>}
          <div className="chips" aria-label="Sources">
            {c.citations.map((id) => {
              const link = citationHref(id);
              const label = citationLabel(parseCitation(id));
              return link ? (
                <a key={id} className="chip" href={link} title={id}>
                  {label}
                </a>
              ) : (
                <span key={id} className="chip" title={id}>
                  {label}
                </span>
              );
            })}
          </div>
        </li>
      ))}
    </ul>
  );
}

export function Rejected({ items }: { items: RejectedClaim[] }) {
  if (!items.length) return null;
  return (
    <details>
      <summary>{items.length} claim(s) hidden: they failed the citation check</summary>
      <ul className="claims" style={{ marginTop: 8 }}>
        {items.map((r, i) => (
          <li key={i} className="muted">
            [{r.section}] {r.text} <em>({r.reason})</em>
          </li>
        ))}
      </ul>
    </details>
  );
}
