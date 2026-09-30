#!/usr/bin/env python3
"""Adds the form-guidance feature to the request form.

Usage (from the project root):
    python apply_guidance_patch.py --frontend client/src/component/request.jsx

Every edit is applied in memory first. If ANY expected snippet is not found
exactly once, NOTHING is written. A backup is saved as request.jsx.bak.
Only client/src/component/request.jsx is touched (no backend, no Legal.jsx).
"""
import argparse, shutil, sys

# ───────────────────────── new code blocks ─────────────────────────

GUIDANCE_BLOCK = r'''/* ─── FORM GUIDANCE ───────────────────────────────────────────
   Helper text, friendly messages and the progress bar. All wording lives
   here so it can be edited without touching the form logic. */
const REQUESTER_HINTS = {
  requester_name: "Your own full name, as shown on your valid ID.",
  requester_relationship: "How you are related to the person on the record. For example: Self, Mother, Spouse.",
  requester_address: "Where you live now. Include street, barangay and city.",
  requester_telephone: "Optional. We may call you if we need to clarify your request.",
  requester_email: "Use an email you check often. You need it, with your control number, to track your request.",
};

const BLOCK_HELP = {
  "Name of Child": "Write the name as it appears on the birth certificate. First name is required. Middle name and surname help us find the record faster.",
  "Date of Birth": "Year is required. If you don't know the exact day, enter the month and year only.",
  "Name of Deceased": "Write the name as it appears on the death certificate. First name is required. Middle name and surname help us find the record faster.",
  "Date of Death": "Year is required. Add the month and day if you know them.",
  "Name of Husband": "Type the husband's complete name.",
  "Maiden Name of Wife": "Use the wife's name before marriage (her maiden surname).",
  "Date of Marriage": "Optional. Type the full date if you know it, for example January 1, 2020.",
};

function validateRequesterField(key, value) {
  const v = (value || "").trim();
  switch (key) {
    case "requester_name":
      return v ? null : "Please type your full name.";
    case "requester_relationship":
      return v ? null : "Please tell us how you are related to the record owner (for example, Self or Parent).";
    case "requester_address":
      return v ? null : "Please type your address.";
    case "requester_email":
      if (!v) return "Please type your email address.";
      return EMAIL_REGEX.test(v) ? null : "This email doesn't look right. It should look like name@example.com.";
    case "requester_telephone":
      return getPhoneError(value);
    default:
      return null;
  }
}

function validateSubjectField(block, key, value) {
  const v = (value || "").trim();
  if (!v) {
    if (!block.required.includes(key)) return null;
    if (block.type === "name") return "Please type the first name.";
    if (block.type === "date") return "Please type the year (4 digits).";
    return `Please type the ${block.heading.toLowerCase()}.`;
  }
  if (block.type === "date") {
    const part = block.keys.indexOf(key);
    if (part === 2 && !/^\d{4}$/.test(v)) return "Year should be 4 digits, like 1990.";
    if (part === 1 && !(/^\d{1,2}$/.test(v) && +v >= 1 && +v <= 31)) return "Day should be a number from 1 to 31.";
  }
  return null;
}

// Scrolls to a part of the form and puts the cursor in its first field.
function goTo(selector) {
  const el = document.querySelector(selector);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
  const fieldSel = 'input:not([type="file"]):not([readonly]):not([disabled])';
  const field = el.matches(fieldSel) ? el : (el.querySelector(fieldSel) || el.nextElementSibling?.querySelector(fieldSel));
  field?.focus({ preventScroll: true });
}

// After a blocked submit: jump to the first field that needs fixing.
function focusFirstError() {
  const el = document.querySelector('.form-paper [aria-invalid="true"]');
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.focus({ preventScroll: true });
}

function FormGuide({ steps }) {
  const next = steps.find((s) => !s.done);
  return (
    <div className="guide-bar">
      <p className="guide-lead">
        Fill in the form from top to bottom. Fields marked <strong>*</strong> are required.
      </p>
      <ol className="guide-steps" aria-label="Form progress">
        {steps.map((s, i) => (
          <li key={s.label}>
            <button type="button" className={`guide-step${s.done ? " done" : ""}${next === s ? " next" : ""}`}
              aria-current={next === s ? "step" : undefined} onClick={() => goTo(s.target)}>
              <span className="guide-dot" aria-hidden="true">{s.done ? "✓" : i + 1}</span>
              {s.label}
              <span className="sr-only">{s.done ? " (done)" : " (not done yet)"}</span>
            </button>
          </li>
        ))}
      </ol>
      <p className="guide-next" role="status" aria-live="polite">
        {next ? `Next: ${next.hint}` : "All set. Press Review Request at the bottom to check your answers."}
      </p>
    </div>
  );
}

'''

VALIDATE_REQUESTER_NEW = r'''function validateRequester(req) {
  const errs = {};
  ["requester_name", "requester_relationship", "requester_address", "requester_email", "requester_telephone"].forEach((k) => {
    const m = validateRequesterField(k, req[k]);
    if (m) errs[k] = m;
  });
  return errs;
}'''

VALIDATE_REQUESTER_OLD = r'''function validateRequester(req) {
  const errs = {};
  if (!req.requester_name.trim()) errs.requester_name = "Full name is required";
  if (!req.requester_relationship.trim()) errs.requester_relationship = "Relationship is required";
  if (!req.requester_address.trim()) errs.requester_address = "Address is required";
  if (!req.requester_email.trim()) errs.requester_email = "Email is required";
  else if (!EMAIL_REGEX.test(req.requester_email.trim())) errs.requester_email = "Enter a valid email address";
  const phoneErr = getPhoneError(req.requester_telephone);
  if (phoneErr) errs.requester_telephone = phoneErr;
  return errs;
}'''

TEXT_FN_OLD = r'''  const text = (key, label, extra = {}) => (
    <div>
      <label className="req-field">
        {label}
        <input type="text" className={errors[key] ? "invalid" : ""} value={data[key]}
          aria-invalid={!!errors[key]} aria-required={label.endsWith("*")}
          onChange={(e) => onChange(key, e.target.value)} {...extra} />
      </label>
      {errors[key] && <div className="field-error" role="alert">{errors[key]}</div>}
    </div>
  );'''

TEXT_FN_NEW = r'''  const text = (key, label, extra = {}) => (
    <div>
      <label className="req-field">
        {label}
        <input type="text" className={errors[key] ? "invalid" : ""} value={data[key]}
          aria-invalid={!!errors[key]} aria-required={label.endsWith("*")}
          aria-describedby={REQUESTER_HINTS[key] ? `hint-${key}` : undefined}
          onChange={(e) => onChange(key, e.target.value)}
          onBlur={() => onBlurField && onBlurField(key)} {...extra} />
      </label>
      {REQUESTER_HINTS[key] && <div className="field-hint" id={`hint-${key}`}>{REQUESTER_HINTS[key]}</div>}
      {errors[key] && <div className="field-error" role="alert">{errors[key]}</div>}
    </div>
  );'''

SUBJECT_RETURN_NEW = r'''  const help = BLOCK_HELP[block.heading];
  return (
    <>
      <div className="section-heading" id={`guide-${block.keys[0]}`}>{block.heading}</div>
      {body}
      {help && <div className="field-hint field-hint--block">{help}</div>}
    </>
  );'''

STEPS_NEW = r'''const purposeText = buildPurposes(purposes, purposeOther);

  // Progress shown in the guide bar at the top of the form (live, nothing is blocked here).
  const steps = [
    { label: "Copies", done: copies !== "Others" || parseInt(copiesOther, 10) > 0,
      target: "#guide-copies", hint: "Choose how many copies you need." },
    { label: "Record details", done: cfg.blocks.every((b) => b.keys.every((k) => !validateSubjectField(b, k, subject[k]))),
      target: `#guide-${cfg.blocks[0].keys[0]}`, hint: "Fill in the details of the record you are requesting." },
    { label: "Your details", done: Object.keys(validateRequester(requester)).length === 0,
      target: "#guide-requester", hint: "Fill in your own details under Requesting Party." },
    { label: "Consent", done: consent,
      target: ".consent-box", hint: "Tick the consent box near the bottom of the form." },
  ];'''

SET_VALUE_NEW = r'''const dirty = useRef(new Set()); // fields the client has typed in (so we don't scold untouched fields)
  const setFieldError = (k, msg) => setErrors((p) => {
    if (!msg && !p[k]) return p;
    const n = { ...p };
    if (msg) n[k] = msg; else delete n[k];
    return n;
  });
  const setValue = (k, v) => {
    dirty.current.add(k);
    setSubject((p) => ({ ...p, [k]: v }));
    // Once a message is showing, re-check while typing so it disappears as soon as it's fixed.
    if (errors[k]) setFieldError(k, validateSubjectField(cfg.blocks.find((b) => b.keys.includes(k)), k, v));
  };
  const blurSubject = (block, k) => {
    if (dirty.current.has(k) || (subject[k] || "").trim() || errors[k])
      setFieldError(k, validateSubjectField(block, k, subject[k]));
  };
  const blurRequester = (k) => {
    if (dirty.current.has(k) || (requester[k] || "").trim() || errors[k])
      setFieldError(k, validateRequesterField(k, requester[k]));
  };
  useEffect(() => {
    if (errors.num_copies && (copies !== "Others" || parseInt(copiesOther, 10) > 0)) setFieldError("num_copies", null);
    if (errors.consent && consent) setFieldError("consent", null);
  }, [copies, copiesOther, consent]); // eslint-disable-line react-hooks/exhaustive-deps
  // "Edit" links on the review screen: go back to the form and scroll to that part.
  const editSection = (target) => {
    setStatus(null);
    setReviewing(false);
    setTimeout(() => goTo(target), 60);
  };'''

UPDATE_R_NEW = r'''const updateR = (k, v) => {
    dirty.current.add(k);
    setRequester((p) => ({ ...p, [k]: v }));
    if (errors[k]) setFieldError(k, validateRequesterField(k, v));
  };'''

INCOMPLETE_OLD = r'''    setErrors(errs);
    if (Object.keys(errs).length > 0) {
      pushToast({ title: "Incomplete form", message: "Please fill in all required fields.", success: false });
      return;
    }'''

INCOMPLETE_NEW = r'''    setErrors(errs);
    const count = Object.keys(errs).length;
    if (count > 0) {
      pushToast({
        title: "Almost there",
        message: `Please fix ${count} highlighted ${count === 1 ? "field" : "fields"} (marked in red), then press Review Request again.`,
        success: false,
      });
      setTimeout(focusFirstError, 0);
      return;
    }'''

GUIDE_CSS = r'''/* ── Form guidance ── */
.guide-bar{padding:12px 28px 10px;background:#fff;border-bottom:1px solid #e2ecf8;}
.guide-lead{font-size:0.78rem;line-height:1.5;margin-bottom:8px;}
.guide-steps{list-style:none;display:flex;flex-wrap:wrap;gap:6px 8px;margin:0 0 8px;}
.guide-step{
  display:inline-flex;align-items:center;gap:6px;min-height:30px;
  font-family:inherit;font-size:0.72rem;padding:3px 12px 3px 4px;
  border:1px solid #c8d9f0;border-radius:100px;background:#fff;cursor:pointer;
}
.guide-step:hover{background:#f4f8fd;}
.guide-dot{
  width:20px;height:20px;border-radius:50%;border:1.5px solid #9fb8d6;
  display:inline-flex;align-items:center;justify-content:center;font-size:0.62rem;font-weight:600;
}
.guide-step.next{border-color:var(--modal-primary);background:var(--modal-tint-bg);}
.guide-step.next .guide-dot{border-color:var(--modal-primary);}
.guide-step.done .guide-dot{background:var(--modal-primary);border-color:var(--modal-primary);}
.form-paper .guide-step.done .guide-dot{color:#fff;}
.guide-next{font-size:0.74rem;line-height:1.45;}
.form-paper .field-hint{font-size:0.7rem;line-height:1.45;color:#3f5b7d;margin-top:3px;}
.form-paper .field-hint--block{margin:-4px 0 10px;}
.form-paper .purpose-section .field-hint{margin:-4px 0 10px;}
.form-paper .copies-row .field-hint{margin:-3px 0 8px;}
.review-edit{
  order:1;font:inherit;font-size:0.72rem;text-transform:none;letter-spacing:0;
  background:none;border:none;cursor:pointer;text-decoration:underline;padding:2px 4px;
}
.form-paper .review-edit{color:var(--modal-primary);}
[id^="guide-"]{scroll-margin-top:12px;}
.sr-only{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0;}
@media(max-width:640px){.guide-bar{padding:12px 18px 10px;}}'''

# ───────────────────────── edit list ─────────────────────────
# (old, new, expected_count)
FE = [
# Guidance helpers + components (inserted just before the purposes helper)
('// Only checked purposes are sent, and the "Others" text is attached only',
 GUIDANCE_BLOCK + '// Only checked purposes are sent, and the "Others" text is attached only', 1),

# Friendlier requester validation (one shared rule set, used live and on submit)
(VALIDATE_REQUESTER_OLD, VALIDATE_REQUESTER_NEW, 1),
('return "Enter a valid mobile number (09XXXXXXXXX or +63 9XXXXXXXXX)";',
 'return "Mobile number should be 11 digits starting with 09 (like 09171234567), or +63 followed by 10 digits.";', 1),

# Copies: anchor, hint, error state on the "Others" box
('<div className="copies-row">', '<div className="copies-row" id="guide-copies">', 1),
('<div className="copies-row-label">Number of Copies — Please check appropriate box</div>',
 '<div className="copies-row-label">Number of Copies — Please check appropriate box</div>\n      <div className="field-hint">Need more than three? Choose Others and type the number.</div>', 1),
('aria-label="Number of copies (other)"', 'aria-label="Number of copies (other)" aria-invalid={!!error}', 1),

# Purpose hint
('<div className="purpose-header">Purpose — Check Appropriate Box</div>',
 '<div className="purpose-header">Purpose — Check Appropriate Box</div>\n      <div className="field-hint">Tick all that apply. This helps the office prepare the right document.</div>', 1),

# Signature note
('<div className="sig-note">PNG, JPG, WEBP or PDF · max 2 MB</div>',
 '<div className="sig-note">Optional. Upload a clear photo or scan of your signature. PNG, JPG, WEBP or PDF, up to 2 MB.</div>', 1),

# Requester fields: hints, blur validation, placeholders
('function RequesterFields({ data, onChange, errors, sigFile, onSigChange, printedName, onPrintedNameChange }) {',
 'function RequesterFields({ data, onChange, errors, onBlurField, sigFile, onSigChange, printedName, onPrintedNameChange }) {', 1),
(TEXT_FN_OLD, TEXT_FN_NEW, 1),
('{text("requester_address", "Address *")}',
 '{text("requester_address", "Address *", { placeholder: "House no., Street, Barangay, City" })}', 1),
('{text("requester_telephone", "Telephone No.", {',
 '{text("requester_telephone", "Telephone No. (optional)", {', 1),
('type: "tel", inputMode: "tel",', 'type: "tel", inputMode: "tel", placeholder: "09171234567",', 1),

# Subject fields: required star on the small label, blur validation
('function SubjectField({ k, cls, placeholder, sub, heading, values, setValue, errors }) {',
 'function SubjectField({ k, cls, placeholder, sub, heading, required = [], onBlur, values, setValue, errors }) {', 1),
('onChange={(e) => setValue(k, e.target.value)} placeholder={placeholder}',
 'onChange={(e) => setValue(k, e.target.value)} onBlur={() => onBlur && onBlur(k)} placeholder={placeholder}', 1),
('aria-invalid={!!errors[k]} />', 'aria-invalid={!!errors[k]} aria-required={required.includes(k)} />', 1),
('<span className="sub-label">{sub}</span>', '<span className="sub-label">{sub}{required.includes(k) ? " *" : ""}</span>', 1),
('function SubjectBlock({ block, values, setValue, errors }) {',
 'function SubjectBlock({ block, values, setValue, errors, onBlurField }) {', 1),
('const common = { values, setValue, errors, heading: block.heading };',
 'const common = { values, setValue, errors, heading: block.heading, required: block.required,\n    onBlur: onBlurField && ((k) => onBlurField(block, k)) };', 1),
('return (<><div className="section-heading">{block.heading}</div>{body}</>);', SUBJECT_RETURN_NEW, 1),

# Review screen: Edit links + clearer intro
('function ReviewSection({ title, rows }) {', 'function ReviewSection({ title, rows, onEdit }) {', 1),
('<div className="section-heading">{title}</div>',
 '<div className="section-heading">{title}{onEdit && <button type="button" className="review-edit" onClick={onEdit} aria-label={`Edit ${title}`}>Edit</button>}</div>', 1),
('function ReviewScreen({ recordWord, theme, sections, sigFile, printedName, status, errorMessage, remember, onRememberChange, onBack, onConfirm }) {',
 'function ReviewScreen({ recordWord, theme, sections, sigFile, printedName, status, errorMessage, remember, onRememberChange, onEdit, onBack, onConfirm }) {', 1),
('Please review the details below carefully. Once you confirm, this request will be',
 'Almost done! Please check your details below. If something is wrong, choose Edit next to that section. When everything looks right, press Confirm & Submit. Your request will then be', 1),
('{sections.map((sec) => <ReviewSection key={sec.title} title={sec.title} rows={sec.rows} />)}',
 '{sections.map((sec) => <ReviewSection key={sec.title} title={sec.title} rows={sec.rows}\n          onEdit={sec.target ? () => onEdit(sec.target) : undefined} />)}', 1),
('<ReviewSection title="Signature" rows={[',
 '<ReviewSection title="Signature" onEdit={() => onEdit("#guide-requester")} rows={[', 1),

# RequestForm: live progress, handlers
('const purposeText = buildPurposes(purposes, purposeOther);', STEPS_NEW, 1),
('const setValue = (k, v) => setSubject((p) => ({ ...p, [k]: v }));', SET_VALUE_NEW, 1),
('const updateR = (k, v) => setRequester((p) => ({ ...p, [k]: v }));', UPDATE_R_NEW, 1),

# Submit-blocking validation (same rules as the live checks)
('''    const errs = validateRequester(requester);
    cfg.blocks.forEach((b) => b.required.forEach((k) => { if (!subject[k].trim()) errs[k] = "Required"; }));''',
 '''    const errs = validateRequester(requester);
    cfg.blocks.forEach((b) => b.keys.forEach((k) => { const m = validateSubjectField(b, k, subject[k]); if (m) errs[k] = m; }));''', 1),
('errs.num_copies = "Enter a number of copies";',
 'errs.num_copies = "Please type how many copies you need (a number, like 4).";', 1),
('errs.consent = "You must give your consent to submit this request.";',
 'errs.consent = "Please tick the consent box to continue.";', 1),
(INCOMPLETE_OLD, INCOMPLETE_NEW, 1),

# Review sections: where "Edit" should take the client
('{ title: "Request Details", rows: [', '{ title: "Request Details", target: "#guide-copies", rows: [', 1),
('{ title: "Record Details", rows: [', '{ title: "Record Details", target: `#guide-${cfg.blocks[0].keys[0]}`, rows: [', 1),
('{ title: "Requesting Party", rows: [', '{ title: "Requesting Party", target: "#guide-requester", rows: [', 1),
('onBack={() => { setStatus(null); setReviewing(false); }} onConfirm={handleConfirmSubmit} />',
 'onEdit={editSection} onBack={() => { setStatus(null); setReviewing(false); }} onConfirm={handleConfirmSubmit} />', 1),

# Form layout: guide bar, blur handlers, anchors, button label
('<FormSubheader />', '<FormSubheader />\n      <FormGuide steps={steps} />', 1),
('<SubjectBlock key={b.heading} block={b} values={subject} setValue={setValue} errors={errors} />',
 '<SubjectBlock key={b.heading} block={b} values={subject} setValue={setValue} errors={errors} onBlurField={blurSubject} />', 1),
('<div className="req-section">', '<div className="req-section" id="guide-requester">', 1),
('<RequesterFields data={requester} onChange={updateR} errors={errors}',
 '<RequesterFields data={requester} onChange={updateR} errors={errors} onBlurField={blurRequester}', 1),
('onCancel={onClose} onSubmit={handleSubmit} />',
 'onCancel={onClose} onSubmit={handleSubmit} submitLabel="Review Request" />', 1),

# CSS (appended to the existing extraStyles block)
('  .form-paper input[type="tel"]{font-size:16px;}\n}',
 '  .form-paper input[type="tel"]{font-size:16px;}\n}\n\n' + GUIDE_CSS, 1),
]


def apply(path, edits):
    raw = open(path, encoding="utf-8", newline="").read()
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")
    problems = []
    for old, new, cnt in edits:
        n = text.count(old)
        if n != cnt:
            problems.append(f"expected {cnt} match(es), found {n}: {old[:80]!r}")
            continue
        text = text.replace(old, new)
    return path, text, crlf, problems


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--frontend", required=True, help="path to client/src/component/request.jsx")
    a = ap.parse_args()
    path, text, crlf, problems = apply(a.frontend, FE)
    if problems:
        for m in problems:
            print(f"[{path}] {m}")
        sys.exit("No files were changed (fix the mismatches above, or send me your current file).")
    shutil.copy(path, path + ".bak")
    open(path, "w", encoding="utf-8", newline="").write(text.replace("\n", "\r\n") if crlf else text)
    print("patched", path, f"({len(FE)} edits, backup: {path}.bak)")