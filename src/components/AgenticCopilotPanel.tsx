import { useCallback, useEffect, useRef, useState } from "react";
import { useDashboardStore } from "@/store/dashboardStore";
import {
    Bot,
    Check,
    FileText,
    Image as ImageIcon,
    Loader2,
    SendHorizontal,
    Sparkles,
    UploadCloud,
    X,
    ChevronRight,
    Stethoscope,
    Activity,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { CaseProfile } from "@/lib/caseProfileTypes";
import {
    createInitialState,
    processIntakeTurn,
    type OrchestratorMessage,
    type OrchestratorPhase,
    type OrchestratorState,
} from "@/lib/agenticOrchestrator";
import { computeProfileConfidence } from "@/lib/caseProfileUtils";

// ─── Props ─────────────────────────────────────────────────────────────────

interface AgenticCopilotPanelProps {
    onProfileUpdate: (profile: CaseProfile) => void;
    onFileForSearch: (file: File) => void;
    onReadyToProceed: () => void;
}

// ─── Phase label map ───────────────────────────────────────────────────────

const PHASE_LABELS: Record<OrchestratorPhase, string> = {
    greeting: "Ready",
    extracting: "Extracting…",
    patching: "Updating profile…",
    questioning: "Awaiting your input",
    ready: "Profile complete",
    expanded: "Profile complete + extended",
};

const PHASE_DOT_CLASS: Record<OrchestratorPhase, string> = {
    greeting: "bg-[var(--mr-border)]",
    extracting: "bg-[var(--mr-action)] animate-pulse",
    patching: "bg-[var(--mr-warning)] animate-pulse",
    questioning: "bg-[var(--mr-action)]",
    ready: "bg-[var(--mr-success)]",
    expanded: "bg-indigo-500",
};

// ─── Sub-components ────────────────────────────────────────────────────────

function ThinkingBubble({ content }: { content: string }) {
    return (
        <div className="flex items-center gap-2.5 self-start rounded-2xl border border-[var(--mr-border)] bg-white/70 px-3.5 py-2.5 text-sm text-[var(--mr-text-secondary)] shadow-sm backdrop-blur">
            <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-[var(--mr-action)]" />
            <span>{content}</span>
        </div>
    );
}

function FieldPatchBadge({ fields }: { fields: string[] }) {
    return (
        <div className="flex flex-wrap gap-1.5 self-start">
            {fields.map(f => (
                <span
                    key={f}
                    className="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-[11px] font-medium text-emerald-700"
                >
                    <Check className="h-2.5 w-2.5" />
                    {f}
                </span>
            ))}
        </div>
    );
}

function SchemaExpansionBadge({ fields }: { fields: string[] }) {
    return (
        <div className="flex flex-wrap gap-1.5 self-start">
            {fields.map(f => (
                <span
                    key={f}
                    className="inline-flex items-center gap-1 rounded-full border border-indigo-200 bg-indigo-50 px-2.5 py-0.5 text-[11px] font-medium text-indigo-700"
                >
                    <Check className="h-2.5 w-2.5" />
                    {f}
                    <span className="ml-0.5 text-[9px] font-semibold uppercase tracking-wide text-indigo-400">ext</span>
                </span>
            ))}
        </div>
    );
}

function ConfidenceBar({ score }: { score: number }) {
    const color =
        score >= 80 ? "var(--mr-success)" : score >= 50 ? "var(--mr-warning)" : "var(--mr-action)";
    const label =
        score >= 80 ? "High completeness" : score >= 50 ? "Moderate completeness" : "Low completeness";

    return (
        <div className="w-full self-start rounded-2xl border border-[var(--mr-border)] bg-[var(--mr-bg-subtle)] px-4 py-3">
            <div className="mb-2 flex items-center justify-between">
                <span className="text-xs font-medium text-[var(--mr-text-secondary)]">Profile completeness</span>
                <span className="text-xs font-semibold" style={{ color }}>{score}% — {label}</span>
            </div>
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-[var(--mr-border)]">
                <div
                    className="h-full rounded-full transition-all duration-700"
                    style={{ width: `${score}%`, backgroundColor: color }}
                />
            </div>
        </div>
    );
}

function CtaBanner({ onProceed }: { onProceed: () => void }) {
    return (
        <button
            type="button"
            onClick={onProceed}
            className="group flex w-full items-center justify-between gap-3 rounded-xl border border-zinc-200 bg-white px-5 py-4 text-left text-zinc-900 shadow-sm transition hover:bg-zinc-50"
        >
            <div className="flex items-center gap-3">
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-zinc-100 border border-zinc-200">
                    <Stethoscope className="h-4 w-4 text-zinc-600" />
                </div>
                <div>
                    <p className="text-sm font-semibold leading-5 text-zinc-900">Profile is ready for matching</p>
                    <p className="text-xs leading-4 text-zinc-500">Click to find the closest case twins</p>
                </div>
            </div>
            <ChevronRight className="h-5 w-5 text-zinc-400 opacity-70 transition group-hover:translate-x-0.5" />
        </button>
    );
}

function FileAttachChip({ name, preview, mimeType, onRemove }: {
    name: string;
    preview?: string;
    mimeType: string;
    onRemove: () => void;
}) {
    const isImage = mimeType.startsWith("image/");
    return (
        <div className="relative inline-flex items-center gap-2 overflow-hidden rounded-xl border border-[var(--mr-border)] bg-white pr-2 shadow-sm">
            {isImage && preview ? (
                <img src={preview} alt={name} className="h-10 w-10 rounded-l-xl object-cover" />
            ) : (
                <div className="flex h-10 w-10 items-center justify-center rounded-l-xl bg-[var(--mr-bg-subtle)]">
                    <FileText className="h-4 w-4 text-[var(--mr-warning)]" />
                </div>
            )}
            <span className="max-w-[130px] truncate text-xs font-medium text-[var(--mr-text)]">{name}</span>
            <button
                type="button"
                onClick={onRemove}
                className="ml-1 text-[var(--mr-text-secondary)] transition hover:text-[var(--mr-error)]"
                aria-label={`Remove ${name}`}
            >
                <X className="h-3.5 w-3.5" />
            </button>
        </div>
    );
}

// ─── Message renderer ──────────────────────────────────────────────────────

function MessageBubble({
    msg,
    onProceed,
}: {
    msg: OrchestratorMessage;
    onProceed: () => void;
}) {
    if (msg.type === "thinking") return <ThinkingBubble content={msg.content} />;
    if (msg.type === "field_patch" && msg.patchedFields) return <FieldPatchBadge fields={msg.patchedFields} />;
    if (msg.type === "schema_expansion" && msg.patchedFields) return <SchemaExpansionBadge fields={msg.patchedFields} />;
    if (msg.type === "confidence_update" && msg.confidence !== undefined) return <ConfidenceBar score={msg.confidence} />;
    if (msg.type === "cta") return <CtaBanner onProceed={onProceed} />;

    const isUser = msg.role === "user";
    const hasFiles = msg.files && msg.files.length > 0;
    const hasText = msg.content.trim().length > 0;

    return (
        <div className={cn("flex", isUser ? "justify-end" : "justify-start")}>
            {!isUser && (
                <div className="mr-2 mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-zinc-100 border border-zinc-200">
                    <Activity className="h-4 w-4 text-[var(--mr-action)]" />
                </div>
            )}
            <div className={cn("max-w-[82%] space-y-2", isUser && "items-end")}>
                {/* File chips in user message */}
                {hasFiles && isUser && (
                    <div className="flex flex-wrap justify-end gap-2">
                        {msg.files!.map((f, i) => (
                            <div key={i} className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--mr-border)] bg-white px-2.5 py-1.5 text-xs shadow-sm">
                                {f.type.startsWith("image/")
                                    ? <ImageIcon className="h-3.5 w-3.5 text-[var(--mr-action)]" />
                                    : <FileText className="h-3.5 w-3.5 text-[var(--mr-warning)]" />
                                }
                                <span className="max-w-[120px] truncate">{f.name}</span>
                            </div>
                        ))}
                    </div>
                )}
                {/* Text bubble */}
                {hasText && (
                    <div
                        className={cn(
                            "rounded-xl px-4 py-2.5 text-[14px] leading-relaxed whitespace-pre-wrap",
                            isUser
                                ? "bg-zinc-800 text-white shadow-sm"
                                : "border border-zinc-200 bg-white text-zinc-900 shadow-sm"
                        )}
                    >
                        {msg.content}
                    </div>
                )}
                {/* Files-only message (no text) */}
                {hasFiles && !hasText && !isUser && (
                    <div className="flex flex-wrap gap-2">
                        {msg.files!.map((f, i) => (
                            <div key={i} className="text-xs text-[var(--mr-text-secondary)]">
                                📎 {f.name}
                            </div>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
}

// ─── Main component ────────────────────────────────────────────────────────

export function AgenticCopilotPanel({
    onProfileUpdate,
    onFileForSearch,
    onReadyToProceed,
}: AgenticCopilotPanelProps) {
    const state = useDashboardStore(s => s.orchestratorState);
    const setState = useDashboardStore(s => s.setOrchestratorState);
    const stateRef = useRef<OrchestratorState>(state);

    const [inputText, setInputText] = useState("");
    const [pendingFiles, setPendingFiles] = useState<File[]>([]);
    const [isDragOver, setIsDragOver] = useState(false);
    const [isProcessing, setIsProcessing] = useState(false);

    const messagesEndRef = useRef<HTMLDivElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const textareaRef = useRef<HTMLTextAreaElement>(null);

    const conf = computeProfileConfidence(state.profile);

    // Keep stateRef in sync so async handlers always read fresh state
    useEffect(() => { stateRef.current = state; }, [state]);

    // Auto-scroll to bottom on new messages
    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }, [state.messages]);

    // Emit profile up when it changes
    useEffect(() => {
        onProfileUpdate(state.profile);
    }, [state.profile, onProfileUpdate]);

    // ── File handling ─────────────────────────────────────────────────────

    const addFiles = useCallback((incoming: FileList | File[]) => {
        const arr = Array.from(incoming);
        setPendingFiles(prev => [...prev, ...arr]);
        // Notify parent of any imaging file for future search
        const imgFile = arr.find(f => f.type === "image/jpeg" || f.type === "image/png" || f.type === "image/webp");
        if (imgFile) onFileForSearch(imgFile);
    }, [onFileForSearch]);

    const removeFile = (index: number) => {
        setPendingFiles(prev => prev.filter((_, i) => i !== index));
    };

    // ── Drag and drop ─────────────────────────────────────────────────────

    const handleDragOver = (e: React.DragEvent) => {
        e.preventDefault();
        setIsDragOver(true);
    };
    const handleDragLeave = (e: React.DragEvent) => {
        // Only clear if leaving the panel entirely
        if (!e.currentTarget.contains(e.relatedTarget as Node)) {
            setIsDragOver(false);
        }
    };
    const handleDrop = (e: React.DragEvent) => {
        e.preventDefault();
        setIsDragOver(false);
        addFiles(e.dataTransfer.files);
    };

    // Tiny local sleep helper
    const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms));

    // ── Send ──────────────────────────────────────────────────────────────

    const handleSend = async () => {
        const text = inputText.trim();
        if (!text && pendingFiles.length === 0) return;
        if (isProcessing) return;

        setInputText("");
        if (textareaRef.current) textareaRef.current.style.height = "auto";
        const filesToSend = [...pendingFiles];
        setPendingFiles([]);
        setIsProcessing(true);

        // Capture clean pre-turn state BEFORE appending any thinking bubbles.
        // This is what we'll pass to the orchestrator so its result is built
        // off clean messages — no thinking bubbles in the lineage.
        const cleanSnapshot = stateRef.current;

        // Build a local user message for the thinking-stage display only.
        // The orchestrator will build its own canonical user message internally.
        const previewUserMsg: OrchestratorMessage = {
            id: `msg-${Date.now()}-u`,
            role: "user",
            type: filesToSend.length > 0 && !text ? "file_attach" : "text",
            content: text,
            files: filesToSend.map(f => ({
                name: f.name,
                type: f.type,
                preview: f.type.startsWith("image/") ? URL.createObjectURL(f) : undefined,
            })),
        };

        const stage1Label = filesToSend.length > 0
            ? `Parsing ${filesToSend.length} file${filesToSend.length > 1 ? "s" : ""}…`
            : "Parsing clinical note…";

        // Stage 1 — show user msg + first thinking bubble
        setState(prev => ({
            ...prev,
            phase: "extracting",
            messages: [
                ...prev.messages,
                previewUserMsg,
                { id: `t1-${Date.now()}`, role: "assistant", type: "thinking", content: stage1Label } as OrchestratorMessage,
            ],
        }));
        await sleep(500);

        // Stage 2
        setState(prev => ({
            ...prev,
            messages: [
                ...prev.messages,
                { id: `t2-${Date.now()}`, role: "assistant", type: "thinking", content: "Extracting structured fields…" } as OrchestratorMessage,
            ],
        }));
        await sleep(500);

        // Stage 3
        setState(prev => ({
            ...prev,
            messages: [
                ...prev.messages,
                { id: `t3-${Date.now()}`, role: "assistant", type: "thinking", content: "Updating profile…" } as OrchestratorMessage,
            ],
        }));
        await sleep(200);


        try {
            // Pass the CLEAN snapshot (no thinking bubbles) to the orchestrator.
            // result.newState.messages will be built from clean history + real results.
            const result = await processIntakeTurn({
                userText: text,
                files: filesToSend,
                currentState: cleanSnapshot,
            });

            // Replace everything (including thinking bubbles) with the real outcome.
            setState(result.newState);

        } catch {
            setState(prev => ({
                ...prev,
                phase: "questioning",
                messages: [
                    ...prev.messages.filter(m => m.type !== "thinking"),
                    { id: `err-${Date.now()}`, role: "assistant", type: "text", content: "Something went wrong during extraction. Please try again." } as OrchestratorMessage,
                ],
            }));
        } finally {
            setIsProcessing(false);
        }
    };




    // Auto-resize textarea
    const handleTextareaChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
        setInputText(e.target.value);
        if (textareaRef.current) {
            textareaRef.current.style.height = "auto";
            textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 120)}px`;
        }
    };

    const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            void handleSend();
        }
    };

    const canSend = (inputText.trim().length > 0 || pendingFiles.length > 0) && !isProcessing;

    return (
        <div
            className={cn(
                "flex h-full flex-col overflow-hidden rounded-xl bg-white transition-all duration-200",
                isDragOver
                    ? "border-2 border-[var(--mr-action)]"
                    : "border border-zinc-200 shadow-sm"
            )}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
        >
            {/* ── Header ── */}
            <div className="flex shrink-0 items-center justify-between border-b border-[var(--mr-border)] bg-white px-5 py-4">
                <div className="flex items-center gap-3">
                    <div className="flex h-9 w-9 items-center justify-center rounded-md bg-zinc-100 border border-zinc-200">
                        <Activity className="h-5 w-5 text-[var(--mr-action)]" />
                    </div>
                    <div>
                        <h2 className="text-[15px] font-semibold leading-5 text-[var(--mr-text)]">
                            Clinical Copilot
                        </h2>
                        <div className="flex items-center gap-1.5">
                            <span
                                className={cn(
                                    "inline-block h-1.5 w-1.5 rounded-full transition-colors",
                                    PHASE_DOT_CLASS[state.phase]
                                )}
                            />
                            <p className="text-[11px] leading-4 text-[var(--mr-text-secondary)]">
                                {PHASE_LABELS[state.phase]}
                            </p>
                        </div>
                    </div>
                </div>

                {/* Compact confidence badge */}
                <div className="flex items-center gap-2">
                    <div className="relative h-10 w-10">
                        <svg className="h-10 w-10 -rotate-90" viewBox="0 0 36 36">
                            <circle cx="18" cy="18" r="15" fill="none" stroke="var(--mr-border)" strokeWidth="3" />
                            <circle
                                cx="18" cy="18" r="15" fill="none"
                                stroke={conf.score >= 80 ? "var(--mr-success)" : conf.score >= 50 ? "var(--mr-warning)" : "var(--mr-action)"}
                                strokeWidth="3"
                                strokeDasharray={`${(conf.score / 100) * 94.2} 94.2`}
                                strokeLinecap="round"
                                className="transition-all duration-700"
                            />
                        </svg>
                        <span className="absolute inset-0 flex items-center justify-center text-[9px] font-bold text-[var(--mr-text)]">
                            {conf.score}%
                        </span>
                    </div>
                </div>
            </div>

            {/* ── Messages thread ── */}
            <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-5 py-4">
                {state.messages.map(msg => (
                    <MessageBubble
                        key={msg.id}
                        msg={msg}
                        onProceed={onReadyToProceed}
                    />
                ))}
                <div ref={messagesEndRef} />
            </div>

            {/* ── Drag overlay ── */}
            {isDragOver && (
                <div className="pointer-events-none absolute inset-0 z-10 flex flex-col items-center justify-center rounded-2xl border-2 border-dashed border-[var(--mr-action)] bg-[rgba(10,103,143,0.06)]">
                    <UploadCloud className="h-10 w-10 text-[var(--mr-action)]" />
                    <p className="mt-2 text-sm font-semibold text-[var(--mr-action)]">Drop files to add them</p>
                    <p className="text-xs text-[var(--mr-text-secondary)]">JPEG, PNG, WebP, PDF, DOCX, TXT</p>
                </div>
            )}

            {/* ── Input area ── */}
            <div className="shrink-0 bg-white p-5 pt-3">
                {/* Pending file chips */}
                {pendingFiles.length > 0 && (
                    <div className="mb-3 flex flex-wrap gap-2">
                        {pendingFiles.map((f, i) => (
                            <FileAttachChip
                                key={`${f.name}-${i}`}
                                name={f.name}
                                mimeType={f.type}
                                preview={f.type.startsWith("image/") ? URL.createObjectURL(f) : undefined}
                                onRemove={() => removeFile(i)}
                            />
                        ))}
                    </div>
                )}

                <div className={cn(
                    "flex items-end gap-3 rounded-2xl border border-zinc-200/80 bg-zinc-50/50 px-3 py-2 transition-all duration-300 shadow-sm",
                    isDragOver ? "border-zinc-400 bg-white shadow-md ring-4 ring-zinc-500/10" : "hover:border-zinc-300/80 hover:bg-white",
                    "focus-within:border-zinc-400 focus-within:bg-white focus-within:shadow-md focus-within:ring-4 focus-within:ring-zinc-500/10"
                )}>
                    {/* File attach button */}
                    <input
                        ref={fileInputRef}
                        type="file"
                        multiple
                        accept=".jpg,.jpeg,.png,.webp,.pdf,.docx,.txt,.json"
                        className="hidden"
                        onChange={e => { addFiles(e.target.files ?? []); if (e.target) e.target.value = ""; }}
                    />
                    <button
                        type="button"
                        onClick={() => fileInputRef.current?.click()}
                        aria-label="Attach files"
                        className="mb-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl text-zinc-400 transition-all duration-200 hover:bg-zinc-200/50 hover:text-zinc-700 active:scale-95"
                    >
                        <UploadCloud className="h-5 w-5" strokeWidth={2.2} />
                    </button>

                    {/* Textarea */}
                    <textarea
                        ref={textareaRef}
                        rows={1}
                        value={inputText}
                        onChange={handleTextareaChange}
                        onKeyDown={handleKeyDown}
                        placeholder={
                            state.phase === "greeting"
                                ? "Ask Copilot, attach a file, or add evidence..."
                                : state.phase === "ready" || state.phase === "expanded"
                                    ? "Profile complete — continue adding supplemental information..."
                                    : "Answer the question above, or add more evidence..."
                        }
                        className="flex-1 resize-none bg-transparent py-2.5 text-[14.5px] leading-relaxed text-zinc-900 placeholder:text-zinc-400 focus:outline-none"
                        style={{ minHeight: "24px" }}
                    />

                    {/* Send button */}
                    <button
                        type="button"
                        onClick={() => void handleSend()}
                        disabled={!canSend}
                        aria-label="Send"
                        className={cn(
                            "mb-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl transition-all duration-200",
                            canSend
                                ? "bg-zinc-900 text-white hover:bg-zinc-800 hover:scale-[1.03] active:scale-[0.97] shadow-md shadow-zinc-900/15"
                                : "bg-zinc-100 text-zinc-400 cursor-not-allowed"
                        )}
                    >
                        {isProcessing
                            ? <Loader2 className="h-4 w-4 animate-spin" />
                            : <SendHorizontal className="h-4 w-4 ml-0.5" strokeWidth={2.5} />
                        }
                    </button>
                </div>

                <p className="mt-4 text-center text-[12px] font-medium text-zinc-400">
                    Drag files anywhere • <kbd className="font-sans px-1.5 py-0.5 mx-0.5 rounded-md bg-zinc-100 border border-zinc-200/60 text-zinc-500 shadow-sm">Enter</kbd> to send • <kbd className="font-sans px-1.5 py-0.5 mx-0.5 rounded-md bg-zinc-100 border border-zinc-200/60 text-zinc-500 shadow-sm">Shift + Enter</kbd> for newline
                </p>
            </div>
        </div>
    );
}
