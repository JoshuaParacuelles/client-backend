#!/usr/bin/env python3
"""Usage: python apply_legal_patch.py --frontend src/App.jsx --backend backend/request.py
Applies every edit in memory first; if ANY expected snippet is not found exactly once,
nothing is written. Backups are saved as *.bak."""
import argparse, shutil, sys

# (old, new, expected_count)  -- specific edits first, replace-all last
FE = [
('import { useState, useEffect, useRef } from "react";',
 'import { useState, useEffect, useRef } from "react";\nimport { LEGAL_CSS, LEGAL_ROUTES, LegalPage, SiteFooter, ConsentCheckbox, useHashRoute } from "./Legal";', 1),

# Checkbox glyph hidden from screen readers
('<span className="check-box">{checked ? "✓" : ""}</span>',
 '<span className="check-box" aria-hidden="true">{checked ? "✓" : ""}</span>', 1),

# Subject fields: accessible names
('function SubjectField({ k, cls, placeholder, sub, values, setValue, errors }) {',
 'function SubjectField({ k, cls, placeholder, sub, heading, values, setValue, errors }) {', 1),
('onChange={(e) => setValue(k, e.target.value)} placeholder={placeholder} />',
 'onChange={(e) => setValue(k, e.target.value)} placeholder={placeholder}\n        aria-label={`${heading} ${sub}`} aria-invalid={!!errors[k]} />', 1),
('const common = { values, setValue, errors };',
 'const common = { values, setValue, errors, heading: block.heading };', 1),

# Requester text fields -> real <label>
('''    <div className="req-field">
      {label}
      <input type="text" className={errors[key] ? "invalid" : ""} value={data[key]}
        onChange={(e) => onChange(key, e.target.value)} {...extra} />
      {errors[key] && <div className="field-error">{errors[key]}</div>}
    </div>''',
'''    <div>
      <label className="req-field">
        {label}
        <input type="text" className={errors[key] ? "invalid" : ""} value={data[key]}
          aria-invalid={!!errors[key]} aria-required={label.endsWith("*")}
          onChange={(e) => onChange(key, e.target.value)} {...extra} />
      </label>
      {errors[key] && <div className="field-error" role="alert">{errors[key]}</div>}
    </div>''', 1),

# Signature upload
('<img src={preview} alt="signature preview" className="sig-preview" />',
 '<img src={preview} alt="Preview of the uploaded signature file" className="sig-preview" />', 1),
('onClick={() => { setError(""); reset(); }} title="Remove">×</button>',
 'onClick={() => { setError(""); reset(); }} title="Remove" aria-label="Remove uploaded signature file">×</button>', 1),
('''      <div className="req-field">
        Signature Over Printed Name
        <input type="text" value={printedName} onChange={(e) => onPrintedNameChange(e.target.value)}
          placeholder="Type the name that appears under your signature" />
      </div>''',
'''      <label className="req-field">
        Signature Over Printed Name
        <input type="text" value={printedName} onChange={(e) => onPrintedNameChange(e.target.value)}
          placeholder="Type the name that appears under your signature" />
      </label>''', 1),

# Groups / unnamed inputs
('<div className="copies-options">', '<div className="copies-options" role="radiogroup" aria-label="Number of copies">', 1),
('<input type="text" inputMode="numeric" className="copies-others-input" value={othersValue}',
 '<input type="text" inputMode="numeric" aria-label="Number of copies (other)" className="copies-others-input" value={othersValue}', 1),
('<div className="purpose-grid">', '<div className="purpose-grid" role="group" aria-label="Purpose of request">', 1),
('<input type="text" value={purposeOther} onChange={(e) => setPurposeOther(e.target.value)} />',
 '<input type="text" aria-label="Specify other purpose" value={purposeOther} onChange={(e) => setPurposeOther(e.target.value)} />', 1),
('<input type="text" value={data.registry_no}', '<input type="text" aria-label="Registry number (office use only)" value={data.registry_no}', 1),
('<input type="date" value={data.date_of_registration}', '<input type="date" aria-label="Date of registration (office use only)" value={data.date_of_registration}', 1),
('<input type="text" value={data.book}', '<input type="text" aria-label="Book (office use only)" value={data.book}', 1),
('<input type="text" value={data.page}', '<input type="text" aria-label="Page (office use only)" value={data.page}', 1),
('<input type="text" value={data.search_by}', '<input type="text" aria-label="Search by (office use only)" value={data.search_by}', 1),
('<input className="sh-value" type="text" readOnly placeholder="Auto-generated" style={{ width: 110 }} />',
 '<input className="sh-value" type="text" readOnly aria-label="Control number (assigned after submission)" placeholder="Auto-generated" style={{ width: 110 }} />', 1),
('<input className="sh-value" type="text" defaultValue={today} readOnly style={{ width: 90 }} />',
 '<input className="sh-value" type="text" defaultValue={today} readOnly aria-label="Date of request" style={{ width: 90 }} />', 1),

# Dialog title + badge
('<div className="header-left"><div className="header-title">{recordWord}</div></div>',
 '<div className="header-left"><h2 id="dialog-title" className="header-title">{recordWord}</h2></div>', 1),
('<div className="header-badge">{recordWord}</div>', '<div className="header-badge" aria-hidden="true">{recordWord}</div>', 1),
('<div className="form-status">{status === "error"', '<div className="form-status" role="status" aria-live="polite">{status === "error"', 1),

# Toasts
('<div className="toast-wrap">', '<div className="toast-wrap" role="status" aria-live="polite">', 1),
('<button className="toast-close" onClick={dismiss}>×</button>',
 '<button className="toast-close" onClick={dismiss} aria-label="Dismiss notification">×</button>', 1),

# Success screen: honest wording about device storage
('function SuccessScreen({ result, type, email, onClose }) {', 'function SuccessScreen({ result, type, email, savedOnDevice, onClose }) {', 1),
("your request. We've also saved it on this device for convenience, but a screenshot or note is safer.",
 'your request.{savedOnDevice ? " As you chose, it is also saved on this device." : " Keep a note or screenshot of it."}', 1),
('<SuccessScreen result={result} type={kind} email={requester.requester_email.trim()} onClose={onClose} />',
 '<SuccessScreen result={result} type={kind} email={requester.requester_email.trim()} savedOnDevice={remember} onClose={onClose} />', 1),

# Consent state, validation, payload, opt-in storage
('  const [reviewing, setReviewing] = useState(false);',
 '  const [reviewing, setReviewing] = useState(false);\n  const [consent, setConsent] = useState(false);\n  const [remember, setRemember] = useState(false);', 1),
('if (copies === "Others" && !(parseInt(copiesOther, 10) > 0)) errs.num_copies = "Enter a number of copies";',
 'if (copies === "Others" && !(parseInt(copiesOther, 10) > 0)) errs.num_copies = "Enter a number of copies";\n    if (!consent) errs.consent = "You must give your consent to submit this request.";', 1),
('signature_printed_name: printedName,', 'signature_printed_name: printedName,\n        consent: "true",', 1),
('if (res.control_no) {', 'if (res.control_no && remember) {', 1),
('''            <IssuancePanel forms={FORM_TYPES[kind]} selected={issuance} onToggle={toggleIssuance} />
          </div>''',
'''            <IssuancePanel forms={FORM_TYPES[kind]} selected={issuance} onToggle={toggleIssuance} />
          </div>
          <ConsentCheckbox checked={consent} onChange={setConsent} error={errors.consent} />''', 1),

# Review screen: optional device-save checkbox
('function ReviewScreen({ recordWord, theme, sections, sigFile, printedName, status, errorMessage, onBack, onConfirm }) {',
 'function ReviewScreen({ recordWord, theme, sections, sigFile, printedName, status, errorMessage, remember, onRememberChange, onBack, onConfirm }) {', 1),
('''          { label: "Signature Over Printed Name", value: printedName },
        ]} />''',
'''          { label: "Signature Over Printed Name", value: printedName },
        ]} />
        <div className="section-heading">Optional</div>
        <div className="review-consent">
          <Checkbox label="Save my control number and email on this device so I can track this request later. I can remove it anytime from the tracking screen."
            checked={remember} onChange={() => onRememberChange(!remember)} />
        </div>''', 1),
('<ReviewScreen recordWord={cfg.word} theme={theme} sections={sections} sigFile={sigFile}',
 '<ReviewScreen recordWord={cfg.word} theme={theme} sections={sections} sigFile={sigFile} remember={remember} onRememberChange={setRemember}', 1),

# Tracking form
('''          <div className="req-field">
            Control Number
            <input type="text" value={controlNo} onChange={(e) => setControlNo(e.target.value)}
              placeholder="BR-20260929-00042" autoCapitalize="characters" />
          </div>
          <div className="req-field">
            Email Address
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="juandelacruz@gmail.com" />
          </div>
          {error && <div className="field-error" style={{ marginTop: 10 }}>{error}</div>}''',
'''          <label className="req-field">
            Control Number
            <input type="text" value={controlNo} onChange={(e) => setControlNo(e.target.value)}
              placeholder="BR-20260929-00042" autoCapitalize="characters" />
          </label>
          <label className="req-field">
            Email Address
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="juandelacruz@gmail.com" />
          </label>
          <p className="track-note">
            We use these only to look up your request. See our{" "}
            <a href="#/privacy" target="_blank" rel="noopener noreferrer">Privacy Policy (opens in a new tab)</a>.
          </p>
          {error && <div className="field-error" role="alert" style={{ marginTop: 10 }}>{error}</div>}''', 1),
('onClick={forgetRecent}>Forget</button>', 'onClick={forgetRecent} aria-label="Forget the request saved on this device">Forget</button>', 1),
('<div className="track-result">', '<div className="track-result" role="status">', 1),

# Modal: dialog semantics, focus trap, focus restore
('  const closeRef = useRef(onClose);', '  const closeRef = useRef(onClose);\n  const overlayRef = useRef(null);', 1),
('const fn = (e) => { if (e.key === "Escape") closeRef.current(); };',
 '''const opener = document.activeElement;
    const sel = 'a[href],button:not([disabled]),input:not([disabled]),select,textarea,[tabindex]:not([tabindex="-1"])';
    const focusables = () => Array.from(overlayRef.current?.querySelectorAll(sel) || [])
      .filter((el) => el.getClientRects().length > 0);
    overlayRef.current?.focus();
    const fn = (e) => {
      if (e.key === "Escape") { closeRef.current(); return; }
      if (e.key !== "Tab") return;
      const items = focusables();
      if (!items.length) { e.preventDefault(); return; }
      const first = items[0], last = items[items.length - 1];
      const inside = overlayRef.current.contains(document.activeElement) && document.activeElement !== overlayRef.current;
      if (e.shiftKey && (!inside || document.activeElement === first)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (!inside || document.activeElement === last)) { e.preventDefault(); first.focus(); }
    };''', 1),
('return () => { document.removeEventListener("keydown", fn); document.body.style.overflow = ""; };',
 'return () => { document.removeEventListener("keydown", fn); document.body.style.overflow = ""; opener?.focus?.(); };', 1),
('<div className="overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>',
 '<div ref={overlayRef} className="overlay" role="dialog" aria-modal="true" aria-labelledby="dialog-title" tabIndex={-1}\n      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>', 1),

# Authorization clause: fix "DAPA" typo, link policy
('/ The DAPA of 2012 (R.A. 10173)',
 'The Data Privacy Act of 2012 (R.A. 10173) applies to the personal information in this request. See our <a href="#/privacy" target="_blank" rel="noopener noreferrer">Privacy Policy (opens in a new tab)</a>.', 1),

# App root: routing, landmarks, headings, footer
('  const [active, setActive] = useState(null);\n  return (',
 '  const [active, setActive] = useState(null);\n  const route = useHashRoute();\n  const legalPage = LEGAL_ROUTES.includes(route) ? route : null;\n  return (', 1),
('<style>{extraStyles}</style>', '<style>{extraStyles}</style>\n      <style>{LEGAL_CSS}</style>', 1),
('      <div className="landing">',
 '      {legalPage ? <LegalPage page={legalPage} /> : (<>\n      <main className="landing">', 1),
('''          <div className="office-name">
            Local Civil Registrar
            <span className="office-loc">San Carlos City, Negros Occidental</span>
          </div>''',
'''          <h1 className="office-name">
            Local Civil Registrar
            <span className="office-loc">San Carlos City, Negros Occidental</span>
          </h1>''', 1),
('<div className="select-prompt">Select record type to request</div>', '<h2 className="select-prompt">Select record type to request</h2>', 1),
('''        {active && <Modal type={active} onClose={() => setActive(null)} />}
      </div>''',
'''        {active && <Modal type={active} onClose={() => setActive(null)} />}
      </main>
      <SiteFooter />
      </>)}''', 1),

# LAST: replace-all so every remaining error message is announced
('<div className="field-error">', '<div className="field-error" role="alert">', None),
]

BE = [
('    missing = [f for f in cfg["required"] + ALWAYS_REQUIRED if not row.get(f)]',
 '''    # Explicit consent must be sent by the form (enforced server-side, not just in the UI).
    if str(data.get("consent", "")).strip().lower() not in ("true", "1", "yes"):
        return jsonify({"error": "Consent to the Privacy Policy is required to submit a request."}), 400

    missing = [f for f in cfg["required"] + ALWAYS_REQUIRED if not row.get(f)]''', 1),
]

def apply(path, edits):
    raw = open(path, encoding="utf-8", newline="").read()
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")
    problems = []
    for old, new, cnt in edits:
        n = text.count(old)
        if (cnt is None and n < 1) or (cnt is not None and n != cnt):
            problems.append(f"expected {cnt or '>=1'} match(es), found {n}: {old[:70]!r}")
            continue
        text = text.replace(old, new)
    return path, text, crlf, problems

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--frontend"); ap.add_argument("--backend")
    a = ap.parse_args()
    jobs = []
    if a.frontend: jobs.append(apply(a.frontend, FE))
    if a.backend: jobs.append(apply(a.backend, BE))
    bad = [(p, m) for p, _, _, pr in jobs for m in pr]
    if bad:
        for p, m in bad: print(f"[{p}] {m}")
        sys.exit("No files were changed (fix the mismatches above, or send me your current file).")
    for p, text, crlf, _ in jobs:
        shutil.copy(p, p + ".bak")
        open(p, "w", encoding="utf-8", newline="").write(text.replace("\n", "\r\n") if crlf else text)
        print("patched", p)