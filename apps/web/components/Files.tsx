"use client";

import { useRef, useState, type ReactNode } from "react";
import { MAX_FILES, MAX_FILE_BYTES, fileHref, fmtBytes } from "@/lib/api";
import type { Attachment } from "@/lib/contracts";
import { cx } from "@/lib/ui";

/* ------------------------------------------------------------------ validation */

/** Split picked files into accepted ones and human-readable rejections (25 MB each, 20 per mission). */
export function acceptFiles(incoming: File[], current: { name: string; size: number }[], alreadyOnMission = 0): { accepted: File[]; errors: string[] } {
  const errors: string[] = [];
  const accepted: File[] = [];
  const room = MAX_FILES - alreadyOnMission - current.length;
  for (const f of incoming) {
    if (f.size > MAX_FILE_BYTES) {
      errors.push(`${f.name} is ${fmtBytes(f.size)} — the limit is 25 MB per file`);
      continue;
    }
    if (current.some((c) => c.name === f.name && c.size === f.size) || accepted.some((c) => c.name === f.name && c.size === f.size)) continue;
    if (accepted.length >= room) {
      errors.push(`At most ${MAX_FILES} files per mission — ${f.name} skipped`);
      continue;
    }
    accepted.push(f);
  }
  return { accepted, errors };
}

/* ------------------------------------------------------------------ icons */

export function FileGlyph({ name, size = 12, color = "currentColor" }: { name: string; size?: number; color?: string }) {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  const sheet = ["xlsx", "xlsm", "xls", "csv"].includes(ext);
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke={color} strokeWidth="1.4" aria-hidden>
      <path d="M4 1.75h5.5L12.5 4.75v9.5H4z" strokeLinejoin="round" />
      <path d="M9.5 1.75v3h3" strokeLinejoin="round" />
      {sheet ? <path d="M6 8h5M6 10.5h5M8.5 7v5" strokeLinecap="round" /> : <path d="M6 8h4.5M6 10.5h3" strokeLinecap="round" />}
    </svg>
  );
}

export function PaperclipIcon({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
      <path d="M13 7.5 8 12.5a3.2 3.2 0 0 1-4.5-4.5L9 2.5a2.1 2.1 0 0 1 3 3L6.6 11a1 1 0 0 1-1.5-1.5L10 4.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function DownloadIcon({ size = 11 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
      <path d="M8 2.5v8m0 0 3.2-3.2M8 10.5 4.8 7.3M3 13.5h10" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/* ------------------------------------------------------------------ drop zone */

export function DropZone({
  onFiles,
  disabled,
  children,
  className,
  hint,
}: {
  onFiles: (files: File[]) => void;
  disabled?: boolean;
  children?: ReactNode;
  className?: string;
  hint?: string;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  return (
    <div
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled}
      onClick={() => !disabled && input.current?.click()}
      onKeyDown={(e) => {
        if (!disabled && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          input.current?.click();
        }
      }}
      onDragOver={(e) => {
        if (disabled) return;
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        if (!disabled && e.dataTransfer.files.length) onFiles([...e.dataTransfer.files]);
      }}
      className={cx(
        "flex cursor-pointer items-center justify-center gap-2 rounded-md border border-dashed px-3 py-2.5 text-center font-mono text-[10.5px] tracking-[0.04em] transition",
        over ? "border-signal/70 bg-signal/[0.07] text-signal" : "border-edge-2 bg-black/20 text-dim hover:border-signal/40 hover:text-slate-300",
        disabled && "cursor-not-allowed opacity-50",
        className,
      )}
    >
      <input
        ref={input}
        type="file"
        multiple
        hidden
        onChange={(e) => {
          const list = e.target.files ? [...e.target.files] : [];
          e.target.value = "";
          if (list.length) onFiles(list);
        }}
      />
      {children ?? (
        <>
          <PaperclipIcon size={13} />
          <span>
            Drop files or <span className="text-signal underline decoration-signal/40 underline-offset-2">browse</span>
          </span>
          {hint && <span className="text-mute">· {hint}</span>}
        </>
      )}
    </div>
  );
}

/** Pending (not yet uploaded) files, removable. */
export function PendingFiles({ files, onRemove }: { files: File[]; onRemove: (i: number) => void }) {
  if (!files.length) return null;
  const total = files.reduce((n, f) => n + f.size, 0);
  return (
    <div className="flex flex-col gap-1">
      <ul className="flex flex-wrap gap-1.5">
        {files.map((f, i) => (
          <li
            key={`${f.name}-${f.size}-${i}`}
            className="flex max-w-full items-center gap-1.5 rounded border border-edge-2 bg-black/30 py-[3px] pr-1 pl-2 font-mono text-[10.5px] text-slate-300"
          >
            <FileGlyph name={f.name} color="#7dd3fc" />
            <span className="max-w-[200px] truncate" title={f.name}>
              {f.name}
            </span>
            <span className="text-mute">{fmtBytes(f.size)}</span>
            <button
              type="button"
              onClick={() => onRemove(i)}
              aria-label={`Remove ${f.name}`}
              className="ml-0.5 flex h-4 w-4 items-center justify-center rounded text-[13px] leading-none text-mute hover:bg-red-500/15 hover:text-red-300"
            >
              ×
            </button>
          </li>
        ))}
      </ul>
      <span className="font-mono text-[9px] tracking-[0.08em] text-mute">
        {files.length}/{MAX_FILES} files · {fmtBytes(total)}
      </span>
    </div>
  );
}

/** A stored file (attachment or deliverable) with its download link. */
export function FileLink({ a, accent = "#7dd3fc", compact }: { a: Attachment; accent?: string; compact?: boolean }) {
  const href = fileHref(a.download_url);
  const body = (
    <>
      <FileGlyph name={a.name} color={accent} size={compact ? 11 : 13} />
      <span className="min-w-0 flex-1 truncate" title={a.name}>
        {a.name}
      </span>
      {a.size_bytes != null && <span className="shrink-0 text-mute">{fmtBytes(a.size_bytes)}</span>}
      {href && (
        <span className="shrink-0 opacity-60 group-hover:opacity-100" style={{ color: accent }}>
          <DownloadIcon />
        </span>
      )}
    </>
  );
  const cls = cx(
    "group flex min-w-0 items-center gap-2 rounded border font-mono transition",
    compact ? "px-1.5 py-[2px] text-[10px]" : "px-2 py-1.5 text-[11px]",
    href ? "text-slate-200 hover:bg-white/[0.04]" : "text-slate-400",
  );
  const style = { borderColor: `${accent}33`, background: `${accent}0a` };
  return href ? (
    <a href={href} download={a.name} target="_blank" rel="noreferrer" className={cls} style={style} title={`Download ${a.name}`}>
      {body}
    </a>
  ) : (
    <span className={cls} style={style}>
      {body}
    </span>
  );
}
