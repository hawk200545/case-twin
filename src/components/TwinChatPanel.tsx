import { useState, useRef, useEffect, useCallback } from "react";
import { Send, Activity, User, Loader2, FileText, X, ChevronRight } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { cn } from "@/lib/utils";
import { type MatchItem } from "@/lib/mockUploadApis";
import { API_BASE } from "@/lib/api";
import type { CaseProfile } from "@/lib/caseProfileTypes";

interface ChatMessage {
    role: "user" | "assistant";
    content: string;
}

interface TwinChatPanelProps {
    isOpen: boolean;
    onClose: () => void;
    match: MatchItem | null;
    currentProfile?: CaseProfile | null;
}

const STARTERS = [
    "How was the twin treated?",
    "Compare the key findings",
    "What was the outcome pathway?",
    "Key differences between cases?",
    "Was ICU required for the twin?",
];

const BACKEND = API_BASE;

export function TwinChatPanel({ isOpen, onClose, match, currentProfile }: TwinChatPanelProps) {
    const [messages, setMessages] = useState<ChatMessage[]>([]);
    const [input, setInput] = useState("");
    const [isLoading, setIsLoading] = useState(false);
    const endRef = useRef<HTMLDivElement>(null);
    const inputRef = useRef<HTMLTextAreaElement>(null);

    // Resizable panel
    const [width, setWidth] = useState(440);
    const isDragging = useRef(false);

    const onMouseDown = useCallback((e: React.MouseEvent) => {
        e.preventDefault();
        isDragging.current = true;
        document.body.style.cursor = "ew-resize";
        document.body.style.userSelect = "none";
        const onMouseMove = (ev: MouseEvent) => {
            if (!isDragging.current) return;
            setWidth(Math.max(340, Math.min(700, window.innerWidth - ev.clientX - 24)));
        };
        const onMouseUp = () => {
            isDragging.current = false;
            document.body.style.cursor = "";
            document.body.style.userSelect = "";
            window.removeEventListener("mousemove", onMouseMove);
            window.removeEventListener("mouseup", onMouseUp);
        };
        window.addEventListener("mousemove", onMouseMove);
        window.addEventListener("mouseup", onMouseUp);
    }, []);

    // Generate greeting when panel opens
    useEffect(() => {
        if (isOpen && match && messages.length === 0) {
            const twinDx = match.diagnosis || "the historical case";
            const twinOutcome = match.outcome ? ` (${match.outcome} outcome)` : "";
            const twinFacility = match.facility ? ` from ${match.facility}` : "";

            let currentCtx = "";
            if (currentProfile) {
                const pat = currentProfile.patient;
                const pres = currentProfile.presentation;
                const age = pat?.age_years ? `${pat.age_years}y ` : "";
                const sex = pat?.sex || "";
                const cc = pres?.chief_complaint || pres?.hpi?.slice(0, 60) || "the current patient";
                currentCtx = ` and your current **${cc}** patient (${age}${sex})`;
            }

            setMessages([{
                role: "assistant",
                content: `I have context on **${twinDx}**${twinOutcome}${twinFacility}${currentCtx}.\n\nAsk me anything about treatment, findings, outcomes, or how the two cases compare.`
            }]);
        }
    }, [isOpen, match]);

    // Reset on new match
    useEffect(() => {
        setMessages([]);
    }, [match?.pmc_id]);

    useEffect(() => {
        endRef.current?.scrollIntoView({ behavior: "smooth" });
    }, [messages, isLoading]);

    // Focus input when panel opens
    useEffect(() => {
        if (isOpen) {
            setTimeout(() => inputRef.current?.focus(), 300);
        }
    }, [isOpen]);

    if (!match) return null;

    const sendMessage = async (text: string) => {
        const userMsg = text.trim();
        if (!userMsg || isLoading) return;

        setInput("");
        setMessages(prev => [...prev, { role: "user", content: userMsg }]);
        setIsLoading(true);

        try {
            const fd = new FormData();
            fd.append("query", userMsg);
            fd.append("case_text", match.case_text || match.summary || match.diagnosis || "");
            if (currentProfile) {
                fd.append("current_profile", JSON.stringify(currentProfile));
            }

            const res = await fetch(`${BACKEND}/chat_twin`, { method: "POST", body: fd });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            setMessages(prev => [...prev, { role: "assistant", content: data.reply }]);
        } catch {
            setMessages(prev => [...prev, {
                role: "assistant",
                content: "I couldn't connect to the reasoning engine right now. Please try again."
            }]);
        } finally {
            setIsLoading(false);
        }
    };

    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage(input);
        }
    };

    const showStarters = messages.length <= 1 && !isLoading;

    // Build context badges
    const twinLabel = match.diagnosis?.slice(0, 28) || "Historical Twin";
    const currentLabel = currentProfile?.patient
        ? `${currentProfile.patient.age_years ?? "?"}y ${currentProfile.patient.sex ?? ""}`.trim()
        : null;

    return (
        <>
            {/* Backdrop */}
            <div
                className={cn(
                    "fixed inset-0 z-50 bg-black/10 backdrop-blur-[2px] transition-opacity duration-300",
                    isOpen ? "opacity-100 pointer-events-auto" : "opacity-0 pointer-events-none"
                )}
                onClick={onClose}
            />

            {/* Panel */}
            <div
                className={cn(
                    "fixed bottom-6 right-6 z-50 flex flex-col bg-white border border-zinc-200/80 shadow-2xl shadow-zinc-300/30 rounded-2xl overflow-hidden transition-all duration-300 ease-[cubic-bezier(0.23,1,0.32,1)]",
                    isOpen ? "opacity-100 scale-100 translate-y-0" : "opacity-0 scale-95 translate-y-4 pointer-events-none"
                )}
                style={{ width, height: "82vh", minHeight: 480, maxHeight: 900 }}
            >
                {/* Drag handle */}
                <div
                    onMouseDown={onMouseDown}
                    className="absolute left-0 top-0 bottom-0 w-1.5 cursor-ew-resize group z-10 flex items-center justify-center"
                >
                    <div className="h-12 w-[3px] bg-zinc-200 rounded-full opacity-0 group-hover:opacity-100 transition-opacity" />
                </div>

                {/* ── Header ─────────────────────────────────────────────── */}
                <div className="shrink-0 px-5 pt-4 pb-3 border-b border-zinc-100 bg-white">
                    <div className="flex items-start justify-between gap-3">
                        <div className="flex items-center gap-3">
                            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-zinc-100 border border-zinc-200">
                                <Activity className="h-4.5 w-4.5 text-zinc-700" />
                            </div>
                            <div>
                                <h2 className="text-[14px] font-semibold text-zinc-900 leading-tight">
                                    Clinical Copilot
                                </h2>
                                <p className="text-[11px] text-zinc-500 mt-0.5 flex items-center gap-1">
                                    <FileText className="h-3 w-3" />
                                    Case Context · {match.pmc_id || "Historical Evidence"}
                                </p>
                            </div>
                        </div>
                        <button
                            onClick={onClose}
                            className="flex h-7 w-7 items-center justify-center rounded-lg text-zinc-400 hover:bg-zinc-100 hover:text-zinc-700 transition-colors mt-0.5"
                        >
                            <X className="h-4 w-4" />
                        </button>
                    </div>

                    {/* Context badges */}
                    <div className="flex flex-wrap gap-1.5 mt-3">
                        <ContextBadge icon="twin" label={twinLabel} sublabel={match.outcome} />
                        {currentLabel && (
                            <ContextBadge icon="current" label={`Current: ${currentLabel}`} sublabel="Active case" />
                        )}
                    </div>
                </div>

                {/* ── Messages ───────────────────────────────────────────── */}
                <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4 bg-zinc-50/40">
                    {messages.map((msg, i) => (
                        <MessageBubble key={i} msg={msg} />
                    ))}

                    {/* Typing indicator */}
                    {isLoading && (
                        <div className="flex gap-3 items-start">
                            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-white border border-zinc-200 mt-0.5">
                                <Activity className="h-3.5 w-3.5 text-zinc-600" />
                            </div>
                            <div className="flex items-center gap-1 px-4 py-3 rounded-2xl rounded-tl-sm bg-white border border-zinc-200 shadow-sm">
                                <span className="h-1.5 w-1.5 rounded-full bg-zinc-400 animate-bounce [animation-delay:0ms]" />
                                <span className="h-1.5 w-1.5 rounded-full bg-zinc-400 animate-bounce [animation-delay:150ms]" />
                                <span className="h-1.5 w-1.5 rounded-full bg-zinc-400 animate-bounce [animation-delay:300ms]" />
                            </div>
                        </div>
                    )}

                    {/* Starter chips */}
                    {showStarters && (
                        <div className="pt-2">
                            <p className="text-[11px] font-medium text-zinc-400 uppercase tracking-wider mb-2 px-1">
                                Suggested questions
                            </p>
                            <div className="flex flex-col gap-1.5">
                                {STARTERS.map((q) => (
                                    <button
                                        key={q}
                                        onClick={() => sendMessage(q)}
                                        className="flex items-center justify-between w-full text-left px-3.5 py-2.5 rounded-xl border border-zinc-200 bg-white hover:bg-zinc-50 hover:border-zinc-300 text-[13px] text-zinc-700 font-medium transition-colors group shadow-sm"
                                    >
                                        {q}
                                        <ChevronRight className="h-3.5 w-3.5 text-zinc-300 group-hover:text-zinc-500 shrink-0 transition-colors" />
                                    </button>
                                ))}
                            </div>
                        </div>
                    )}

                    <div ref={endRef} />
                </div>

                {/* ── Input ──────────────────────────────────────────────── */}
                <div className="shrink-0 px-4 py-3 bg-white border-t border-zinc-100">
                    <div className="flex items-end gap-2 bg-zinc-50 border border-zinc-200 rounded-xl px-3 py-2 focus-within:ring-2 focus-within:ring-zinc-400/20 focus-within:border-zinc-300 transition-all">
                        <textarea
                            ref={inputRef}
                            value={input}
                            onChange={e => setInput(e.target.value)}
                            onKeyDown={handleKeyDown}
                            placeholder="Ask about context, outcomes, treatment…"
                            rows={1}
                            className="flex-1 max-h-28 min-h-[36px] bg-transparent border-none focus:ring-0 resize-none px-0 py-1 text-[13.5px] text-zinc-900 placeholder:text-zinc-400 focus:outline-none"
                        />
                        <button
                            onClick={() => sendMessage(input)}
                            disabled={!input.trim() || isLoading}
                            className="flex shrink-0 items-center justify-center h-8 w-8 bg-zinc-900 text-white rounded-lg hover:bg-zinc-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed mb-0.5 shadow-sm"
                        >
                            {isLoading ? (
                                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            ) : (
                                <Send className="h-3.5 w-3.5 ml-0.5" />
                            )}
                        </button>
                    </div>
                    <p className="text-[10.5px] text-zinc-400 mt-1.5 px-1 text-center">
                        Enter to send · Shift+Enter for newline · Grounded in provided case evidence
                    </p>
                </div>
            </div>
        </>
    );
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function ContextBadge({ icon, label, sublabel }: { icon: "twin" | "current"; label: string; sublabel?: string }) {
    return (
        <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-zinc-100 border border-zinc-200/80">
            <div className={cn(
                "h-1.5 w-1.5 rounded-full shrink-0",
                icon === "twin" ? "bg-emerald-500" : "bg-blue-500"
            )} />
            <span className="text-[11px] font-medium text-zinc-700 max-w-[140px] truncate">{label}</span>
            {sublabel && (
                <span className="text-[10px] text-zinc-400 capitalize shrink-0">· {sublabel}</span>
            )}
        </div>
    );
}

function MessageBubble({ msg }: { msg: ChatMessage }) {
    const isUser = msg.role === "user";

    if (isUser) {
        return (
            <div className="flex gap-2.5 justify-end items-start">
                <div className="max-w-[80%] px-3.5 py-2.5 rounded-2xl rounded-tr-sm bg-zinc-900 text-white text-[13.5px] leading-[1.55] shadow-sm">
                    {msg.content}
                </div>
                <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-zinc-200 border border-zinc-300 mt-0.5">
                    <User className="h-3.5 w-3.5 text-zinc-600" />
                </div>
            </div>
        );
    }

    return (
        <div className="flex gap-2.5 items-start">
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-white border border-zinc-200 mt-0.5">
                <Activity className="h-3.5 w-3.5 text-zinc-700" />
            </div>
            <div className="max-w-[88%] px-4 py-3 rounded-2xl rounded-tl-sm bg-white border border-zinc-200 shadow-sm text-[13.5px] text-zinc-900 leading-[1.6]">
                <ReactMarkdown
                    components={{
                        p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
                        strong: ({ children }) => <strong className="font-semibold text-zinc-900">{children}</strong>,
                        ul: ({ children }) => <ul className="mt-1 mb-2 space-y-0.5 pl-4 list-disc marker:text-zinc-400">{children}</ul>,
                        li: ({ children }) => <li className="text-zinc-800">{children}</li>,
                        h3: ({ children }) => <h3 className="text-[13px] font-semibold text-zinc-900 mt-2 mb-1">{children}</h3>,
                        code: ({ children }) => <code className="px-1 py-0.5 rounded bg-zinc-100 text-[12px] font-mono text-zinc-700">{children}</code>,
                    }}
                >
                    {msg.content}
                </ReactMarkdown>
            </div>
        </div>
    );
}
