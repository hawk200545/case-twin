import { useState, useRef, useCallback, useEffect } from "react";
import { createPortal } from "react-dom";
import { X, Loader2, Stethoscope } from "lucide-react";
import { cn } from "@/lib/utils";

interface SelectionState {
    text: string;
    context: string;
    x: number; // viewport coords of selection rect
    y: number;
    width: number;
}

interface PopoverState {
    explanation: string;
    x: number;
    y: number;
    width: number;
}

import { API_BASE } from "@/lib/api";
const BACKEND = API_BASE;

/**
 * Wraps children with a text-selection listener.
 * When the user highlights text inside, a small "Explain" button appears
 * (Notion-style: clean, gray, medical icon). Clicking it calls MedGemma
 * and shows a compact explanation popover near the selected text.
 */
export function SelectionExplainPopover({
    children,
    className,
}: {
    children: React.ReactNode;
    className?: string;
}) {
    const wrapperRef = useRef<HTMLDivElement>(null);
    const [selection, setSelection] = useState<SelectionState | null>(null);
    const [popover, setPopover] = useState<PopoverState | null>(null);
    const [loading, setLoading] = useState(false);

    const clearAll = useCallback(() => {
        setSelection(null);
        setPopover(null);
        setLoading(false);
    }, []);

    const handleMouseUp = useCallback(() => {
        const sel = window.getSelection();
        if (!sel || sel.isCollapsed || !sel.toString().trim()) return;

        const text = sel.toString().trim();
        if (text.length < 3 || text.length > 400) return;

        const wrapper = wrapperRef.current;
        if (!wrapper) return;
        const range = sel.getRangeAt(0);
        if (!wrapper.contains(range.commonAncestorContainer)) return;

        // Grab surrounding context from anchor node
        const anchorText = sel.anchorNode?.textContent ?? "";
        const anchorOffset = sel.anchorOffset;
        const ctx = anchorText.slice(Math.max(0, anchorOffset - 150), anchorOffset + 150);

        const rect = range.getBoundingClientRect();
        setPopover(null);
        setSelection({ text, context: ctx, x: rect.left, y: rect.top, width: rect.width });
    }, []);

    const handleDocMouseDown = useCallback(
        (e: MouseEvent) => {
            const floatEl = document.getElementById("mg-sel-float");
            if (floatEl && floatEl.contains(e.target as Node)) return;
            clearAll();
        },
        [clearAll]
    );

    useEffect(() => {
        document.addEventListener("mousedown", handleDocMouseDown);
        return () => document.removeEventListener("mousedown", handleDocMouseDown);
    }, [handleDocMouseDown]);

    const handleExplain = useCallback(async () => {
        if (!selection) return;
        const { text, context, x, y, width } = selection;
        setLoading(true);
        setSelection(null);
        try {
            const fd = new FormData();
            fd.append("selected_text", text);
            fd.append("context", context);
            const res = await fetch(`${BACKEND}/explain_selection`, { method: "POST", body: fd });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            setPopover({ explanation: data.explanation ?? "No explanation available.", x, y, width });
        } catch {
            setPopover({ explanation: "Unable to reach MedGemma right now. Please try again.", x, y, width });
        } finally {
            setLoading(false);
        }
    }, [selection]);

    return (
        <div ref={wrapperRef} className={cn("relative", className)} onMouseUp={handleMouseUp}>
            {children}
            {createPortal(
                <>
                    {selection && !loading && <AskButton selection={selection} onExplain={handleExplain} />}
                    {loading && <ThinkingBadge />}
                    {popover && <ExplainPopover popover={popover} onDismiss={clearAll} />}
                </>,
                document.body
            )}
        </div>
    );
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function AskButton({ selection, onExplain }: { selection: SelectionState; onExplain: () => void }) {
    const btnW = 148;
    const rawLeft = selection.x + selection.width / 2 - btnW / 2;
    const left = Math.max(8, Math.min(window.innerWidth - btnW - 8, rawLeft));
    const top = Math.max(8, selection.y - 38);

    return (
        <div
            id="mg-sel-float"
            style={{ position: "fixed", top, left, zIndex: 99999, width: btnW }}
            className="animate-in fade-in zoom-in-95 duration-100"
        >
            <button
                onMouseDown={(e) => e.preventDefault()}
                onClick={onExplain}
                className={cn(
                    "w-full flex items-center justify-center gap-1.5 px-3 py-[5px] rounded-md",
                    "text-[12px] font-medium text-zinc-700 select-none whitespace-nowrap",
                    "bg-white border border-zinc-300 shadow-md shadow-zinc-200/60",
                    "hover:bg-zinc-50 hover:border-zinc-400 hover:text-zinc-900",
                    "transition-colors duration-100"
                )}
            >
                <Stethoscope className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
                Explain with AI
            </button>
            {/* Downward caret */}
            <div className="absolute -bottom-[5px] left-1/2 -translate-x-1/2 h-2.5 w-2.5 rotate-45 bg-white border-r border-b border-zinc-300" />
        </div>
    );
}

function ThinkingBadge() {
    return (
        <div
            id="mg-sel-float"
            style={{ position: "fixed", bottom: 28, left: "50%", transform: "translateX(-50%)", zIndex: 99999 }}
            className="animate-in fade-in slide-in-from-bottom-3 duration-200"
        >
            <div className="flex items-center gap-2 px-4 py-2 rounded-md bg-white border border-zinc-200 shadow-lg text-[13px] font-medium text-zinc-600">
                <Loader2 className="h-3.5 w-3.5 animate-spin text-zinc-400" />
                MedGemma is thinking…
            </div>
        </div>
    );
}

function ExplainPopover({ popover, onDismiss }: { popover: PopoverState; onDismiss: () => void }) {
    const cardRef = useRef<HTMLDivElement>(null);
    const [style, setStyle] = useState<React.CSSProperties>({ visibility: "hidden", position: "fixed", zIndex: 99999 });
    const [caretDir, setCaretDir] = useState<"up" | "down">("up");

    const cardW = 300;
    const MARGIN = 10; // min gap from all viewport edges

    useEffect(() => {
        if (!cardRef.current) return;
        const cardH = cardRef.current.offsetHeight;
        const vw = window.innerWidth;
        const vh = window.innerHeight;

        // Horizontal: center on selection, clamp within viewport
        const rawLeft = popover.x + popover.width / 2 - cardW / 2;
        const left = Math.max(MARGIN, Math.min(vw - cardW - MARGIN, rawLeft));

        // Vertical: prefer below selection (+14px gap), flip above if no room
        const belowTop = popover.y + 14;
        const aboveTop = popover.y - 14 - cardH;

        let top: number;
        let dir: "up" | "down";

        if (belowTop + cardH + MARGIN <= vh) {
            // fits below
            top = belowTop;
            dir = "up"; // caret points up (sits on top of card pointing at highlighted text)
        } else if (aboveTop >= MARGIN) {
            // fits above
            top = aboveTop;
            dir = "down"; // caret points down
        } else {
            // Neither fits cleanly — pin to bottom margin, no caret
            top = Math.max(MARGIN, vh - cardH - MARGIN);
            dir = "up";
        }

        setStyle({ position: "fixed", top, left, zIndex: 99999, width: cardW, visibility: "visible" });
        setCaretDir(dir);
    }, [popover]);

    return (
        <div
            id="mg-sel-float"
            ref={cardRef}
            style={style}
            className="animate-in fade-in duration-150"
        >
            {/* Upward caret (card is below selection) */}
            {caretDir === "up" && style.visibility !== "hidden" && (
                <div className="absolute -top-[5px] left-1/2 -translate-x-1/2 h-2.5 w-2.5 rotate-45 bg-white border-t border-l border-zinc-200" />
            )}

            {/* Card */}
            <div className="rounded-lg border border-zinc-200/80 bg-white shadow-xl shadow-zinc-200/50 overflow-hidden">
                {/* Header */}
                <div className="flex items-center justify-between px-3.5 py-2.5 border-b border-zinc-100 bg-zinc-50/80">
                    <div className="flex items-center gap-1.5">
                        <Stethoscope className="h-3.5 w-3.5 text-zinc-500 shrink-0" />
                        <span className="text-[11px] font-semibold text-zinc-600 uppercase tracking-wide">
                            MedGemma
                        </span>
                    </div>
                    <button
                        onClick={onDismiss}
                        className="flex h-5 w-5 items-center justify-center rounded text-zinc-400 hover:bg-zinc-200 hover:text-zinc-700 transition-colors"
                        aria-label="Dismiss"
                    >
                        <X className="h-3 w-3" />
                    </button>
                </div>

                {/* Explanation body */}
                <div className="px-3.5 py-3">
                    <p className="text-[13px] leading-[1.6] text-zinc-800">
                        {popover.explanation}
                    </p>
                    <p className="mt-2 text-[10.5px] text-zinc-400">
                        AI-generated · not clinical advice
                    </p>
                </div>
            </div>

            {/* Downward caret (card is above selection) */}
            {caretDir === "down" && style.visibility !== "hidden" && (
                <div className="absolute -bottom-[5px] left-1/2 -translate-x-1/2 h-2.5 w-2.5 rotate-45 bg-white border-r border-b border-zinc-200" />
            )}
        </div>
    );
}
